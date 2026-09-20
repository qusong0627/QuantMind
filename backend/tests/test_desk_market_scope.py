"""交易台市场维度测试（T-FE-market）：切市场必须整屏跟手，不得回落 A 股。

背景（2026-09-20 实测）：模拟交易顶栏账户已带 market 参数，但
① 前端 fetchData 闭包漏了 currentMarket → 切市场不重取；
② `/desk/today` 根本没有 market 维度 → 港股/美股页签一直显示 A 股信号与盈亏。

本文件锁三件事：
- **能按市场取的必须按市场取**（信号/盈亏/健康 C01·C02·C08/特征分区/交易日历）；
- **取不了的必须如实说不可用**（委托表无 market 列），不得退化成"0 笔委托"假证据；
- **市场闸门**：页面市场 ≠ 活跃策略市场时不得执行/预演该策略。
"""

from __future__ import annotations

import pytest


# ── 活跃策略市场（纯函数）────────────────────────────────────────────────


@pytest.mark.unit
def test_active_strategy_market_reads_live_trade_config():
    from backend.services.api.routers.desk import active_strategy_market

    assert active_strategy_market({"live_trade_config": {"market": "hk"}}) == "HK"


@pytest.mark.unit
def test_active_strategy_market_falls_back_to_execution_config():
    """live_trade_config 无 market 时读 execution_config——与启动链路同序。"""
    from backend.services.api.routers.desk import active_strategy_market

    assert active_strategy_market({"execution_config": {"market": "US"}}) == "US"


@pytest.mark.unit
@pytest.mark.parametrize("payload", [None, {}, {"live_trade_config": None}, "not-a-dict"])
def test_active_strategy_market_defaults_to_cn(payload):
    from backend.services.api.routers.desk import active_strategy_market

    assert active_strategy_market(payload) == "CN"


@pytest.mark.unit
def test_active_strategy_market_prefers_live_over_execution():
    from backend.services.api.routers.desk import active_strategy_market

    payload = {"live_trade_config": {"market": "US"}, "execution_config": {"market": "HK"}}
    assert active_strategy_market(payload) == "US"


# ── 委托归集能力（纯函数）────────────────────────────────────────────────


@pytest.mark.unit
def test_orders_unsupported_only_for_non_cn():
    """CN 不受影响（旧行为）；非 CN 如实返回不可用块，绝不返回空数字冒充。"""
    from backend.services.api.routers.desk import _orders_market_unsupported

    assert _orders_market_unsupported("CN") is None

    block = _orders_market_unsupported("HK")
    assert block is not None
    assert block["available"] is False and block["market"] == "HK"
    assert "sim_orders" in block["reason"]


# ── 证据矩阵：委托不可归集时是"无证据"，不是"0 笔委托"────────────────────


def _minimal_sources(**overrides):
    sources = {"health_items": {}, "eval_summary": {}, "features_latest": None,
               "signals": {"market": "CN", "trade_date": None},
               "execution": {}, "shadow": {}}
    sources.update(overrides)
    return sources


@pytest.mark.unit
def test_evidence_execution_ring_is_no_evidence_when_orders_unscoped():
    """不可归集 ≠ 没交易——退化成"今日无委托（正常空态）"会把口径缺陷说成业务正常。"""
    from backend.services.api.routers.desk import build_evidence_rings

    rings = build_evidence_rings(
        _minimal_sources(
            execution={
                "available": False,
                "market": "HK",
                "reason": "HK 市场委托暂不可按市场归集：委托表 sim_orders 无 market 列",
                "source": "desk:market-scope",
            }
        )
    )
    ring = next(r for r in rings if r["key"] == "execution")
    item = ring["items"][0]
    assert item["level"] == "no_evidence"
    assert "no market" in item["detail"] or "不可按市场归集" in item["detail"]
    assert ring["level"] == "no_evidence"


@pytest.mark.unit
def test_evidence_execution_ring_still_reports_real_orders():
    """CN 路径（可用）必须仍是 ok/warn，不能在改造中丢掉真实委托数。"""
    from backend.services.api.routers.desk import build_evidence_rings

    rings = build_evidence_rings(
        _minimal_sources(execution={"available": True, "sim_count": 3, "filled": 2, "rejected": 0})
    )
    item = next(r for r in rings if r["key"] == "execution")["items"][0]
    assert item["level"] == "ok"
    assert "委托 3 笔" in item["detail"]


# ── 计划闸门：只在**声明**市场时生效 ─────────────────────────────────────


def _patch_active(monkeypatch, payload):
    from backend.services.api.routers import desk

    def _fake(tenant_id, raw_user):  # 同步函数（_collect_plan 直接解包，不 await）
        return payload, None

    monkeypatch.setattr(desk, "_resolve_active_strategy", _fake)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_plan_gate_blocks_mismatched_declared_market(monkeypatch):
    """声明了 HK 而活跃策略是 CN → 如实拒绝，不拿 A 股计划充港股。"""
    from backend.services.api.routers.desk import _collect_plan

    _patch_active(monkeypatch, {"strategy_id": "s1", "live_trade_config": {"market": "CN"}})
    out = await _collect_plan("default", "1", None, "HK")
    assert out["available"] is False
    assert out["strategy_market"] == "CN" and out["market"] == "HK"
    assert "不一致" in out["reason"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_plan_gate_silent_when_market_not_declared(monkeypatch):
    """**不传 market ≠ 传 CN**：未声明页签市场时不设闸门，保持改造前行为。

    缺省成 CN 会把港股活跃策略在港股页签（「手动任务」调用点不传 market）上
    误判为「与页签（CN）不一致」，连带隐藏一键执行按钮——回退。
    """
    from backend.services.api.routers.desk import _collect_plan

    _patch_active(monkeypatch, {"strategy_id": "s1", "live_trade_config": {"market": "HK"}})

    async def _fake_preview(**kwargs):
        return {"available": True, "orders": []}

    monkeypatch.setattr(
        "backend.services.simulation.services.simulation_hosted_scheduler."
        "preview_simulation_plan_for_active",
        _fake_preview,
    )
    out = await _collect_plan("default", "1", None, None)
    assert out.get("available") is not False, "未声明市场时不得触发闸门"
    assert out["market"] == "HK", "未声明时应回报策略自身市场，而不是伪造 CN"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_plan_gate_passes_matching_declared_market(monkeypatch):
    from backend.services.api.routers.desk import _collect_plan

    _patch_active(monkeypatch, {"strategy_id": "s1", "live_trade_config": {"market": "hk"}})

    async def _fake_preview(**kwargs):
        return {"available": True, "orders": []}

    monkeypatch.setattr(
        "backend.services.simulation.services.simulation_hosted_scheduler."
        "preview_simulation_plan_for_active",
        _fake_preview,
    )
    out = await _collect_plan("default", "1", None, "HK")
    assert out.get("available") is not False
    assert out["market"] == "HK"


# ── 影子对照日报：全局一份、无市场段 → 非 CN 不可声称是本市场结论 ─────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shadow_unavailable_for_non_cn():
    from backend.services.api.routers.desk import _collect_shadow

    out = await _collect_shadow("HK")
    assert out["available"] is False and out["market"] == "HK"
    assert "shadow" in out["source"] or "market" in out["source"]


@pytest.mark.unit
def test_shadow_ring_reports_no_evidence_when_market_scoped():
    from backend.services.api.routers.desk import build_evidence_rings

    rings = build_evidence_rings(
        _minimal_sources(
            shadow={"available": False, "market": "HK",
                    "reason": "HK 市场无独立的影子对照日报", "suggestion": "给日报键加市场段"}
        )
    )
    item = next(r for r in rings if r["key"] == "simulation")["items"][0]
    assert item["level"] == "no_evidence"
    assert item["suggestion"] == "给日报键加市场段"


# ── 交易日历按市场 ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_previous_trading_ymd_uses_market_calendar():
    """2026-07-04：CN 上一交易日 07-03，US 因独立日休市应为 07-02。

    拿 XSHG 的上一交易日当美股基准，跨市场节假日会把"同类交易日"判成滞后，
    特征新鲜度环会假报 warn（或反之漏报）。
    """
    from backend.services.api.routers.desk import _previous_trading_ymd

    assert _previous_trading_ymd("20260704", "CN") == "20260703"
    assert _previous_trading_ymd("20260704", "US") == "20260702"


@pytest.mark.unit
def test_previous_trading_ymd_futures_falls_back_to_natural_day():
    """FUTURES/CRYPTO 无 exchange_calendars 日历 → 自然日前一天（不抛异常返回 None）。"""
    from backend.services.api.routers.desk import _previous_trading_ymd

    assert _previous_trading_ymd("20260921", "FUTURES") == "20260920"


@pytest.mark.unit
def test_previous_trading_ymd_bad_input_returns_none():
    from backend.services.api.routers.desk import _previous_trading_ymd

    assert _previous_trading_ymd("not-a-date", "CN") is None


# ── 体检 C01/C02/C08 按市场取数 ──────────────────────────────────────────


class _FakeCtx:
    """记录 (sql, params)，按注册的返回值出数。"""

    def __init__(self, market: str, rows: list[list[dict]] | None = None):
        self.market = market
        self.calls: list[tuple[str, dict]] = []
        self._rows = list(rows or [])
        self._redis: dict[str, str] = {}

    def query(self, sql: str, **params):
        self.calls.append((sql, params))
        return self._rows.pop(0) if self._rows else []

    def redis_get(self, key: str, db: int):
        return self._redis.get(key)

    def redis_scan(self, pattern: str, db: int):
        return []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_c01_filters_signal_distribution_by_market():
    from backend.scripts.diagnose.health import check_c01_signal_distribution

    ctx = _FakeCtx(
        "HK",
        rows=[[{"signal_side": "BUY", "n": 3}, {"signal_side": "SELL", "n": 3},
               {"signal_side": "HOLD", "n": 4}]],
    )
    result = await check_c01_signal_distribution(ctx)

    sql, params = ctx.calls[0]
    # 必须走 COALESCE：market 列可空且契约只加列不回填，裸等号在未回填实例上把 CN 查空
    assert "COALESCE(market, 'CN') = :m" in sql, "信号分布未按市场过滤"
    assert params == {"m": "HK"}
    assert result.metrics["market"] == "HK"
    assert result.level == "ok"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_c01_empty_market_names_the_market():
    from backend.scripts.diagnose.health import check_c01_signal_distribution

    result = await check_c01_signal_distribution(_FakeCtx("US", rows=[[]]))
    assert result.level == "fail"
    assert "US" in result.detail


@pytest.mark.unit
@pytest.mark.asyncio
async def test_c02_ready_marker_key_is_market_scoped():
    """就绪标记键是 qm:signal:ready:{market}:{date}——不按市场取会永远读不到港股标记。"""
    from backend.scripts.diagnose.health import check_c02_signal_readiness

    ctx = _FakeCtx("HK", rows=[[{"d": "2026-09-21"}], [{"n": 1}]])
    ctx._redis["qm:signal:ready:HK:2026-09-21"] = "ok"
    result = await check_c02_signal_readiness(ctx)

    assert result.level == "ok"
    assert "就绪标记=" in result.detail
    assert result.metrics["market"] == "HK"
    assert all(p.get("m") == "HK" for _, p in ctx.calls)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_c08_uses_market_sync_token_not_cn_default():
    """同步键的市场码 CN→A、CRYPTO→BC；写死 "A" 会让港股/美股永远查不到同步记录。"""
    from backend.scripts.diagnose.health import check_c08_data_sync_freshness

    ctx = _FakeCtx("HK", rows=[[{"d": "2026-09-21"}]])
    ctx._redis["quantmind:sync_schedule_last_run:HK:2026-09-21"] = "1"
    result = await check_c08_data_sync_freshness(ctx)

    assert result.level == "ok"
    assert result.metrics["sync_token"] == "HK"

    ctx2 = _FakeCtx("HK", rows=[[{"d": "2026-09-21"}]])
    assert (await check_c08_data_sync_freshness(ctx2)).level == "warn"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_c08_crypto_maps_to_bc_token():
    from backend.scripts.diagnose.health import check_c08_data_sync_freshness

    ctx = _FakeCtx("CRYPTO", rows=[[{"d": "2026-09-21"}]])
    ctx._redis["quantmind:sync_schedule_last_run:BC:2026-09-21"] = "1"
    result = await check_c08_data_sync_freshness(ctx)
    assert result.level == "ok" and result.metrics["sync_token"] == "BC"


# ── 同步市场码映射 ───────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "business,sync_token",
    [("CN", "A"), ("A", "A"), ("cn", "A"), ("HK", "HK"), ("US", "US"),
     ("CRYPTO", "BC"), ("BC", "BC"), ("FUTURES", "FUTURES"), ("CUSTOM", "CUSTOM")],
)
def test_sync_market_token_mapping(business, sync_token):
    from backend.services.engine.tasks.market_sync_scheduler import sync_market_token

    assert sync_market_token(business) == sync_token
    assert sync_market_token(business) in __import__(
        "backend.services.engine.tasks.market_sync_scheduler", fromlist=["MARKETS"]
    ).MARKETS


@pytest.mark.unit
def test_sync_market_token_unknown_passthrough():
    from backend.services.engine.tasks.market_sync_scheduler import sync_market_token

    assert sync_market_token("weird") == "WEIRD"
    assert sync_market_token(None) == "A"
