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
    BUY,
    HOLD,
    PCT_GIVEN,
    SELL,
    STATUS_OK,
    Decision,
    DecisionBatch,
    Pct,
)
from backend.shared.decision.execution import Holding, Leg, Quote
from backend.shared.decision.gates import BuyGate

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


def _buy(code: str, pct: float = 0.5) -> Decision:
    return Decision(
        action=BUY,
        code=code,
        pct=Pct(pct, PCT_GIVEN),
        reason="建仓",
    )


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
async def test_default_submitter_closes_the_agent_into_the_order_request(
    monkeypatch,
) -> None:
    """agent 与 tenant/账户同处理：由**构造时闭合**（「这一轮是谁在跑」不是每条腿
    各自决定的事）。它随 ``OrderRequest`` 落 ``sim_orders.agent`` 并跟着镜像进
    ``orders.agent``——成交回报带回来的只有订单，读不出归属就没法分账（P2.7）。"""
    from backend.services.simulation.services import order_router as router

    captured: list[object] = []

    async def _fake_submit_order(db, redis, req):  # noqa: ANN001 - 与真实签名同形
        captured.append(req)
        return _FakeOutcome(order_id="ord-1")

    monkeypatch.setattr(router, "submit_order", _fake_submit_order)
    submit = dx._make_default_submitter(
        db=object(),
        redis=object(),
        tenant_id="default",
        user_id=str(_ACCOUNT),
        source="llm_decision",
        agent="deepseek-v4-flash",
    )
    leg = Leg(
        index=0,
        symbol="600036.SH",
        side="sell",
        quantity=100.0,
        limit_price=38.0,
        reason="止盈",
    )
    await submit(leg, "lld-rnd1-600036.SH-sell", True)

    assert len(captured) == 1
    req = captured[0]
    assert req.agent == "deepseek-v4-flash"
    assert (req.tenant_id, req.user_id) == ("default", _ACCOUNT)
    assert (req.symbol, req.quantity, req.client_order_id) == (
        "600036.SH",
        100.0,
        "lld-rnd1-600036.SH-sell",
    )
    # 真单腿才镜像；模拟腿 mirror=False（别把 agent 也当成「这条要镜像」的信号）
    assert req.mirror is True
    assert req.mirror_source == "llm_decision"


@pytest.mark.asyncio
async def test_default_submitter_without_an_agent_sends_an_empty_string(
    monkeypatch,
) -> None:
    """单 agent 轮次留空 → ``OrderRequest.agent`` 是空串（入口形态与历史一致），
    由落库侧的 ``normalize_agent`` 统一转成 NULL。"""
    from backend.services.simulation.services import order_router as router

    captured: list[object] = []

    async def _fake_submit_order(db, redis, req):  # noqa: ANN001
        captured.append(req)
        return _FakeOutcome(order_id="ord-2")

    monkeypatch.setattr(router, "submit_order", _fake_submit_order)
    submit = dx._make_default_submitter(
        db=object(),
        redis=object(),
        tenant_id="default",
        user_id=str(_ACCOUNT),
        source="llm_decision",
    )
    await submit(
        Leg(
            index=0,
            symbol="600036.SH",
            side="buy",
            quantity=100.0,
            limit_price=38.0,
            reason="建仓",
        ),
        None,
        False,
    )
    assert captured[0].agent == ""
    assert captured[0].client_order_id is None


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


# ---------------------------------------------------------------------------
# 8. 执行户注资：买单现金墙（2026-10-09 实盘事故）的修复面
# ---------------------------------------------------------------------------
#
# 事故：决策轮买单全拒 ``Insufficient cash for buy order``——Lua 现金守卫只读
# 执行户 ``cash``（¥4,070.93，持仓占满现金），而 sizing 用的是桥户剩余/分账
# vcash（8.5 万级），三本账量级差 ~200 倍。修法：real 轮在**提交前**把执行户
# 现金补到「本轮买单地板」（快照价 × 数量 × 缓冲），Redis 原子入金 + PG 台账行
# 同额落痕；注资失败 fail-open——最坏是撞旧墙（拒单留痕），绝不因注资故障吞掉
# 本来能成的卖单/小额单。


def _buy_gate(*codes: str) -> BuyGate:
    return BuyGate(pool_codes=frozenset(codes))


class _FakeFunder:
    """注资器替身：记录 ``(market, floor, round_id, agent)``；可安排异常/记序。"""

    def __init__(
        self, *, log: list | None = None, error: Exception | None = None
    ) -> None:
        self.calls: list[tuple] = []
        self._log = log
        self._error = error

    async def __call__(self, market: str, floor: float, round_id: str, agent: str):
        if self._log is not None:
            self._log.append("fund")
        self.calls.append((market, floor, round_id, agent))
        if self._error is not None:
            raise self._error
        return {"funded": floor}


class _LoggingSubmitter(_FakeSubmitter):
    """与 ``_FakeFunder`` 共享一条事件序列，钉「注资在提交之前」。"""

    def __init__(self, log: list) -> None:
        super().__init__()
        self._log = log

    async def __call__(self, leg, client_order_id, real):
        self._log.append("submit")
        return await super().__call__(leg, client_order_id, real)


def test_buy_funding_floor_sums_buy_legs_only_with_buffer() -> None:
    """地板 = Σ(买单腿 数量×价格) ×(1+缓冲)；卖单腿不参与（它们回笼资金）。"""
    legs = (
        Leg(0, "600036.SH", "buy", 500.0, None, ""),
        Leg(1, "600519.SH", "sell", 100.0, None, ""),
        Leg(2, "000001.SZ", "buy", 200.0, None, ""),
    )
    quotes = {
        "600036.SH": Quote(symbol="600036.SH", price=10.0),
        "000001.SZ": Quote(symbol="000001.SZ", price=5.0),
    }
    floors = dx.buy_funding_floor(legs, quotes)
    assert floors["CN"] == pytest.approx((500.0 * 10.0 + 200.0 * 5.0) * 1.10)


def test_buy_funding_floor_prefers_quote_then_limit_then_skips_unpriced() -> None:
    """价格口径：本轮快照价优先（执行段填价的基础），缺快照回退腿上限价；
    两者都缺 → 该腿不计入（不猜价——那腿到撮合也是「无法获取实时行情」）。"""
    legs = (
        Leg(0, "600036.SH", "buy", 100.0, 9.0, ""),  # 快照 10 → 用 10，不用限价 9
        Leg(1, "600519.SH", "buy", 10.0, 100.0, ""),  # 无快照 → 限价 100
        Leg(2, "600000.SH", "buy", 10.0, 8.0, ""),  # 快照在但无价 → 限价 8
        Leg(3, "000001.SZ", "buy", 100.0, None, ""),  # 两头都缺 → 跳过
    )
    quotes = {
        "600036.SH": Quote(symbol="600036.SH", price=10.0),
        "600000.SH": Quote(symbol="600000.SH", price=None),
    }
    floors = dx.buy_funding_floor(legs, quotes, buffer=0.0)
    assert floors == {"CN": pytest.approx(1000.0 + 1000.0 + 80.0)}


def test_buy_funding_floor_groups_by_market() -> None:
    """按市场分组：账户键有市场维度（港股地板不能记进 A 股执行户）。"""
    legs = (
        Leg(0, "600036.SH", "buy", 100.0, 10.0, ""),
        Leg(1, "00700.HK", "buy", 100.0, 400.0, ""),
    )
    floors = dx.buy_funding_floor(legs, {}, buffer=0.0)
    assert floors == {"CN": pytest.approx(1000.0), "HK": pytest.approx(40000.0)}


def test_buy_funding_floor_without_buy_legs_is_empty() -> None:
    legs = (Leg(0, "600036.SH", "sell", 100.0, 10.0, ""),)
    assert dx.buy_funding_floor(legs, {}) == {}


@pytest.mark.asyncio
async def test_execute_batch_funds_real_buy_legs_before_submitting(monkeypatch) -> None:
    """real 轮：注资发生在**任何提交之前**，金额=本轮地板，带 round/agent 留痕坐标。"""
    monkeypatch.setenv("QM_DECISION_ROUND_EXEC_FUNDING", "true")
    log: list[str] = []
    funder = _FakeFunder(log=log)
    submitter = _LoggingSubmitter(log)
    outcome = await dx.execute_batch(
        _batch(_buy("600036.SH", 0.5)),
        round_id="rnd-1",
        holdings={},
        quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
        gate=_buy_gate("600036.SH"),
        quota=10000.0,
        real=True,
        agent="deepseek-v4-pro",
        submitter=submitter,
        funder=funder,
    )
    assert log == ["fund", "submit"]
    market, floor, round_id, agent = funder.calls[0]
    assert (market, round_id, agent) == ("CN", "rnd-1", "deepseek-v4-pro")
    assert floor == pytest.approx(500.0 * 10.0 * 1.10)
    assert outcome.summary()["submitted"] == 1


@pytest.mark.asyncio
async def test_execute_batch_never_funds_paper_rounds(monkeypatch) -> None:
    """影子期（real=False）不注资：模拟腿没有真单，注资只会污染执行户台账。"""
    monkeypatch.setenv("QM_DECISION_ROUND_EXEC_FUNDING", "true")
    funder = _FakeFunder()
    await dx.execute_batch(
        _batch(_buy("600036.SH", 0.5)),
        round_id="rnd-1",
        holdings={},
        quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
        gate=_buy_gate("600036.SH"),
        quota=10000.0,
        real=False,
        submitter=_FakeSubmitter(),
        funder=funder,
    )
    assert funder.calls == []


@pytest.mark.asyncio
async def test_execute_batch_skips_funding_when_there_are_no_buy_legs(
    monkeypatch,
) -> None:
    """卖单轮不需要地板：卖单回笼资金，不注资。"""
    monkeypatch.setenv("QM_DECISION_ROUND_EXEC_FUNDING", "true")
    funder = _FakeFunder()
    submitter = _FakeSubmitter()
    await dx.execute_batch(
        _batch(_sell("600036.SH", 1.0)),
        round_id="rnd-1",
        holdings=dict([_held("600036.SH")]),
        quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
        real=True,
        submitter=submitter,
        funder=funder,
    )
    assert funder.calls == []
    assert len(submitter.calls) == 1


@pytest.mark.asyncio
async def test_execute_batch_funding_kill_switch(monkeypatch) -> None:
    """运维闸：``QM_DECISION_ROUND_EXEC_FUNDING=false`` 时注资整段跳过（腿照发）。"""
    monkeypatch.setenv("QM_DECISION_ROUND_EXEC_FUNDING", "false")
    funder = _FakeFunder()
    submitter = _FakeSubmitter()
    await dx.execute_batch(
        _batch(_buy("600036.SH", 0.5)),
        round_id="rnd-1",
        holdings={},
        quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
        gate=_buy_gate("600036.SH"),
        quota=10000.0,
        real=True,
        submitter=submitter,
        funder=funder,
    )
    assert funder.calls == []
    assert len(submitter.calls) == 1


@pytest.mark.asyncio
async def test_execute_batch_funding_is_on_by_default(monkeypatch) -> None:
    """默认开：变量缺席时注资照跑（修复默认生效，除非显式关掉）。"""
    monkeypatch.delenv("QM_DECISION_ROUND_EXEC_FUNDING", raising=False)
    funder = _FakeFunder()
    await dx.execute_batch(
        _batch(_buy("600036.SH", 0.5)),
        round_id="rnd-1",
        holdings={},
        quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
        gate=_buy_gate("600036.SH"),
        quota=10000.0,
        real=True,
        submitter=_FakeSubmitter(),
        funder=funder,
    )
    assert len(funder.calls) == 1


@pytest.mark.asyncio
async def test_execute_batch_survives_a_failing_funder(monkeypatch, caplog) -> None:
    """注资失败 fail-open：腿照发（最坏撞旧墙=拒单留痕），错误必须响。"""
    monkeypatch.setenv("QM_DECISION_ROUND_EXEC_FUNDING", "true")
    funder = _FakeFunder(error=RuntimeError("redis 挂了"))
    submitter = _FakeSubmitter()
    with caplog.at_level("ERROR"):
        outcome = await dx.execute_batch(
            _batch(_buy("600036.SH", 0.5)),
            round_id="rnd-1",
            holdings={},
            quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
            gate=_buy_gate("600036.SH"),
            quota=10000.0,
            real=True,
            submitter=submitter,
            funder=funder,
        )
    assert len(submitter.calls) == 1
    assert outcome.aborted == ""
    assert any("注资" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_run_round_wires_the_default_funder_from_its_own_coordinates(
    monkeypatch,
) -> None:
    """``run_round`` 没收到注资器时用账户坐标构造默认件（db/redis/tenant/user 闭合）。"""
    monkeypatch.setenv("QM_DECISION_ROUND_EXEC_FUNDING", "true")
    built: list[dict] = []
    funder = _FakeFunder()

    def _fake_make(**kw):
        built.append(kw)
        return funder

    monkeypatch.setattr(dx, "_make_default_funder", _fake_make)
    await dx.run_round(
        _batch(_buy("600036.SH", 0.5)),
        round_id="rnd-1",
        holdings={},
        quotes={"600036.SH": Quote(symbol="600036.SH", price=10.0)},
        gate=_buy_gate("600036.SH"),
        quota=10000.0,
        inflight=frozenset(),
        real=True,
        db="DB",
        redis="REDIS",
        tenant_id="default",
        user_id=_ACCOUNT,
        submitter=_FakeSubmitter(),
    )
    assert built and built[0]["tenant_id"] == "default"
    assert built[0]["user_id"] == _ACCOUNT
    assert len(funder.calls) == 1


# ── 默认注资器的两本书编排（Redis 原子入金 + PG 台账行）──────────────────
class _FunderRecorder:
    def __init__(self) -> None:
        self.funded: list[tuple] = []
        self.ledger: list[dict] = []
        self.commits = 0
        self.events: list[str] = []


def _install_fake_funding_stack(
    monkeypatch,
    *,
    snapshot: dict | None,
    fund_result: dict | None = None,
    ledger_boom: Exception | None = None,
) -> tuple:
    """把默认注资器的两个外部依赖换成替身（源码里的惰性 import 也拦得到）。

    ``_Mgr`` 伪造的是 manager 的**注资面**（``ensure_cash_floor``：锁内读账 →
    算缺口 → 原子入金，一体）。锁纪律留在 manager 里（它拥有账户键），dx 层
    只调用该方法——故此处的替身没有锁/读账/入金三个分步。
    """
    import backend.services.simulation.services.ledger_service as ledger_mod
    import backend.services.trade_shared.simulation_manager as manager_mod

    rec = _FunderRecorder()

    class _Mgr:
        def __init__(self, redis):
            pass

        async def ensure_cash_floor(
            self, user_id, floor, tenant_id="default", market="CN", min_deficit=0.0
        ):
            rec.events.append("ensure")
            if fund_result is not None:
                return fund_result
            if snapshot is None:
                return {"success": False, "reason": "ACCOUNT_NOT_FOUND"}
            cash = float(snapshot.get("cash") or 0.0)
            deficit = float(floor) - cash
            if deficit <= float(min_deficit):
                return {
                    "success": True,
                    "funded": 0.0,
                    "cash": cash,
                    "snapshot": snapshot,
                }
            rec.funded.append((user_id, deficit, tenant_id, market))
            return {
                "success": True,
                "funded": deficit,
                "cash_before": cash,
                "cash": cash + deficit,
                "snapshot": snapshot,
            }

    class _Ledger:
        def __init__(self, db):
            pass

        async def record_cash_adjustment(self, **kw):
            if ledger_boom is not None:
                raise ledger_boom
            rec.events.append("ledger")
            rec.ledger.append(kw)
            return 250.0

    class _DB:
        async def commit(self):
            rec.events.append("commit")
            rec.commits += 1

    monkeypatch.setattr(manager_mod, "SimulationAccountManager", _Mgr)
    monkeypatch.setattr(ledger_mod, "SimulationLedgerService", _Ledger)
    funder = dx._make_default_funder(
        db=_DB(), redis=object(), tenant_id="default", user_id=_ACCOUNT
    )
    return funder, rec


@pytest.mark.asyncio
async def test_default_funder_tops_up_the_deficit_into_both_books(monkeypatch) -> None:
    """缺口 = 地板 − 现现金；Redis 先加、PG 台账行同额跟上（提交权在调用方）。"""
    snapshot = {"cash": 100.0, "available_cash": 100.0, "total_asset": 900.0}
    funder, rec = _install_fake_funding_stack(monkeypatch, snapshot=snapshot)
    out = await funder("CN", 1000.0, "rnd-1", "agent-x")
    assert rec.funded == [(_ACCOUNT, 900.0, "default", "CN")]
    assert rec.ledger[0]["amount"] == 900.0
    assert rec.ledger[0]["ref_id"] == "rnd-1"
    assert rec.ledger[0]["account_snapshot"] is snapshot  # 用注资前的 Redis 快照建行
    assert rec.commits == 1
    assert rec.events == ["ensure", "ledger", "commit"]
    assert out["funded"] == 900.0


@pytest.mark.asyncio
async def test_default_funder_skips_when_cash_already_covers_the_floor(
    monkeypatch,
) -> None:
    """现金已够地板：一分不加（宁可上轮余量留着，也不写无意义的台账行）。"""
    funder, rec = _install_fake_funding_stack(monkeypatch, snapshot={"cash": 2000.0})
    out = await funder("CN", 1000.0, "rnd-1", "")
    assert rec.funded == []
    assert rec.ledger == []
    assert rec.commits == 0
    assert out["funded"] == 0.0


@pytest.mark.asyncio
async def test_default_funder_skips_when_the_account_does_not_exist(
    monkeypatch,
) -> None:
    """执行户不存在 → 跳过（首笔成交会自动按 100 万建账，现金墙天然不存在）。"""
    funder, rec = _install_fake_funding_stack(monkeypatch, snapshot=None)
    out = await funder("CN", 1000.0, "rnd-1", "")
    assert rec.funded == []
    assert out["funded"] == 0.0


@pytest.mark.asyncio
async def test_default_funder_reports_a_rejected_redis_fund(
    monkeypatch, caplog
) -> None:
    """Redis 入金被拒（账没了/脚本异常）→ 不写台账行、不提交、如实回报告。"""
    funder, rec = _install_fake_funding_stack(
        monkeypatch,
        snapshot={"cash": 0.0},
        fund_result={"success": False, "reason": "ACCOUNT_NOT_FOUND"},
    )
    with caplog.at_level("ERROR"):
        out = await funder("CN", 500.0, "rnd-1", "")
    assert rec.ledger == []
    assert rec.commits == 0
    assert out["funded"] == 0.0
    assert any("注资" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_default_funder_keeps_the_redis_bump_when_the_ledger_write_fails(
    monkeypatch, caplog
) -> None:
    """PG 台账失败**不撤销** Redis 入金（撤销会再开一条竞态）：执行口径优先，
    缺行 ERROR 留痕（多余现金无害，下一轮地板把它算进 cash）。"""
    funder, rec = _install_fake_funding_stack(
        monkeypatch,
        snapshot={"cash": 0.0},
        ledger_boom=RuntimeError("PG 挂了"),
    )
    with caplog.at_level("ERROR"):
        out = await funder("CN", 500.0, "rnd-1", "")
    assert rec.funded  # Redis 已加
    assert rec.commits == 0
    assert out["funded"] == 500.0
    assert any("台账" in r.getMessage() for r in caplog.records)
