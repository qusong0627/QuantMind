"""滚动重训调度器测试（P1 · 设计文档 §4.2，验收 ②）。

锁死三件事（每条都有事故形态对应）：
- **mark-after-dispatch**：只有端点回 dispatched/duplicate 才写 last_run；busy /
  HTTP 失败 / 数据未就绪一律不写 → 下一 tick 补发（验收 ② 的全部机制——先写了
  标记再派发，任何一次抖动都会留下「本月跑过了」的假记录，重训整月不补）；
- **due 语义**：``now >= due_moment`` 而非时刻等号——首交易日 15:30 那分钟被训练
  占用时，同 due 期内的后续任意 tick 仍判 due；跨月换标记键；
- **守卫不被 --force 绕过**：重跑是「立刻试一轮」，内存/busy 闸门照旧。
"""

from __future__ import annotations

import asyncio
import http.server
import json
import socket
import threading
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from backend.services.engine.tasks import retrain_scheduler as rts

_DUE_DAY = date(2026, 10, 1)  # 本月首交易日（测试内由 _month_sessions 打桩提供）
_SESSIONS = [_DUE_DAY, date(2026, 10, 2)]


class _FakeRedis:
    """get/set(nx,ex)/exists 最小面——alert_once 的 SETNX 去重也走这里。"""

    def __init__(self):
        self.store: dict = {}
        self.set_calls: list = []

    def get(self, key):
        item = self.store.get(key)
        return item[0] if item else None

    def set(self, key, value, ex=None, nx=False):
        self.set_calls.append((key, value, ex, nx))
        if nx and key in self.store:
            return None
        self.store[key] = (value, ex)
        return True

    def exists(self, key):
        return 1 if key in self.store else 0


class _Harness:
    def __init__(self, monkeypatch):
        self.redis = _FakeRedis()
        self.posts: list[dict] = []
        self.alerts: list[str] = []
        self.busy = {"busy": False, "reason": "idle"}
        self.mem = {"ok": True, "available_gb": 100.0, "min_gb": 50.0}
        self.sessions: list | None = list(_SESSIONS)
        self.cfg = dict(rts.DEFAULT_SCHEDULE)
        self.cfg.update({"enabled": True, "recipe_id": "cn_nativetft_base"})
        self.post_result = {
            "ok": True,
            "status_code": 200,
            "body": {
                "status": "dispatched",
                "campaign_id": "rc_cn_cn_nativetft_base_20260930",
                "run_id": "run_1",
                "anchor_date": "2026-09-30",
            },
        }

        monkeypatch.setattr(rts, "_month_sessions", lambda market, today: self.sessions)
        monkeypatch.setattr(rts, "_busy_probe", lambda client: dict(self.busy))
        monkeypatch.setattr(rts, "mem_guard", lambda: dict(self.mem))
        monkeypatch.setattr(rts, "get_schedule", lambda market, redis_client=None: dict(self.cfg))

        def _post(body, timeout=30):
            self.posts.append(dict(body))
            return self.post_result

        monkeypatch.setattr(rts, "_post_dispatch", _post)

        import backend.shared.qq_notify as qq

        monkeypatch.setattr(
            qq, "alert_async", lambda **kw: self.alerts.append(str(kw.get("title") or ""))
        )

    def tick(self, now=datetime(2026, 10, 1, 15, 31), *, force=False):
        return rts.dispatch_due_retrains(
            now=now, markets=["CN"], force=force, redis_client=self.redis
        )

    def last_run_marked(self) -> bool:
        return rts.last_run_key("CN", _DUE_DAY) in self.redis.store

    def sample_market(self):
        return self.tick()["results"][0]


# ---------------------------------------------------------------------------
# judge_due（纯函数）
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kwargs", "due", "reason"),
    [
        (
            {"day_rule": "bogus", "month_sessions": _SESSIONS, "today": _DUE_DAY,
             "now_hm": "16:00", "cfg_time": "15:30"},
            False, "unknown_day_rule",
        ),
        (
            {"day_rule": "first_trading_day", "month_sessions": None, "today": _DUE_DAY,
             "now_hm": "16:00", "cfg_time": "15:30"},
            False, "calendar_unavailable",
        ),
        (
            {"day_rule": "first_trading_day", "month_sessions": [], "today": _DUE_DAY,
             "now_hm": "16:00", "cfg_time": "15:30"},
            False, "no_session_in_month",
        ),
        (
            # 首交易日还没到（月历只回看当月，理论上不出现，防御性分支）
            {"day_rule": "first_trading_day", "month_sessions": [date(2026, 10, 5)],
             "today": _DUE_DAY, "now_hm": "16:00", "cfg_time": "15:30"},
            False, "not_due_yet",
        ),
        (
            {"day_rule": "first_trading_day", "month_sessions": _SESSIONS, "today": _DUE_DAY,
             "now_hm": "15:29", "cfg_time": "15:30"},
            False, "before_time",
        ),
        (
            {"day_rule": "first_trading_day", "month_sessions": _SESSIONS, "today": _DUE_DAY,
             "now_hm": "15:30", "cfg_time": "15:30"},
            True, "due",
        ),
        (
            # 月内补发：due 日已过、未派发 → 仍判 due（不等号语义）
            {"day_rule": "first_trading_day", "month_sessions": _SESSIONS,
             "today": date(2026, 10, 20), "now_hm": "09:00", "cfg_time": "15:30"},
            True, "due",
        ),
    ],
)
def test_judge_due_matrix(kwargs, due, reason):
    out = rts.judge_due(**kwargs)
    assert out["due"] is due and out["reason"] == reason


@pytest.mark.unit
def test_judge_due_due_date_is_first_session():
    out = rts.judge_due(
        day_rule="first_trading_day",
        month_sessions=_SESSIONS,
        today=date(2026, 10, 20),
        now_hm="09:00",
        cfg_time="15:30",
    )
    assert out["due_date"] == _DUE_DAY


@pytest.mark.unit
def test_normalize_keeps_unknown_day_rule_and_falls_back_time():
    out = rts._normalize({"time": "25:99", "day_rule": "Weekly", "observation_days": "x"})
    assert out["time"] == rts.DEFAULT_SCHEDULE["time"]  # 坏时刻回退默认档
    assert out["day_rule"] == "weekly"  # 未知规则原样保留 → judge_due 拒绝并告警
    assert out["observation_days"] == 20
    assert out["enabled"] is False


# ---------------------------------------------------------------------------
# tick 全流程
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_happy_path_dispatches_marks_and_then_already_run(monkeypatch):
    h = _Harness(monkeypatch)
    out = h.tick()
    first = out["results"][0]
    assert first["status"] == "dispatched" and first["campaign_id"].startswith("rc_cn_")
    assert out["dispatched"] == [first]
    assert h.posts == [
        {
            "market": "CN",
            "recipe_id": "cn_nativetft_base",
            "trigger": "schedule",
            "dry_run": False,
        }
    ]
    assert h.last_run_marked()  # mark-after-dispatch：2xx 且 dispatched 才写

    second = h.sample_market()
    assert second["status"] == "already_run"
    assert len(h.posts) == 1  # 本期不再重复派发


@pytest.mark.unit
def test_busy_at_due_tick_skips_then_next_tick_catches_up(monkeypatch):
    """验收 ②：busy 时跳过且**不写标记**，下一 tick 自动补发。"""
    h = _Harness(monkeypatch)
    h.busy = {"busy": True, "reason": "training_singleflight", "holder": "run_x"}
    first = h.sample_market()
    assert first["status"] == "busy"
    assert h.posts == [] and not h.last_run_marked()
    assert any("训练占位" in t for t in h.alerts)

    h.busy = {"busy": False, "reason": "idle"}
    second = h.sample_market()  # 下一 tick（一分钟后）
    assert second["status"] == "dispatched"
    assert h.last_run_marked()
    assert len(h.posts) == 1


@pytest.mark.unit
def test_before_time_not_due_no_side_effects(monkeypatch):
    h = _Harness(monkeypatch)
    out = h.tick(now=datetime(2026, 10, 1, 9, 0))
    assert out["results"][0]["status"] == "not_due:before_time"
    assert h.posts == [] and not h.last_run_marked() and h.alerts == []


@pytest.mark.unit
def test_disabled_market_skipped_without_probe(monkeypatch):
    h = _Harness(monkeypatch)
    h.cfg["enabled"] = False
    assert h.sample_market()["status"] == "disabled"
    assert h.posts == []


@pytest.mark.unit
def test_http_failure_retries_next_tick_and_alerts_once_per_day(monkeypatch):
    h = _Harness(monkeypatch)
    h.post_result = {"ok": False, "status_code": 500, "error": "boom"}
    assert h.sample_market()["status"] == "http_error"
    assert not h.last_run_marked()  # 失败不写标记 → 可重试
    assert len(h.alerts) == 1

    assert h.sample_market()["status"] == "http_error"
    assert len(h.posts) == 2  # 下一 tick 仍然真的重试
    assert len(h.alerts) == 1  # 但告警当日只发一条


@pytest.mark.unit
def test_endpoint_skipped_data_lag_not_marked(monkeypatch):
    h = _Harness(monkeypatch)
    h.post_result = {
        "ok": True,
        "status_code": 200,
        "body": {"status": "skipped", "reason": "data_lag", "detail": {"a": 1}},
    }
    entry = h.sample_market()
    assert entry["status"] == "skipped:data_lag"
    assert not h.last_run_marked()
    assert h.alerts == []  # 数据未就绪由端点按日告警，调度侧不重复喊


@pytest.mark.unit
def test_endpoint_busy_skip_alerts_scheduler_side(monkeypatch):
    h = _Harness(monkeypatch)
    h.post_result = {"ok": True, "status_code": 200, "body": {"status": "skipped", "reason": "busy"}}
    entry = h.sample_market()
    assert entry["status"] == "skipped:busy"
    assert not h.last_run_marked()
    assert any("占位" in t for t in h.alerts)


@pytest.mark.unit
def test_duplicate_counts_as_success_and_marks(monkeypatch):
    h = _Harness(monkeypatch)
    h.post_result = {
        "ok": True,
        "status_code": 200,
        "body": {"status": "duplicate", "reason": "already_registered", "campaign_id": "rc_x"},
    }
    assert h.sample_market()["status"] == "duplicate"
    assert h.last_run_marked()


@pytest.mark.unit
def test_duplicate_planned_is_pending_not_marked(monkeypatch):
    """duplicate+planned = 崩溃残留未真实受理：不记账，下一 tick 继续追（审查 F1）。"""
    h = _Harness(monkeypatch)
    h.post_result = {
        "ok": True,
        "status_code": 200,
        "body": {
            "status": "duplicate",
            "reason": "planned_in_flight",
            "campaign_status": "planned",
            "campaign_id": "rc_cn_cn_nativetft_base_20260930",
            "anchor_date": "2026-09-30",
        },
    }
    first = h.sample_market()
    assert first["status"] == "planned_pending"
    assert not h.last_run_marked()

    # 行转 dispatched 后的 duplicate 照常记账（in-flight/已完成的语义不变）
    h.post_result = {
        "ok": True,
        "status_code": 200,
        "body": {
            "status": "duplicate",
            "reason": "in_flight",
            "campaign_status": "dispatched",
            "campaign_id": "rc_cn_cn_nativetft_base_20260930",
        },
    }
    assert h.sample_market()["status"] == "duplicate"
    assert h.last_run_marked()


@pytest.mark.unit
def test_low_memory_skips_and_alerts(monkeypatch):
    h = _Harness(monkeypatch)
    h.mem = {"ok": False, "reason": "low_memory", "available_gb": 40.0, "min_gb": 50.0}
    assert h.sample_market()["status"] == "low_memory"
    assert h.posts == [] and not h.last_run_marked()
    assert any("内存" in t for t in h.alerts)


@pytest.mark.unit
def test_remote_executor_not_wired_keeps_mem_guard_and_alerts(monkeypatch):
    """executor=remote 未接线：不得解除内存守卫（防「以为远程、实际本地」OOM 裸奔）。"""
    h = _Harness(monkeypatch)
    h.cfg["executor"] = "remote"
    h.mem = {"ok": False, "reason": "low_memory", "available_gb": 10.0, "min_gb": 50.0}
    assert h.sample_market()["status"] == "low_memory"
    assert h.posts == [] and not h.last_run_marked()
    assert any("remote" in t for t in h.alerts)

    # 内存充足时照常派发（只是守卫不再被跳过，且醒目告警「未接线」）
    h2 = _Harness(monkeypatch)
    h2.cfg["executor"] = "remote"
    assert h2.sample_market()["status"] == "dispatched"
    assert any("remote" in t for t in h2.alerts)


@pytest.mark.unit
def test_calendar_unavailable_alerts_and_skips(monkeypatch):
    h = _Harness(monkeypatch)
    h.sessions = None
    assert h.sample_market()["status"] == "not_due:calendar_unavailable"
    assert h.posts == [] and any("日历" in t for t in h.alerts)


@pytest.mark.unit
def test_recipe_invalid_alerts_and_skips(monkeypatch):
    h = _Harness(monkeypatch)
    h.cfg["recipe_id"] = "no_such_recipe"
    entry = h.sample_market()
    assert entry["status"] == "recipe_invalid" and h.posts == []
    assert any("配方" in t for t in h.alerts)


@pytest.mark.unit
def test_force_bypasses_enabled_and_due_but_not_mem_guard(monkeypatch):
    h = _Harness(monkeypatch)
    h.cfg["enabled"] = False
    out = h.tick(now=datetime(2026, 10, 20, 9, 0), force=True)  # 非 due 时刻也试
    assert out["results"][0]["status"] == "dispatched"
    assert h.last_run_marked()

    # --force 不绕过内存守卫：重跑不是绕过安全闸
    h2 = _Harness(monkeypatch)
    h2.mem = {"ok": False, "reason": "low_memory", "available_gb": 39.0, "min_gb": 50.0}
    assert h2.tick(force=True)["results"][0]["status"] == "low_memory"
    assert h2.posts == []


@pytest.mark.unit
def test_force_marked_does_not_double_dispatch(monkeypatch):
    h = _Harness(monkeypatch)
    assert h.tick(force=True)["results"][0]["status"] == "dispatched"
    h.post_result = {
        "ok": True,
        "status_code": 200,
        "body": {"status": "duplicate", "reason": "already_registered", "campaign_id": "rc_x"},
    }
    # 已派发过再 force：端点的 campaign 幂等键把它变成 duplicate，绝不重复训练
    assert h.tick(force=True)["results"][0]["status"] == "duplicate"
    assert len(h.posts) == 2


@pytest.mark.unit
def test_single_market_failure_does_not_stop_others(monkeypatch):
    h = _Harness(monkeypatch)

    def _boom(market, **kwargs):
        raise RuntimeError("market CN exploded")

    monkeypatch.setattr(rts, "_dispatch_one", _boom)
    out = rts.dispatch_due_retrains(
        now=datetime(2026, 10, 1, 15, 31),
        markets=["CN", "US", "HK"],
        redis_client=h.redis,
    )
    assert [r["status"] for r in out["results"]] == ["error", "error", "error"]
    assert all(r["market"] in {"CN", "US", "HK"} for r in out["results"])


@pytest.mark.unit
def test_dispatch_one_market_mismatch_alerts_and_skips(monkeypatch):
    """双保险：端点保存口已拒错配，但配置可经 redis 直写绕过——调度侧兜底。"""
    h = _Harness(monkeypatch)

    import backend.shared.training.recipe_registry as rr

    monkeypatch.setattr(
        rr,
        "load_recipe",
        lambda rid: SimpleNamespace(market="HK", calendar_market="HK", recipe_id=rid),
    )
    entry = h.sample_market()
    assert entry["status"] == "market_mismatch"
    assert h.posts == [] and not h.last_run_marked()
    assert any("市场不匹配" in t for t in h.alerts)


@pytest.mark.unit
def test_unexpected_endpoint_status_alerts_and_not_marked(monkeypatch):
    """端点回了个白名单外的 status：不写标记 + 告警——绝不静默当成功。"""
    h = _Harness(monkeypatch)
    h.post_result = {"ok": True, "status_code": 200, "body": {"status": "who_knows"}}
    entry = h.sample_market()
    assert entry["status"] == "unexpected:who_knows"
    assert not h.last_run_marked()
    assert any("未知状态" in t for t in h.alerts)


# ---------------------------------------------------------------------------
# 配置面（保存/读取往返 + 名单来源）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recipe_markets_filters_invalid_and_normalizes(monkeypatch):
    import backend.shared.training.recipe_registry as rr

    monkeypatch.setattr(
        rr,
        "list_recipes",
        lambda: [
            {"market": "cn", "valid": True},
            {"market": "HK", "valid": False},
            {"market": "us", "valid": True},
            {"market": "", "valid": True},
        ],
    )
    assert rts.recipe_markets() == ["CN", "US"]


@pytest.mark.unit
def test_schedule_save_get_round_trip_and_defaults():
    fake = _FakeRedis()
    saved = rts.save_schedule(
        "CN", {"enabled": True, "recipe_id": "r", "time": "09:45"}, redis_client=fake
    )
    assert saved["time"] == "09:45"  # 合法时刻直通（非回退分支）
    assert rts.get_schedule("CN", redis_client=fake) == saved
    fresh = rts.get_schedule("US", redis_client=fake)  # 未配置市场 → 纯默认档
    assert fresh["enabled"] is False
    assert fresh["recipe_id"] == rts.DEFAULT_SCHEDULE["recipe_id"]


@pytest.mark.unit
def test_get_all_schedules_iterates_recipe_markets(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(rts, "recipe_markets", lambda: ["CN", "HK"])
    rts.save_schedule("CN", {"enabled": True, "recipe_id": "r"}, redis_client=fake)
    out = rts.get_all_schedules(redis_client=fake)
    assert sorted(out) == ["CN", "HK"]
    assert out["CN"]["enabled"] is True and out["HK"]["enabled"] is False


@pytest.mark.unit
def test_redis_factory_pins_socket_timeout():
    """tick 内任何 Redis 调用最多阻塞 3s——不能让一个挂死的 Redis 卡住 beat。"""
    client = rts._redis()
    assert client.connection_pool.connection_kwargs.get("socket_timeout") == 3


@pytest.mark.unit
def test_month_sessions_uses_real_calendar():
    """真日历集成：CN 2026-10 首交易日必在国庆假期后（>10-01），且区间不空。"""
    sessions = rts._month_sessions("CN", date(2026, 10, 8))
    assert sessions, "日历覆盖应包含 2026-10"
    assert sessions[0] > date(2026, 10, 1)
    assert all(s.month == 10 and s <= date(2026, 10, 8) for s in sessions)


@pytest.mark.unit
def test_busy_probe_inside_running_loop_is_conservatively_busy():
    """事件循环内被误调 → 不能裸抛，保守按 busy（宁可跳过一轮）。"""

    async def _call():
        return rts._busy_probe(None)

    out = asyncio.run(_call())
    assert out["busy"] is True
    assert out["reason"] == "busy_probe_failed"


# ---------------------------------------------------------------------------
# _post_dispatch 的真实 HTTP 契约（本地回环服务端；成功 / 4xx / 连接拒绝）
# ---------------------------------------------------------------------------


class _DispatchHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.server.received.append(
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": json.loads(self.rfile.read(length) or b"{}"),
            }
        )
        if self.server.response_raw is not None:
            payload = self.server.response_raw.encode()
        else:
            payload = json.dumps(self.server.response_body).encode()
        self.send_response(self.server.response_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # 静音测试输出
        pass


@pytest.fixture()
def dispatch_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _DispatchHandler)
    server.received = []
    server.response_code = 200
    server.response_body = {"status": "dispatched"}
    server.response_raw = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def _point_api_at(monkeypatch, base_url):
    import backend.shared.auth as shared_auth
    import backend.shared.training_runtime as runtime

    monkeypatch.setattr(shared_auth, "get_internal_call_secret", lambda: "sekret")
    monkeypatch.setattr(runtime, "default_api_base_url", lambda: base_url)


@pytest.mark.unit
def test_post_dispatch_success_sends_secret_and_parses_body(monkeypatch, dispatch_server):
    _point_api_at(monkeypatch, f"http://127.0.0.1:{dispatch_server.server_port}")
    out = rts._post_dispatch({"market": "CN", "recipe_id": "r"})
    assert out == {"ok": True, "status_code": 200, "body": {"status": "dispatched"}}
    (req,) = dispatch_server.received
    assert req["path"] == "/api/v1/internal/rolling/dispatch"
    assert req["headers"]["x-internal-call-secret"] == "sekret"
    assert req["body"] == {"market": "CN", "recipe_id": "r"}


@pytest.mark.unit
def test_post_dispatch_missing_secret_fails_closed(monkeypatch):
    import backend.shared.auth as shared_auth

    monkeypatch.setattr(shared_auth, "get_internal_call_secret", lambda: "")
    out = rts._post_dispatch({"market": "CN"})
    assert out == {"ok": False, "error": "internal_call_secret_missing"}


@pytest.mark.unit
def test_post_dispatch_http_error_returns_detail_not_raises(monkeypatch, dispatch_server):
    _point_api_at(monkeypatch, f"http://127.0.0.1:{dispatch_server.server_port}")
    dispatch_server.response_code = 401
    dispatch_server.response_body = {"detail": "Invalid internal call secret"}
    out = rts._post_dispatch({"market": "CN"})
    assert out["ok"] is False and out["status_code"] == 401
    assert out["error"] == {"detail": "Invalid internal call secret"}

    # 非 JSON 错误体 → 原文降级（不用 str() 假装解析成功）
    dispatch_server.response_raw = "gateway exploded"
    out = rts._post_dispatch({"market": "CN"})
    assert out["ok"] is False and out["error"] == "gateway exploded"


@pytest.mark.unit
def test_post_dispatch_connection_refused_returns_error_not_raises(monkeypatch):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    _point_api_at(monkeypatch, f"http://127.0.0.1:{dead_port}")
    out = rts._post_dispatch({"market": "CN"})
    assert out["ok"] is False
    assert "无法连接" in str(out["error"])
