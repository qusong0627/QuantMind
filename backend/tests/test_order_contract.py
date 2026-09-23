"""T-P1-03 测试：Order/Fill 契约列 + 客户端幂等键。

覆盖：
1. build_sim_client_order_id 纯函数（确定性/缺参/截断）；
2. 列清单与迁移 SQL 幂等形态（两表）；
3. db_init.sql 同步（新装部署）；
4. 三个写入点接线源断言（engine/order_service/stream_consumer）；
5. 两个模型字段存在（SimOrder / trade Order）。
"""

import pytest
from pathlib import Path

from backend.shared.order_contract import (
    MAX_CLIENT_ORDER_ID_LEN,
    ORDER_COLUMNS,
    SIM_ORDER_COLUMNS,
    SOURCE_REBALANCE,
    _missing_for,
    build_sim_client_order_id,
)

_BACKEND = Path(__file__).resolve().parents[1]


def test_build_sim_client_order_id():
    assert (
        build_sim_client_order_id("run_20260914_8933f9af", "600036.SH", "BUY")
        == "sim-run_20260914_8933f9af-600036.SH-buy"
    )
    # 确定性：同输入同键（重跑可观测/去重基础）
    assert build_sim_client_order_id("r1", "000001.SZ", "SELL") == build_sim_client_order_id(
        "r1", "000001.SZ", "sell"
    )
    # 缺参不强造
    assert build_sim_client_order_id("", "000001.SZ", "BUY") is None
    assert build_sim_client_order_id("r1", "", "BUY") is None
    assert build_sim_client_order_id("r1", "000001.SZ", "") is None
    # 截断不超过列宽
    long_id = build_sim_client_order_id("r" * 200, "s" * 60, "buy")
    assert long_id is not None and len(long_id) <= MAX_CLIENT_ORDER_ID_LEN


def test_column_lists():
    assert {n for n, _ in SIM_ORDER_COLUMNS} == {"client_order_id", "source", "agent"}
    assert {n for n, _ in ORDER_COLUMNS} == {"price_source", "source", "agent"}


def test_missing_columns_logic():
    """安全化自愈：列齐全 → 零 DDL；缺列 → 只列缺口（2026-09-16 锁事故后重构）。"""
    assert _missing_for("sim_orders", {"client_order_id", "source", "agent"}) == []
    gaps = _missing_for("sim_orders", {"source"})
    assert gaps == [("client_order_id", "VARCHAR(100)"), ("agent", "VARCHAR(64)")]
    assert _missing_for("orders", set()) == list(ORDER_COLUMNS)


def test_ensure_is_lock_safe():
    """自愈迁移必须 ① existence 预检（information_schema）② lock_timeout ③ 不抛出。"""
    for name in ("signal_contract.py", "order_contract.py"):
        src = (_BACKEND / "shared" / name).read_text(encoding="utf-8")
        assert "information_schema.columns" in src, name
        assert "lock_timeout" in src, name
        assert "不阻断业务" in src, name


def test_db_init_synced():
    ddl = (_BACKEND / "shared/db_init.sql").read_text(encoding="utf-8")
    assert "T-P1-03 Order 契约列" in ddl
    # 两表同口径：裸列 + 各自的部分唯一索引（P2.7-⑧ 把 orders 从「列上全库 UNIQUE」
    # 收敛到与 sim_orders 相同的 (tenant_id, user_id, client_order_id) 限定唯一）
    assert "client_order_id VARCHAR(100) UNIQUE" not in ddl
    assert "client_order_id VARCHAR(100)," in ddl
    assert "uq_sim_orders_scope_client_order_id" in ddl
    assert "uq_orders_scope_client_order_id" in ddl
    assert "WHERE client_order_id IS NOT NULL" in ddl


def test_engine_writer_wired():
    """T-P2-01 后：引擎改经 OrderRouter；幂等键合成与封列自愈分别在引擎/Router。"""
    engine_src = (_BACKEND / "services/simulation/engine.py").read_text(encoding="utf-8")
    assert "build_sim_client_order_id(" in engine_src
    assert "SOURCE_REBALANCE" in engine_src
    assert "order_router import" in engine_src

    router_src = (
        _BACKEND / "services/simulation/services/order_router.py"
    ).read_text(encoding="utf-8")
    assert "ensure_order_contract_columns_async()" in router_src
    assert "trigger_source=req.source" in router_src


def test_order_service_writer_wired():
    src = (_BACKEND / "services/simulation/services/order_service.py").read_text(
        encoding="utf-8"
    )
    assert "order.client_order_id = client_order_id" in src
    assert "SOURCE_MANUAL" in src
    assert "ensure_order_contract_columns_async()" in src


def test_stream_consumer_writer_wired():
    src = (_BACKEND / "services/trade/services/execution_stream_consumer.py").read_text(
        encoding="utf-8"
    )
    assert "PRICE_SOURCE_BROKER_FILL" in src
    assert "ensure_order_contract_columns_async()" in src


def test_trade_startup_ensures_contract_columns():
    """启动期补齐契约列：列自愈此前只挂在写入路径，老库升级后读路径先用到
    （超时扫描器 orders.price_source、EOD simulation_accounts.market、
    热集 engine_signal_scores.market）会每轮报 UndefinedColumn；
    trade 启动期要把它读到的各契约 ensure 都跑一遍。"""
    src = (_BACKEND / "services/trade/main.py").read_text(encoding="utf-8")
    for ensure in (
        "ensure_order_contract_columns_async",
        "ensure_accounts_market_contract_async",
        "ensure_ledger_contract_columns_async",
        "ensure_fund_snapshot_contract_async",
        "ensure_signal_contract_columns_async",
        "ensure_eval_scores_table_async",
    ):
        assert ensure in src


def test_models_have_contract_fields():
    from backend.services.simulation.models.order import SimOrder
    from backend.services.trade_shared.models.order import Order

    assert hasattr(SimOrder, "client_order_id") and hasattr(SimOrder, "source")
    assert hasattr(Order, "price_source") and hasattr(Order, "source")
    # P2.7 分账归属列：成交回报只带来订单，这两列是「这笔该记进哪本分账」的唯一出处
    assert hasattr(SimOrder, "agent") and hasattr(Order, "agent")


def test_agent_column_width_is_one_number_in_all_four_write_points():
    """``agent`` 宽度必须四处同数：契约列（启动期自愈的 DDL）/ ``db_init.sql``（新装）/
    两个 ORM 模型（create_all 路径）。改一处漏一处 = PG 在入库时**静默截断**，
    同一条腿在两张表里成了两个 agent，对账时查不出原因（见 ``normalize_agent``）。
    """
    import re

    from backend.shared.order_contract import AGENT_LEN

    for name, col_type in (*SIM_ORDER_COLUMNS, *ORDER_COLUMNS):
        if name == "agent":
            assert col_type == f"VARCHAR({AGENT_LEN})"

    ddl = (_BACKEND / "shared/db_init.sql").read_text(encoding="utf-8")
    for table in ("orders", "sim_orders"):
        block = ddl.split(f"CREATE TABLE IF NOT EXISTS {table} (", 1)[1].split(");", 1)[0]
        assert re.search(rf"\bagent\s+VARCHAR\({AGENT_LEN}\)", block), table
        # 必须可空：非 LLM 腿（人点/风控/托管）本来就没有归属，NOT NULL 会让它们插不进去
        assert re.search(r"\bagent\s+VARCHAR\(\d+\)[^,]*NOT NULL", block) is None, table

    for rel in (
        "services/trade_shared/models/order.py",
        "services/simulation/models/order.py",
    ):
        src = (_BACKEND / rel).read_text(encoding="utf-8")
        assert re.search(rf"agent[^\n]*String\({AGENT_LEN}\)", src), rel


def test_normalize_agent_strips_and_truncates_never_raises():
    from backend.shared.order_contract import AGENT_LEN, normalize_agent

    assert normalize_agent("  deepseek-v4-flash ") == "deepseek-v4-flash"
    assert normalize_agent(None) == ""
    assert normalize_agent("") == ""
    assert normalize_agent("   ") == ""
    long_name = normalize_agent("x" * (AGENT_LEN + 20))
    assert len(long_name) == AGENT_LEN


def test_create_schemas_keep_the_agent_field():
    """两个下单 schema 都必须**显式声明** ``agent``。

    ``SimOrderCreate`` 是 ``extra="ignore"``：没声明的字段会被静默丢掉——调用方传了、
    台账里却是 NULL，且没有任何报错（有 agent 与没 agent 在库里长得一样，
    分账到时候只能数出一本空账）。宽度则按列封顶：超宽**抛校验错**而不是交给 PG 截断
    （调用方一律先过 ``normalize_agent``，走到 schema 时已经合法）。
    """
    from pydantic import ValidationError

    from backend.services.simulation.models.order import OrderSide, OrderType
    from backend.services.simulation.schemas.order import SimOrderCreate
    from backend.services.trade_shared.models.enums import (
        OrderSide as TOrderSide,
        OrderType as TOrderType,
    )
    from backend.services.trade_shared.schemas.order import OrderCreate

    sim_kw = {
        "symbol": "600036.SH",
        "side": OrderSide("buy"),
        "order_type": OrderType("market"),
        "quantity": 100.0,
    }
    assert SimOrderCreate(**sim_kw, agent="deepseek-v4-flash").agent == "deepseek-v4-flash"
    with pytest.raises(ValidationError):
        SimOrderCreate(**sim_kw, agent="x" * 100)

    real_kw = {
        "portfolio_id": 0,
        "symbol": "600036.SH",
        "side": TOrderSide("buy"),
        "order_type": TOrderType("market"),
        "quantity": 100.0,
    }
    assert OrderCreate(**real_kw, agent="deepseek-v4-flash").agent == "deepseek-v4-flash"
    with pytest.raises(ValidationError):
        OrderCreate(**real_kw, agent="x" * 100)


def test_source_taxonomy_constants():
    from backend.shared import order_contract as oc

    assert {
        oc.SOURCE_REBALANCE,
        oc.SOURCE_MANUAL,
        oc.SOURCE_INTERNAL,
        oc.SOURCE_MIRROR,
        oc.SOURCE_SLTP,
        oc.SOURCE_SANDBOX,
        oc.SOURCE_TDX_ROLLING,
        oc.SOURCE_HOSTED,
        oc.SOURCE_FORCED_LIQUIDATION,
        oc.SOURCE_LLM_DECISION,
    } == {
        "rebalance",
        "manual",
        "internal",
        "mirror",
        "sltp",
        "sandbox",
        "tdx_rolling",
        "hosted",
        "forced_liquidation",
        "llm_decision",
    }
    assert SOURCE_REBALANCE == "rebalance"


def test_source_values_are_distinct_and_fit_the_column():
    """``source`` 是 ``VARCHAR(32)``：新增来源撞值或超宽都会静默截断/串类。"""
    from backend.shared import order_contract as oc

    pairs = [
        (name, getattr(oc, name))
        for name in dir(oc)
        if name.startswith("SOURCE_") and isinstance(getattr(oc, name), str)
    ]
    values = [v for _, v in pairs]
    assert len(values) == len(set(values)), "两个 SOURCE_* 常量取了同一个值"
    for name, value in pairs:
        assert value == value.strip().lower(), f"{name}={value!r} 不是小写下划线形态"
        assert len(value) <= 32, f"{name}={value!r} 超出 orders.source 列宽 32"


# ── P2.3b：决策层 LLM 调仓腿的幂等键 ─────────────────────────────────


def test_build_llm_decision_client_order_id_is_round_scoped():
    from backend.shared.order_contract import (
        build_llm_decision_client_order_id as build,
    )

    key = build("rnd-20260924-0930-flash", "600036.SH", "SELL")
    # 非字母数字被剥掉后正好 20 字符（未触及 24 位上限）
    assert key == "lld-rnd202609240930flash-600036.SH-sell"
    # 同轮同标的同方向 → 同键（轮内重试/桥回执丢失后的补投都落在它上面）
    assert key == build("rnd-20260924-0930-flash", "600036.SH", "sell")
    # 换轮 / 换标的 / 换方向 → 必须换键（否则真单被静默去重）
    assert key != build("rnd-20260924-1000-flash", "600036.SH", "SELL")
    assert key != build("rnd-20260924-0930-flash", "600519.SH", "SELL")
    assert key != build("rnd-20260924-0930-flash", "600036.SH", "BUY")
    assert key.startswith("lld-")


def test_build_llm_decision_client_order_id_separates_agents_in_one_round():
    """**一轮多 agent**（P2.7 多模型竞争）：同一轮的同一标的同一方向必须是**两个键**。

    不带 agent 时两家模型算出同一个键 → 后一家被静默去重（「模型让卖、系统不卖」）。
    留空则与历史键**逐字节一致**（不改单 agent 台账的去重口径）。
    """
    from backend.shared.order_contract import (
        build_llm_decision_client_order_id as build,
    )

    args = ("rnd-20260924-0930", "600036.SH", "SELL")
    legacy = build(*args)
    assert build(*args, agent="") == legacy
    a = build(*args, agent="native-tft")
    b = build(*args, agent="lgbm-238")
    assert a is not None and b is not None
    assert a != legacy and b != legacy and a != b
    # agent 段只留 [A-Za-z0-9]；≤8 位与历史键**逐字节一致**（既有台账的去重口径不动）
    assert build(*args, agent="native tft/v5") == build(*args, agent="nativetftv5")
    assert build(*args, agent="pro") == "lld-rnd202609240930-pro-600036.SH-sell"
    # 长名带区分码尾巴：前 8 位相同的两家**不能**算出同一个键（见 _agent_segment）
    assert build(*args, agent="deepseek-v4-flash") != build(
        *args, agent="deepseek-v4-pro"
    )


def test_agent_segment_keeps_long_names_distinct_and_short_names_verbatim():
    """``[:8]`` 会把 ``deepseek-v4-flash`` 与 ``deepseek-v4-pro`` 截成同一个 ``deepseek``：
    同轮同标的同方向的两条腿算出**一个**键，后一家被 ``uq_sim_orders_scope_client_order_id``
    当重复单丢掉——多模型分账刚落地就被自己的幂等键吃掉一半腿，而台账上只留一行
    ``duplicate``。
    """
    import hashlib as _hashlib

    from backend.shared.order_contract import _agent_segment

    # 短名逐字保留（既有台账里 pro/flash 这类键不变），剥掉非字母数字再算长度
    assert _agent_segment("pro") == "pro"
    assert _agent_segment("pro max") == "promax"
    assert _agent_segment("native tft/v5") == _agent_segment("nativetftv5")  # 11 位 → 带尾巴
    # 长名 = 前 8 位 + 6 位 SHA1（钉住算法：换哈希函数会悄悄重新发键）
    raw = "deepseekv4flash"
    assert (
        _agent_segment("deepseek-v4-flash")
        == f"deepseek-{_hashlib.sha1(raw.encode('utf-8')).hexdigest()[:6]}"
    )
    assert _agent_segment("deepseek-v4-flash") != _agent_segment("deepseek-v4-pro")
    # 确定性：跨进程重放同一轮要落在同一个键上
    assert _agent_segment("deepseek-v4-flash") == _agent_segment("deepseek-v4-flash")
    # 空 → 空段（键里不出现空段，单 agent 轮次的键与历史一致）
    assert _agent_segment("") == "" and _agent_segment(None) == ""
    # 只要有字母数字就非空；纯符号名算「没有归属」
    assert _agent_segment("---") == ""


def test_build_llm_decision_client_order_id_refuses_to_fabricate():
    """**缺参不强造**：固定占位符会把「不知道这是哪一轮」变成「所有未知轮是同一轮」，
    于是后续轮次里同标的同方向的真单会被静默去重。缺参一律 ``None``。"""
    from backend.shared.order_contract import (
        build_llm_decision_client_order_id as build,
    )

    assert build("", "600036.SH", "BUY") is None
    assert build("r1", "", "BUY") is None
    assert build("r1", "600036.SH", "") is None
    assert build(None, None, None) is None  # type: ignore[arg-type]


def test_build_llm_decision_client_order_id_truncates_to_the_column_width():
    from backend.shared.order_contract import (
        build_llm_decision_client_order_id as build,
    )

    key = build("r" * 200, "600036.SH", "buy")
    assert key is not None and len(key) <= MAX_CLIENT_ORDER_ID_LEN
    # 截断只吃 round_id（前缀+标的+方向必须完整保留，否则键会串到别的票上）
    assert key.endswith("-600036.SH-buy")


# ── T-P2-08：幂等键唯一索引（部分索引）+ 重复语义 ─────────────────────


def test_unique_index_migration_source_guards():
    """唯一索引迁移三纪律 + 存量重复前置（有重复不建索引、不静默删行）。"""
    src = (_BACKEND / "shared/order_contract.py").read_text(encoding="utf-8")
    assert "SIM_ORDER_UNIQUE_INDEX" in src
    assert "WHERE client_order_id IS NOT NULL" in src
    assert "HAVING count(*) > 1" in src  # 存量重复预检
    assert "lock_timeout" in src and "不阻断" in src
    assert "repair_sim_order_duplicates" in src  # 修复脚本指路

    # 写入侧：create_order 必须把 IntegrityError 转 DuplicateSimOrderError（不得 500）
    svc = (
        _BACKEND / "services/simulation/services/order_service.py"
    ).read_text(encoding="utf-8")
    assert "class DuplicateSimOrderError" in svc
    assert "except IntegrityError" in svc
    assert "get_sim_order_by_client_order_id" in svc

    # 三条调用方都要转既有 duplicate 语义
    router = (
        _BACKEND / "services/simulation/services/order_router.py"
    ).read_text(encoding="utf-8")
    assert "except DuplicateSimOrderError" in router
    assert "get_sim_order_by_client_order_id" in router  # 台账本体先查（投影可能为空）

    submission = (
        _BACKEND / "services/simulation/services/order_submission_service.py"
    ).read_text(encoding="utf-8")
    assert "except DuplicateSimOrderError" in submission
    assert "get_sim_order_by_client_order_id" in submission

    route = (
        _BACKEND / "services/simulation/routers/simulation_orders.py"
    ).read_text(encoding="utf-8")
    assert "except DuplicateSimOrderError" in route

    # 调度器幂等判定改用 cid 列（remarks 前缀被 mark_rejected 覆写的历史坑）
    dispatcher = (
        _BACKEND / "services/live_trading/services/internal_strategy_dispatcher.py"
    ).read_text(encoding="utf-8")
    assert "SimOrder.client_order_id == client_order_id" in dispatcher


async def _ensure_db_pool_tp208():
    from sqlalchemy import text as _t

    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(_t("SELECT 1"))
        return
    except Exception:  # noqa: BLE001
        await close_database()
    async with get_session(read_only=True) as probe:
        await probe.execute(_t("SELECT 1"))


@pytest.mark.asyncio
async def test_unique_index_real_db_and_duplicate_create_e2e():
    """真库 E2E：索引建成（带 WHERE）→ 同幂等键二次 create_order →
    DuplicateSimOrderError（非 500）且库内仍单行 → 清理。"""
    import uuid as _uuid

    for attempt in range(2):
        try:
            await _ensure_db_pool_tp208()
            break
        except Exception:  # noqa: BLE001
            if attempt == 1:
                pytest.skip("数据库不可用")
    from sqlalchemy import text as sa_text

    from backend.services.simulation.models.order import OrderSide, OrderType
    from backend.services.simulation.schemas.order import SimOrderCreate
    from backend.services.simulation.services.order_service import (
        DuplicateSimOrderError,
        SimOrderService,
    )
    from backend.shared.database_manager_v2 import close_database, get_session
    from backend.shared.order_contract import ensure_sim_order_unique_index_async

    assert await ensure_sim_order_unique_index_async() is True
    async with get_session(read_only=True) as session:
        row = (
            await session.execute(
                sa_text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'uq_sim_orders_scope_client_order_id'"
                )
            )
        ).fetchone()
    assert row is not None
    assert "WHERE (client_order_id IS NOT NULL)" in str(row[0])

    user = f"99{_uuid.uuid4().int % 1_000_000:06d}"
    cid = f"sim-e2e-{_uuid.uuid4().hex[:8]}-600036.SH-buy"
    try:
        async with get_session(read_only=False) as session:
            svc = SimOrderService(session)
            first = await svc.create_order(
                "default",
                user,
                SimOrderCreate(
                    portfolio_id=0,
                    client_order_id=cid,
                    symbol="600036.SH",
                    side=OrderSide("buy"),
                    order_type=OrderType("market"),
                    quantity=100.0,
                    price=40.0,
                    remarks="T-P2-08 E2E",
                ),
                trigger_source="manual",
            )
            assert first.client_order_id == cid
        async with get_session(read_only=False) as session:
            svc2 = SimOrderService(session)
            with pytest.raises(DuplicateSimOrderError) as exc:
                await svc2.create_order(
                    "default",
                    user,
                    SimOrderCreate(
                        portfolio_id=0,
                        client_order_id=cid,
                        symbol="600036.SH",
                        side=OrderSide("buy"),
                        order_type=OrderType("market"),
                        quantity=100.0,
                        price=40.0,
                        remarks="T-P2-08 E2E dup",
                    ),
                    trigger_source="manual",
                )
            assert exc.value.client_order_id == cid
            assert str(exc.value.existing.order_id) == str(first.order_id)
        # 库内仍单行（唯一索引兜底生效）
        async with get_session(read_only=True) as session:
            n = (
                await session.execute(
                    sa_text(
                        "SELECT count(*) FROM sim_orders "
                        "WHERE tenant_id='default' AND user_id=:u AND client_order_id=:c"
                    ),
                    {"u": int(user), "c": cid},
                )
            ).scalar_one()
        assert int(n) == 1
    finally:
        async with get_session(read_only=False) as session:
            await session.execute(
                sa_text(
                    "DELETE FROM sim_orders WHERE tenant_id='default' AND client_order_id=:c"
                ),
                {"c": cid},
            )
        await close_database()


# ── P2.7-⑧：orders(REAL) 幂等键唯一索引 —— 补上租户/账户维度 ────────────
#
# 病灶（2026-09-24 实测，dev 库 pg_constraint）：
#   ``orders_client_order_id_key`` = ``UNIQUE (client_order_id)`` —— **全库唯一**，
#   而派发层查重与落账都按 ``(tenant_id, user_id, client_order_id)`` 限定。两个口径
#   不一致时跨租户同键的后果是：INSERT 撞全局唯一 → IntegrityError → 兜底按 (租户,用户)
#   反查**查不到**冲突行（它属于别家）→ raise → HTTP 500，**真单发不出去**。
#   ``lld-*`` 的 round 段是 ``rnd-{日期}-{槽位}``（不含租户），两个租户在同一决策槽
#   必然算出同一个键 —— 不是概率问题而是构造性碰撞。``sim_orders`` 早已是
#   ``(tenant_id, user_id, client_order_id) WHERE client_order_id IS NOT NULL``
#   部分唯一索引（T-P2-08），本批把 REAL 台账对齐到同一口径。


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeSession:
    """按 SQL 文本返回预置行；记录语句顺序与提交次数（可注入失败）。"""

    def __init__(self, *, index_exists=False, dupes=(), legacy=()):
        self.statements: list[str] = []
        self.commits = 0
        self.raise_on: str | None = None
        self._index_exists = index_exists
        self._dupes = list(dupes)
        self._legacy = list(legacy)

    async def execute(self, stmt, params=None):  # noqa: ARG002 - 假体
        sql = str(stmt)
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError(f"injected: {sql}")
        self.statements.append(sql)
        if "pg_indexes" in sql:
            return _FakeResult([(1,)] if self._index_exists else [])
        if "HAVING count(*) > 1" in sql:
            return _FakeResult(self._dupes)
        if "pg_constraint" in sql:
            return _FakeResult([(n,) for n in self._legacy])
        return _FakeResult([])

    async def commit(self):
        self.commits += 1


def _wire_sessions(monkeypatch, read: _FakeSession, write: _FakeSession):
    """把 order_contract 用到的 get_session 换成本地假体，并清进程内缓存。"""
    import contextlib

    from backend.shared import database_manager_v2 as dbm
    from backend.shared import order_contract as oc

    used: list[bool] = []

    @contextlib.asynccontextmanager
    async def _get_session(read_only=False):
        used.append(bool(read_only))
        yield read if read_only else write

    monkeypatch.setattr(dbm, "get_session", _get_session)
    monkeypatch.setattr(oc, "_order_scope_index_ready", None, raising=False)
    return used


def _ensure_orders_index():
    from backend.shared.order_contract import ensure_real_order_scope_unique_index_async

    return ensure_real_order_scope_unique_index_async()


@pytest.mark.asyncio
async def test_real_order_scope_index_creates_then_drops_global_constraint(monkeypatch):
    """迁移顺序：**先建限定索引，再删全局约束** —— 中间不留「无唯一性」窗口。

    反过来（先删后建）若 CREATE 失败，台账就退回「cid 可以重复落两行」，
    而重复的 cid 会让幂等反查拿到任意一行 ⇒「模型让卖、系统报成功但没卖」。
    """
    read = _FakeSession(index_exists=False, legacy=["orders_client_order_id_key"])
    write = _FakeSession()
    used = _wire_sessions(monkeypatch, read, write)

    assert await _ensure_orders_index() is True
    assert used == [True, False]  # 探读一次，写一次

    ddl = [s for s in write.statements if s.startswith(("CREATE", "ALTER"))]
    assert len(ddl) == 2
    assert ddl[0].startswith("CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_scope_client_order_id")
    assert "(tenant_id, user_id, client_order_id)" in ddl[0]
    assert "WHERE client_order_id IS NOT NULL" in ddl[0]
    assert ddl[1] == 'ALTER TABLE orders DROP CONSTRAINT "orders_client_order_id_key"'
    assert write.commits == 1


@pytest.mark.asyncio
async def test_real_order_scope_index_refuses_when_duplicates_exist(monkeypatch):
    """存量重复（同租户同用户同键）存在时**什么都不做**：不建索引、不删全局约束。

    删了全局约束又建不成限定索引，等于把台账的唯一性整个拿掉 —— 比重复本身更危险。
    """
    read = _FakeSession(
        index_exists=False, dupes=[("default", "10000001", "cand-x-600036.SH-buy", 2)]
    )
    write = _FakeSession()
    _wire_sessions(monkeypatch, read, write)

    assert await _ensure_orders_index() is False
    assert write.statements == []


@pytest.mark.asyncio
async def test_real_order_scope_index_fast_path_when_already_scoped(monkeypatch):
    """已经是限定索引且全局约束已删 → 零 DDL 快路径（不写库）。"""
    read = _FakeSession(index_exists=True, legacy=[])
    write = _FakeSession()
    _wire_sessions(monkeypatch, read, write)

    assert await _ensure_orders_index() is True
    assert write.statements == []
    assert read.commits == 0


@pytest.mark.asyncio
async def test_real_order_scope_index_only_drops_when_half_migrated(monkeypatch):
    """半迁移态（索引已在、全局约束还在）：只补删全局约束，不重复建索引。"""
    read = _FakeSession(index_exists=True, legacy=["orders_client_order_id_key"])
    write = _FakeSession()
    _wire_sessions(monkeypatch, read, write)

    assert await _ensure_orders_index() is True
    ddl = [s for s in write.statements if s.startswith(("CREATE", "ALTER"))]
    assert ddl == ['ALTER TABLE orders DROP CONSTRAINT "orders_client_order_id_key"']


@pytest.mark.asyncio
async def test_real_order_scope_index_failure_is_not_fatal(monkeypatch):
    """DDL 失败只告警不抛（旧语义=全库唯一仍在，业务不中断），下次启动重试。"""
    read = _FakeSession(index_exists=False, legacy=["orders_client_order_id_key"])
    write = _FakeSession()
    write.raise_on = "CREATE UNIQUE INDEX"
    _wire_sessions(monkeypatch, read, write)

    assert await _ensure_orders_index() is False
    # 建索引失败 ⇒ 绝不走到删约束那一步
    assert not any(s.startswith("ALTER") for s in write.statements)


def test_orders_scope_index_declared_in_ddl_model_and_startup():
    """三处同步：db_init.sql（新装）/ ORM 模型（create_all 路径）/ 启动期自愈接线。"""
    ddl = (_BACKEND / "shared/db_init.sql").read_text(encoding="utf-8")
    orders_block = ddl.split("CREATE TABLE IF NOT EXISTS orders (", 1)[1].split(");", 1)[0]
    assert "client_order_id VARCHAR(100) UNIQUE" not in orders_block
    assert "client_order_id VARCHAR(100)," in orders_block
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_scope_client_order_id" in ddl
    assert (
        "ON orders (tenant_id, user_id, client_order_id)" in ddl
        and "WHERE client_order_id IS NOT NULL" in ddl
    )

    model = (_BACKEND / "services/trade_shared/models/order.py").read_text(encoding="utf-8")
    cid_line = [
        ln for ln in model.splitlines() if ln.strip().startswith("client_order_id = Column")
    ]
    assert len(cid_line) == 1
    assert "unique=True" not in cid_line[0]  # 全库唯一由模型删掉（DB 侧改限定索引）

    startup = (_BACKEND / "services/trade/main.py").read_text(encoding="utf-8")
    assert "ensure_real_order_scope_unique_index_async" in startup


@pytest.mark.asyncio
async def test_real_order_scope_index_e2e_on_live_db():
    """真库 E2E（**全程 rollback，不留任何订单行**）：自愈后

    1. 索引形态 = ``(tenant_id, user_id, client_order_id) WHERE client_order_id IS NOT NULL``；
    2. 旧的 ``UNIQUE (client_order_id)`` 全库约束已不在（不删它，跨租户同键照样 500）；
    3. 行为面：不同租户**同键可共存**（修复点），同租户同用户同键**仍被拒**（唯一性没丢）。
    """
    import uuid as _uuid

    for attempt in range(2):
        try:
            await _ensure_db_pool_tp208()
            break
        except Exception:  # noqa: BLE001
            if attempt == 1:
                pytest.skip("数据库不可用")

    from sqlalchemy import text as sa_text
    from sqlalchemy.exc import IntegrityError

    from backend.shared.database_manager_v2 import close_database, get_session
    from backend.shared.order_contract import ensure_real_order_scope_unique_index_async

    assert await ensure_real_order_scope_unique_index_async() is True
    async with get_session(read_only=True) as session:
        idx = (
            await session.execute(
                sa_text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'uq_orders_scope_client_order_id'"
                )
            )
        ).fetchone()
        legacy = (
            await session.execute(
                sa_text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'orders'::regclass AND contype = 'u' "
                    "AND pg_get_constraintdef(oid) = 'UNIQUE (client_order_id)'"
                )
            )
        ).fetchall()
    assert idx is not None, "限定索引未建立"
    assert "(tenant_id, user_id, client_order_id)" in str(idx[0])
    assert "WHERE (client_order_id IS NOT NULL)" in str(idx[0])
    assert legacy == [], f"旧全库唯一约束仍在: {legacy}"

    cid = f"t-e2e-{_uuid.uuid4().hex[:8]}-600036.SH-buy"
    insert = sa_text(
        "INSERT INTO orders (order_id, tenant_id, user_id, portfolio_id, symbol, "
        "side, position_side, order_type, trading_mode, status, quantity, "
        "client_order_id) VALUES (gen_random_uuid(), :t, :u, 0, '600036.SH', "
        "'buy', 'LONG', 'limit', 'REAL', 'pending', 100, :cid)"
    )
    try:
        async with get_session(read_only=False) as session:
            await session.execute(insert, {"t": "t-a", "u": "t-a", "cid": cid})
            # 跨租户同键：修复前这里会 IntegrityError（全局唯一）⇒ 真单发不出去
            await session.execute(insert, {"t": "t-b", "u": "t-b", "cid": cid})
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.execute(insert, {"t": "t-a", "u": "t-a", "cid": cid})
            # 显式回滚：上面的两行是**行为探针**，不许落进真台账
            await session.rollback()
    finally:
        await close_database()


@pytest.mark.asyncio
async def test_agent_column_exists_and_reaches_the_table_on_the_live_db():
    """真库 E2E（**自己清理，不留行**；``t-`` 随机租户）：

    1. 启动期自愈后 ``orders``/``sim_orders`` 都有**可空**的 ``agent VARCHAR(64)``
       ——「老库升级后这条腿的归属写不进去」是本批最可能的线上形态；
    2. 行为面：``create_order(agent=...)`` 真的落进 ``sim_orders.agent``，超宽名按
       列宽截断；不传的单是 NULL（非 LLM 腿不会被上一轮的归属串味）。
    """
    import uuid as _uuid

    for attempt in range(2):
        try:
            await _ensure_db_pool_tp208()
            break
        except Exception:  # noqa: BLE001
            if attempt == 1:
                pytest.skip("数据库不可用")

    from sqlalchemy import text as sa_text

    from backend.services.simulation.models.order import OrderSide, OrderType
    from backend.services.simulation.schemas.order import SimOrderCreate
    from backend.services.simulation.services.order_service import SimOrderService
    from backend.shared.database_manager_v2 import get_session
    from backend.shared.order_contract import (
        AGENT_LEN,
        ensure_order_contract_columns_async,
        normalize_agent,
    )

    # 异步变体的契约是「补齐列」而不是「返回布尔」（返回 None）；真正的断言是下面
    # 的 information_schema —— 列在不在，只有库知道。
    await ensure_order_contract_columns_async()
    async with get_session(read_only=True) as session:
        rows = (
            await session.execute(
                sa_text(
                    "SELECT table_name, is_nullable, character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_name IN ('orders', 'sim_orders') AND column_name = 'agent'"
                )
            )
        ).fetchall()
    got = {str(r[0]): (str(r[1]), int(r[2])) for r in rows}
    assert got == {"orders": ("YES", AGENT_LEN), "sim_orders": ("YES", AGENT_LEN)}, got

    tenant = f"t-agent-{_uuid.uuid4().hex[:8]}"
    user = "10000001"
    long_name = "m" * (AGENT_LEN + 16)
    cid_owned = f"{tenant}-600036.SH-buy"
    cid_plain = f"{tenant}-600519.SH-buy"
    try:
        async with get_session(read_only=False) as session:
            svc = SimOrderService(session)
            for cid, raw_agent in ((cid_owned, long_name), (cid_plain, "")):
                await svc.create_order(
                    tenant,
                    user,
                    SimOrderCreate(
                        portfolio_id=0,
                        client_order_id=cid,
                        symbol="600036.SH",
                        side=OrderSide("buy"),
                        order_type=OrderType("market"),
                        quantity=100.0,
                        price=40.0,
                        remarks="P2.7 agent E2E",
                        # 生产链一律先归一（决策轮/派发器/提交段三处）——schema 的
                        # max_length 是**兜底**，超宽直接抛错；库侧再截断一次
                        agent=normalize_agent(raw_agent),
                    ),
                    trigger_source="llm_decision",
                )
        async with get_session(read_only=True) as session:
            stored = dict(
                (
                    await session.execute(
                        sa_text(
                            "SELECT client_order_id, agent FROM sim_orders "
                            "WHERE tenant_id = :t AND client_order_id = ANY(:c)"
                        ),
                        {"t": tenant, "c": [cid_owned, cid_plain]},
                    )
                ).fetchall()
            )
        assert stored[cid_owned] == normalize_agent(long_name) == "m" * AGENT_LEN
        assert stored[cid_plain] is None, "没归属的单必须是 NULL（空串会与真归属混在一起）"
    finally:
        async with get_session(read_only=False) as session:
            for table in ("sim_orders", "simulation_orders"):
                await session.execute(
                    sa_text(f"DELETE FROM {table} WHERE tenant_id = :t"),
                    {"t": tenant},
                )


@pytest.mark.asyncio
async def test_real_order_scope_index_process_cache_short_circuits(monkeypatch):
    """进程内缓存命中 → 连库都不连（启动期每次都会调，不能每次三趟查询）。"""
    read = _FakeSession()
    write = _FakeSession()
    used = _wire_sessions(monkeypatch, read, write)
    from backend.shared import order_contract as oc

    monkeypatch.setattr(oc, "_order_scope_index_ready", True, raising=False)
    assert await _ensure_orders_index() is True
    assert used == [] and read.statements == [] and write.statements == []
