"""P2.3b 执行段 IO 适配：**取数 → 提交 → 回执**（``decision_executor``）。

纯核心的用例在 ``test_decision_execution.py``；这里只回答「接线接对了吗」：

1. **同一份快照**：行情切片由调用点读好的快照翻译而来，执行段自己**不重读**行情
   （否则模型按 A 价决策、系统按 B 价报单）；
2. **阈值单源**：涨跌停阈值走注入的 ``threshold_of``（线上是
   ``local_market_data.limit_threshold``），算不出来就是 ``None`` 而不是「大概 10%」；
3. **在途账两本书**：模拟台账（``sim_orders``，int 用户列）+ 实盘台账（``orders``，
   String 用户列）取并集，身份经 ``simulation_account_keys`` 收口；
4. **读不到在途 = 本轮不下单**（fail-closed）：零提交，但计划照算（留痕「本来想做什么」）；
5. **提交契约**：幂等键 ``lld-…``、``source/trading_mode/mirror/real_limit_price``
   与 ``push_orders`` 同形、不自己拿撮合锁、单腿失败不阻断其余；
6. **回执 → 审计表**：``outcomes`` 按**决策序号**回填（同批同标的也有各自的行）。

全部用例不碰 PG / Redis：读者与提交器都是注入的替身。
"""

from __future__ import annotations

import pytest

from backend.services.trade.services import decision_executor as dx
from backend.shared.decision.contract import (
    HOLD,
    PCT_GIVEN,
    SELL,
    STATUS_OK,
    Decision,
    DecisionBatch,
    Pct,
)
from backend.shared.decision.execution import Holding, Quote

# ---------------------------------------------------------------------------
# 构造器（用例读起来要像在说交易场景）
# ---------------------------------------------------------------------------


def _batch(*decisions: Decision) -> DecisionBatch:
    return DecisionBatch(status=STATUS_OK, schema="intraday", decisions=decisions)


#: 实盘账户的规范身份（``simulation_account_keys`` 的收口值）——测试一律显式传它，
#: 好让「没给账户」这种默认值走不通时立刻暴露，而不是悄悄少读一本在途账。
_ACCOUNT = 10000001


def _sell(code: str, pct: float = 0.5, **kw: object) -> Decision:
    return Decision(
        action=SELL,
        code=code,
        pct=Pct(pct, PCT_GIVEN),
        reason=str(kw.pop("reason", "减仓")),
        **kw,  # type: ignore[arg-type]
    )


def _hold(code: str) -> Decision:
    return Decision(action=HOLD, code=code, reason="观望")


def _held(code: str, available: float = 1000.0) -> tuple[str, Holding]:
    return code, Holding(symbol=code, available=available, name="测试")


def _snap(now: float = 10.0, pre: float = 10.0, **kw: object) -> dict[str, object]:
    out = {"Now": now, "PreClose": pre}
    out.update(kw)
    return out


class _FakeOutcome:
    """``RouterOutcome`` 的形状（成功/失败/幂等命中三态）。"""

    def __init__(
        self,
        success: bool = True,
        order_id: str = "ord-1",
        message: str = "",
        duplicate: bool = False,
        mirror: dict | None = None,
    ) -> None:
        self.success = success
        self.order_id = order_id
        self.message = message
        self.duplicate = duplicate
        self.mirror = mirror


class _FakeSubmitter:
    """记录每次调用；可按标的安排返回值/异常。"""

    def __init__(self, *, results: dict | None = None) -> None:
        self.calls: list[tuple[str, str, str | None, bool]] = []
        self._results = results or {}

    async def __call__(self, leg, client_order_id, real):
        self.calls.append((leg.symbol, leg.side, client_order_id, real))
        planned = self._results.get(
            leg.symbol, _FakeOutcome(order_id=f"ord-{leg.symbol}")
        )
        if isinstance(planned, Exception):
            raise planned
        return planned


# ---------------------------------------------------------------------------
# 1. 持仓行 → 执行段持仓
# ---------------------------------------------------------------------------


def test_holdings_from_rows_normalizes_to_suffix_and_reads_avail() -> None:
    """``HoldingRow``（上下文取数的形状）→ 后缀式键的 ``Holding``。"""
    from backend.shared.decision.context import HoldingRow

    row = HoldingRow(
        code="SH600036",
        name="招商银行",
        volume=1000,
        cost=38.0,
        price=40.0,
        pnl_pct=5.2,
        day_chg=1.1,
        avail=600,
    )
    book = dx.holdings_from_rows([row])
    assert set(book) == {"600036.SH"}
    assert book["600036.SH"].available == 600
    assert book["600036.SH"].name == "招商银行"


def test_holdings_from_rows_accepts_dict_shape_too() -> None:
    """同形 dict（API/桥形状）等价——两种形状不被区别对待。"""
    book = dx.holdings_from_rows(
        [{"code": "600519.SH", "name": "贵州茅台", "avail": 100}]
    )
    assert book["600519.SH"].available == 100


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_holdings_from_rows_drops_rows_without_a_code(bad: object) -> None:
    """没有代码的行直接丢：留着会让 ``sell_not_held`` 面对一个空串键。"""
    assert dx.holdings_from_rows([{"code": bad, "avail": 100}]) == {}


def test_holdings_from_rows_keeps_suspicious_but_non_empty_codes() -> None:
    """**只丢空码**。可疑但非空的码（``"??"``）照留：判据比过滤器更会说话——
    真出了这种行，账里有一笔「可卖 0 股」，否决会带确切理由，而不是行凭空消失。"""
    book = dx.holdings_from_rows([{"code": "??", "avail": 0}])
    assert set(book) == {"??"}


def test_holdings_from_rows_treats_unknown_avail_as_unsellable() -> None:
    """可卖量缺失/脏 → 0（不可卖），**不是**「全可卖」：T+1 未解禁时后者会下出
    一张必被券商废掉的单。"""
    book = dx.holdings_from_rows(
        [
            {"code": "600036.SH"},  # 缺字段
            {"code": "600519.SH", "avail": "不是数字"},
            {"code": "000001.SZ", "avail": None},
        ]
    )
    assert book["600036.SH"].available == 0
    assert book["600519.SH"].available == 0
    assert book["000001.SZ"].available == 0


def test_holdings_from_rows_empty_input_is_an_empty_book() -> None:
    assert dx.holdings_from_rows([]) == {}
    assert dx.holdings_from_rows(None) == {}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. 快照 → 行情切片（量纲与「不编默认值」）
# ---------------------------------------------------------------------------


def _threshold(value: float = 0.1):
    """注入式阈值函数（线上是 ``local_market_data.limit_threshold``）。"""

    def _f(symbol, *, is_st, trade_date):
        return value

    return _f


def test_quotes_for_reads_price_and_day_change_as_a_ratio() -> None:
    """``day_chg_ratio`` 是**比例**：10 元跌到 9.02 = −0.098（不是 −9.8）。

    这条钉的是与 ``at_limit_down`` 的量纲一致——拿 ``context_source.day_change_pct``
    （百分点，已 ×100）塞这里的字段，跌停判定会宽 100 倍。
    """
    quotes = dx.quotes_for(
        ["600036.SH"],
        {"600036.SH": _snap(now=9.02, pre=10.0)},
        trade_date="2026-09-24",
        # ST 判定显式给出：默认走名称索引（仓库外的文件），用例不该依赖它存不存在
        is_st_of=lambda s: False,
        threshold_of=_threshold(0.1),
    )
    quote = quotes["600036.SH"]
    assert quote.price == pytest.approx(9.02)
    assert quote.day_chg_ratio == pytest.approx(-0.098)
    assert quote.limit_threshold_ratio == pytest.approx(0.1)
    assert quote.halted is None  # 快照没有停牌字段 = 不知道（不是「没停牌」）


def test_quotes_for_passes_st_and_trade_date_into_the_single_source() -> None:
    """ST 判定与交易日必须**传进去**（板别不同、ST 不同、阈值就不同）。"""
    seen: list[tuple] = []

    def _f(symbol, *, is_st, trade_date):
        seen.append((symbol, is_st, trade_date))
        return 0.1

    dx.quotes_for(
        ["600036.SH"],
        {"600036.SH": _snap()},
        trade_date="2026-09-24",
        is_st_of=lambda s: True,
        threshold_of=_f,
    )
    assert seen == [("600036.SH", True, "2026-09-24")]


def test_quotes_for_leaves_threshold_none_when_the_source_fails() -> None:
    """阈值取不到 → ``None``（跌停不判 + 纯核心留痕），**绝不**填一个「大概 10%」：
    那会让 ST 股（5% 板）的跌停腿照卖。"""

    def _boom(symbol, *, is_st, trade_date):
        raise RuntimeError("行情库没连上")

    quotes = dx.quotes_for(
        ["600036.SH"],
        {"600036.SH": _snap()},
        trade_date="2026-09-24",
        threshold_of=_boom,
    )
    assert quotes["600036.SH"].limit_threshold_ratio is None
    assert quotes["600036.SH"].price == 10.0  # 价格仍然照给：一项判不了不影响另一项


def test_quotes_for_never_guesses_a_board_when_the_st_flag_is_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """名称不可得（``is_st_of`` 给 ``None``）→ 阈值 ``None`` + **一条**汇总告警。

    绝不落回「大概不是 ST 的 10% 板」：对一只 5% 板的 ST 股，那样会让跌停腿照卖、
    封板腿照买，而整条链路上看不出任何异常。板别有一半不知道 = 整个阈值不猜。
    """
    with caplog.at_level("WARNING"):
        quotes = dx.quotes_for(
            ["600036.SH"],
            {"600036.SH": _snap()},
            trade_date="2026-09-24",
            is_st_of=lambda s: None,
            threshold_of=_threshold(0.1),  # 阈值函数本身是好的：不该被白跑一次
        )
    assert quotes["600036.SH"].limit_threshold_ratio is None
    log = " ".join(r.getMessage() for r in caplog.records)
    assert "ST 判据不可用" in log and "1/1" in log


def test_is_st_by_name_has_three_states_not_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``True``/``False``/``None``（名称不可得）——三态，别把 ``None`` 压成 ``False``。"""
    mapper = "backend.shared.stock_name_mapper"
    monkeypatch.setattr(f"{mapper}.resolve_name", lambda s: "招商银行")
    assert dx.is_st_by_name("600036.SH") is False
    monkeypatch.setattr(f"{mapper}.resolve_name", lambda s: "ST 三圣")
    assert dx.is_st_by_name("600036.SH") is True
    monkeypatch.setattr(f"{mapper}.resolve_name", lambda s: "")  # 未收录
    assert dx.is_st_by_name("600036.SH") is None

    def _boom(s: str) -> str:
        raise RuntimeError("索引文件读不了")

    monkeypatch.setattr(f"{mapper}.resolve_name", _boom)
    assert dx.is_st_by_name("600036.SH") is None


def test_quotes_for_missing_snapshot_yields_a_no_price_slice() -> None:
    """没采到快照 ≠ 快照是空的：出一片只有代码的切片，交给 ``l3.no_quote`` 判，
    不在这一层猜价。"""
    quotes = dx.quotes_for(
        ["600036.SH", "600519.SH"],
        {"600519.SH": _snap()},
        trade_date="2026-09-24",
        threshold_of=_threshold(),
    )
    assert set(quotes) == {"600036.SH", "600519.SH"}  # 缺席的也在（显式无价）
    assert quotes["600036.SH"].price is None
    assert quotes["600519.SH"].price == 10.0


@pytest.mark.parametrize("junk", [0, -1.0, "x", True, float("nan"), float("inf"), ""])
def test_quotes_for_treats_unusable_prices_as_missing(junk: object) -> None:
    """0/负/非数/布尔/NaN/Inf 一律「没有价」（0 是「没采到」的常见写法，不是「免费」）。"""
    quotes = dx.quotes_for(
        ["600036.SH"],
        {"600036.SH": _snap(now=junk)},
        trade_date="2026-09-24",
        threshold_of=_threshold(),
    )
    assert quotes["600036.SH"].price is None


@pytest.mark.parametrize("zero_like", [0, -1.0])
def test_quotes_for_never_turns_a_zero_price_into_a_fabricated_limit_down(
    zero_like: float,
) -> None:
    """0 价不能算出 ``day_chg_ratio = 0/10 − 1 = −100%``——那是一个凭空的跌停，
    会拦下本该卖出的腿并把它记到 ``l4.sell_limit_down`` 名下（归因错到另一个事故上）。"""
    quotes = dx.quotes_for(
        ["600036.SH"],
        {"600036.SH": _snap(now=zero_like)},
        trade_date="2026-09-24",
        threshold_of=_threshold(),
    )
    assert quotes["600036.SH"].price is None
    assert quotes["600036.SH"].day_chg_ratio is None


def test_quotes_for_prefix_keyed_snapshots_degrade_visibly_not_silently() -> None:
    """喂进前缀式键的快照（违反 ``read_snapshots`` 契约）→ 每只都是「无价」。

    这不是静默错误：所有腿会带 ``l3.no_quote`` 被拦，审计行看得出来「整轮没价」
    （哪天真发生了，排查方向是上游喂了哪份 dict，而不是「模型突然不下单了」）。
    """
    quotes = dx.quotes_for(
        ["600036.SH"],
        {"SH600036": _snap()},
        trade_date="2026-09-24",
        threshold_of=_threshold(),
    )
    assert quotes["600036.SH"].price is None


def test_quotes_for_normalizes_prefix_input_and_halters_field() -> None:
    """输入可以是前缀式（持仓行的常见形态），查表用归一后的后缀式键。"""
    quotes = dx.quotes_for(
        ["SH600036"],
        {"600036.SH": _snap(halted=True)},
        trade_date="2026-09-24",
        threshold_of=_threshold(),
    )
    assert quotes["600036.SH"].halted is True


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (True, True),
        (1, True),
        ("1", True),
        ("true", True),
        (" YES ", True),
        (False, False),
        (0, False),
        ("0", False),  # bool("0") 是 True——快照里 0 恰恰是「没停牌」的常见写法
        ("false", False),
        (0.0, False),
        ("", None),  # 字段在但没值 = 不知道（不是「没停牌」）
        ("停牌", None),  # 认不出的词不进猜测
        (float("nan"), None),
        (float("inf"), None),
        (None, None),
        ([], None),
    ],
)
def test_halted_flag_never_reads_zero_or_junk_as_halted(
    raw: object, want: object
) -> None:
    """``bool("0")`` 为 ``True``——直接 ``bool()`` 会把「没停牌」读成「停牌」，
    拦下一批本该买的单还把它记在 ``l4.halted``（归因指向另一个事故）名下。"""
    assert dx._flag(raw) is want
    assert (
        dx.quotes_for(
            ["600036.SH"],
            {"600036.SH": _snap(halted=raw)},
            trade_date="2026-09-24",
            is_st_of=lambda s: False,
            threshold_of=_threshold(),
        )["600036.SH"].halted
        is want
    )


# ---------------------------------------------------------------------------
# 3. 在途账：两本书 + 身份收口 + 「读不到 ≠ 没有」
# ---------------------------------------------------------------------------


def test_inflight_key_normalizes_enum_side_and_symbol_form() -> None:
    """方向取枚举 ``.value``：py3.10 下 ``str(OrderSide.BUY)`` 是 ``"OrderSide.BUY"``，
    直接用会让键永远匹配不上（静默失效）。"""
    from backend.services.simulation.models.order import OrderSide

    assert dx._inflight_key("SH600036", OrderSide.SELL) == ("600036.SH", "sell")
    assert dx._inflight_key("600036.SH", "BUY") == ("600036.SH", "buy")
    assert dx._inflight_key("600036.SH", " sell ") == ("600036.SH", "sell")


def test_inflight_key_rejects_unusable_rows() -> None:
    assert dx._inflight_key("", "buy") is None
    assert dx._inflight_key("600036.SH", "") is None
    assert dx._inflight_key("600036.SH", "hold") is None  # 不是委托方向


@pytest.mark.asyncio
async def test_read_inflight_unions_both_books(monkeypatch) -> None:
    """模拟台账（int 用户列）+ 实盘台账（String 用户列）取并集，条数如实记录。"""

    async def _sim(db, *, tenant_id, user_id, limit):
        return {("600036.SH", "sell")}, ""

    async def _real(db, *, tenant_id, user_id, limit):
        return {("600519.SH", "buy")}, ""

    monkeypatch.setattr(dx, "_read_sim_pending", _sim)
    monkeypatch.setattr(dx, "_read_real_pending", _real)

    read = await dx.read_inflight(None, tenant_id="default", user_id=10000001)
    assert read.ok
    assert read.keys == frozenset({("600036.SH", "sell"), ("600519.SH", "buy")})
    assert read.counts == {"sim": 1, "real": 1}


@pytest.mark.asyncio
async def test_read_inflight_normalizes_identity_for_both_columns(monkeypatch) -> None:
    """同一个账户在两本书里的列型不同（int / str），但必须是**同一个身份**：
    各写各的名字正是历史上委托唯一键冲突的成因。"""
    seen: list[tuple] = []

    async def _sim(db, *, tenant_id, user_id, limit):
        seen.append(("sim", tenant_id, user_id, type(user_id).__name__))
        return set(), ""

    async def _real(db, *, tenant_id, user_id, limit):
        seen.append(("real", tenant_id, user_id, type(user_id).__name__))
        return set(), ""

    monkeypatch.setattr(dx, "_read_sim_pending", _sim)
    monkeypatch.setattr(dx, "_read_real_pending", _real)

    # "1" 是管理员族的旧口径 → 收口成规范名 10000001
    await dx.read_inflight(None, tenant_id="default", user_id="1")
    assert seen == [
        ("sim", "default", 10000001, "int"),
        ("real", "default", "10000001", "str"),
    ]


@pytest.mark.asyncio
async def test_read_inflight_skips_the_sim_book_for_a_non_numeric_account(
    monkeypatch,
) -> None:
    """非数字账户在 ``sim_orders.user_id``（Integer）上结构性不存在——是「查不到行」
    不是「读失败」，故不计 error（但仍如实记 0 条）。"""

    async def _boom(db, *, tenant_id, user_id, limit):  # pragma: no cover - 不该被调用
        raise AssertionError("非数字账户不该查 sim 台账")

    async def _real(db, *, tenant_id, user_id, limit):
        return set(), ""

    monkeypatch.setattr(dx, "_read_sim_pending", _boom)
    monkeypatch.setattr(dx, "_read_real_pending", _real)

    read = await dx.read_inflight(None, tenant_id="default", user_id="svc-account")
    assert read.ok
    assert read.counts["sim"] == 0


@pytest.mark.asyncio
async def test_read_inflight_reports_a_failed_book_as_an_error(monkeypatch) -> None:
    """任一本读失败 → ``errors`` 非空（**不**把「查不到」当「没有」）。另一本照读，
    但 ``ok`` 为假，调用点必须放弃本轮。"""

    async def _sim(db, *, tenant_id, user_id, limit):
        return set(), "sim_orders 读取失败：OperationalError: 连接被重置"

    async def _real(db, *, tenant_id, user_id, limit):
        return {("600519.SH", "buy")}, ""

    monkeypatch.setattr(dx, "_read_sim_pending", _sim)
    monkeypatch.setattr(dx, "_read_real_pending", _real)

    read = await dx.read_inflight(None, tenant_id="default", user_id=10000001)
    assert not read.ok
    assert "sim_orders" in read.errors[0]


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)


class _FakeDB:
    """只回答「查出来哪几行」的替身（SQL 语句本身不解释——那是 PG 的事）。"""

    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _FakeResult(self._rows)


@pytest.mark.asyncio
async def test_sim_reader_treats_an_over_cap_result_as_untrustworthy() -> None:
    """在途账**读到上限 = 读不完 = 账不可信**（fail-closed），不是「取前 N 条」：
    去重查询少读一行，结果就是多下一笔真委托。"""
    from backend.services.simulation.models.order import OrderSide

    rows = [("600036.SH", OrderSide.SELL)] * 3
    keys, err = await dx._read_sim_pending(
        _FakeDB(rows), tenant_id="default", user_id=10000001, limit=2
    )
    assert keys == set()
    assert "超过 2 条" in err


@pytest.mark.asyncio
async def test_sim_reader_returns_keys_when_under_the_cap() -> None:
    from backend.services.simulation.models.order import OrderSide

    rows = [("SH600036", OrderSide.SELL), ("600519.SH", "buy")]
    keys, err = await dx._read_sim_pending(
        _FakeDB(rows), tenant_id="default", user_id=10000001, limit=2
    )
    assert err == ""
    assert keys == {("600036.SH", "sell"), ("600519.SH", "buy")}


@pytest.mark.asyncio
async def test_real_reader_treats_an_over_cap_result_as_untrustworthy() -> None:
    """实盘那本同理（两本书的上限口径必须一致，否则一处静默截断）。"""
    rows = [("600036.SH", "sell")] * 3
    keys, err = await dx._read_real_pending(
        _FakeDB(rows), tenant_id="default", user_id="10000001", limit=2
    )
    assert keys == set()
    assert "超过 2 条" in err


@pytest.mark.asyncio
async def test_readers_turn_a_broken_query_into_an_error_not_an_empty_book() -> None:
    """查询抛异常 → 错误文案（**不是**空集合）：「查不到」被当成「没有残留」正是
    重复下单的成因。"""

    class _BoomDB:
        async def execute(self, statement):
            raise RuntimeError("connection reset")

    keys, err = await dx._read_sim_pending(
        _BoomDB(), tenant_id="default", user_id=10000001, limit=500
    )
    assert keys == set()
    assert "connection reset" in err

    keys, err = await dx._read_real_pending(
        _BoomDB(), tenant_id="default", user_id="10000001", limit=500
    )
    assert keys == set()
    assert "connection reset" in err


# ---------------------------------------------------------------------------
# 4. 提交：幂等键 / 实盘开关 / 单腿失败不阻断
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_batch_returns_no_receipts_when_there_are_no_legs() -> None:
    submitter = _FakeSubmitter()
    outcome = await dx.execute_batch(
        _batch(_hold("600036.SH")),
        round_id="rnd-1",
        holdings={},
        quotes={},
        submitter=submitter,
    )
    assert outcome.receipts == ()
    assert submitter.calls == []
    assert outcome.plan.noops == ("600036.SH",)


@pytest.mark.asyncio
async def test_execute_batch_submits_with_round_scoped_idempotency_key() -> None:
    submitter = _FakeSubmitter()
    outcome = await dx.execute_batch(
        _batch(_sell("600036.SH", 1.0)),
        round_id="rnd-20260924",
        holdings=dict([_held("600036.SH")]),
        quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
        real=False,
        submitter=submitter,
    )
    assert len(outcome.receipts) == 1
    symbol, side, key, real = submitter.calls[0]
    assert (symbol, side, real) == ("600036.SH", "sell", False)
    assert key == "lld-rnd20260924-600036.SH-sell"
    assert outcome.submitted[0].order_id == "ord-600036.SH"
    assert outcome.summary()["submitted"] == 1


@pytest.mark.asyncio
async def test_execute_batch_puts_the_agent_into_the_idempotency_key() -> None:
    """一轮多 agent（P2.7 竞争）：两家的腿必须落在**两个键**上。

    不带 agent 时两家模型算出同一个键，后一家被静默去重——「模型让卖、系统不卖」。
    """
    keys: list[str | None] = []
    for agent in ("", "native-tft", "lgbm-238"):
        submitter = _FakeSubmitter()
        await dx.execute_batch(
            _batch(_sell("600036.SH", 1.0)),
            round_id="rnd-20260924",
            holdings=dict([_held("600036.SH")]),
            quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
            real=False,
            agent=agent,
            submitter=submitter,
        )
        keys.append(submitter.calls[0][2])
    assert keys[0] == "lld-rnd20260924-600036.SH-sell"  # 留空 = 与历史一致
    assert len(set(keys)) == 3


@pytest.mark.asyncio
async def test_execute_batch_counts_an_idempotent_hit_without_calling_it_a_new_order() -> (
    None
):
    """幂等命中（``duplicate``）是**成功**（那张单已在），但不计入「本轮新发」——
    否则报表会把旧单算成新单。"""
    submitter = _FakeSubmitter(
        results={
            "600036.SH": _FakeOutcome(
                success=True, duplicate=True, message="duplicate client_order_id"
            )
        }
    )
    outcome = await dx.execute_batch(
        _batch(_sell("600036.SH", 1.0)),
        round_id="rnd-1",
        holdings=dict([_held("600036.SH")]),
        quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
        real=False,
        submitter=submitter,
    )
    assert outcome.receipts[0].duplicate is True
    assert outcome.receipts[0].success is True
    assert outcome.submitted == ()
    assert outcome.failed == ()
    assert outcome.summary()["duplicates"] == 1
    assert outcome.outcomes[0]["armed"] is True  # 单在，决策算落地


@pytest.mark.asyncio
async def test_execute_batch_keeps_going_when_one_leg_raises() -> None:
    """单腿失败不阻断其余（与 ``push_orders`` 同纪律）：`orders` 的第一条挂掉，
    第二条照发——否则一个标的的偶发错误会吞掉整轮调仓。"""
    submitter = _FakeSubmitter(results={"600036.SH": RuntimeError("券商通道超时")})
    outcome = await dx.execute_batch(
        _batch(_sell("600036.SH", 1.0), _sell("600519.SH", 1.0)),
        round_id="rnd-1",
        holdings=dict([_held("600036.SH"), _held("600519.SH")]),
        quotes={
            "600036.SH": Quote(symbol="600036.SH", price=10.0),
            "600519.SH": Quote(symbol="600519.SH", price=100.0),
        },
        real=False,
        submitter=submitter,
    )
    assert len(submitter.calls) == 2
    assert len(outcome.failed) == 1
    assert "RuntimeError" in outcome.failed[0].message
    assert outcome.summary()["submitted"] == 1
    assert outcome.outcomes[0]["armed"] is False
    assert outcome.outcomes[1]["armed"] is True


@pytest.mark.asyncio
async def test_execute_batch_refuses_legs_it_cannot_key_idempotently() -> None:
    """``round_id`` 缺失 ⇒ 一个幂等键都构造不出来 ⇒ **一张单都不下**。

    带占位符硬发会让后续轮次的同标的同方向真单撞上同一个键被静默去重
    （即「模型让卖、系统静默不卖」，而且是**下一轮**才发作）。"""
    submitter = _FakeSubmitter()
    outcome = await dx.execute_batch(
        _batch(_sell("600036.SH", 1.0)),
        round_id="",
        holdings=dict([_held("600036.SH")]),
        quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
        real=False,
        submitter=submitter,
    )
    assert submitter.calls == []
    assert outcome.receipts[0].success is False
    assert "幂等键" in outcome.receipts[0].message


@pytest.mark.asyncio
async def test_execute_batch_reads_the_live_switch_only_when_unspecified(
    monkeypatch,
) -> None:
    """``real=None`` 时才现读 ``is_real_trading_enabled()``（进程级 env，一轮内不变）；
    显式传入的值不被环境覆盖（本机容器里实盘开关是开的，测试必须能钉住两态）。"""
    import backend.shared.live_trading_gate as gate

    monkeypatch.setattr(gate, "is_real_trading_enabled", lambda: True)
    explicit = _FakeSubmitter()
    await dx.execute_batch(
        _batch(_sell("600036.SH", 1.0)),
        round_id="rnd-1",
        holdings=dict([_held("600036.SH")]),
        quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
        real=False,
        submitter=explicit,
    )
    assert explicit.calls[0][3] is False

    from_env = _FakeSubmitter()
    await dx.execute_batch(
        _batch(_sell("600036.SH", 1.0)),
        round_id="rnd-1",
        holdings=dict([_held("600036.SH")]),
        quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
        submitter=from_env,
    )
    assert from_env.calls[0][3] is True


# ---------------------------------------------------------------------------
# 5. 回执 → 审计表（``build_records(outcomes=…)``）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outcomes_are_keyed_by_decision_index_not_symbol() -> None:
    """同批里两个同标的（第二个被去重拦掉）不能挂错行：一条成功、一条被拦。"""
    submitter = _FakeSubmitter()
    outcome = await dx.execute_batch(
        _batch(_sell("600036.SH", 1.0), _sell("600036.SH", 1.0)),
        round_id="rnd-1",
        holdings=dict([_held("600036.SH")]),
        quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
        real=False,
        submitter=submitter,
    )
    assert len(submitter.calls) == 1  # 第二条被 l3.duplicate_fingerprint 拦下
    armed = sorted(i for i, o in outcome.outcomes.items() if o["armed"])
    blocked = sorted(i for i, o in outcome.outcomes.items() if not o["armed"])
    assert armed == [0]
    assert blocked == [1]
    assert outcome.outcomes[1]["reject_reason"].startswith("l3.")


@pytest.mark.asyncio
async def test_outcomes_report_the_veto_rule_id_as_the_reject_reason() -> None:
    """被拦的决策记 **rule id**（可聚合进影子代价账），理由文字进 notes。"""
    outcome = await dx.execute_batch(
        _batch(_sell("600036.SH", 1.0)),
        round_id="rnd-1",
        holdings={},  # 本账没有它
        quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
        real=False,
        submitter=_FakeSubmitter(),
    )
    assert outcome.outcomes[0]["armed"] is False
    assert outcome.outcomes[0]["reject_reason"] == "l2.sell_not_held"
    assert outcome.outcomes[0]["notes"]  # 人话理由仍然留着


@pytest.mark.asyncio
async def test_outcomes_do_not_invent_reasons_for_hold_and_watch() -> None:
    """``hold``/``watch`` 不进 outcomes：它们的「为什么没单」就在决策原文里，
    系统再编一条 reject_reason 等于把模型的话复述成系统的话。"""
    from backend.shared.decision.contract import WATCH

    outcome = await dx.execute_batch(
        _batch(_hold("600036.SH"), Decision(action=WATCH, code="600519.SH")),
        round_id="rnd-1",
        holdings={},
        quotes={},
        submitter=_FakeSubmitter(),
    )
    assert outcome.outcomes == {}
    assert outcome.plan.noops == ("600036.SH",)
    assert outcome.plan.watches == ("600519.SH",)


# ---------------------------------------------------------------------------
# 6. 编排：在途账读不到就一张单都不发（fail-closed）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_round_aborts_without_sending_anything_when_inflight_is_unreadable(
    monkeypatch,
) -> None:
    """读不到在途 = 本轮不下单，但**计划照算**（「本来想做什么」要留痕）。"""

    async def _bad(db, *, tenant_id, user_id, limit):
        return set(), "orders(REAL) 读取失败：OperationalError: 连接被重置"

    async def _real(db, *, tenant_id, user_id, limit):
        return set(), ""

    monkeypatch.setattr(dx, "_read_sim_pending", _bad)
    monkeypatch.setattr(dx, "_read_real_pending", _real)

    submitter = _FakeSubmitter()
    outcome = await dx.run_round(
        _batch(_sell("600036.SH", 1.0)),
        round_id="rnd-1",
        holdings=dict([_held("600036.SH")]),
        quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
        real=False,
        submitter=submitter,
        db=None,
        user_id=_ACCOUNT,
    )
    assert submitter.calls == []
    assert outcome.receipts == ()
    assert len(outcome.plan.legs) == 1  # 计划里腿在（想做什么）
    assert "fail-closed" in outcome.aborted
    assert outcome.outcomes[0]["armed"] is False
    assert "fail-closed" in outcome.outcomes[0]["reject_reason"]


@pytest.mark.asyncio
async def test_run_round_feeds_read_inflight_keys_into_the_plan(monkeypatch) -> None:
    """取到的在途键真的参与判定：同标的同方向 → ``l3.inflight_dup``，不下第二张。"""

    async def _sim(db, *, tenant_id, user_id, limit):
        return {("600036.SH", "sell")}, ""

    async def _real(db, *, tenant_id, user_id, limit):
        return set(), ""

    monkeypatch.setattr(dx, "_read_sim_pending", _sim)
    monkeypatch.setattr(dx, "_read_real_pending", _real)

    submitter = _FakeSubmitter()
    outcome = await dx.run_round(
        _batch(_sell("600036.SH", 1.0)),
        round_id="rnd-1",
        holdings=dict([_held("600036.SH")]),
        quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
        real=False,
        submitter=submitter,
        db=None,
        user_id=_ACCOUNT,
    )
    assert submitter.calls == []
    assert outcome.aborted == ""
    assert [v.rule for v in outcome.plan.vetoes] == ["l3.inflight_dup"]


@pytest.mark.asyncio
async def test_read_inflight_refuses_a_blank_account_instead_of_reading_nothing() -> (
    None
):
    """``user_id=0``（各入口的「没给」默认值）归一后是**空串**——若照常往下走，两本书
    都「查到 0 条」，「不知道是谁的账」就被读成「这个账户没有在途」，静默拆掉一道防线。
    """
    for blank in (0, "", None):
        read = await dx.read_inflight(None, tenant_id="default", user_id=blank)
        assert not read.ok, blank
        assert read.keys == frozenset()
        assert "账户身份为空" in read.errors[0]


@pytest.mark.asyncio
async def test_run_round_with_explicit_inflight_skips_the_read(monkeypatch) -> None:
    """显式给出在途（调用点刚从券商对账回来）→ 不取数；空集 = 「我确认没有在途」，
    与「读不到」是两回事。"""

    async def _boom(*a, **kw):  # pragma: no cover - 不该被调用
        raise AssertionError("显式在途不应触发取数")

    monkeypatch.setattr(dx, "read_inflight", _boom)

    submitter = _FakeSubmitter()
    outcome = await dx.run_round(
        _batch(_sell("600036.SH", 1.0)),
        round_id="rnd-1",
        holdings=dict([_held("600036.SH")]),
        quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
        inflight=frozenset(),
        real=False,
        submitter=submitter,
    )
    assert [c[0] for c in submitter.calls] == ["600036.SH"]
    assert outcome.aborted == ""


# ---------------------------------------------------------------------------
# 7. 默认提交器：与 ``push_orders`` 同形的 ``OrderRequest`` 契约
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_submitter_matches_the_candidate_push_convention(
    monkeypatch,
) -> None:
    """``source``/``mirror``/``mirror_source``/``real_limit_price``/``client_order_id``
    一律照 ``push_orders`` 的候选推送口径——别在这里另创一套。

    **不包** ``locked_execution``：``submit_order`` 内部已持同用户撮合临界区锁。
    """
    from backend.services.simulation.services import order_router

    sent: list = []

    async def _fake_submit(db, redis, req):
        sent.append(req)
        return _FakeOutcome(order_id="ord-x", mirror={"order_id": "real-x"})

    monkeypatch.setattr(order_router, "submit_order", _fake_submit)

    submit = dx._make_default_submitter(
        db=None, redis=None, tenant_id="default", user_id="1", source="llm_decision"
    )
    leg = dx.Leg(
        index=0,
        symbol="600036.SH",
        side="sell",
        quantity=500.0,
        limit_price=9.9,
        reason="模型减仓",
    )
    await submit(leg, "lld-rnd-1-600036.SH-sell", True)

    req = sent[0]
    assert req.tenant_id == "default"
    assert req.user_id == 10000001  # 旧口径 "1" → 规范名（int 列）
    assert req.symbol == "600036.SH"
    assert req.side == "sell"
    assert req.quantity == 500.0
    assert req.order_type == "market"  # 模拟腿取服务端快照价
    assert req.source == "llm_decision"
    assert req.client_order_id == "lld-rnd-1-600036.SH-sell"
    assert req.mirror is True
    assert req.mirror_source == "llm_decision"
    assert req.real_limit_price == 9.9  # 限价只约束镜像出去的那一笔
    assert "模型减仓" in req.remarks


@pytest.mark.asyncio
async def test_default_submitter_omits_the_real_order_fields_in_paper_mode(
    monkeypatch,
) -> None:
    """影子期（``real=False``）：不镜像、不带真单限价——但幂等键照给（模拟台账也要去重）。"""
    from backend.services.simulation.services import order_router

    sent: list = []

    async def _fake_submit(db, redis, req):
        sent.append(req)
        return _FakeOutcome()

    monkeypatch.setattr(order_router, "submit_order", _fake_submit)

    submit = dx._make_default_submitter(
        db=None,
        redis=None,
        tenant_id="default",
        user_id=10000001,
        source="llm_decision",
    )
    leg = dx.Leg(
        index=0,
        symbol="600036.SH",
        side="buy",
        quantity=100.0,
        limit_price=10.1,
        reason="补仓",
    )
    await submit(leg, "lld-rnd-1-600036.SH-buy", False)

    req = sent[0]
    assert req.mirror is False
    assert req.mirror_source == ""
    assert req.real_limit_price is None
    assert req.client_order_id == "lld-rnd-1-600036.SH-buy"


def test_executor_does_not_take_the_match_lock_or_bypass_the_router() -> None:
    """两条「别在这里补」的纪律做成源码闸门：

    * 不自己拿 ``locked_execution``（重入死锁）；
    * ``submit_order`` 是唯一入口（并且只在这一处 ``await``）。
    """
    from pathlib import Path

    src = Path(dx.__file__).read_text(encoding="utf-8")
    assert "locked_execution(" not in src
    assert src.count("await submit_order(") == 1
