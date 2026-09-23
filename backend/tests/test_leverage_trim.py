"""减仓执行器接线层（``leverage_trim``）的契约测试（P2.6）。

替身全部注入（``TrimDeps`` 逐项换掉），**不碰网络/账户/真 Redis**。这些用例钉的是
「真钱动作的每一步姿态」：

* 不满足前提时**一次委托都不提交**（暂停/非交易时段/实盘闸关/券商不对/档位无键）；
* 该减却减不动 → ``blocked`` + 一次告警；缺键（档位有意不给）→ ``idle`` 且**不读账户**；
* 报价走保护价（``max(跌停价, 现价×0.99)``）、备注带 ``trim:``、幂等号带代次；
* 废单不变成循环：同标的当日失败到顶即停手，且幂等命中**不计**失败也不递增代次。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from backend.services.trade.services import leverage_trim as lt
from backend.services.trade.services import leverage_trim_core as core
from backend.services.trade.services import leverage_trim_io as io
from backend.services.trade.services import leverage_trim_submit as sub
from backend.services.trade.services import leverage_trim_runner as runner
from backend.services.trade.services.decision_executor import InflightRead

CST = ZoneInfo("Asia/Shanghai")
_DAY = "2026-09-24"
_NOW = datetime(2026, 9, 24, 10, 0, tzinfo=CST)


# ── 替身 ────────────────────────────────────────────────────────────
class FakeRedis:
    def __init__(self, config: dict | None = None) -> None:
        self.store: dict = {}
        self.lists: dict = {}
        if config:
            self.store[io.CONFIG_KEY] = config

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):  # noqa: A002 - 对齐 redis-py 形参名
        self.store[key] = value

    def hgetall(self, key):
        return self.store.get(key) or {}

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[: end + 1]


class FakeClient:
    """QMT 执行端替身：账户、持仓、行情、合约详情四路都可单独失败。"""

    def __init__(
        self,
        *,
        asset: dict | None = None,
        positions: list | None = None,
        ticks: dict | None = None,
        details: dict | None = None,
        asset_error: Exception | None = None,
        tick_error: Exception | None = None,
        detail_error: Exception | None = None,
    ) -> None:
        self.asset = (
            asset
            if asset is not None
            else {
                "total_asset": 1_000_000.0,
                "cash": 100_000.0,
                "market_value": 900_000.0,
            }
        )
        self.positions = positions if positions is not None else []
        self.ticks = ticks or {}
        self.details = details or {}
        self.asset_error = asset_error
        self.tick_error = tick_error
        self.detail_error = detail_error
        self.asset_calls = 0
        self.tick_calls: list[list[str]] = []
        self.detail_calls: list[str] = []

    async def get_asset(self):
        self.asset_calls += 1
        if self.asset_error:
            raise self.asset_error
        return dict(self.asset)

    async def get_positions(self):
        return list(self.positions)

    async def get_full_tick(self, codes):
        self.tick_calls.append(list(codes))
        if self.tick_error:
            raise self.tick_error
        return {code: self.ticks[code] for code in codes if code in self.ticks}

    async def get_instrument_detail(self, code):
        self.detail_calls.append(code)
        if self.detail_error:
            raise self.detail_error
        if code in self.details:  # 显式给空字典 = 「要这份空详情」，不许被默认值顶掉
            return dict(self.details[code])
        return {"DownStopPrice": 88.0, "PreClose": 100.0}


class FakeDispatch:
    def __init__(self, responses: list[dict] | None = None) -> None:
        self.calls: list[dict] = []
        self.responses = list(responses or [])

    async def __call__(self, order_data: dict) -> dict:
        self.calls.append(dict(order_data))
        if self.responses:
            return self.responses.pop(0)
        return {"status": "success", "execution": "direct", "order_id": "o-1"}


class FakeNotify:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(
        self, user_id, title, content, level="info", tenant_id="default"
    ):
        self.calls.append(
            {"user_id": user_id, "title": title, "content": content, "level": level}
        )
        return True


class FakeTier:
    def __init__(
        self, budget: dict | None, level: str = "defensive", source: str = "producer"
    ):
        self.budget = budget if budget is not None else {}
        self.level = level
        self.source = source
        self.date = _DAY


def _tier_budget(max_lev: float = 1.0, trim_to: float = 1.0) -> dict:
    return {
        "leverage_max": max_lev,
        "per_stock_pct": 0.10,
        "max_new_buys": 1,
        "leverage_trim_to": trim_to,
        "per_stock_pos_pct": 0.15,
    }


def _position(
    code: str,
    volume: float,
    *,
    can_use: float | None = None,
    mv: float | None = None,
    name: str = "",
) -> dict:
    return {
        "stock_code": code,
        "volume": volume,
        "can_use_volume": volume if can_use is None else can_use,
        "market_value": mv if mv is not None else 0.0,
        "instrument_name": name,
    }


def _tick(price: float, pre_close: float | None = None) -> dict:
    return {
        "lastPrice": price,
        "lastClose": pre_close if pre_close is not None else price,
    }


def _deps(
    client: FakeClient,
    redis: FakeRedis | None = None,
    *,
    tier: FakeTier | None = None,
    inflight: InflightRead | None = None,
    inflight_error: Exception | None = None,
    broker: str = "qmt_exec",
    broker_error: Exception | None = None,
    real: bool = True,
    trading: bool = True,
    dispatch: FakeDispatch | None = None,
    notify: FakeNotify | None = None,
    risk_config: Any = None,
    risk_config_error: Exception | None = None,
) -> tuple[io.TrimDeps, FakeDispatch, FakeNotify, FakeRedis]:
    redis = redis or FakeRedis()
    disp = dispatch or FakeDispatch()
    note = notify or FakeNotify()

    def _broker() -> str:
        if broker_error:
            raise broker_error
        return broker

    async def _inflight() -> InflightRead:
        if inflight_error is not None:
            raise inflight_error
        return inflight if inflight is not None else InflightRead()

    def _load_risk_config():
        if risk_config_error:
            raise risk_config_error
        return risk_config

    deps = io.TrimDeps(
        client=client,
        redis=redis,
        dispatch=disp,
        notify=note,
        load_tier=lambda: tier if tier is not None else FakeTier(_tier_budget()),
        read_inflight=_inflight,
        selected_broker=_broker,
        real_enabled=lambda: real,
        is_trading_time=lambda: trading,
        now=lambda: _NOW,
        user_id="10000001",
        load_risk_config=_load_risk_config,
    )
    return deps, disp, note, redis


def _risk_config(enabled: bool = True, shadow: bool = False, **params: Any):
    """``RiskConfig`` 的鸭子替身：``enabled`` 且非影子期 = 配置侧正在强制这条规则。"""
    return SimpleNamespace(
        enabled=enabled,
        shadow=shadow,
        rules={"l1.leverage_cap": dict(params)},
    )


class _IdempotentDispatch(FakeDispatch):
    """派发层替身的**先查后插**语义：同一 ``client_order_id`` 第二次进来返回幂等命中。

    真派发层（``internal_strategy_dispatcher``）就是按
    ``orders.client_order_id`` 唯一索引先查后插，且**不看已有行的状态**——被拒的委托行
    也是落过库的。少了这一条，「代次复用」这个真实故障模式在测试里根本复现不出来。
    """

    def __init__(self, response: dict | None = None) -> None:
        super().__init__()
        self.response = dict(
            response
            or {"status": "rejected", "execution": "lot_blocked", "order_id": "o-x"}
        )
        self.seen: set[str] = set()

    async def __call__(self, order_data: dict) -> dict:
        self.calls.append(dict(order_data))
        cid = str(order_data.get("client_order_id") or "")
        if cid in self.seen:
            return {
                "status": "success",
                "execution": "duplicate_skipped",
                "order_id": f"o-{cid}",
            }
        self.seen.add(cid)
        return dict(self.response)


def _over_limit_account() -> tuple[FakeClient, dict]:
    """杠杆 1.3 的账户：权益 1,000,000、持仓 1,300,000（一只票 13,000 股 × 100 元）。"""
    client = _over_limit_client()
    return client, dict(client.asset)


def _over_limit_client() -> FakeClient:
    """同上的柜台替身（只读场景用它：``_over_limit_account`` 的第二项是资产原始字典）。"""
    return FakeClient(
        asset={"total_asset": 1_000_000.0, "cash": 0.0, "market_value": 1_300_000.0},
        positions=[_position("600036.SH", 13_000.0, mv=1_300_000.0)],
        ticks={"600036.SH": _tick(100.0)},
    )


# ── 配置 ────────────────────────────────────────────────────────────
def test_load_config_defaults_when_key_absent() -> None:
    cfg = io.load_config(FakeRedis())
    assert cfg["paused"] is False
    assert cfg["interval_sec"] == 60
    assert cfg["protect_price_mode"] == "aggressive"


@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on"])
def test_load_config_parses_paused(raw: str) -> None:
    assert io.load_config(FakeRedis({"paused": raw}))["paused"] is True


def test_load_config_clamps_interval_and_survives_junk() -> None:
    """节拍**两端都夹**（评审 M6）：下限防打爆柜台，上限防「配错一位就整日不再巡」。

    一个 9999 的配置值若被照读，执行器会在超限的账户上一次都不再动作，而 heartbeat
    还在跳——从外面看它是活的。
    """
    assert io.load_config(FakeRedis({"interval_sec": "1"}))["interval_sec"] == 5
    assert io.load_config(FakeRedis())["interval_sec"] == 60
    assert io.load_config(FakeRedis({"interval_sec": "9999"}))["interval_sec"] == 120
    assert io.load_config(FakeRedis({"interval_sec": "abc"}))["interval_sec"] == 60


def test_load_config_survives_redis_failure() -> None:
    class Broken:
        def hgetall(self, key):
            raise RuntimeError("redis down")

    cfg = io.load_config(Broken())
    assert cfg["paused"] is False  # 读失败不回退成「暂停」——真正的护栏在档位/账户那两道


# ── build_legs：柜台 → 腿输入 ───────────────────────────────────────
def test_build_legs_maps_position_and_tick() -> None:
    legs = io.build_legs(
        [_position("600036.SH", 1000.0, can_use=800.0, mv=99_000.0, name="招商银行")],
        {"600036.SH": _tick(100.0, 99.0)},
        inflight_keys=frozenset({("600036.SH", "sell")}),
        trade_date=date(2026, 9, 24),
        is_st_of=lambda symbol: False,
        threshold_of=lambda symbol, **kw: 0.10,
    )
    leg = legs[0]
    assert leg.symbol == "600036.SH"
    assert leg.volume == 1000.0
    assert leg.available == 800.0
    assert leg.price == 100.0
    assert leg.day_chg_ratio == pytest.approx(100.0 / 99.0 - 1)
    assert leg.limit_threshold_ratio == pytest.approx(0.10)
    assert leg.inflight is True
    assert leg.name == "招商银行"


def test_build_legs_normalizes_prefix_codes() -> None:
    """柜台有时给前缀式：必须归一到后缀式，否则行情查空、在途匹配不上。"""
    legs = io.build_legs(
        [_position("SH600036", 100.0)],
        {"600036.SH": _tick(10.0)},
        inflight_keys=frozenset(),
        trade_date=date(2026, 9, 24),
        is_st_of=lambda symbol: False,
        threshold_of=lambda symbol, **kw: 0.10,
    )
    assert legs[0].symbol == "600036.SH"
    assert legs[0].price == 10.0


def test_build_legs_missing_tick_is_no_quote_not_zero() -> None:
    legs = io.build_legs(
        [_position("600036.SH", 1000.0, mv=12_345.0)],
        {},
        inflight_keys=frozenset(),
        trade_date=date(2026, 9, 24),
        is_st_of=lambda symbol: False,
        threshold_of=lambda symbol, **kw: 0.10,
    )
    assert legs[0].price is None
    assert legs[0].value == pytest.approx(12_345.0)  # 估值退回柜台自报市值


def test_build_legs_st_unknown_means_threshold_unknown() -> None:
    """ST 名称不可得（``None``）→ 阈值不猜（``None`` ⇒ 核心不判跌停）。"""
    calls: list = []
    legs = io.build_legs(
        [_position("600036.SH", 1000.0)],
        {"600036.SH": _tick(100.0)},
        inflight_keys=frozenset(),
        trade_date=date(2026, 9, 24),
        is_st_of=lambda symbol: None,
        threshold_of=lambda *a, **kw: calls.append(a) or 0.10,
    )
    assert legs[0].limit_threshold_ratio is None
    assert calls == []


def test_build_legs_threshold_failure_does_not_break_the_round() -> None:
    def _boom(symbol, **kw):
        raise RuntimeError("行情库不在")

    legs = io.build_legs(
        [_position("600036.SH", 1000.0)],
        {"600036.SH": _tick(100.0)},
        inflight_keys=frozenset(),
        trade_date=date(2026, 9, 24),
        is_st_of=lambda symbol: False,
        threshold_of=_boom,
    )
    assert legs[0].limit_threshold_ratio is None  # 取不到就不判，不抛


def test_build_legs_skips_junk_rows() -> None:
    legs = io.build_legs(
        ["oops", {}, None, _position("", 100.0), _position("600036.SH", 100.0)],
        {"600036.SH": _tick(10.0)},
        inflight_keys=frozenset(),
        trade_date=date(2026, 9, 24),
        is_st_of=lambda symbol: False,
        threshold_of=lambda symbol, **kw: 0.10,
    )
    assert [leg.symbol for leg in legs] == ["600036.SH"]


# ── equity_from_asset：分母口径 ─────────────────────────────────────
def test_equity_prefers_account_reported_total_asset() -> None:
    assert io.equity_from_asset(
        {"total_asset": 1_000_000.0, "cash": 1.0, "market_value": 2.0}
    ) == pytest.approx(1_000_000.0)


def test_equity_rebuilds_from_cash_plus_market_value() -> None:
    assert io.equity_from_asset(
        {"cash": 400.0, "market_value": 600.0}
    ) == pytest.approx(1000.0)


def test_equity_is_none_when_market_value_missing() -> None:
    """有现金没市值 → 不拿现金冒充净资产（那会把杠杆算小、把该减的仓放过去）。"""
    assert io.equity_from_asset({"cash": 400.0}) is None
    assert (
        io.equity_from_asset({"total_asset": 0.0, "cash": 0.0, "market_value": 0.0})
        is None
    )
    assert io.equity_from_asset({}) is None


# ── 前置闸：不满足前提时一次委托都不提交 ───────────────────────────
def test_paused_with_real_trading_on_reports_what_it_would_do() -> None:
    """暂停 = **只报不卖**（隔壁 leverage_guard 的口径）。

    暂停不是「什么都别做」，而是「别下手、但要说清本该做什么」：运维解暂停之前
    最需要知道的就是「这段时间账户还在不在越限」。所以这一轮照读账、照算腿、
    照定价，只有提交被按住。
    """
    client, _ = _over_limit_account()
    redis = FakeRedis({"paused": "true"})
    deps, disp, notify, _ = _deps(client, redis)

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_IDLE
    assert lt.PAUSED_PREFIX in summary["reason"]
    assert summary["paused_would"] == core.ACTION_TRIM  # 本该减仓
    assert summary["leverage"] == pytest.approx(1.3)  # 账真的读了
    assert [leg["symbol"] for leg in summary["legs"]] == ["600036.SH"]
    assert summary["legs"][0]["price"] > 0  # 报价链也真的走了
    assert disp.calls == []  # 一笔未提交
    assert notify.calls == []  # 暂停是运维自己按的手，不喊人


def test_paused_does_not_burn_the_daily_attempt_budget() -> None:
    """暂停期间的「失败」不得记进当日尝试次数。

    记进去就会出现最坏的自伤：暂停一整天 → 次数顶到上限 → 解暂停后第一拍
    直接判定「连续失败已停手」而拒绝出动。沿用 dry-run 的同一套抑制机制。
    """
    client, _ = _over_limit_account()
    redis = FakeRedis({"paused": "true"})
    deps, _, _, _ = _deps(client, redis)

    asyncio.run(lt.run_trim_cycle(deps))

    state = io.load_state(redis, _DAY)
    assert not state.get("attempts")
    assert not state.get("submitted")


def test_paused_with_real_trading_off_does_not_even_read_account() -> None:
    """两道闸都关着 = 这座账户没有真单在跑：没有要保护的东西，连账都不读。

    与上一例配对：只报不卖的前提是「真有仓位在跑」，否则每 60s 白读一次柜台。
    """
    client, _ = _over_limit_account()
    redis = FakeRedis({"paused": "true"})
    deps, disp, notify, _ = _deps(client, redis, real=False)

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_IDLE
    assert "暂停" in summary["reason"]
    assert summary["paused_would"] is None
    assert client.asset_calls == 0
    assert disp.calls == []
    assert notify.calls == []


def test_paused_reports_blocked_when_account_is_unreadable() -> None:
    """暂停中账户读不出：落 idle（没动手）但原因里要看得见故障，且**不告警**。"""
    client, _ = _over_limit_account()
    client.asset_error = RuntimeError("qmt 掉线")
    redis = FakeRedis({"paused": "true"})
    deps, disp, notify, _ = _deps(client, redis)

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_IDLE
    assert summary["paused_would"] == core.ACTION_BLOCKED
    assert "账户" in summary["reason"]
    assert disp.calls == []
    assert notify.calls == []  # 暂停期间只报不喊


def test_real_trading_disabled_stays_idle_without_alarm() -> None:
    client, _ = _over_limit_account()
    deps, disp, notify, _ = _deps(client, tier=FakeTier(_tier_budget()), real=False)

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_IDLE
    assert "实盘闸门关闭" in summary["reason"]
    assert disp.calls == []
    assert notify.calls == []


def test_outside_trading_hours_stays_idle() -> None:
    client, _ = _over_limit_account()
    deps, disp, _, _ = _deps(client, trading=False)

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_IDLE
    assert "非交易时段" in summary["reason"]
    assert disp.calls == []


def test_unreadable_broker_selection_blocks_the_round() -> None:
    """读不到选定值 ≠ 没选过：不知道下单会去哪座账户 → 不读账、不动作。"""
    client, _ = _over_limit_account()
    deps, disp, notify, _ = _deps(client, broker_error=RuntimeError("redis 挂了"))

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_BLOCKED
    assert "券商选定读取失败" in summary["reason"]
    assert client.asset_calls == 0
    assert disp.calls == []
    assert len(notify.calls) == 1


def test_broker_mismatch_stays_idle() -> None:
    """读的账户必须就是下单去的那座：选定别家时本执行器没有（也不该有）读取口径。"""
    client, _ = _over_limit_account()
    deps, disp, notify, _ = _deps(client, broker="tdx_bridge")

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_IDLE
    assert "tdx_bridge" in summary["reason"]
    assert client.asset_calls == 0
    assert notify.calls == []


def test_tier_without_trim_keys_is_idle_and_reads_nothing() -> None:
    """档位缺减仓键（``FALLBACK_LIMITS`` 形态）= 有意姿态：不读账户、不告警。"""
    from backend.shared.risk.tiers import FALLBACK_LIMITS

    client, _ = _over_limit_account()
    deps, disp, notify, _ = _deps(
        client, tier=FakeTier(dict(FALLBACK_LIMITS), source="fallback")
    )

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_IDLE
    assert "未给出减仓参数" in summary["reason"]
    assert summary["tier"]["source"] == "fallback"
    assert client.asset_calls == 0
    assert notify.calls == []


def test_dirty_tier_document_is_idle_but_alerts_once() -> None:
    """带外目标是脏文档：宁可不动手并喊人，也不把账户减到 1%。

    与「缺键」（``absent``）的关键差别：缺键是有意姿态（静默），脏文档要喊人。
    """
    client, _ = _over_limit_account()
    deps, disp, notify, _ = _deps(client, tier=FakeTier(_tier_budget(1.0, 0.05)))

    summary = asyncio.run(lt.run_trim_cycle(deps))
    asyncio.run(lt.run_trim_cycle(deps))  # 同一份脏文档，当日只响一次

    assert summary["action"] == core.ACTION_IDLE
    assert "带外" in summary["reason"]
    assert summary["limits_kind"] == "dirty"
    assert disp.calls == []
    assert client.asset_calls == 0  # 不可用参数连账户都不读
    assert len(notify.calls) == 1
    assert "脏文档" in notify.calls[0]["title"]


# ── 取数失败 → blocked + 告警去重 ───────────────────────────────────
def test_account_read_failure_blocks_and_alerts_once() -> None:
    client = FakeClient(asset_error=RuntimeError("桥断了"))
    deps, disp, notify, _ = _deps(client, tier=FakeTier(_tier_budget()))

    first = asyncio.run(lt.run_trim_cycle(deps))
    second = asyncio.run(lt.run_trim_cycle(deps))  # 60s 后同一成因再来一轮

    assert first["action"] == core.ACTION_BLOCKED
    assert second["action"] == core.ACTION_BLOCKED
    assert disp.calls == []
    assert len(notify.calls) == 1  # 同一成因当日只响一次


def test_tick_failure_blocks_the_round() -> None:
    client = FakeClient(
        positions=[_position("600036.SH", 100.0)],
        tick_error=RuntimeError("行情通道不可用"),
    )
    deps, disp, notify, _ = _deps(client, tier=FakeTier(_tier_budget()))

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_BLOCKED
    assert "实时行情拉取失败" in summary["reason"]
    assert disp.calls == []


def test_unreadable_inflight_ledger_blocks_the_round() -> None:
    """在途账读不出 → 不去猜有没有重复，本轮不动作（同决策执行段的口径）。"""
    client, _ = _over_limit_account()
    bad = InflightRead(errors=("orders 表读取失败：boom",))
    deps, disp, _, _ = _deps(client, tier=FakeTier(_tier_budget()), inflight=bad)

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_BLOCKED
    assert "在途委托账不可信" in summary["reason"]
    assert disp.calls == []


def test_raising_inflight_reader_blocks_instead_of_escaping_the_round() -> None:
    """在途账**抛异常**（不是返回带 ``errors`` 的记录）也必须留在轮内（评审 M3）。

    生产实现走 ``get_session(read_only=True)``：DB 一抖动异常就穿出整轮，而编排层对轮次
    函数没有 try —— worker 按「相同文本去重」打一行 ERROR、心跳照打，运维面板停上一轮
    摘要、当日计数不动、**一条告警都没有**。读不出在途 = 账不可信 → ``blocked`` +
    每日一次告警，且一笔都不许提交（fail-closed，不是「当作没有在途」）。
    """
    client, _ = _over_limit_account()
    deps, disp, notify, _ = _deps(
        client,
        tier=FakeTier(_tier_budget()),
        inflight_error=RuntimeError("连接池耗尽"),
    )

    first = asyncio.run(lt.run_trim_cycle(deps))
    second = asyncio.run(lt.run_trim_cycle(deps))

    assert first["action"] == core.ACTION_BLOCKED
    assert second["action"] == core.ACTION_BLOCKED
    assert "在途委托读取失败" in first["reason"]
    assert "连接池耗尽" in first["reason"]
    assert disp.calls == []
    assert len(notify.calls) == 1  # 同一成因当日只响一次


def test_unwired_risk_config_leaves_the_tier_line_in_charge() -> None:
    """没接线 ``load_risk_config`` 的调用点（测试替身/旧入口）不许因此崩掉。

    配置侧缺席 = 不构成约束（``enforcing_cap=None``），按档位单独走——与「读配置失败」
    是两件事：那是 ``cap-unusable``（fail-closed）。这条同时钉住取数分支本身。
    """
    client, _ = _over_limit_account()
    deps, disp, _, _ = _deps(client, tier=FakeTier(_tier_budget()))
    deps = replace(deps, load_risk_config=None)

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_TRIM  # 档位照常压回，不因缺少配置侧而停手
    assert [c["client_order_id"] for c in disp.calls]  # 真提交了


def test_full_exit_leg_carries_the_full_position_sell_flag() -> None:
    """整仓卖光的腿带 ``full_position_sell``（评审 M4）。

    派发层的整手预检只能看**当日快照**的可用量；本执行器读的是柜台**实时**持仓。
    持仓 2,600 股但实时可用只有 200（T+1 未解锁）时，缺口 600 股只能把可用的 200 股
    全清——碎股，快照一旦比实时大就会被判 ``lot_blocked``，而委托行已落库（HIGH-1
    的同一机制），该清的仓永远清不掉。断言的是**订单报文**，不是内部字段。
    """
    client = FakeClient(
        asset={"total_asset": 200_000.0, "cash": 0.0, "market_value": 260_000.0},
        positions=[_position("600036.SH", 2_600.0, can_use=200.0, mv=260_000.0)],
        ticks={"600036.SH": _tick(100.0)},
    )
    deps, disp, _, _ = _deps(client, tier=FakeTier(_tier_budget()))

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_TRIM
    assert disp.calls[0]["quantity"] == pytest.approx(200.0)
    assert disp.calls[0]["full_position_sell"] is True


def test_partial_leg_does_not_carry_the_full_position_sell_flag() -> None:
    """部分卖出的腿**不许**带整仓断言：那会让派发层跳过本该做的整手预检。"""
    client, _ = _over_limit_account()  # 持仓 13,000 股可卖 13,000，缺口只要 3,000
    deps, disp, _, _ = _deps(client, tier=FakeTier(_tier_budget()))

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_TRIM
    assert disp.calls[0]["quantity"] == pytest.approx(3_000.0)
    assert disp.calls[0]["full_position_sell"] is False


def test_missing_reported_value_with_no_legs_blocks_the_round() -> None:
    """空仓时柜台自报的是 0.0；自报缺失 + 逐腿为空 = 读取异常，不许读成「未超限」。"""
    client = FakeClient(asset={"total_asset": 1_000_000.0, "cash": 0.0}, positions=[])
    deps, disp, _, _ = _deps(client, tier=FakeTier(_tier_budget()))

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_BLOCKED
    assert "读取异常" in summary["reason"]
    assert disp.calls == []


def test_clean_flat_account_is_idle() -> None:
    """真·空仓（自报 0.0）不算异常：杠杆 0 ≤ 上限 → idle。"""
    client = FakeClient(
        asset={"total_asset": 1_000_000.0, "cash": 1_000_000.0, "market_value": 0.0},
        positions=[],
    )
    deps, disp, notify, _ = _deps(client, tier=FakeTier(_tier_budget()))

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_IDLE
    assert "未超上限" in summary["reason"]
    assert notify.calls == []


# ── 超限：真的提交 ──────────────────────────────────────────────────
def test_over_limit_submits_sell_with_protect_price_and_trim_mark() -> None:
    client, _ = _over_limit_account()
    deps, disp, notify, redis = _deps(client, tier=FakeTier(_tier_budget()))

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_TRIM
    assert len(disp.calls) == 1
    order = disp.calls[0]
    assert order["side"] == "SELL"
    assert order["trading_mode"] == "REAL"
    assert order["symbol"] == "600036.SH"
    assert order["quantity"] == pytest.approx(3000.0)  # 缺口 300,000 / 价 100
    assert order["price"] == pytest.approx(99.0)  # max(跌停价 88, 现价×0.99)
    assert order["client_order_id"] == f"trim-600036.SH-{_DAY}-g1"
    assert order["remarks"].startswith("trim:")
    assert len(notify.calls) == 1
    assert "减仓已下单" in notify.calls[0]["title"]
    # 状态键已写：运维端点读的就是它
    assert io.read_status(redis)["action"] == core.ACTION_TRIM


def test_second_trim_same_symbol_same_day_uses_next_generation() -> None:
    """同一天真的该再减一次 → 代次 +1（既挡崩溃重试，也不挡合法的第二次）。"""
    client, _ = _over_limit_account()
    deps, disp, _, _ = _deps(client, tier=FakeTier(_tier_budget()))

    asyncio.run(lt.run_trim_cycle(deps))
    asyncio.run(lt.run_trim_cycle(deps))

    assert [c["client_order_id"] for c in disp.calls] == [
        f"trim-600036.SH-{_DAY}-g1",
        f"trim-600036.SH-{_DAY}-g2",
    ]


def test_duplicate_skipped_is_not_a_fresh_submission() -> None:
    """崩溃重试撞同一号 → ``duplicate_skipped``：**不算新提交、不报平安**。

    三件事一起钉住：

    * 不算「已下单」——计入成功会发一条「减仓已下单」通知，而这一轮**一笔都没发**。
      一个卡住的执行器（在途委托一直不落地）就会每分钟给运维报一次假平安（评审 H2）。
      正确的姿态：``legs`` 里标幂等命中、``duplicates`` 计数、当日一次告警说清在途未落地；
    * 不算失败——不烧当日尝试预算（烧了会在柜台抽风时把「停手」提前触发）；
    * **号就此作废**（``burned``）：撞上的既有行可能是**终态**（此前那轮被拒，行仍在库，
      先查后插不看状态）。不作废就永远撞回同一行，尝试计数一次都不增长（HIGH-1），
      故下一轮必须换号——真在途的那种由 ``read_inflight`` 逐腿跳过，换号不会重复下单。
    """
    client, _ = _over_limit_account()
    dup = {"status": "success", "execution": "duplicate_skipped", "order_id": "o-9"}
    deps, disp, notify, redis = _deps(
        client, tier=FakeTier(_tier_budget()), dispatch=FakeDispatch([dup])
    )

    summary = asyncio.run(lt.run_trim_cycle(deps))

    # 计划与在途都真实存在 → 动作仍是 trim（不是 idle：账户还超着限，缺口靠在途委托补）
    assert summary["action"] == core.ACTION_TRIM
    assert summary["legs"][0]["execution"] == sub._EXEC_DUPLICATE
    assert summary["duplicates"] == 1
    assert "0 腿新发" in summary["reason"]
    state = io.load_state(redis, _DAY)
    assert not state.get("submitted")  # 一笔都没「确认提交」
    assert not state.get("attempts")  # 幂等命中不是失败，不烧尝试预算
    assert state["burned"]["600036.SH"] == 1  # 这个号当日用过了，不许再用
    assert "幂等命中" in summary["legs"][0]["note"]
    # **没有**「已下单」平安通知；只有一条当日一次的「全部腿幂等命中」告警
    assert [c["title"] for c in notify.calls] == [
        "减仓执行器：本轮全部腿幂等命中（无新委托）"
    ]
    assert notify.calls[0]["level"] == "error"

    # 隔一轮再来（同一份状态）仍然只有一条告警：去重键是当日成因，不是每轮一条。
    # 同时**必须换号**（g2）——否则真被拒过的那种会永远撞回同一行。
    deps2, disp2, notify2, _redis2 = _deps(
        client, tier=FakeTier(_tier_budget()), dispatch=FakeDispatch([dup])
    )
    _redis2.store[io.state_key(_DAY)] = json.dumps(io.load_state(redis, _DAY))
    asyncio.run(lt.run_trim_cycle(deps2))
    assert notify2.calls == []
    assert disp2.calls[0]["client_order_id"] == f"trim-600036.SH-{_DAY}-g2"


def test_mixed_round_counts_only_fresh_legs_in_the_notice() -> None:
    """一半新发、一半幂等命中：通知里的腿数**只数新发的那几条**（H2 的第二个出口）。

    混合轮最容易被写成「已下单 2 腿」——运维据此以为两笔都出去了，而其中一笔其实还挂在
    在途。计划、在途、缺口三者在摘要里各归各位。
    """
    client = FakeClient(
        asset={"total_asset": 1_000_000.0, "cash": 0.0, "market_value": 1_300_000.0},
        positions=[
            _position("600036.SH", 7_000.0, mv=700_000.0),
            _position("000001.SZ", 6_000.0, mv=600_000.0),
        ],
        ticks={"600036.SH": _tick(100.0), "000001.SZ": _tick(100.0)},
    )
    dup = {"status": "success", "execution": "duplicate_skipped", "order_id": "o-9"}
    deps, disp, notify, _ = _deps(
        client,
        tier=FakeTier(_tier_budget(trim_to=0.5)),  # 目标 50 万 ⇒ 两条腿才够
        dispatch=FakeDispatch([{"status": "success", "order_id": "o-1"}, dup]),
    )

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert [leg["symbol"] for leg in summary["planned"]] == ["600036.SH", "000001.SZ"]
    assert summary["duplicates"] == 1
    assert summary["action"] == core.ACTION_TRIM
    assert "已提交 1/2 腿" in summary["reason"]
    assert "另有 1 腿幂等命中" in summary["reason"]
    assert len(disp.calls) == 2
    titles = [c["title"] for c in notify.calls]
    assert titles == ["减仓已下单 1 腿"], titles  # 不是 2 腿：幂等命中不算「已下单」
    assert "000001.SZ" not in notify.calls[0]["content"]  # 通知里也不点它的名


def test_price_protection_failure_skips_the_leg_without_dispatch() -> None:
    """拿不到跌停价下限 → 不报价、不下单（绝不退回写死的 −2%）。"""
    client = FakeClient(
        asset={"total_asset": 1_000_000.0, "cash": 0.0, "market_value": 1_300_000.0},
        positions=[_position("600036.SH", 13_000.0, mv=1_300_000.0)],
        ticks={"600036.SH": _tick(100.0)},
        details={"600036.SH": {}},  # 无 DownStopPrice 也无 PreClose
    )
    deps, disp, notify, redis = _deps(client, tier=FakeTier(_tier_budget()))

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert disp.calls == []
    assert summary["action"] == core.ACTION_BLOCKED  # 该减却一腿都没发出去
    assert "保护价不可得" in summary["legs"][0]["note"]
    assert io.load_state(redis, _DAY)["attempts"]["600036.SH"] == 1
    assert len(notify.calls) == 1


def test_rejected_leg_is_recorded_and_counted() -> None:
    rejected = {
        "status": "rejected",
        "execution": "risk_blocked",
        "order_id": "o-2",
        "violations": [{"rule": "l3.price_deviation", "message": "价格偏离"}],
    }
    client, _ = _over_limit_account()
    deps, disp, notify, redis = _deps(
        client, tier=FakeTier(_tier_budget()), dispatch=FakeDispatch([rejected])
    )

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_BLOCKED
    assert "risk_blocked" in summary["legs"][0]["note"]
    assert io.load_state(redis, _DAY)["attempts"]["600036.SH"] == 1
    assert notify.calls  # 全部腿失败 → 告警


def test_gives_up_after_max_attempts_per_symbol_per_day() -> None:
    """废单不刷成循环：当日失败到顶即停手（隔壁 002074：42 笔废单、每 2 分钟一笔）。"""
    rejected = {"status": "rejected", "execution": "lot_blocked", "order_id": "o-3"}
    client, _ = _over_limit_account()
    deps, disp, notify, redis = _deps(
        client,
        tier=FakeTier(_tier_budget()),
        dispatch=FakeDispatch([rejected, rejected, rejected]),
    )

    for _ in range(io.MAX_ATTEMPTS_PER_SYMBOL_PER_DAY + 2):
        asyncio.run(lt.run_trim_cycle(deps))

    assert len(disp.calls) == io.MAX_ATTEMPTS_PER_SYMBOL_PER_DAY  # 第 4、5 轮不再提交
    titles = [c["title"] for c in notify.calls]
    assert any("停手" in t for t in titles)
    state = io.load_state(redis, _DAY)
    assert state["attempts"]["600036.SH"] == io.MAX_ATTEMPTS_PER_SYMBOL_PER_DAY


def test_inflight_leg_is_skipped_by_the_core() -> None:
    client, _ = _over_limit_account()
    inflight = InflightRead(keys=frozenset({("600036.SH", "sell")}), counts={"real": 1})
    deps, disp, _, _ = _deps(client, tier=FakeTier(_tier_budget()), inflight=inflight)

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert disp.calls == []
    assert summary["action"] == core.ACTION_BLOCKED  # 超限但无可执行腿
    assert "在途卖单" in summary["skipped"][0][1]


def test_limit_down_leg_is_not_sold_through_the_wired_path() -> None:
    """已封跌停不接（阈值经 ST 判定链注入，避免依赖仓库外的名称索引）。"""
    client = FakeClient(
        asset={"total_asset": 1_000_000.0, "cash": 0.0, "market_value": 1_300_000.0},
        positions=[_position("600036.SH", 13_000.0, mv=1_300_000.0)],
        # 跌 10.5%（跌停线下）：**刻意不用恰好在 -10.0% 的价**，见下一条用例
        ticks={"600036.SH": {"lastPrice": 89.5, "lastClose": 100.0}},
    )
    legs = io.build_legs(
        client.positions,
        client.ticks,
        inflight_keys=frozenset(),
        trade_date=date(2026, 9, 24),
        is_st_of=lambda symbol: False,
        threshold_of=lambda symbol, **kw: 0.10,
    )
    assert legs[0].day_chg_ratio == pytest.approx(-0.105, abs=1e-6)

    plan = core.plan_trim(
        equity=1_000_000.0,
        position_value=1_300_000.0,
        legs=legs,
        limits=core.limits_from_budget(_tier_budget()).limits,
    )

    assert plan.legs == ()
    assert "跌停" in plan.skipped[0][1]


def test_exact_limit_down_price_is_epsilon_above_the_ratio_line() -> None:
    """**已知口径边界**（不是本执行器的缺陷，故不在此处修）：恰好在跌停价上的报价，
    按比例比较会差一个浮点 epsilon 而判成「未跌停」。

    ``90.00 / 100.00 - 1`` 在 IEEE754 下是 ``-0.09999999999999998``，而阈值 ``0.10``
    是 ``-0.1000000000000000055…``——``chg <= -thr`` 差 2.8e-17，恒假。这是共享谓词
    ``at_limit_down``（决策执行段 ``quotes_for`` 也用它）的固有性质：**改它等于同时改
    既有闸门族的行为**，须单独评审。本例把这个边界钉住，免得后来者以为是本模块的 bug。
    """
    from backend.shared.decision.execution import at_limit_down

    assert 90.0 / 100.0 - 1 > -0.10  # 关键事实：等价写法在浮点下并不等价
    assert at_limit_down(90.0 / 100.0 - 1, 0.10) is False
    assert at_limit_down(89.5 / 100.0 - 1, 0.10) is True  # 掉下这条线才拦


def test_state_and_log_keys_are_written_for_ops() -> None:
    client, _ = _over_limit_account()
    deps, _, _, redis = _deps(client, tier=FakeTier(_tier_budget()))

    asyncio.run(lt.run_trim_cycle(deps))

    assert redis.store.get(io.LAST_KEY)
    assert json.loads(redis.store[io.LAST_KEY])["legs"][0]["symbol"] == "600036.SH"
    assert redis.lists.get(io.LOG_KEY)
    assert io.load_state(redis, _DAY)["submitted"]["600036.SH"] == 1


def test_dry_run_plans_without_submitting_counting_or_alerting() -> None:
    """``--dry-run``：按计划回报，但**不提交、不计失败、不告警、不动状态**。

    少了这三道闸，跑三次演练就会把当日尝试次数顶到上限，真 worker 随后误判
    「连续失败已停手」不再减仓——只读演练把真执行器钉死。

    关键断言是 ``disp.calls == []``：**用真派发替身**（而不是会抛的哨兵）跑，
    才能证明「没提交」出自实现里的闸门，而不是出自「碰巧换了个会抛的 dispatch」
    ——后者抛出的异常会被 ``submit_leg`` 吞成「派发异常」腿，演练看着没提交、
    实则已经把单打到柜台上了。
    """
    client, _ = _over_limit_account()
    deps, disp, notify, redis = _deps(client, tier=FakeTier(_tier_budget()))
    deps.dry_run = True
    deps.rehearsal = True

    summary = asyncio.run(lt.run_trim_cycle(deps))
    asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_TRIM  # 报告的是计划，不是替身拒发
    assert "dry-run" in summary["reason"]
    assert summary["dry_run"] is True
    assert summary["legs"][0]["price"] == pytest.approx(99.0)  # 报价照样算出来
    assert summary["legs"][0]["execution"] == sub._EXEC_DRY_RUN
    assert disp.calls == []  # 真派发替身一次都没被调到
    assert notify.calls == []
    assert io.load_state(redis, _DAY) == {}  # 一次状态都没落


def test_dry_run_never_reaches_the_cli_sentinel() -> None:
    """CLI 的 ``_refuse_dispatch`` 是**兜底哨兵**，不是演练的防线。

    真正的闸在 ``submit_leg``（deps 属性）。哨兵若被调到 = 防线已破，它抛的
    异常会被吞成失败腿，运维只能看到「派发异常」而看不出「未提交」的承诺已破。
    所以这里断言：演练连哨兵都碰不到。
    """
    client, _ = _over_limit_account()
    deps, _, _, _ = _deps(client, tier=FakeTier(_tier_budget()))
    deps.dry_run = True
    hit: list[str] = []

    async def sentinel(order_data: dict) -> dict:
        hit.append(str(order_data.get("client_order_id")))
        raise RuntimeError("dry-run 不允许提交委托")

    deps.dispatch = sentinel
    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert hit == []
    assert "派发异常" not in summary["legs"][0]["note"]


# ── CLI：运维真正敲的那两下 ─────────────────────────────────────────
def _patch_cli(monkeypatch, deps, redis) -> None:
    """换掉 CLI 的两个边界（Redis、生产接线），其余照真跑。"""
    import backend.shared.database_manager_v2 as dbm

    async def _noop() -> None:
        return None

    monkeypatch.setattr(runner, "default_trim_deps", lambda _redis: deps)
    monkeypatch.setattr("backend.services.trade_shared.deps.get_redis", lambda: redis)
    monkeypatch.setattr(dbm, "close_database", _noop)


def test_cli_dry_run_prints_plan_without_dispatching(monkeypatch, capsys) -> None:
    """``--dry-run`` 是运维在盘中确认「这一轮本来会卖什么」的入口。

    它比 worker 多放宽一处：**不看时段**（盘后也能演练同一份取数/计划代码）。
    """
    client, _ = _over_limit_account()
    redis = FakeRedis()
    deps, disp, _, _ = _deps(client, redis, trading=False)  # 盘后照样能演练
    _patch_cli(monkeypatch, deps, redis)

    rc = runner.main(["--once", "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 0
    assert disp.calls == []
    summary = json.loads(captured.out)
    assert summary["dry_run"] is True
    assert summary["action"] == core.ACTION_TRIM
    assert summary["legs"][0]["execution"] == sub._EXEC_DRY_RUN
    assert "未提交" in captured.err


def test_cli_respects_pause_and_force_lifts_it(monkeypatch, capsys) -> None:
    """``--force`` 只抬 ``paused``：真出手，且明说其余三道闸照判。"""
    client, _ = _over_limit_account()
    redis = FakeRedis({"paused": "true"})
    deps, disp, _, _ = _deps(client, redis)
    _patch_cli(monkeypatch, deps, redis)

    # 不加 --force：只报不卖
    runner.main(["--once"])
    assert disp.calls == []
    assert json.loads(capsys.readouterr().out)["paused_would"] == core.ACTION_TRIM

    # 加 --force：真的减
    rc = runner.main(["--once", "--force"])
    captured = capsys.readouterr()
    assert rc == 0
    assert [call["symbol"] for call in disp.calls] == ["600036.SH"]
    summary = json.loads(captured.out)
    assert summary["action"] == core.ACTION_TRIM
    assert summary["paused_would"] is None
    assert "--force" in captured.err  # 提示里点名了它的边界


def test_read_status_survives_garbage() -> None:
    class Broken:
        def get(self, key):
            raise RuntimeError("boom")

    assert io.read_status(Broken()) == {}
    assert io.read_status(FakeRedis()) == {}


# ── 运维端点 ────────────────────────────────────────────────────────
def test_trim_status_endpoint_is_reachable() -> None:
    """端点必须真的挂上去（本仓的 ``include_router`` 是 `_IncludedRouter`，
    ``r.path`` 上取不到路径——枚举只能走 ``openapi()``）。"""
    import warnings

    from fastapi import FastAPI

    from backend.services.trade.routers import risk_ctl

    probe = FastAPI()
    probe.include_router(risk_ctl.router, prefix="/api/v1")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        paths = probe.openapi().get("paths", {})

    assert "get" in paths.get("/api/v1/risk/trim", {})


def test_trim_status_endpoint_payload_shape() -> None:
    """面板要的三块：最近一轮摘要、配置、当日计数（含逐腿明细）。"""
    from backend.services.trade.routers.risk_ctl import risk_trim_status

    client, _ = _over_limit_account()
    deps, _, _, redis = _deps(client, tier=FakeTier(_tier_budget()))
    asyncio.run(lt.run_trim_cycle(deps))

    payload = asyncio.run(risk_trim_status(redis=redis, auth=None))

    assert payload["success"] is True
    data = payload["data"]
    assert data["configured"] is True
    assert data["last"]["action"] == core.ACTION_TRIM
    assert data["last"]["legs"][0]["symbol"] == "600036.SH"
    assert data["config"]["protect_price_mode"] == "aggressive"
    assert data["today"]["submitted"]["600036.SH"] == 1
    assert "blocked" in data["caliber"]  # 口径随数据一起下发（面板不猜语义）


def test_trim_status_endpoint_on_empty_redis_is_not_an_error() -> None:
    """从没跑过（或键已过期）→ ``configured=false`` 的空摘要，不是 500。"""
    from backend.services.trade.routers.risk_ctl import risk_trim_status

    payload = asyncio.run(risk_trim_status(redis=FakeRedis(), auth=None))

    assert payload["data"]["configured"] is False
    assert payload["data"]["last"] == {}


def test_default_deps_wiring_smoke() -> None:
    """生产接线可构造（捕签名漂移；不连柜台、不碰 Redis）。"""
    deps = io.default_trim_deps(redis=FakeRedis())
    assert deps.client is not None
    assert callable(deps.selected_broker)
    assert callable(deps.load_tier)
    assert deps.expected_broker == io.EXPECTED_BROKER
    assert deps.tenant_id == "default"


def test_remark_prefix_is_registered_as_forced_exit() -> None:
    """``trim:`` 必须被风险闸认作强平族——否则一次快速下跌里自己的闸会拒掉自己的单。"""
    from backend.services.trade.services.risk_gate_service import _is_forced_exit

    assert io.REMARK_PREFIX == "trim:"
    assert _is_forced_exit(
        f"{io.REMARK_PREFIX}减仓执行器 激进保护价 99.00（现价 100.00 −1%）"
    )


# ── 2026-09-24 评审回归（C1/H2/H3/M4/M6/L7/L9）───────────────────────
class WrapperShapedRedis:
    """``trade_shared.redis_client.RedisClient`` 的形态：只有 ``get/set``、值按 JSON 编解码、
    **异常被吞成 None**，原生句柄藏在 ``.client``。

    这一层替身是 C1 的判据：用它跑得通，才说明实现走的是 ``_client()`` 解包后的原生句柄，
    而不是「碰巧测试里给的是原生客户端」。
    """

    def __init__(self, native: FakeRedis) -> None:
        self.client = native
        self.raw = native

    def get(self, key):
        try:
            value = self.client.get(key)
            return json.loads(value) if value else None
        except Exception:  # noqa: BLE001 - 包装的行为就是吞
            return None

    def set(self, key, value, ttl=None):
        try:
            self.client.set(key, json.dumps(value), ex=ttl)
        except Exception:  # noqa: BLE001
            pass


class NativeShapedRedis(FakeRedis):
    """原生 ``redis.Redis`` 的形态：它**自己就有** ``client()`` 方法（redis-py 的连接工厂）。

    只判 ``is not None`` 的解包会把这个**方法**当成包装拆出来，随后 ``.hgetall/.get``
    全变成 ``'function' object has no attribute …'``。失败方向是最坏的一种：配置回默认值
    （``paused=False`` → 暂停开关按不动）、状态读成空（当日失败计数恒 0 → 第 3 道防废单闸
    失效、告警去重失效）、状态写静默丢弃（面板空白）。``shared/risk/tiers.py`` 在
    2026-09-23 定档首跑踩过同一个坑（那里的注释写着同一句判据）。
    """

    def client(self):
        raise AssertionError("原生客户端的 .client() 是连接工厂，不该被解包拆出来")


def test_key_io_works_through_a_wrapper_shaped_client() -> None:
    """C1 回归：配置/状态/报告三组键都要能穿过**包装形态**的客户端。

    包装客户端没有 ``hgetall``、``get`` 会把状态 JSON 再解一层、异常被吞成 None——
    用包装客户端读写这三组键，故障表现是「暂停开关按不动、当日计数恒为 0、状态键空白」，
    而代码看起来一切正常（这正是评审 C1 在真环境里的形态）。
    """
    native = FakeRedis({"paused": "true", "interval_sec": "30"})
    wrapped = WrapperShapedRedis(native)

    # 配置：包装客户端连 hgetall 都没有（不解包就是 AttributeError → 静默吃默认值）
    cfg = io.load_config(wrapped)
    assert cfg["paused"] is True and cfg["interval_sec"] == 30

    # 状态：写进去的必须是**一层** JSON（包装的 set 会把 dict 再编一层）
    io.save_state(wrapped, _DAY, {"attempts": {"600036.SH": 2}})
    assert json.loads(native.store[io.state_key(_DAY)]) == {
        "attempts": {"600036.SH": 2}
    }
    assert io.load_state(wrapped, _DAY) == {"attempts": {"600036.SH": 2}}

    # 报告键：``lpush``/``ltrim`` 同样只有原生客户端有
    io.write_status(wrapped, {"action": "idle", "reason": "x"})
    assert io.read_status(wrapped)["action"] == "idle"
    assert native.lists[io.LOG_KEY][0]


def test_key_io_survives_a_native_shaped_client() -> None:
    """``_client`` 的契约「原生原样返回」本身：解包必须**幂等**。

    非幂等的后果不是崩，而是**静默退化**：任何把原生客户端直接喂进本模块的调用方
    （新脚本、新端点、运维手敲的一段）都会读到「默认配置 + 空状态」，也就是 C1 那套
    症状换了个入口原地复活 —— 所以这里断言的是「读到的是存进去的值」，不是「没抛异常」。
    """
    native = NativeShapedRedis({"paused": "true", "interval_sec": "30"})

    assert io._client(native) is native  # 原生进、原生出
    assert io._client(WrapperShapedRedis(native)) is native  # 包装进、原生出

    cfg = io.load_config(native)
    assert cfg["paused"] is True and cfg["interval_sec"] == 30  # 不是默认值

    io.save_state(native, _DAY, {"attempts": {"600036.SH": 1}})
    assert io.load_state(native, _DAY) == {"attempts": {"600036.SH": 1}}

    io.write_status(native, {"action": "idle"})
    assert io.read_status(native)["action"] == "idle"


def test_native_redis_client_factories_agree_on_the_keyspace() -> None:
    """两处原生客户端工厂（决策轮 / 减仓）必须同参同库——否则键位悄悄地分成两座。

    用**包装客户端**取原生句柄时，库由包装的 ``settings.REDIS_DB`` 决定；用
    ``native_redis_client()`` 时由 ``REDIS_DB_TRADE`` 决定。两者一旦不一致，
    「worker 写的状态、运维端点读不到」这类故障会在真环境里长出来。
    """
    import redis as redis_lib

    from backend.services.trade.services import decision_round_io
    from backend.services.trade_shared.trade_config import settings

    seen: list[dict] = []
    real_cls = redis_lib.Redis

    class _Recorder(real_cls):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs):
            seen.append(kwargs)
            super().__init__(**kwargs)

    redis_lib.Redis = _Recorder
    try:
        trim_kwargs = io.native_redis_client()
        round_kwargs = decision_round_io.native_redis_client()
    finally:
        redis_lib.Redis = real_cls

    assert isinstance(trim_kwargs, _Recorder) and isinstance(round_kwargs, _Recorder)
    a, b = seen[0], seen[1]
    assert (a["host"], a["port"], a["db"], a["password"]) == (
        b["host"],
        b["port"],
        b["db"],
        b["password"],
    )
    # 包装客户端（派发链与 ``_client()`` 解包走的都是它）必须落在同一座库
    assert a["db"] == int(settings.REDIS_DB)
    assert a["decode_responses"] is True


def test_io_module_has_no_bare_redis_key_calls() -> None:
    """反漂移：io 层的键位读写一律经 ``_client(redis)``。

    直接 ``redis.hgetall/get/set/lpush/ltrim`` 在包装客户端上要么不存在、要么吞异常——
    这条守卫让「新加一处键位读写时忘了解包」当场变红，而不是等到运维发现暂停按不动。
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "services/trade/services/leverage_trim_io.py"
    ).read_text(encoding="utf-8")
    for banned in (
        "redis.hgetall(",
        "redis.get(",
        "redis.set(",
        "redis.lpush(",
        "redis.ltrim(",
        "self.redis.get(",
    ):
        assert banned not in src, f"io 层出现了未解包的键位调用：{banned}"


def test_dirty_limits_alert_uses_a_stable_key() -> None:
    """H3 回归：脏档位的告警去重键必须是**稳定成因码**，不能带每轮都在变的数。

    旧实现拿 ``reason[:80]`` 当键，而 reason 里带着 ``max=1.2 trim_to=1.15`` 这类数字，
    于是「同一成因当日一次」退化成「每 60s 一次」。
    """
    client, _ = _over_limit_account()
    dirty = FakeTier({"leverage_max": 1.2, "leverage_trim_to": 0.1})  # 带外 → dirty
    now = [_NOW]

    deps, _disp, notify, redis = _deps(client, tier=dirty)
    deps.now = lambda: now[0]
    asyncio.run(lt.run_trim_cycle(deps))
    assert [c["title"] for c in notify.calls] == ["档位减仓参数疑似脏文档"]
    assert io.load_state(redis, _DAY)["alerted"] == ["dirtylimits:out-of-band"]

    # 下一轮：仍是「带外」但**数字变了**（0.1 → 0.3）——成因码不变 ⇒ 不再喊第二次。
    # 旧实现拿 ``reason[:80]`` 当键，这里会喊出第二条（reason 里带着新数字）。
    dirty.budget = {"leverage_max": 1.2, "leverage_trim_to": 0.3}
    deps2, _disp2, notify2, redis2 = _deps(client, tier=dirty)
    redis2.store[io.state_key(_DAY)] = json.dumps(io.load_state(redis, _DAY))
    asyncio.run(lt.run_trim_cycle(deps2))
    assert notify2.calls == []

    # 换了**成因**（带外 → 不可解析）就该再喊：去重是按成因，不是按「今天喊过一次」
    dirty.budget = {"leverage_max": 1.2, "leverage_trim_to": None}
    deps3, _disp3, notify3, redis3 = _deps(client, tier=dirty)
    redis3.store[io.state_key(_DAY)] = json.dumps(io.load_state(redis, _DAY))
    asyncio.run(lt.run_trim_cycle(deps3))
    assert [c["title"] for c in notify3.calls] == ["档位减仓参数疑似脏文档"]


def test_idle_round_writes_no_state_key() -> None:
    """M4/L9：在限额内的一轮**不写状态键**（60s 一拍的空转不该每轮写一次，
    也不该往文档里塞 ``{"attempts": {}…}`` 这种空壳）。"""
    client = FakeClient(
        asset={"total_asset": 1_000_000.0, "cash": 0.0, "market_value": 900_000.0},
        positions=[_position("600036.SH", 9_000.0, mv=900_000.0)],
        ticks={"600036.SH": _tick(100.0)},
    )
    deps, _disp, _notify, redis = _deps(client, tier=FakeTier(_tier_budget()))

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_IDLE
    assert io.state_key(_DAY) not in redis.store  # 键压根不存在，不是「存在且为空」
    assert json.loads(redis.store[io.LAST_KEY])["action"] == core.ACTION_IDLE


def test_state_merge_keeps_the_other_writers_counters() -> None:
    """M4 合并写：同一把状态键有**两个写者**（常驻 worker 与操作员 ``--once``）。

    后写的整份覆盖会把另一进程刚记下的失败次数/已提交笔数抹回旧值——表现为
    「失败计数到不了上限、废单一直重试」。计数器单调，故按标的取大合并。
    """
    native = FakeRedis()
    io.save_state(
        native,
        _DAY,
        {"attempts": {"600036.SH": 2}, "burned": {"600036.SH": 2}, "alerted": ["a"]},
    )
    io.save_state(
        native,
        _DAY,
        {"attempts": {"600036.SH": 1}, "burned": {"600036.SH": 1}, "alerted": ["b"]},
    )

    merged = io.load_state(native, _DAY)
    assert merged["attempts"]["600036.SH"] == 2  # 取大，不回退
    # 作废计数与尝试计数**同一条纪律**：回退会让代次重算成旧号（HIGH-1 的死结）
    assert merged["burned"]["600036.SH"] == 2
    assert merged["alerted"] == ["a", "b"]  # 去重集合取并集


def test_market_order_leg_counts_as_priced() -> None:
    """M6：市价腿的 ``price`` **合法地是 0**，演练回报里不能数成「未定价」。"""
    client, _ = _over_limit_account()
    deps, disp, _notify, _redis = _deps(
        client,
        FakeRedis({"protect_price_mode": "market"}),
        tier=FakeTier(_tier_budget()),
    )
    deps.dry_run = True
    deps.rehearsal = True

    summary = asyncio.run(lt.run_trim_cycle(deps))

    leg = summary["legs"][0]
    assert leg["order_type"] == "MARKET" and leg["price"] == 0.0
    assert sub.is_priced(sub.LegOutcome("600036.SH", 1, 0.0, True, order_type="MARKET"))
    assert "1 腿已定价" in summary["reason"]
    assert disp.calls == []


def test_summary_reports_remaining_value() -> None:
    """L7：计划覆盖不到的缺口必须显式进摘要（``remaining_value``）——「修了多少、
    还剩多少」是运维判断下一拍会不会再动的唯一依据。"""
    client = FakeClient(
        asset={"total_asset": 1_000_000.0, "cash": 0.0, "market_value": 1_300_000.0},
        positions=[
            # 持仓 13,000 股但只有 1,000 股可用（其余是当日买入未解锁）→ 卖不满缺口
            _position("600036.SH", 13_000.0, can_use=1_000.0, mv=1_170_000.0),
            _position("000001.SZ", 1_300.0, can_use=0.0, mv=130_000.0),  # T+1 锁定
        ],
        ticks={"600036.SH": _tick(100.0), "000001.SZ": _tick(100.0)},
    )
    deps, _disp, _notify, _redis = _deps(client, tier=FakeTier(_tier_budget()))

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_TRIM
    # 敞口按**实时价 × 持仓量**重建：14,300 股 × 100 元 = 1,430,000（柜台自报的
    # market_value 只是无价时的兜底），目标 1.0×权益 → 缺口 430,000
    assert summary["position_value"] == pytest.approx(1_430_000.0)
    assert summary["need_value"] == pytest.approx(1_430_000.0 - 1_000_000.0)
    # 可卖量只有 1,000 股 → 本轮只排得出 100,000 元的腿
    assert summary["planned"][0]["quantity"] == pytest.approx(1_000.0)
    assert summary["planned"][0]["value"] == pytest.approx(100_000.0)
    # **修不完就说清还剩多少**（这正是运维判断「下一拍还会不会动」的依据）
    assert summary["remaining_value"] == pytest.approx(330_000.0)
    assert "缺口未覆盖" in summary["reason"]
    # 覆盖不到的腿在 skipped 里说得清（不是静默少卖）
    assert any(sym == "000001.SZ" for sym, _why in summary["skipped"])


def test_trim_converges_and_stops_without_a_rejection_loop() -> None:
    """收敛：按保护价成交时，几轮内减到带宽内并**自己停下**（不出现「拒 → 再试 → 拒」）。

    保护价比现价低 1%（``aggressive``），所以单轮卖得比缺口略少——缺口按几何级数收敛。
    这条用例把「报价 → 成交 → 再读账户」的闭环真的跑起来：成交按**报价**成交（不是按
    现价），断言 3 轮内落 ``idle``、且累计卖出量正好补齐缺口。
    """
    client, _ = _over_limit_account()  # 13,000 股 × 100 元 / 权益 1,000,000 = 1.3×
    market = {"px": 100.0}

    class FillingDispatch:
        """真成交替身：按报单价成交，并把成交从持仓里扣掉（下一轮读到的是新持仓）。"""

        def __init__(self) -> None:
            self.fills: list[tuple[str, float, float]] = []

        async def __call__(self, order_data: dict) -> dict:
            symbol = order_data["symbol"]
            qty = float(order_data["quantity"])
            price = float(order_data["price"])  # 保护价（< 现价）
            self.fills.append((symbol, qty, price))
            proceeds = qty * price
            for pos in client.positions:
                if pos["stock_code"] != symbol:
                    continue
                pos["volume"] = max(0.0, float(pos["volume"]) - qty)
                pos["can_use_volume"] = float(pos["volume"])
                # 卖出 = 持仓变现金：市值三处读数都要跟着动，否则下一轮读到的是旧账
                pos["market_value"] = max(0.0, float(pos["market_value"]) - proceeds)
            client.asset["market_value"] = max(
                0.0, float(client.asset["market_value"]) - proceeds
            )
            client.asset["cash"] = float(client.asset.get("cash") or 0.0) + proceeds
            market["px"] = price  # 成交在保护价上，下一轮的现价就落在那里
            return {
                "status": "success",
                "execution": "direct",
                "order_id": f"o-{len(self.fills)}",
            }

    disp = FillingDispatch()
    rounds: list[dict] = []
    for _ in range(6):
        client.ticks["600036.SH"] = _tick(market["px"], pre_close=100.0)
        deps, _d, _n, _r = _deps(
            client,
            tier=FakeTier(_tier_budget()),
            dispatch=disp,  # type: ignore[arg-type]
        )
        summary = asyncio.run(lt.run_trim_cycle(deps))
        rounds.append(summary)
        if summary["action"] != core.ACTION_TRIM:
            break

    assert len(rounds) <= 3, [r["reason"] for r in rounds]
    assert rounds[-1]["action"] == core.ACTION_IDLE  # 自己停下，不是被判拒到停
    assert rounds[-1]["leverage"] <= 1.0 + 1e-9
    sold = sum(q for _s, q, _p in disp.fills)
    assert sold >= 3_000  # 卖够缺口（10,000 股 = 1.0×）
    assert all(r["action"] != core.ACTION_BLOCKED for r in rounds)


# ── 当日幂等号：用过的号不复用（评审 HIGH-1）─────────────────────────
def test_rejected_generation_is_burned_so_the_next_round_uses_a_fresh_id() -> None:
    """被拒的那一轮**用掉了一个号**：下一轮必须换号，否则卡死在「幂等命中」上。

    真派发层按 ``client_order_id`` 先查后插且**不看状态**（被拒的委托行也落过库：
    ``create_order`` 先 commit，预检/风控在其后才转 REJECTED）。代次 = 已提交 + 已作废 + 1，
    少了「已作废」这一项，被拒的 g1 会在下一轮被重算出来 → 派发层认成幂等命中并返回
    ``ok=True`` → 尝试计数永不增长 → 防废单的「停手等人工」永不触发 → 账户一整天钉在
    超限上，而账面上看是「已受理」。这里用**带先查后插语义**的替身把整条链钉住。
    """
    client, _ = _over_limit_account()
    disp = _IdempotentDispatch()  # 每轮都拒：委托行落库后转 REJECTED
    deps, _d, notify, redis = _deps(
        client, tier=FakeTier(_tier_budget()), dispatch=disp
    )

    for _ in range(io.MAX_ATTEMPTS_PER_SYMBOL_PER_DAY + 2):
        asyncio.run(lt.run_trim_cycle(deps))

    assert [c["client_order_id"] for c in disp.calls] == [
        f"trim-600036.SH-{_DAY}-g1",
        f"trim-600036.SH-{_DAY}-g2",
        f"trim-600036.SH-{_DAY}-g3",
    ]
    state = io.load_state(redis, _DAY)
    assert state["attempts"]["600036.SH"] == io.MAX_ATTEMPTS_PER_SYMBOL_PER_DAY
    assert state["burned"]["600036.SH"] == io.MAX_ATTEMPTS_PER_SYMBOL_PER_DAY
    assert not state.get("submitted")  # 一笔都没「确认提交」
    assert any("停手" in c["title"] for c in notify.calls)
    assert len(set(disp.seen)) == len(disp.calls)  # 没有一次落回旧号


def test_overshoot_from_lot_rounding_is_disclosed_in_summary_and_notice() -> None:
    """缺口 5,000 元、最小一手 10,000 元：按手只能超卖，而超卖要**看得见**（评审 M5）。

    停手会把账户**永久留在触发线上方**（``idle`` 与 ``lev > 上限`` 并存会让面板自相
    矛盾），而执行器唯一的职责就是把它压回去——故处置是披露：摘要字段 + 计划文案 +
    通知里的一句话，不是不动手。
    """
    client = FakeClient(
        asset={"total_asset": 1_000_000.0, "cash": 0.0, "market_value": 1_005_000.0},
        positions=[_position("600036.SH", 10_050.0, mv=1_005_000.0)],
        ticks={"600036.SH": _tick(100.0)},
    )
    deps, disp, notify, _redis = _deps(client, tier=FakeTier(_tier_budget()))

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_TRIM
    assert summary["need_value"] == pytest.approx(5_000.0)
    assert summary["overshoot_value"] == pytest.approx(5_000.0)
    assert summary["remaining_value"] == 0.0
    assert "整手取整超卖" in summary["reason"]
    assert disp.calls[0]["quantity"] == pytest.approx(100.0)
    assert "比缺口多卖 5000 元" in notify.calls[0]["content"]


# ── 配置侧强制上限：死区、冲突、读不出（评审 M2）──────────────────────
def test_tighter_config_cap_trims_the_dead_zone_the_tier_alone_would_miss() -> None:
    """档位 1.5/1.3（calm）与配置上限 1.1 同时生效：账户 1.3 倍时**只读档位会判「没超」**。

    闸门拦买用的是 ``min(配置, 档位)``（``tiers.apply_to_rules`` 取更严者），故
    ``[1.1, 1.5)`` 是一段**死区**：闸门已按 1.1 拒买，而只读档位的执行器在 1.3 倍上判
    未超限，账户整天没人压仓。这条用例钉住「按配置上限减、且说出来为什么」。
    """
    client, _ = _over_limit_account()  # 杠杆 1.3
    deps, disp, _n, _r = _deps(
        client,
        tier=FakeTier(_tier_budget(1.5, 1.3)),
        risk_config=_risk_config(max_leverage=1.1),
    )

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["enforcing_cap"] == pytest.approx(1.1)
    assert summary["action"] == core.ACTION_TRIM
    assert summary["target_value"] == pytest.approx(1_100_000.0)  # 目标被夹到配置上限
    assert len(disp.calls) == 1
    assert "1.1" in summary["reason"]  # 「档位写着 1.5，为什么按 1.1 减」答得出来


def test_config_cap_equal_or_looser_leaves_the_tier_line_in_charge() -> None:
    """配置更松或相等 → 更严的是档位：不夹取、不误伤（账户在档位带内就不动）。"""
    client, _ = _over_limit_account()  # 1.3 倍，在 calm 档的 1.5 线以内
    deps, disp, _n, _r = _deps(
        client,
        tier=FakeTier(_tier_budget(1.5, 1.3)),
        risk_config=_risk_config(max_leverage=2.0),
    )

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["enforcing_cap"] == pytest.approx(2.0)  # 在强制，只是不构成约束
    assert summary["action"] == core.ACTION_IDLE
    assert disp.calls == []


def test_config_cap_far_below_the_tier_line_blocks_instead_of_liquidating() -> None:
    """配置上限 0.6 vs 档位 1.5（差到带外）：按**冲突**处理，不动手并喊人一次。

    typo（1.5 打成 0.15）若被照单执行就是拿错数当清仓指令——强减是真实卖出、不可逆，
    可疑的输入不产生不可逆动作。
    """
    client, _ = _over_limit_account()
    deps, disp, notify, _r = _deps(
        client,
        tier=FakeTier(_tier_budget(1.5, 1.3)),
        risk_config=_risk_config(max_leverage=0.6),
    )

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_IDLE
    assert disp.calls == []
    assert "矛盾" in summary["reason"]
    assert [c["title"] for c in notify.calls] == ["档位减仓参数疑似脏文档"]
    assert (
        "cap-conflict" in notify.calls[0]["content"]
        or "矛盾" in notify.calls[0]["content"]
    )


def test_unusable_config_cap_blocks_before_touching_the_account() -> None:
    """配置说自己在强制、却读不出强制多少 → **停手**（数据故障不是卖出信号）。

    与「配置关着」严格区分：关着是 ``(None, "")``（按档位走），读不出是问题串（停下
    喊人）。把后者并进前者会让一个坏掉的配置值静默地取消全部减仓保护。
    """
    client, _ = _over_limit_account()
    deps, disp, notify, _r = _deps(client, risk_config=_risk_config(max_leverage=0))

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_BLOCKED
    assert summary["enforcing_cap"] is None
    assert client.asset_calls == 0  # 根本没读账户
    assert disp.calls == []
    assert [c["title"] for c in notify.calls] == ["减仓执行器读不到风险配置的强制上限"]


def test_unreadable_risk_config_blocks_the_round() -> None:
    """读配置抛异常 = 不知道它在强制什么 → 停手（``load_config`` 读失败本就是 fail-closed）。"""
    client, _ = _over_limit_account()
    deps, disp, notify, _r = _deps(client, risk_config_error=RuntimeError("redis 挂了"))

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_BLOCKED
    assert client.asset_calls == 0
    assert "风险配置读取失败" in summary["reason"]
    assert len(notify.calls) == 1


def test_summary_always_carries_enforcing_cap_and_dry_run() -> None:
    """两个键**每个收尾口**都要在：缺了会让「配置关着」与「没跑过合并」、「演练」与
    「真跑」在运维面板上同形（都是踩过的坑）。"""
    flat = FakeClient(
        asset={"total_asset": 1_000_000.0, "cash": 0.0, "market_value": 900_000.0}
    )
    cases: list[Any] = [
        _deps(_over_limit_client())[0],  # trim
        _deps(_over_limit_client(), trading=False)[0],  # 非交易时段
        _deps(_over_limit_client(), real=False)[0],  # 实盘闸关
        _deps(flat)[0],  # 未超限
        _deps(_over_limit_client(), tier=FakeTier({}))[0],  # 档位无键
        _deps(_over_limit_client(), risk_config=_risk_config(max_leverage=0))[0],
    ]
    for deps in cases:
        summary = asyncio.run(lt.run_trim_cycle(deps))
        assert "enforcing_cap" in summary, summary["action"]
        assert "overshoot_value" in summary, summary["action"]
        assert summary["dry_run"] is False

    rehearsal = _deps(_over_limit_client())[0]
    rehearsal.dry_run = True
    rehearsal.rehearsal = True
    assert asyncio.run(lt.run_trim_cycle(rehearsal))["dry_run"] is True


def test_cli_requires_an_explicit_mode() -> None:
    """裸敲模块名**不能**等于「真减一次」（评审 LOW-3）：真钱路径上最不该有的默认值。"""
    with pytest.raises(SystemExit) as err:
        runner.main([])

    assert err.value.code == 2


def test_risk_ctl_client_unwraps_wrappers_but_never_a_method() -> None:
    """``risk_ctl._client``：包装客户端的 ``.client`` 是**句柄**，原生客户端的
    ``client`` 是**方法**（连接工厂）——只判 ``is not None`` 会把方法当句柄拆出来，
    端点在 ``hgetall`` 上 500（评审 M1 的病灶，``tiers`` / ``leverage_trim_io`` 同款）。"""
    from backend.services.trade.routers import risk_ctl

    class Wrapper:
        def __init__(self) -> None:
            self.client = Native()

    class Native:
        def client(self) -> Any:  # 同名但可调用：是方法，不是句柄
            raise AssertionError("不该被当成句柄取出来")

    wrapper = Wrapper()
    assert risk_ctl._client(wrapper) is wrapper.client
    native = Native()
    assert risk_ctl._client(native) is native


def test_risk_trim_endpoint_requires_admin() -> None:
    """L8：运维端点必须挂在管理员依赖上（它是账户级的风控读数，不是公开信息）。"""
    from fastapi import FastAPI

    from backend.services.trade.routers import risk_ctl
    from backend.services.trade_shared.deps import require_admin

    route = next(
        r for r in risk_ctl.router.routes if getattr(r, "path", "") == "/risk/trim"
    )
    deps = [d.call for d in route.dependant.dependencies]  # type: ignore[attr-defined]
    assert require_admin in deps
    assert FastAPI is not None  # 端点所属的 app 由 trade/main.py 装配


# ── 单点故障的方向：读不出 → 不动作；写不进/喊不出 → 不留假平安 ────────
class _HalfBrokenClient(FakeClient):
    """能读资产、读不出持仓的柜台——最危险的一类（「读不出」会伪装成「在限额内」）。"""

    async def get_positions(self):
        raise RuntimeError("持仓接口 500")


def test_positions_read_failure_blocks_instead_of_reading_as_flat() -> None:
    """持仓读不出 ≠ 没有持仓：必须 blocked（把读失败当空仓就是假平安）。"""
    client = _HalfBrokenClient(
        asset={"total_asset": 1_000_000.0, "cash": 0.0, "market_value": 1_300_000.0}
    )
    deps, disp, notify, _ = _deps(client, tier=FakeTier(_tier_budget()))

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_BLOCKED
    assert "持仓读取失败" in summary["reason"]
    assert disp.calls == []
    assert len(notify.calls) == 1


def test_non_mapping_asset_blocks_the_round() -> None:
    """资产返回形态异常（不是映射）= 账不可信，不许「解不出权益就按 0 算」。"""

    class _WeirdAsset(FakeClient):
        async def get_asset(self):
            return [1_000_000.0]

    deps, disp, _, _ = _deps(_WeirdAsset(), tier=FakeTier(_tier_budget()))

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_BLOCKED
    assert "形态异常" in summary["reason"]
    assert disp.calls == []


def test_tier_read_exception_blocks_before_reading_the_account() -> None:
    """档位读不到 = 不知道目标 → 不读账、不动作（fail-closed），且响亮留痕。"""
    client, _ = _over_limit_account()
    deps, disp, notify, _ = _deps(client, tier=FakeTier(_tier_budget()))

    def _boom():
        raise RuntimeError("档位键损坏")

    deps.load_tier = _boom

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert summary["action"] == core.ACTION_BLOCKED
    assert "档位读取异常" in summary["reason"]
    assert client.asset_calls == 0  # 不知道目标就不读账
    assert disp.calls == []
    assert len(notify.calls) == 1


def test_instrument_detail_failure_is_fail_closed_without_dispatch() -> None:
    """拿不到合约详情 = 拿不到跌停价保护位 → 不报价、不提交（宁可不动手）。"""
    client, _ = _over_limit_account()
    client.detail_error = RuntimeError("详情接口超时")
    deps, disp, _, _ = _deps(client, tier=FakeTier(_tier_budget()))

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert disp.calls == []
    assert summary["legs"][0]["ok"] is False
    assert summary["legs"][0]["price"] == 0.0
    assert "合约详情读取失败" in summary["legs"][0]["note"]
    assert summary["action"] == core.ACTION_BLOCKED  # 全部腿失败 = blocked，不是 idle


def test_dispatch_exception_is_a_failed_leg_and_counts_an_attempt() -> None:
    """派发抛异常 → 记成失败腿 + 计当日尝试（喂给「连续失败即停手」那道闸）。"""
    client, _ = _over_limit_account()
    calls: list[dict] = []

    async def _explode(order_data):
        calls.append(dict(order_data))
        raise RuntimeError("内部派发链断了")

    deps, _, _, redis = _deps(
        client,
        tier=FakeTier(_tier_budget()),
        dispatch=_explode,  # type: ignore[arg-type]
    )

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert len(calls) == 1
    assert summary["legs"][0]["ok"] is False
    assert "派发异常" in summary["legs"][0]["note"]
    assert summary["action"] == core.ACTION_BLOCKED
    assert sub._attempts(io.load_state(redis, _DAY), "600036.SH") == 1


def test_notify_failure_does_not_change_what_was_done() -> None:
    """通知发不出去不改变「单已经下了」这个事实：照旧按已提交收尾。"""
    client, _ = _over_limit_account()

    class _BrokenNotify:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("通知服务不可用")

    note = _BrokenNotify()
    deps, disp, _, _ = _deps(
        client,
        tier=FakeTier(_tier_budget()),
        notify=note,  # type: ignore[arg-type]
    )

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert note.calls == 1
    assert len(disp.calls) == 1
    assert summary["action"] == core.ACTION_TRIM
    assert "已提交 1/1 腿" in summary["reason"]


def test_state_read_failure_degrades_to_an_empty_state() -> None:
    """状态键读不出 → 空壳继续（方向安全：计数从零起 = 重试，不是停手/静默）。

    反过来说，它**不能**变成「本轮不动作」：状态键丢了是观测层的事故，不是「账户不可信」。
    """

    class _BrokenGet(FakeRedis):
        def get(self, key):
            raise RuntimeError("redis 连接被重置")

    client, _ = _over_limit_account()
    deps, disp, _, redis = _deps(
        client,
        _BrokenGet(),
        tier=FakeTier(_tier_budget()),  # type: ignore[arg-type]
    )

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert len(disp.calls) == 1  # 照常减仓
    assert summary["action"] == core.ACTION_TRIM
    assert io.load_state(redis, _DAY) == {}  # 读不出就是读不出，不假装有


def test_undelivered_alert_is_not_burned_for_the_day(caplog) -> None:
    """**告警未送达不算喊过**：``publish_notification`` 失败时返回 False 而不抛，
    先记去重键会把一条真钱风险告警整日吞掉。未送达 → 下一拍重试（送达后才去重）。
    """
    client = FakeClient(asset_error=RuntimeError("桥断了"))
    sent: list[str] = []

    class _FlakyNotify:
        """第一拍返回 False、第二拍抛异常（通知库两种失败形态），之后恢复正常。"""

        def __init__(self) -> None:
            self.attempts = 0

        async def __call__(self, *args, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                return False
            if self.attempts == 2:
                raise RuntimeError("通知库崩了")
            sent.append(args[1])
            return True

    note = _FlakyNotify()
    deps, _, _, _ = _deps(
        client,
        tier=FakeTier(_tier_budget()),
        notify=note,  # type: ignore[arg-type]
    )

    with caplog.at_level(
        "ERROR", logger="backend.services.trade.services.leverage_trim_submit"
    ):
        rounds = [asyncio.run(lt.run_trim_cycle(deps)) for _ in range(4)]

    assert {r["action"] for r in rounds} == {core.ACTION_BLOCKED}
    assert note.attempts == 3, "两种失败形态都必须重试；送达之后必须停"
    assert len(sent) == 1  # 第三次真的送达了（第四轮没再喊）
    messages = [r.getMessage() for r in caplog.records]
    # 失败两拍共三行：返回 False 一拍一行；抛异常那拍两行（异常 + 未送达）
    assert len(messages) == 3, messages
    assert "告警未送达" in messages[0]
    assert "告警发送异常" in messages[1]
    assert "告警未送达" in messages[2]


def test_state_and_status_write_failures_do_not_break_the_round() -> None:
    """Redis 写失败**不改变动作**（单照下、摘要照回），但必须留一行痕迹。"""
    client, _ = _over_limit_account()

    class _ReadOnlyRedis(FakeRedis):
        def set(self, key, value, ex=None):  # noqa: A002 - 对齐 redis-py 形参名
            raise RuntimeError("redis 只读（主从切换中）")

    redis = _ReadOnlyRedis()
    deps, disp, _, _ = _deps(
        client,
        redis,
        tier=FakeTier(_tier_budget()),  # type: ignore[arg-type]
    )

    summary = asyncio.run(lt.run_trim_cycle(deps))

    assert len(disp.calls) == 1
    assert summary["action"] == core.ACTION_TRIM
    assert summary["legs"][0]["ok"] is True


def test_code_normalization_failure_falls_back_to_the_raw_code(monkeypatch) -> None:
    """归一失败按原样兜底——**绝不把一条持仓丢掉**（丢腿 = 少卖 = 假平安）。"""
    import backend.shared.stock_utils as stock_utils

    def _boom(code):
        raise RuntimeError("归一表损坏")

    monkeypatch.setattr(stock_utils.StockCodeUtil, "to_suffix", staticmethod(_boom))

    assert io._norm_code("sh600036") == "SH600036"


def test_counter_merge_keeps_the_valid_side_of_a_dirty_value() -> None:
    """脏值只作废**它那一侧**：合并是为了「别抹掉另一个写者刚记下的数」，
    整对丢弃正是那种抹除（``"??"`` 一侧坏掉不该把另一侧的有效计数一起带走）。"""
    merged = io._merge_counters(
        {"600036.SH": 2, "000001.SZ": "??"},
        {"600036.SH": 1, "000001.SZ": 3, "600519.SH": 4},
    )
    assert merged == {"600036.SH": 2, "000001.SZ": 3, "600519.SH": 4}


# ── 常驻 worker（驱动层）：出事只能是「不跑」，不能是「乱下」──────────
class _WorkerHarness:
    """把 worker 的四个边界换掉：开关、心跳、Redis、生产接线。"""

    def __init__(self, monkeypatch, *, flag: bool, config: dict | None = None) -> None:
        self.redis = FakeRedis(config)
        self.heartbeats: list[str] = []
        self.redis_calls = 0
        monkeypatch.setattr("backend.shared.env_flags.env_flag", lambda _flag: flag)
        monkeypatch.setattr(
            "backend.shared.scheduler_registry.heartbeat",
            lambda key: self.heartbeats.append(key) or True,
        )
        monkeypatch.setattr(
            "backend.services.trade_shared.deps.get_redis", self._get_redis
        )

    def _get_redis(self):
        self.redis_calls += 1
        return self.redis


def test_worker_never_starts_when_the_switch_is_off(monkeypatch) -> None:
    """开关只认 ``"true"``（``env_flag``）：关着时**连 Redis 都不连、心跳都不打**。

    这是本层唯一该做的事——关着的 worker 不该在体检里留下任何「有人在跑」的痕迹
    （C07 靠心跳区分「有意关着」与「开着却停摆」）。
    """
    harness = _WorkerHarness(monkeypatch, flag=False)

    assert asyncio.run(runner.run_leverage_trim_worker()) is None
    assert harness.heartbeats == []
    assert harness.redis_calls == 0


def _lifeline(rounds: int) -> tuple[list[float], Any]:
    """替身 ``asyncio.sleep``：记录每轮实际睡多久，第 ``rounds+1`` 次调用即取消。

    取消点放在 ``await asyncio.sleep(...)`` 里（那行在 worker 的 try 之外）——既终止
    了测试，也证明取消能穿出循环（``docker compose stop`` / 任务取消时不许卡在循环里）。
    """
    calls: list[float] = []

    async def _sleep(seconds: float) -> None:
        if len(calls) >= rounds:
            raise asyncio.CancelledError()
        calls.append(seconds)

    return calls, _sleep


def test_worker_loop_heartbeats_survives_failures_and_stops_on_cancel(
    monkeypatch, caplog
) -> None:
    """循环体三条姿态：每拍心跳、单轮异常不致命且同一异常不刷屏、取消立即退出。

    另钉两条口径：**配置里的 ``interval_sec`` 生效**（运维不用改 env 重启），
    异常回落 env 拍（``QM_LEVERAGE_TRIM_POLL_S``，下限 5s）。
    """
    harness = _WorkerHarness(
        monkeypatch,
        flag=True,
        config={"interval_sec": "9", "protect_price_mode": "aggressive"},
    )
    monkeypatch.setenv(io.ENV_POLL_S, "7")
    rounds = [
        RuntimeError("柜台连接池耗尽"),
        RuntimeError("柜台连接池耗尽"),  # 同一异常：只报一次，不刷屏
        {"action": core.ACTION_TRIM, "reason": "减仓 1 腿", "leverage": 1.3},
        {"action": core.ACTION_IDLE, "reason": "非交易时段"},  # 稳态不记日志
        {"action": core.ACTION_IDLE, "reason": "在限额内"},  # 取消点落在这一拍之后
    ]

    async def _cycle(_deps, config=None):
        item = rounds.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(runner, "run_trim_cycle", _cycle)
    monkeypatch.setattr(runner, "default_trim_deps", lambda _redis: object())
    calls, _sleep = _lifeline(rounds=4)
    monkeypatch.setattr(asyncio, "sleep", _sleep)

    with caplog.at_level(
        "INFO", logger="backend.services.trade.services.leverage_trim_runner"
    ):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(runner.run_leverage_trim_worker())

    assert harness.heartbeats == ["leverage_trim"] * 5  # 每拍先心跳（含失败那两拍）
    assert harness.redis_calls == 1  # 连接只建一次（重连风暴的对面）
    assert calls == [7, 7, 9, 9]  # 异常回落 env；正常拍用配置里的 9s
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    assert "轮询异常" in errors[0].getMessage()
    infos = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert len(infos) == 2  # 启动一条 + 非 idle 的一轮一条（idle 轮不记）
    assert "trim 减仓 1 腿（杠杆=1.3）" in infos[1]


def test_worker_survives_a_heartbeat_failure(monkeypatch) -> None:
    """心跳是**尽力而为**：心跳键写不进去不许把减仓循环带走（观测层故障 ≠ 执行层停摆）。"""
    harness = _WorkerHarness(monkeypatch, flag=True)
    ticks: list[int] = []

    def _boom(_key):
        raise RuntimeError("心跳键写入失败")

    async def _cycle(_deps, config=None):
        ticks.append(1)
        return {"action": core.ACTION_IDLE, "reason": "在限额内"}

    monkeypatch.setattr("backend.shared.scheduler_registry.heartbeat", _boom)
    monkeypatch.setattr(runner, "run_trim_cycle", _cycle)
    monkeypatch.setattr(runner, "default_trim_deps", lambda _redis: object())
    _calls, _sleep = _lifeline(rounds=2)
    monkeypatch.setattr(asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.run_leverage_trim_worker())

    assert ticks == [1, 1, 1]  # 心跳每拍都炸，循环照跑
    assert harness.redis_calls == 1


def test_worker_propagates_cancellation_from_a_round(monkeypatch) -> None:
    """轮内被取消 → 立刻穿出，不吞、不再睡（关停时不许卡在循环里）。"""
    harness = _WorkerHarness(monkeypatch, flag=True)

    async def _cycle(_deps, config=None):
        raise asyncio.CancelledError()

    monkeypatch.setattr(runner, "run_trim_cycle", _cycle)
    monkeypatch.setattr(runner, "default_trim_deps", lambda _redis: object())
    calls, _sleep = _lifeline(rounds=1)
    monkeypatch.setattr(asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.run_leverage_trim_worker())

    assert calls == []  # 取消发生在睡之前：没有「取消请求之后再打一拍」
    assert harness.heartbeats == ["leverage_trim"]


def test_worker_poll_interval_floor_and_garbage(monkeypatch) -> None:
    """轮询拍的解析：下限 5s（别把柜台刷穿）、**上限 120s**（< 心跳 TTL 300s，
    否则活着的执行器会被 C07 判 stale，而体检说明写着「--force 重跑」= 真减一轮）、
    坏值/未配置回落默认。"""
    monkeypatch.setenv(io.ENV_POLL_S, "0")
    assert runner._poll_s() == 5
    monkeypatch.setenv(io.ENV_POLL_S, "1")
    assert runner._poll_s() == 5
    monkeypatch.setenv(io.ENV_POLL_S, "7")
    assert runner._poll_s() == 7
    monkeypatch.setenv(io.ENV_POLL_S, "100000")
    # 与配置侧同一对常量，不再有第二个口子
    assert runner._poll_s() == io.MAX_INTERVAL_SEC
    monkeypatch.setenv(io.ENV_POLL_S, "abc")
    assert runner._poll_s() == 60
    monkeypatch.delenv(io.ENV_POLL_S)
    assert runner._poll_s() == 60
    assert runner._poll_s(15) == 15


def test_dry_run_sentinel_refuses_to_dispatch() -> None:
    """``--dry-run`` 的兜底派发替身**必须真的抛**（「演练不提交」不能只靠一道闸）。

    真闸是 ``submit_leg`` 里 ``deps.dry_run`` 的提前返回；这个哨兵只在**闸破了**时才被
    调到，它被调到就等于演练已经破功。因此它的价值全在「不许静默返回成功」——若把它
    改成空返回，演练就成了**没有留痕的真下单路径**。这里直接调它，钉住「抛 + 带委托号」。
    """
    with pytest.raises(RuntimeError, match="trim-600036.SH-20260924-g1"):
        asyncio.run(
            runner._refuse_dispatch({"client_order_id": "trim-600036.SH-20260924-g1"})
        )


# ── 分层守卫（与决策轮 P2.8 同一张图）────────────────────────────────
def test_modules_stay_within_the_file_budget_and_layering() -> None:
    """五块分工：core 无 IO、io 只管取数/键位、submit 只管发单、cycle 只编排、runner 只驱动。

    依赖方向单向 ``runner → cycle → {submit, io} → core``、``submit → io``。
    ``leverage_trim.py`` 曾在一个文件里同时装「一轮里按什么顺序做」与「账户/键位长什么样」
    （1019 行），拆成四块才回到预算内；P2.6 评审的几处修复又把 io 顶回 823 行，于是把
    「腿 → 一张真委托」再拆出来（``leverage_trim_submit``）。这条守卫让「顺手把取数或
    编排塞回上一层」当场变红。
    """
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "services/trade/services"
    src = {
        name: (
            (base / "leverage_trim.py")
            if name == "cycle"
            else (base / f"leverage_trim_{name}.py")
        ).read_text(encoding="utf-8")
        for name in ("core", "io", "submit", "cycle", "runner")
    }
    for name, text in src.items():
        assert len(text.splitlines()) < 800, f"leverage_trim_{name} 超出单文件上限"

    # core 不许碰 IO（否则「纯函数可单测」这条就没了）
    for banned in (
        "import redis",
        "database_manager",
        "sqlalchemy",
        "requests",
        "httpx",
        "asyncio",
    ):
        assert banned not in src["core"], f"core 里出现了 IO 依赖：{banned}"

    # io 层：键位读写必须经 ``_client()`` 解包（评审 C1 的病灶就是这里）
    assert "_client(redis)" in src["io"] and "def native_redis_client" in src["io"]
    # io 层不许出现编排（它只回答「外面长什么样」，不回答「一轮里按什么顺序做」）
    for banned in ("run_trim_cycle", "ACTION_BLOCKED", "paused_would"):
        assert banned not in src["io"], f"io 层里出现了编排逻辑：{banned}"
    # 发单层管「一张单怎么出去」：报价、幂等号、派发、告警闸都在这里。
    # 它**不读账户**（那是 io 的事）：混进来就会出现「两处口径各读一遍账户」。
    for banned in ("read_account", "build_legs", "run_trim_cycle"):
        assert banned not in src["submit"], f"submit 层里出现了取数/编排：{banned}"

    # 编排层不许直接摸 Redis 键/客户端（那些都在 io 层）
    for banned in (
        "native_redis_client",
        "hgetall",
        "lpush",
        "import redis",
        "_client(",
    ):
        assert banned not in src["cycle"], f"编排层直接拿了 Redis 键位：{banned}"

    # 驱动层**不许出现下单/取数**：这里出事只能是「不跑」，不能是「乱下」。
    for banned in (
        "qmt_exec_client",
        "dispatch_internal_strategy_order",
        "resolve_protect_price",
        "get_session",  # 驱动层不读库：CLI 只允许 import close_database 收连接池
        "internal_strategy_dispatcher",
    ):
        assert banned not in src["runner"], f"runner 里出现了执行/取数代码：{banned}"

    # 依赖方向单向：反向 import 会成环或成「谁都能抓谁」的泥球
    forbidden = {
        "core": ("leverage_trim_io import", "leverage_trim_runner import"),
        "io": ("leverage_trim_runner import",),
        "cycle": ("leverage_trim_runner import",),
    }
    for layer, markers in forbidden.items():
        for marker in markers:
            # 编排层的 ``from ...leverage_trim import`` 是 runner 的，不在此列
            assert marker not in src[layer], f"{layer} 反向依赖了上层：{marker}"
    # 发单层只许向下（core）与向取数层（io）；反向 import 会让「谁先跑」变成谜。
    assert "leverage_trim_io import" in src["submit"], (
        "submit 应当从 io 取 TrimDeps 与备注前缀"
    )
    for marker in (
        "leverage_trim_runner import",
        "services.leverage_trim import",  # 编排层（cycle）
    ):
        assert marker not in src["submit"], f"submit 反向依赖了上层：{marker}"
    # io 不许反向依赖发单层（键位层不该知道单子怎么发）
    assert "leverage_trim_submit import" not in src["io"]
    assert "from backend.services.trade.services.leverage_trim import" not in src["io"]
    assert (
        "from backend.services.trade.services.leverage_trim import" not in src["core"]
    )
