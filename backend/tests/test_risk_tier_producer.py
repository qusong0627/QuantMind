"""P1.8 批次 C：风险档位**生产者**测试（取数口径 + 落盘 + worker 生命周期）。

分四段：
1. 纯计算（vol20 / 回撤 / 台账分族合并）——口径与隔壁逐式对齐处必须锁死；
2. 三项取数的失败映射——**取不到必须 None，绝不拿 0 顶替**（0 会被 decide_level
   读成"最松的市场状态"，正是本层要消灭的形态）；
3. `run_tier_decision` 组装（fail-safe / 防抖 / 非交易日不写）；
4. worker 日键生命周期（成功留键、失败删键重试、非交易日不留键）+ 接线源断言。
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import date

import pytest

from backend.services.trade.services import risk_tier_producer as P
from backend.shared.risk import tiers as T

#: 2026-09-23 是周三（用固定日期，避免测试依赖运行日）
DAY = date(2026, 9, 23)
PREV = date(2026, 9, 22)


# ── 假 Redis（hash + string + NX）────────────────────────────────────


class _FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    # hash
    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def hset(self, key: str, mapping: dict | None = None, **kw) -> int:
        h = self.hashes.setdefault(key, {})
        h.update({str(k): str(v) for k, v in (mapping or {}).items()})
        return 1

    def expire(self, key: str, ttl: int) -> bool:
        self.ttls[key] = ttl
        return True

    # string
    def set(self, key: str, value, nx: bool = False, ex: int | None = None):
        if nx and key in self.strings:
            return None
        self.strings[key] = str(value)
        self.ttls[key] = ex
        return True

    def delete(self, key: str) -> int:
        existed = key in self.strings
        self.strings.pop(key, None)
        return 1 if existed else 0


# ── 1. 纯计算 ────────────────────────────────────────────────────────


def test_vol20_is_population_std_not_sample():
    """rets = [1.0] + [0.0]×9：总体 sd = 0.30，样本 sd = 0.32——必须取总体（隔壁同式）。"""
    closes = [100.0, 101.0] + [101.0] * 9
    assert P.compute_vol20(closes) == 0.30


def test_vol20_matches_pstdev_oracle():
    closes = [100 + (i % 7) * 1.3 for i in range(21)]
    rets = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
    assert P.compute_vol20(closes) == round(statistics.pstdev(rets), 2)


def test_vol20_min_returns_threshold_is_ten():
    assert P.compute_vol20([100.0 + i for i in range(11)]) is not None  # 10 个收益
    assert P.compute_vol20([100.0 + i for i in range(10)]) is None  # 9 个收益


def test_vol20_uses_last_21_closes_only():
    tail = [100.0] + [101.0] * 20
    wild_head = [50.0, 500.0, 1.0, 900.0, 3.0, 700.0, 20.0, 400.0, 60.0]
    assert P.compute_vol20(wild_head + tail) == P.compute_vol20(tail)


def test_vol20_filters_none_and_non_positive():
    tail = [100.0] + [101.0] * 20
    noisy = [None, 0.0, -5.0] + tail
    assert P.compute_vol20(noisy) == P.compute_vol20(tail)
    # 过滤后不足 11 个有效值 → None（不是拿剩下的硬算）
    assert P.compute_vol20([0.0] * 30) is None
    assert P.compute_vol20([None] * 30) is None


def test_drawdown_is_window_span_not_rolling():
    """[50, 100, 60, …]：窗口极差 = (100−50)/100 = 50%；滚动回撤只有 40%——锁口径。"""
    assert P.max_drawdown_pct([50.0, 100.0, 60.0, 70.0, 80.0]) == 50.0


def test_drawdown_formula_and_rounding():
    assert P.max_drawdown_pct([100.0, 110.0, 88.0, 90.0, 95.0]) == 20.0


def test_drawdown_min_points_threshold_is_five():
    assert P.max_drawdown_pct([100.0, 99.0, 98.0, 97.0, 96.0]) is not None
    assert P.max_drawdown_pct([100.0, 99.0, 98.0, 97.0]) is None


def test_drawdown_uses_last_20_points_only():
    crash_head = [1000.0, 500.0, 200.0, 100.0, 10.0]
    tail = [100.0] * 19 + [95.0]
    assert P.max_drawdown_pct(crash_head + tail) == 5.0


def test_drawdown_filters_non_positive_values():
    assert P.max_drawdown_pct([0.0, None, -3.0, 1.0, 2.0, 3.0, 4.0]) is None


# ── 2. 台账分族合并（假回撤的雷区）──────────────────────────────────


def test_merge_picks_family_with_most_days_not_largest_values():
    """tdx 3 天 ~92 万 vs qmt 偶发 1 天 2385 万：**不许按日求和**（会造 −96% 假回撤）。"""
    rows = [
        ("tdx-default-10000001", date(2026, 9, 19), 918_000.0),
        ("tdx-default-10000001", date(2026, 9, 20), 920_000.0),
        ("tdx-default-10000001", date(2026, 9, 21), 925_000.0),
        ("tdx-default-10000001", date(2026, 9, 22), 928_000.0),
        ("tdx-default-10000001", date(2026, 9, 23), 930_000.0),
        ("qmt-default-10000001", date(2026, 9, 23), 23_850_000.0),
    ]
    fam, series = P.merge_ledger_rows(rows)
    assert fam == "tdx-default"
    assert [v for _, v in series] == [
        918_000.0,
        920_000.0,
        925_000.0,
        928_000.0,
        930_000.0,
    ]
    dd = P.max_drawdown_pct([v for _, v in series])
    assert dd is not None and dd < 2  # 按日求和的口径会得到 ~96


def test_merge_folds_account_rename_into_one_family():
    """用户 id 规范化（00000001 → 10000001）会把同一座账本拆名——必须并回一族。"""
    rows = [
        ("tdx-default-00000001", date(2026, 9, 22), 900_000.0),
        ("tdx-default-10000001", date(2026, 9, 23), 910_000.0),
    ]
    fam, series = P.merge_ledger_rows(rows)
    assert fam == "tdx-default"
    assert len(series) == 2


def test_merge_same_day_duplicate_keeps_later_row():
    """重叠日两行（改名当天）：入参按 last_snapshot_at 升序，后者覆盖前者。"""
    rows = [
        ("tdx-default-10000001", date(2026, 9, 23), 100.0),
        ("tdx-default-10000001", date(2026, 9, 23), 120.0),
    ]
    _, series = P.merge_ledger_rows(rows)
    assert series == [(date(2026, 9, 23), 120.0)]


def test_merge_drops_zero_equity_rows():
    """空账户/未回填在库里是 0（列 non-null default 0）——纳入会造 −100% 假回撤。"""
    rows = [
        ("tdx-default-10000001", date(2026, 9, 23), 0.0),
        ("qmt-default-10000001", date(2026, 9, 23), 1_000_000.0),
    ]
    fam, series = P.merge_ledger_rows(rows)
    assert fam == "qmt-default"
    assert series == [(date(2026, 9, 23), 1_000_000.0)]


def test_merge_all_unusable_is_empty_not_zero():
    assert P.merge_ledger_rows([]) == ("", [])
    assert P.merge_ledger_rows([("tdx-default-1", date(2026, 9, 23), 0.0)]) == ("", [])
    assert P.merge_ledger_rows([(None, date(2026, 9, 23), 5.0)]) == ("", [])


def test_merge_series_sorted_by_date():
    rows = [
        ("tdx-default-1", date(2026, 9, 23), 3.0),
        ("tdx-default-1", date(2026, 9, 21), 1.0),
        ("tdx-default-1", date(2026, 9, 22), 2.0),
    ]
    _, series = P.merge_ledger_rows(rows)
    assert [d for d, _ in series] == [
        date(2026, 9, 21),
        date(2026, 9, 22),
        date(2026, 9, 23),
    ]


# ── 3. 取数失败的映射：None，不是 0 ─────────────────────────────────


@pytest.fixture()
def breadth_src(monkeypatch):
    """替掉 market_breadth_stats 的实现（生产者内部是调用时导入）。"""
    import backend.services.api.market_analysis.quantdb_service as svc

    def _install(fake):
        monkeypatch.setattr(svc, "market_breadth_stats", fake)

    return _install


def test_limit_up_empty_dict_is_none_not_zero(breadth_src):
    breadth_src(lambda trade_date=None: {})
    assert P.limit_up_count(DAY) == (None, "")


def test_limit_up_missing_key_is_none_not_zero(breadth_src):
    breadth_src(lambda trade_date=None: {"trade_date": "20260922", "up_count": 3000})
    assert P.limit_up_count(DAY) == (None, "")


def test_limit_up_reads_value_and_source_date(breadth_src):
    breadth_src(lambda trade_date=None: {"trade_date": "20260922", "limit_up": 66})
    assert P.limit_up_count(DAY) == (66, "20260922")


def test_limit_up_exception_is_none_not_zero(breadth_src):
    def _boom(trade_date=None):
        raise RuntimeError("duckdb down")

    breadth_src(_boom)
    assert P.limit_up_count(DAY) == (None, "")


# ── 4. run_tier_decision 组装 ────────────────────────────────────────


def _fake_inputs(monkeypatch, *, vol, dd, zt, trading=True):
    async def _is_trading(day):
        return trading

    monkeypatch.setattr(P, "_is_trading_day", _is_trading)
    monkeypatch.setattr(
        P,
        "index_vol20",
        lambda today=None: (vol, "000001.SH" if vol is not None else ""),
    )
    monkeypatch.setattr(
        P,
        "account_drawdown20",
        lambda today=None: _coro(
            (
                dd,
                {"source": "fake", "points": 6}
                if dd is not None
                else {"source": "fake"},
            )
        ),
    )
    monkeypatch.setattr(
        P,
        "limit_up_count",
        lambda today=None: (zt, "20260922" if zt is not None else ""),
    )


def _coro(value):
    async def _inner():
        return value

    return _inner()


@pytest.mark.asyncio
async def test_decision_writes_calm_when_all_inputs_benign(monkeypatch):
    r = _FakeRedis()
    _fake_inputs(monkeypatch, vol=0.5, dd=1.0, zt=60)

    result = await P.run_tier_decision(today=DAY, redis=r)

    assert result["ok"] and result["level"] == "calm"
    doc = T.parse_tier_doc(r.hashes[T.TIER_KEY], today=DAY)
    assert doc.source == "doc" and doc.level == "calm"
    # inputs 在 hash 里是 JSON 串（端点是 json.loads 后展示），直接读原文断言
    inputs = json.loads(r.hashes[T.TIER_KEY]["inputs"])
    assert inputs["vol20"] == 0.5
    assert inputs["index_source"] == "000001.SH"
    assert T.TIER_DETAIL_KEY.format(date="20260923") in r.hashes  # 当日明细留痕
    assert r.hashes[T.TIER_KEY]["source"] == "producer"


@pytest.mark.asyncio
async def test_decision_missing_all_inputs_degrades_to_defensive(monkeypatch):
    """取不到数据 = 收紧，不是放行——必须落到防守且原因点名缺失项。"""
    r = _FakeRedis()
    _fake_inputs(monkeypatch, vol=None, dd=None, zt=None)

    result = await P.run_tier_decision(today=DAY, redis=r)

    assert result["level"] == "defensive"
    assert any("风控数据缺失" in x and "降级防守" in x for x in result["reasons"])
    assert result["inputs"]["vol20"] is None
    assert result["inputs"]["drawdown20"] is None
    assert result["inputs"]["limit_up"] is None


@pytest.mark.asyncio
async def test_decision_missing_one_input_is_at_least_caution(monkeypatch):
    r = _FakeRedis()
    _fake_inputs(monkeypatch, vol=0.5, dd=1.0, zt=None)

    result = await P.run_tier_decision(today=DAY, redis=r)

    assert result["level"] == "caution"
    assert any("至少谨慎" in x for x in result["reasons"])


@pytest.mark.asyncio
async def test_decision_same_day_debounce_keeps_tighter_level(monkeypatch):
    r = _FakeRedis()
    T.save_tier(r, level="defensive", today=DAY)  # 当天已定防守（如早盘一次暴跌）
    _fake_inputs(monkeypatch, vol=0.5, dd=1.0, zt=60)  # 重算 → calm

    result = await P.run_tier_decision(today=DAY, redis=r)

    assert result["level"] == "defensive" and result["computed"] == "calm"
    assert result["debounced"] is True
    assert any("防抖" in x for x in result["reasons"])


@pytest.mark.asyncio
async def test_decision_next_day_restores_by_latest_state(monkeypatch):
    r = _FakeRedis()
    T.save_tier(r, level="defensive", today=PREV)  # 昨天的防守档
    _fake_inputs(monkeypatch, vol=0.5, dd=1.0, zt=60)

    result = await P.run_tier_decision(today=DAY, redis=r)

    assert result["level"] == "calm" and not result["debounced"]


@pytest.mark.asyncio
async def test_decision_non_trading_day_writes_nothing(monkeypatch):
    r = _FakeRedis()
    _fake_inputs(monkeypatch, vol=0.5, dd=1.0, zt=60, trading=False)

    result = await P.run_tier_decision(today=DAY, redis=r)

    assert result["skipped"] == "non_trading_day" and not result["ok"]
    assert T.TIER_KEY not in r.hashes


# ── 5. worker 日键生命周期 ───────────────────────────────────────────


@pytest.fixture()
def fake_redis_wired(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(P, "_trade_redis_client", lambda *a, **k: r)
    return r


@pytest.mark.asyncio
async def test_decide_once_keeps_day_key_on_success(monkeypatch, fake_redis_wired):
    r = fake_redis_wired
    calls = []

    async def _fake_decision(*, today=None, redis=None):
        calls.append(today)
        return {"date": today.isoformat(), "ok": True, "level": "calm"}

    monkeypatch.setattr(P, "run_tier_decision", _fake_decision)
    await P._decide_once(DAY)

    key = f"{P.DONE_KEY_PREFIX}{DAY.isoformat()}"
    assert calls == [DAY]
    assert r.strings.get(key) == "1"
    assert r.ttls.get(key) == P.DONE_TTL_S


@pytest.mark.asyncio
async def test_decide_once_deletes_day_key_on_failure(monkeypatch, fake_redis_wired):
    """失败删键 = 下一轮重试；不删的话一次抖动会把当天档位永久留空。"""
    r = fake_redis_wired

    async def _boom(*, today=None, redis=None):
        raise RuntimeError("pg down")

    monkeypatch.setattr(P, "run_tier_decision", _boom)
    with pytest.raises(RuntimeError):
        await P._decide_once(DAY)

    assert f"{P.DONE_KEY_PREFIX}{DAY.isoformat()}" not in r.strings


@pytest.mark.asyncio
async def test_decide_once_deletes_day_key_when_skipped(monkeypatch, fake_redis_wired):
    r = fake_redis_wired

    async def _skipped(*, today=None, redis=None):
        return {"date": today.isoformat(), "ok": False, "skipped": "non_trading_day"}

    monkeypatch.setattr(P, "run_tier_decision", _skipped)
    await P._decide_once(DAY)

    assert f"{P.DONE_KEY_PREFIX}{DAY.isoformat()}" not in r.strings


@pytest.mark.asyncio
async def test_decide_once_skips_when_already_claimed(monkeypatch, fake_redis_wired):
    r = fake_redis_wired
    r.strings[f"{P.DONE_KEY_PREFIX}{DAY.isoformat()}"] = "1"

    async def _must_not_run(*, today=None, redis=None):
        raise AssertionError("日键已存在却又跑了一次定档")

    monkeypatch.setattr(P, "run_tier_decision", _must_not_run)
    await P._decide_once(DAY)  # 不抛即通过


@pytest.mark.asyncio
async def test_worker_loop_heartbeats_and_runs_at_most_once(
    monkeypatch, fake_redis_wired
):
    """循环体：心跳 → 到点则定档一次 → sleep。第一轮 sleep 抛 _Stop 收尾。

    心跳经过注册表 `heartbeat`（生产走 sentinel 客户端）——测试替掉它，既避免
    真连 Redis，也把"worker 必须打心跳"这条写成断言（C07 体检靠它判活）。
    """
    import backend.shared.scheduler_registry as registry

    beats: list[str] = []
    monkeypatch.setattr(
        registry, "heartbeat", lambda key, **kw: beats.append(key) or True
    )
    seen = []

    async def _fake_decision(*, today=None, redis=None):
        seen.append(today)
        return {"date": (today or DAY).isoformat(), "ok": True, "level": "calm"}

    monkeypatch.setattr(P, "run_tier_decision", _fake_decision)

    async def _is_trading(day):
        return True

    monkeypatch.setattr(P, "_is_trading_day", _is_trading)

    class _Stop(Exception):
        pass

    async def _stop_sleep(_s):
        raise _Stop

    class _AsyncioShim:
        sleep = staticmethod(_stop_sleep)

    monkeypatch.setattr(P, "asyncio", _AsyncioShim)  # 只换本模块的引用，不动真 asyncio
    with pytest.raises(_Stop):
        await P.run_risk_tier_worker()

    assert beats == ["risk_tier"]
    # 到点（≥09:10 且"交易日"）才定档；测试多在工作时段外跑 → 两种都合法，但最多一次
    assert len(seen) <= 1


# ── 5b. 原生客户端 / 包装形态（首次真跑踩中的坑）────────────────────


class _RawRedisLike(_FakeRedis):
    """模仿**原生** `redis.Redis`：自带一个 `client()` 方法（redis-py 的连接工厂）。

    这正是 2026-09-23 定档首次真跑炸掉的原因：谁把原生客户端再"拆一层 .client"，
    拿到的就是这个方法对象，随后 `.hset` → `'function' object has no attribute`。
    """

    def client(self):  # 与 redis-py 同名同形（不可当包装拆）
        return self


class _WrapperLike:
    """模仿 trade_shared.RedisClient：`.client` 是**实例属性**（真客户端对象）。"""

    def __init__(self, raw) -> None:
        self.client = raw


@pytest.mark.asyncio
async def test_decision_accepts_wrapper_client(monkeypatch):
    """生产接线：deps/worker 给的是包装 → 档位必须落到底层原生客户端上。"""
    raw = _RawRedisLike()
    _fake_inputs(monkeypatch, vol=0.5, dd=1.0, zt=60)

    result = await P.run_tier_decision(today=DAY, redis=_WrapperLike(raw))

    assert result["ok"] and result["level"] == "calm"
    assert raw.hashes[T.TIER_KEY]["level"] == "calm"


@pytest.mark.asyncio
async def test_decide_once_claims_day_key_through_wrapper(monkeypatch):
    """日键走原生客户端（NX/EX），档位走包装——同一次定档两处都要落上。"""
    raw = _RawRedisLike()
    monkeypatch.setattr(P, "_trade_redis_client", lambda *a, **k: _WrapperLike(raw))
    _fake_inputs(monkeypatch, vol=0.5, dd=1.0, zt=60)

    await P._decide_once(DAY)

    assert raw.strings.get(f"{P.DONE_KEY_PREFIX}{DAY.isoformat()}") == "1"
    assert raw.hashes[T.TIER_KEY]["level"] == "calm"


@pytest.mark.asyncio
async def test_decide_once_tolerates_raw_redis_like_client(monkeypatch):
    """即便拿到原生形态（自带 client() 方法），也不能把方法当包装拆掉。"""
    raw = _RawRedisLike()
    monkeypatch.setattr(P, "_trade_redis_client", lambda *a, **k: raw)
    _fake_inputs(monkeypatch, vol=0.5, dd=1.0, zt=60)

    await P._decide_once(DAY)

    assert raw.strings.get(f"{P.DONE_KEY_PREFIX}{DAY.isoformat()}") == "1"
    assert raw.hashes[T.TIER_KEY]["level"] == "calm"


# ── 6. CLI 与接线源断言 ─────────────────────────────────────────────


def test_main_non_trading_day_exits_zero(monkeypatch, capsys):
    async def _skipped(*, today=None, redis=None):
        return {"date": DAY.isoformat(), "ok": False, "skipped": "non_trading_day"}

    monkeypatch.setattr(P, "run_tier_decision", _skipped)
    assert P.main() == 0
    assert "非交易日" in capsys.readouterr().out


def test_main_ok_prints_level(monkeypatch, capsys):
    async def _ok(*, today=None, redis=None):
        return {
            "date": DAY.isoformat(),
            "ok": True,
            "level": "caution",
            "label": "谨慎",
            "computed": "caution",
            "debounced": False,
            "inputs": {"vol20": 1.3},
        }

    monkeypatch.setattr(P, "run_tier_decision", _ok)
    assert P.main() == 0
    out = capsys.readouterr().out
    assert "caution" in out and "谨慎" in out


def test_worker_switch_and_cancel_wired_in_trade_main():
    """trade/main.py 必须能起这个 worker、且 shutdown 时收得回来。"""
    from pathlib import Path

    from backend.shared.scheduler_registry import JOBS_BY_KEY

    src = (Path(__file__).resolve().parents[1] / "services/trade/main.py").read_text(
        encoding="utf-8"
    )
    spec = JOBS_BY_KEY["risk_tier"]
    assert spec.switch_env and spec.switch_env in src, "开关名必须与注册表一致"
    assert "run_risk_tier_worker()" in src
    assert "risk_tier_task" in src
    # shutdown 的 cancel 元组必须包含它，否则退出时任务泄漏。
    # **按元组成员判定，不按排版**：原先断言的是两个字面量写在同一行
    # （``"tdx_hot_set_feed_task, risk_tier_task"``），而这一行早就被格式化成各占
    # 一行——断言因此恒假，「红灯的守卫等于没有守卫」（2026-09-24 决策轮接线时发现）。
    cancel = re.search(r"for task in \(([^)]*)\)", src)
    assert cancel is not None, "找不到 shutdown 的取消清单"
    cancelled = {n.strip() for n in cancel.group(1).split(",") if n.strip()}
    assert "risk_tier_task" in cancelled


def test_decide_time_matches_registry_schedule():
    """注册表文案里的时点与代码常量同源——两处漂移时测试红。"""
    from backend.shared.scheduler_registry import JOBS_BY_KEY

    spec = JOBS_BY_KEY["risk_tier"]
    hhmm = f"{P.DECIDE_HHMM[0]:02d}:{P.DECIDE_HHMM[1]:02d}"
    assert hhmm in spec.schedule
    assert spec.owner == "trade" and spec.kind == "worker"
