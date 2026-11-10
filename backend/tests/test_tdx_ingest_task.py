"""档位主循环行为测试：纯函数（到点/宽限/目标日/触发键）+ run_dataset 状态机。

全部离线：假桥客户端 + 假令牌桶 + monkeypatch sink（不碰真桥/真 PG/真 Redis）。
真桥路径由 `test_tdx_ingest_bridge.py`（integration）覆盖；本文件钉的是
**分类与记账语义**——每种结局（ok/partial/suspended/blocked/rate_limited/
error）在台账、水位、`done` 判定上的表现，以及档位纯函数的边界。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from backend.services.live_trading.services.tdx_ingest import sinks
from backend.services.live_trading.services.tdx_ingest import task
from backend.services.live_trading.services.tdx_ingest.bridge_client import BridgeError
from backend.services.live_trading.services.tdx_ingest.datasets import get_dataset_spec
from backend.services.live_trading.services.tdx_ingest.fetchers import FetchContext

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ============ 假件 ============


class _FakeBudget:
    def __init__(self) -> None:
        self.acquired = 0
        self.limited = 0
        self.success = 0
        self.base_rate = 180
        self.effective_rate = 180

    async def acquire(self) -> None:
        self.acquired += 1

    def on_rate_limited(self) -> None:
        self.limited += 1

    def on_success(self) -> None:
        self.success += 1


class _FakeClient:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._result = result
        self._error = error

    async def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, dict(params)))
        if self._error is not None:
            raise self._error
        return self._result


@pytest.fixture
def sinks_spy(monkeypatch):
    """把 sink 全部换成记录桩（run_dataset 的对外副作用面）。"""
    spy = {"runs": [], "cursors": [], "rows": None, "archive": None}

    async def _record_run(**kw):
        spy["runs"].append(kw)

    async def _update_cursor(dataset, window_key, **kw):
        spy["cursors"].append((dataset, window_key, kw))

    async def _upsert(spec, rows):
        spy["rows"] = rows
        return len(rows)

    async def _archive(spec, rows, target_date, run_key=None):
        spy["archive"] = (target_date, run_key, len(rows))
        return f"/tmp/{spec.key}-{target_date}.parquet", len(rows)

    monkeypatch.setattr(sinks, "record_run", _record_run)
    monkeypatch.setattr(sinks, "update_cursor", _update_cursor)
    monkeypatch.setattr(sinks, "upsert_rows", _upsert)
    monkeypatch.setattr(sinks, "write_archive", _archive)
    return spy


def _pricevol_payload():
    return json.loads(
        (FIXTURES / "tdx_ingest_pricevol_sample.json").read_text(encoding="utf-8")
    )


@pytest.fixture(autouse=True)
def _stub_universe(monkeypatch):
    """宇宙一律用桩：单测不碰真 PG 清单、也不让 FakeClient 的固定应答被
    get_stock_list 分支误吃（pricevol 等取数会先要宇宙）。用例体内再
    monkeypatch 的同名桩按设置顺序覆盖本 fixture。"""

    async def _universe(self):
        return ["600036.SH"]

    monkeypatch.setattr(FetchContext, "universe_codes", _universe)


# ============ 档位纯函数 ============


def test_slot_due_at():
    now = datetime(2026, 10, 15, 14, 7, 33)
    assert task.slot_due_at(now, "15:31") == datetime(2026, 10, 15, 15, 31)


def test_due_slots_before_first_slot_is_empty():
    assert task.due_slots(datetime(2026, 10, 15, 15, 30, 59)) == []


def test_due_slots_after_1531():
    due = task.due_slots(datetime(2026, 10, 15, 15, 31, 0))
    assert due == ["15:31"]


def test_due_slots_grace_boundary_1531():
    """宽限 = 到点后 4h：边界当口仍在、过 1 秒即出（不追历史缺口）。"""
    at_edge = datetime(2026, 10, 15, 19, 31, 0)
    just_past = datetime(2026, 10, 15, 19, 31, 1)
    assert "15:31" in task.due_slots(at_edge)
    assert "15:31" not in task.due_slots(just_past)


def test_due_slots_morning_window_and_midnight_no_span():
    """06:30 档 10:30 整仍在宽限内；23:50 的宽限**不跨午夜**（次日 01:00
    是空档——06:30 目标日=上一交易日的档才是补采腿，见注册表）。"""
    assert task.due_slots(datetime(2026, 10, 15, 6, 30, 0)) == ["06:30"]
    assert task.due_slots(datetime(2026, 10, 15, 10, 30, 0)) == ["06:30"]
    assert task.due_slots(datetime(2026, 10, 15, 10, 30, 1)) == []
    assert task.due_slots(datetime(2026, 10, 15, 1, 0, 0)) == []


def test_due_slots_multiple_overlap():
    """21:00 时窗内两档并存（17:20/17:30 都在 4h 宽限内），更早的 16:45 已过窗。"""
    assert task.due_slots(datetime(2026, 10, 15, 21, 0, 0)) == ["17:20", "17:30"]
    # 23:55 只余 23:50（17:30 + 4h = 21:30 已过）
    assert task.due_slots(datetime(2026, 10, 15, 23, 55, 0)) == ["23:50"]


def test_next_slot_at():
    assert task.next_slot_at(datetime(2026, 10, 15, 15, 0)) == datetime(
        2026, 10, 15, 15, 31
    )
    assert task.next_slot_at(datetime(2026, 10, 15, 0, 10)) == datetime(
        2026, 10, 15, 6, 30
    )
    # 06:30 整点当口：now < due 不成立 → 下一档是 15:31
    assert task.next_slot_at(datetime(2026, 10, 15, 6, 30)) == datetime(
        2026, 10, 15, 15, 31
    )
    # 全档过完 → 明日首档（06:30）
    assert task.next_slot_at(datetime(2026, 10, 15, 23, 59)) == datetime(
        2026, 10, 16, 6, 30
    )


# ============ 触发键 / 目标日 ============


def test_parse_trigger_forms():
    assert task.parse_trigger(None) == ("", False)
    assert task.parse_trigger("") == ("", False)
    assert task.parse_trigger(" 20261014 ") == ("20261014", False)
    assert task.parse_trigger('{"target": "20261014", "force": true}') == (
        "20261014",
        True,
    )
    assert task.parse_trigger('{"target": "20261014"}') == ("20261014", False)
    # 坏 JSON：整体当纯日期文本（不丢触发，后续校验兜底）
    assert task.parse_trigger("{bad") == ("{bad", False)


@pytest.mark.asyncio
async def test_resolve_target_plain_slot_is_today():
    now = datetime(2026, 10, 15, 15, 31)
    assert await task.resolve_target("15:31", now) == "20261015"


class _FakeCalendar:
    def __init__(self, ret=None, boom: bool = False) -> None:
        self._ret = ret
        self._boom = boom
        self.calls: list[dict] = []

    async def prev_trading_day(self, **kwargs):
        self.calls.append(kwargs)
        if self._boom:
            raise RuntimeError("calendar down")
        return self._ret


@pytest.mark.parametrize("ret", ["2026-10-09", datetime(2026, 10, 9).date()])
@pytest.mark.asyncio
async def test_resolve_target_0630_uses_prev_trading_day(monkeypatch, ret):
    monkeypatch.setattr(
        "backend.shared.trading_calendar.calendar_service", _FakeCalendar(ret=ret)
    )
    now = datetime(2026, 10, 12, 6, 30)  # 周一清晨
    assert await task.resolve_target("06:30", now) == "20261009"


@pytest.mark.asyncio
async def test_resolve_target_0630_calendar_down_falls_back_to_weekend_approx(
    monkeypatch,
):
    """日历不可用 → 周末近似（周一 06:30 → 上周五），绝不让 06:30 档失去目标日。"""
    monkeypatch.setattr(
        "backend.shared.trading_calendar.calendar_service", _FakeCalendar(boom=True)
    )
    assert await task.resolve_target("06:30", datetime(2026, 10, 12, 6, 30)) == (
        "20261009"
    )


class _KVRedis:
    def __init__(self, store: dict | None = None) -> None:
        self.store = dict(store or {})
        self.deleted: list[str] = []
        self.sets: list[tuple] = []

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.deleted.append(key)
        return self.store.pop(key, None)

    def set(self, key, value, ex=None, nx=False):
        self.sets.append((key, value, nx))
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


@pytest.mark.asyncio
async def test_claim_triggers_get_del_once():
    redis = _KVRedis(
        {
            "qm:tdx_ingest:run_once:zdt": b'{"target": "20261015", "force": true}',
            "qm:tdx_ingest:run_once:pricevol": b"20261014",
        }
    )
    claimed = await task._claim_triggers(redis)
    got = {(spec.key, target, force) for spec, target, force in claimed}
    assert got == {("zdt", "20261015", True), ("pricevol", "20261014", False)}
    assert "qm:tdx_ingest:run_once:zdt" in redis.deleted, "认领即删（防重复跑）"
    assert "qm:tdx_ingest:run_once:zdt" not in redis.store
    assert redis.store.keys() == set(), "两个触发键都被消费"


@pytest.mark.asyncio
async def test_claim_triggers_none_redis_noop():
    assert await task._claim_triggers(None) == []


# ============ 告警去重 ============


@pytest.fixture
def alert_stub(monkeypatch):
    import backend.shared.qq_notify as qq_notify

    calls: list[dict] = []

    def _alert(**kw):
        calls.append(kw)

    monkeypatch.setattr(qq_notify, "alert_async", _alert)
    return calls


@pytest.mark.asyncio
async def test_alert_once_dedups_by_slot_and_target(alert_stub):
    redis = _KVRedis()
    await task._alert_once(redis, "23:50", "20261015", "sc 失败")
    await task._alert_once(redis, "23:50", "20261015", "sc 失败（重试）")
    await task._alert_once(redis, "23:50", "20261014", "另一天")
    assert len(alert_stub) == 2, "同 slot@目标日 只发一次；换目标日另算"


@pytest.mark.asyncio
async def test_alert_once_without_redis_still_alerts(alert_stub):
    """Redis 缺席时去重失效但告警照发——宁可重复，不可静默。"""
    await task._alert_once(None, "23:50", "20261015", "boom")
    assert len(alert_stub) == 1


# ============ run_dataset 状态机 ============


@pytest.mark.asyncio
async def test_run_dataset_ok_path(monkeypatch, sinks_spy):
    client = _FakeClient(result={"ErrorId": "0", "Value": _pricevol_payload()})
    budget = _FakeBudget()
    spec = get_dataset_spec("pricevol")

    res = await task.run_dataset(
        spec,
        slot="15:31",
        target_date="20261015",
        client=client,
        budget=budget,
        redis_client=None,
    )

    assert res["status"] == "ok"
    assert res["rows_written"] == 3
    assert res["rows_archived"] == 3
    assert res["calls"] == 1
    assert res["done"] is True
    assert res["archive"].endswith(".parquet")
    assert client.calls[0][0] == "get_pricevol"
    assert budget.success == 1, "成功后令牌桶收到自愈信号"

    # upsert 行就是解析器输出（symbol 前缀式等由解析器金样守）
    assert {r["symbol"] for r in sinks_spy["rows"]} == {
        "SZ000001",
        "SZ300750",
        "SH600036",
    }
    # 台账 + 水位都记了 ok
    assert sinks_spy["runs"][0]["status"] == "ok"
    assert sinks_spy["runs"][0]["rows_written"] == 3
    assert sinks_spy["cursors"][0][0] == "pricevol"
    assert sinks_spy["cursors"][0][2]["last_target_date"] == "20261015"


@pytest.mark.asyncio
async def test_run_dataset_suspended_never_calls_bridge(sinks_spy):
    client = _FakeClient(result={"ErrorId": "0", "Value": {}})
    res = await task.run_dataset(
        get_dataset_spec("zdt"),
        slot="23:50",
        target_date="20261015",
        client=client,
        budget=_FakeBudget(),
        redis_client=None,
    )
    assert res["status"] == "suspended"
    assert res["done"] is True
    assert client.calls == [], "挂起数据集不得打桥"
    assert "短线宝" in res["error"]
    assert sinks_spy["runs"][0]["status"] == "suspended"


@pytest.mark.asyncio
async def test_run_dataset_suspended_force_goes_to_bridge(monkeypatch, sinks_spy):
    """--force（盘中复测）语义：挂起照跑。universe 用桩避免碰真 PG。"""

    async def _universe(self):
        return ["600036.SH"]

    monkeypatch.setattr(FetchContext, "universe_codes", _universe)
    client = _FakeClient(result={"ErrorId": "0", "Value": {}})
    res = await task.run_dataset(
        get_dataset_spec("zdt"),
        slot="manual",
        target_date="20261015",
        client=client,
        budget=_FakeBudget(),
        redis_client=None,
        force=True,
    )
    assert res["status"] == "ok"
    assert client.calls[0][0] == "get_zdt_data"


@pytest.mark.asyncio
async def test_run_dataset_blocked_family(monkeypatch, sinks_spy):
    async def _universe(self):
        return ["600036.SH"]

    monkeypatch.setattr(FetchContext, "universe_codes", _universe)
    client = _FakeClient(
        error=BridgeError(
            "blocked",
            "get_gpjy_value 桥失败: json has no table_list",
            method="get_gpjy_value",
        )
    )
    res = await task.run_dataset(
        get_dataset_spec("gp"),
        slot="17:30",
        target_date="20261015",
        client=client,
        budget=_FakeBudget(),
        redis_client=None,
    )
    assert res["status"] == "blocked"
    assert res["done"] is True, "blocked 是终结态：不重试"
    assert "table_list" in res["error"]
    assert len(client.calls) == 1, "blocked 即停，不逐批硬撞"


@pytest.mark.asyncio
async def test_run_dataset_require_nonempty_empty_marks_blocked(sinks_spy):
    """SC 家族桥返回 ErrorId=0 但 Value=null：空集不是合法结果，必须记 blocked。

    2026-11-10 实测 SC01–SC42 对 3 周区间全返 null，旧逻辑把空集写成 ok，
    面板一直是绿的——「存了看不见」的又一处。
    """
    client = _FakeClient(result={"ErrorId": "0", "Value": None})
    res = await task.run_dataset(
        get_dataset_spec("sc"),
        slot="23:50",
        target_date="20261015",
        client=client,
        budget=_FakeBudget(),
        redis_client=None,
    )
    assert res["status"] == "blocked"
    assert res["rows_written"] == 0
    assert "空" in res["error"]
    assert sinks_spy["runs"][0]["status"] == "blocked"


@pytest.mark.asyncio
async def test_run_dataset_rate_limited_retries_then_reports(sinks_spy):
    client = _FakeClient(error=BridgeError("rate_limited", "RATE_LIMITED"))
    budget = _FakeBudget()
    res = await task.run_dataset(
        get_dataset_spec("pricevol"),
        slot="15:31",
        target_date="20261015",
        client=client,
        budget=budget,
        redis_client=None,
    )
    assert res["status"] == "rate_limited"
    assert res["done"] is False, "非终结态：下个档期/下轮 tick 再来"
    assert res["calls"] == 3, "fetch 内共 3 次尝试（含首次）"
    assert budget.limited == 2, "前两次失败回报降速；第三次上抛"
    assert sinks_spy["runs"][0]["status"] == "rate_limited"


@pytest.mark.asyncio
async def test_run_dataset_bridge_error_is_error(sinks_spy):
    client = _FakeClient(error=BridgeError("bridge", "get_pricevol 返回 result=None"))
    res = await task.run_dataset(
        get_dataset_spec("pricevol"),
        slot="15:31",
        target_date="20261015",
        client=client,
        budget=_FakeBudget(),
        redis_client=None,
    )
    assert res["status"] == "error"
    assert res["done"] is False
    assert "result=None" in res["error"]


@pytest.mark.asyncio
async def test_run_dataset_unexpected_exception_is_contained(sinks_spy):
    """单数据集任意异常不得炸整档——归类 error 并把异常类名带进台账。"""
    client = _FakeClient(error=ValueError("boom"))
    res = await task.run_dataset(
        get_dataset_spec("pricevol"),
        slot="15:31",
        target_date="20261015",
        client=client,
        budget=_FakeBudget(),
        redis_client=None,
    )
    assert res["status"] == "error"
    assert res["error"].startswith("ValueError: boom")


@pytest.mark.asyncio
async def test_run_dataset_archive_failure_is_partial(monkeypatch, sinks_spy):
    """PG 已成、归档失败 = partial（数据在，档案缺）——这是**终结态**，
    不允许因档案重跑把同一批行反复 upsert。"""

    async def _boom(spec, rows, target_date, run_key=None):
        raise RuntimeError("disk full")

    monkeypatch.setattr(sinks, "write_archive", _boom)
    client = _FakeClient(result={"ErrorId": "0", "Value": _pricevol_payload()})
    res = await task.run_dataset(
        get_dataset_spec("pricevol"),
        slot="15:31",
        target_date="20261015",
        client=client,
        budget=_FakeBudget(),
        redis_client=None,
    )
    assert res["status"] == "partial"
    assert res["rows_written"] == 3
    assert res["done"] is True
    assert res["error"].startswith("归档失败")
    assert sinks_spy["runs"][0]["status"] == "partial"


# ============ run_trigger / run_slot / 主循环开关 ============


@pytest.fixture
def captured_run_dataset(monkeypatch):
    captured: list[dict] = []

    async def _stub(spec, **kw):
        captured.append({"key": spec.key, **kw})
        return {"key": spec.key, "status": "ok", "done": True}

    monkeypatch.setattr(task, "run_dataset", _stub)
    return captured


@pytest.mark.asyncio
async def test_run_trigger_valid_target_passthrough(captured_run_dataset):
    await task.run_trigger(
        get_dataset_spec("pricevol"),
        "20261015",
        False,
        client=None,
        budget=None,
        redis_client=None,
    )
    assert captured_run_dataset[0]["target_date"] == "20261015"
    assert captured_run_dataset[0]["slot"] == "manual"
    assert captured_run_dataset[0]["force"] is True, "显式触发一律 force"


@pytest.mark.asyncio
async def test_run_trigger_bad_target_falls_back_to_today(
    monkeypatch, captured_run_dataset
):
    async def _is_td(day):
        return True

    monkeypatch.setattr(task, "_is_trading_day", _is_td)
    await task.run_trigger(
        get_dataset_spec("pricevol"),
        "2026-10-15",  # 非法写法（非 YYYYMMDD）
        False,
        client=None,
        budget=None,
        redis_client=None,
    )
    from backend.services.live_trading.services.trading_session import TZ

    assert captured_run_dataset[0]["target_date"] == datetime.now(TZ).strftime("%Y%m%d")


@pytest.mark.asyncio
async def test_run_trigger_nontrading_day_falls_back_to_prev(
    monkeypatch, captured_run_dataset
):
    async def _is_td(day):
        return False

    monkeypatch.setattr(task, "_is_trading_day", _is_td)
    monkeypatch.setattr(
        "backend.shared.trading_calendar.calendar_service",
        _FakeCalendar(ret="2026-10-09"),
    )
    await task.run_trigger(
        get_dataset_spec("pricevol"),
        "",
        False,
        client=None,
        budget=None,
        redis_client=None,
    )
    assert captured_run_dataset[0]["target_date"] == "20261009"


@pytest.mark.asyncio
async def test_run_slot_all_terminal_no_alert(monkeypatch, alert_stub):
    async def _stub(spec, **kw):
        return {"key": spec.key, "status": "ok", "done": True}

    monkeypatch.setattr(task, "run_dataset", _stub)
    ok = await task.run_slot(
        "15:31",
        "20261015",
        client=None,
        budget=None,
        redis_client=None,
    )
    assert ok is True
    assert alert_stub == []


@pytest.mark.asyncio
async def test_run_slot_error_alerts_and_not_terminal(monkeypatch, alert_stub):
    statuses = {"gp": "ok", "sc": "error", "bk": "ok", "zdt": "suspended"}

    async def _stub(spec, **kw):
        return {"key": spec.key, "status": statuses[spec.key]}

    monkeypatch.setattr(task, "run_dataset", _stub)
    ok = await task.run_slot(
        "23:50",
        "20261015",
        client=None,
        budget=None,
        redis_client=None,
    )
    assert ok is False, "有 error 即非终结（回 tick 重试，MAX 3 次）"
    assert len(alert_stub) == 1 and "sc" in alert_stub[0]["content"]


@pytest.mark.asyncio
async def test_run_slot_force_keys_passthrough(monkeypatch):
    seen: dict[str, bool] = {}

    async def _stub(spec, **kw):
        seen[spec.key] = kw["force"]
        return {"key": spec.key, "status": "ok"}

    monkeypatch.setattr(task, "run_dataset", _stub)
    await task.run_slot(
        "23:50",
        "20261015",
        client=None,
        budget=None,
        redis_client=None,
        force_keys={"zdt"},
    )
    assert seen == {"gp": False, "sc": False, "bk": False, "zdt": True}


@pytest.mark.asyncio
async def test_main_loop_disabled_returns_immediately(monkeypatch):
    """TDX_INGEST_ENABLED=false → 任务直接退役（不建客户端、不打桥）。"""
    monkeypatch.setenv("TDX_INGEST_ENABLED", "false")
    await asyncio.wait_for(task.run_tdx_ingest_task(), timeout=2)
