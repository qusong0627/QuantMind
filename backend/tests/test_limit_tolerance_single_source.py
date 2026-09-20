"""涨跌停容差**唯一事实源**的收敛护栏。

背景：0.5pp 的取整容差曾在 6 个模块里各持一份同值字面量
（cn_exchange / trading_cost / broker_client / execution_engine /
extended_strategies / trading_cost），另有一族 0.2pp 的旧余量散落在 3 个文件
（build_factor_custom_dataset / enrich_sdl_data / inference_backtest_service）
和 factor_deep_dive 的 0.002。同值不代表同源 —— 下一次改口径只会改到一份。

两类断言，缺一不可：

1. **收敛性**：任何生产模块都不得再自持容差字面量，且各消费点取到的值
   确实等于 ``local_market_data.LIMIT_TOLERANCE``。
2. **机制性**：0.5pp 这个数不是拍的，它必须覆盖「涨跌停价按分取整」造成的
   全部偏差。这条用 ``compute_limits`` 的性质断言钉住，而不是靠几个样本 ——
   样本只能证明「那一天那几只够用」。
"""
from __future__ import annotations

import ast
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from backend.services.simulation.services.local_market_data import (
    LIMIT_TOLERANCE,
    LIMIT_TOLERANCE_BSE,
    compute_limits,
    limit_pct,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 与各消费点做等值比较用的字面量。刻意写成字面量而非从常量现算：
#: 现算的期望值恒等于实现，改错了也照样绿。
_TOL_LITERAL = 0.005  # fidelity: allow-limit-threshold — 期望值，钉住既有口径
_TOL_BSE_LITERAL = 0.01  # fidelity: allow-limit-threshold — 期望值，钉住既有口径

#: 扫描根与 ``check_market_fidelity.py`` 保持一致。
_SCAN_ROOTS = ("backend", "scripts", "tools")

#: 自持容差的写法：`_LIMIT_TOLERANCE = ...` / `_LIMIT_SLACK_PCT = ...` /
#: `TOUCH_TOLERANCE = ...` / `PRICE_EPS_YUAN = ...`。
#: 只认**赋值**，不认注释里的叙述 —— 本次收敛在多处留了「本模块曾有 X」的说明。
_ASSIGN_RE = re.compile(
    r"^\s*_?(?:"
    r"LIMIT_(?:TOLERANCE\w*|SLACK\w*)"
    r"|TOUCH_TOLERANCE\w*"
    r"|PRICE_EPS\w*"
    r")\s*=\s*[0-9]"
)


def _iter_py_files() -> list[Path]:
    out: list[Path] = []
    for root in _SCAN_ROOTS:
        base = _REPO_ROOT / root
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            parts = set(p.parts)
            if "__pycache__" in parts or "node_modules" in parts:
                continue
            out.append(p)
    return out


def test_canonical_values_are_pinned():
    """两个规范容差的数值本身 —— 改它必须是一次有意识的改动。"""
    assert LIMIT_TOLERANCE == _TOL_LITERAL
    assert LIMIT_TOLERANCE_BSE == _TOL_BSE_LITERAL
    # 北交所按截尾取整，偏差上界翻倍；宽于沪深是刻意的，不是笔误。
    assert LIMIT_TOLERANCE_BSE == pytest.approx(2 * LIMIT_TOLERANCE)


def test_market_breadth_derives_instead_of_restating():
    """market_breadth 的百分数形态必须由规范值换算而来。"""
    from backend.shared import market_breadth as mb

    assert mb.TOL_SHSZ == LIMIT_TOLERANCE * 100
    assert mb.TOL_BJ == LIMIT_TOLERANCE_BSE * 100
    assert mb.TOL_SHSZ == 0.5  # fidelity: allow-limit-threshold — 期望值
    assert mb.TOL_BJ == 1.0  # fidelity: allow-limit-threshold — 期望值


def test_review_stats_forwards_to_market_breadth():
    """review_stats 曾是 market_breadth 的整份副本（24 个同名定义）。

    现在只允许是转发层：常量与函数都必须是**同一个对象**，不是同值。
    """
    import importlib.util

    path = _REPO_ROOT / "backend" / "scripts" / "review_stats.py"
    spec = importlib.util.spec_from_file_location("_review_stats_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from backend.shared import market_breadth as mb

    assert mod.TOL_SHSZ is mb.TOL_SHSZ
    assert mod.TOL_BJ is mb.TOL_BJ
    assert mod.compute_limits is mb.compute_limits
    assert mod.limit_pct is mb.limit_pct
    assert mod.market_breadth is mb.market_breadth


def test_no_production_module_holds_its_own_tolerance_literal():
    """生产代码里不得再出现容差字面量赋值。

    唯一允许的两处是规范模块自己的定义，以及测试文件（期望值）。
    ``backend/tests/`` 里的字面量由 ``allow-limit-threshold`` 标记管理，
    不在本测试的靶心内。
    """
    offenders: list[str] = []
    for p in _iter_py_files():
        rel = p.relative_to(_REPO_ROOT).as_posix()
        if rel == "backend/services/simulation/services/local_market_data.py":
            continue  # 唯一事实源
        if "/tests/" in f"/{rel}" or rel.startswith("tests/"):
            continue  # 测试里的期望值由豁免标记管理
        try:
            src = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(src.splitlines(), 1):
            # 先剥行内注释：本次收敛在多处留了「本模块曾有 X = 0.005」的说明，
            # 那些是叙述不是赋值。
            code = line.split("#", 1)[0]
            if _ASSIGN_RE.match(code):
                offenders.append(f"{rel}:{i} — {line.strip()}")
    assert not offenders, "以下位置自持涨跌停容差，应改为导入 LIMIT_TOLERANCE：\n" + "\n".join(
        offenders
    )


@pytest.mark.parametrize(
    "module_path,attr",
    [
        ("backend.services.engine.qlib_app.utils.cn_exchange", None),
        ("backend.services.engine.inference.trading_cost", None),
        ("backend.services.engine.qlib_app.utils.extended_strategies", None),
    ],
)
def test_consumers_no_longer_export_a_tolerance_attribute(module_path, attr):
    """消费点不应再留有 `_LIMIT_TOLERANCE` 这类模块/类属性。"""
    import importlib

    mod = importlib.import_module(module_path)
    assert not hasattr(mod, "_LIMIT_TOLERANCE"), f"{module_path} 仍有模块级 _LIMIT_TOLERANCE"


def test_cn_exchange_threshold_equals_authority_minus_tolerance():
    """qlib 撮合层的阈值契约：**恒等于**权威口径减容差。"""
    from backend.services.engine.qlib_app.utils.cn_exchange import CnExchange

    for symbol, td in [
        ("SH600036", date(2026, 9, 18)),
        ("SZ300750", date(2026, 9, 18)),
        ("SH688981", date(2026, 9, 18)),
        ("SZ300750", date(2019, 6, 3)),  # 注册制改革前 = 主板 10%
    ]:
        expected = float(limit_pct(symbol, is_st=False, trade_date=td)) - LIMIT_TOLERANCE  # fidelity: allow-limit-threshold — 显式传参：夹具里没有 ST，两侧同传非 ST 才可比
        assert CnExchange._get_limit_threshold(symbol, trade_date=td) == expected


def test_trading_cost_carries_no_threshold_table_any_more():
    """`trading_cost` 里那张板块表必须保持退役。

    它曾按 `listing_market` 的**中文板名**查表（「沪市主板」→ 9.5%），而库里
    该列的真实取值是 `SH`/`SZ`/`BJ`/字符串 `"None"` —— **一处都匹配不上**，
    于是每一行都落到 9.5% 兜底：2026 年特征快照实测 436 行被剔、其中 132 行是
    误剔（20% 板 115 行 + 北交所 17 行，被剔行 |涨跌%| 中位数 11.4%）。

    这里钉住「符号不存在」而不是钉住某个数值：真正的回归风险是有人再种一张
    按板名/板别查表的常量表 —— 板别**永远**定不出阈值（`SZ` 同时覆盖 10% 与 20%）。
    """
    import importlib

    mod = importlib.import_module("backend.services.engine.inference.trading_cost")
    for name in (
        "limit_threshold",
        "price_limit_for_market",
        "_LIMIT_BY_MARKET",
        "_DEFAULT_PRICE_LIMIT",
    ):
        assert not hasattr(mod, name), f"trading_cost 又长出了 {name}"


@pytest.mark.parametrize(
    ("symbol", "tolerance"),
    [
        ("SH600036", LIMIT_TOLERANCE),
        ("SZ300750", LIMIT_TOLERANCE),
        ("SH688981", LIMIT_TOLERANCE),
        ("BJ430047", LIMIT_TOLERANCE_BSE),  # 截尾取整 → 容差翻倍
    ],
)
def test_simulation_fallback_threshold_agrees_with_authority(symbol, tolerance):
    """模拟撮合的兜底阈值 = 权威口径 − **该板别的**容差。

    期望值手工拼出来（而不是调用权威的 `limit_threshold`）：被测函数内部就是调它，
    拿它当期望会退化成恒真断言。这里独立地钉住「北交所换容差」这件事本身。
    """
    from backend.services.simulation.services.execution_engine import (
        SimulationExecutionEngine,
    )

    expected = (
        float(limit_pct(symbol, is_st=False, trade_date=date.today())) - tolerance  # fidelity: allow-limit-threshold — 显式传参：非 ST 一侧，独立于被测函数手拼期望值
    )
    assert SimulationExecutionEngine._board_limit_threshold(
        symbol
    ) == pytest.approx(expected), f"{symbol} 的撮合兜底阈值未跟随唯一事实源"


def test_legacy_two_tenths_family_is_gone():
    """0.2pp 旧余量族（9.8 = 10 − 0.2 的产物）必须已退役。

    它只在股价 ≥ ¥2.50 时够用；低于此会漏判真涨停 —— 实测 41 个交易日 ×
    2864 个封板事件里漏了 4 条（前收 1.23 / 1.43 / 1.44 / 1.74）。
    """
    from backend.scripts.build_factor_custom_dataset import (
        LIMIT_RULE_VERSION,
        _limit_slack,
    )

    assert _limit_slack(LIMIT_TOLERANCE) == Decimal("0.005")
    # 口径变了版本号必须 +1，否则 can_incremental 会复用旧口径的历史分区，
    # 磁盘上留下看不出接缝的两套线。
    assert LIMIT_RULE_VERSION == 3


def test_factor_deep_dive_slack_follows_the_authority():
    """factor_deep_dive 的贴板缓冲同样收敛（原为 0.002 = 0.2pp）。"""
    from backend.scripts.factor_deep_dive import limit_threshold

    td = "20260918"
    expected = float(limit_pct("600036.SH", is_st=False, trade_date=date(2026, 9, 18)))  # fidelity: allow-limit-threshold — 显式传参：主板非 ST 一侧，与权威同传才可比
    assert limit_threshold("600036.SH", td) == expected - LIMIT_TOLERANCE


@pytest.mark.parametrize(
    "symbol,is_st",
    [
        ("600036.SH", False),  # 主板 10%
        ("300750.SZ", False),  # 创业板 20%
        ("688981.SH", False),  # 科创板 20%
        ("830799.BJ", False),  # 北交所 30%
        ("600036.SH", True),   # ST 主板 5%
    ],
)
def test_tolerance_covers_cent_rounding_for_every_tradeable_price(symbol, is_st):
    """**机制性**断言：容差必须覆盖按分取整造成的全部偏差。

    封板价 = round_half_up(pre_close × (1 + 板别), 2)，故实际涨幅最多比名义板别
    低 ``0.005 / pre_close``（比例）。所以「容差 ≥ 0.005 / pre_close」⇔
    「pre_close ≥ 0.005 / 容差」，对沪深即 pre_close ≥ ¥1.00、北交所 ≥ ¥0.50。

    ¥1 是 A 股的**面值退市线**（连续 20 个交易日低于 1 元即终止上市），
    因此这个区间不是随手取的一段，而是「实际会出现的全部价位」。
    """
    tol = LIMIT_TOLERANCE_BSE if symbol.endswith(".BJ") else LIMIT_TOLERANCE
    nominal = float(limit_pct(symbol, is_st=is_st, trade_date=date(2026, 9, 18)))
    floor = Decimal("0.005") / Decimal(str(tol))

    for cents in range(100, 301):  # ¥1.00 … ¥3.00，逐分
        pre_close = Decimal(cents) / 100
        up_price, _ = compute_limits(
            symbol, float(pre_close), is_st=is_st, trade_date=date(2026, 9, 18)
        )
        if up_price <= 0:
            continue  # 无涨跌幅限制（新股首日等）
        actual_pct = float(Decimal(str(up_price)) / pre_close - 1)
        assert actual_pct >= nominal - tol, (
            f"{symbol} pre_close={pre_close} 封板价={up_price} 实际涨幅 "
            f"{actual_pct:.6f} 超出 容差 {tol} 的覆盖范围（下界 ¥{floor}）"
        )


def test_low_price_case_that_two_tenths_would_have_missed():
    """实测漏判样本的算术锚点：前收 ¥1.44，封板价 ¥1.58。

    实际涨幅 9.7222%，比名义 10% 低 0.278pp —— 0.2pp 容差判不出（9.8 > 9.7222），
    0.5pp 判得出（9.5 ≤ 9.7222）。这条断言的价值在于把「0.2pp 是错的」钉在
    测试里，而不是只写在注释中。
    """
    pre_close, up_price = Decimal("1.44"), Decimal("1.58")
    actual_pct = float(up_price / pre_close - 1) * 100
    nominal_pct = 10.0

    assert actual_pct == pytest.approx(9.7222, abs=1e-4)
    assert nominal_pct - actual_pct > 0.2  # 旧容差覆盖不到
    assert nominal_pct - actual_pct <= 0.5  # 新容差覆盖得到
    # 与实现同口径再确认一次：真封板价确实等于 compute_limits 的限价
    up, _ = compute_limits("000639.SZ", 1.44, is_st=False, trade_date=date(2026, 9, 18))  # fidelity: allow-limit-threshold — 显式传参：该票当日非 ST，与权威同传才可比
    assert float(up) == 1.58


def test_ast_scan_finds_no_python2_style_assignment_regression():
    r"""守卫本文件自己的扫描器：正则必须认得实现里出现过的两种写法。

    这条是元测试 —— 如果 ``_ASSIGN_RE`` 写坏了（例如漏了 ``\s*``），收敛扫描会
    静默返回 0 命中，绿灯变成「没扫到」而不是「扫过了」。
    """
    assert _ASSIGN_RE.match("_LIMIT_TOLERANCE = 0.005")
    assert _ASSIGN_RE.match("    _LIMIT_TOLERANCE = 0.005")
    assert _ASSIGN_RE.match("_LIMIT_SLACK_PCT = 0.2")
    assert _ASSIGN_RE.match("LIMIT_TOLERANCE_BSE = 0.01")
    # 贴板容差族与绝对价格容差族同样在扫描范围内
    assert _ASSIGN_RE.match("TOUCH_TOLERANCE = 0.0015")
    assert _ASSIGN_RE.match("_TOUCH_TOLERANCE = 0.0015")
    assert _ASSIGN_RE.match("PRICE_EPS_YUAN = 0.004")
    # 注释里的叙述不该命中（调用点会先剥注释，这里验证模式本身）
    assert not _ASSIGN_RE.match("    # 本模块曾有 _LIMIT_TOLERANCE = 0.005")


def test_scan_actually_covers_the_modules_that_were_converged():
    """守卫扫描范围：被收敛过的文件必须真的在扫描集里。

    没有这条，``_SCAN_ROOTS`` 被写错（比如漏了 ``scripts``）时上面那条
    「无违规」照样绿。
    """
    rels = {p.relative_to(_REPO_ROOT).as_posix() for p in _iter_py_files()}
    for expected in [
        "backend/services/engine/qlib_app/utils/cn_exchange.py",
        "backend/services/engine/inference/trading_cost.py",
        "backend/services/live_trading/services/broker_client.py",
        "backend/services/simulation/services/execution_engine.py",
        "backend/services/engine/qlib_app/utils/extended_strategies.py",
        "scripts/gen_ashare_strategy_templates.py",
        "backend/scripts/build_factor_custom_dataset.py",
        "backend/scripts/enrich_sdl_data.py",
        "backend/scripts/factor_deep_dive.py",
        "backend/services/engine/inference/inference_backtest_service.py",
    ]:
        assert expected in rels, f"扫描集缺少 {expected}"
    assert len(rels) > 200, f"扫描集只有 {len(rels)} 个文件，疑似根目录写错"


def test_no_production_import_goes_through_the_legacy_shim_tree():
    """``services/trade/simulation/`` 是只剩 ``import *`` 的并行旧树，不该再被引用。

    它转发的是同一批对象（实测 ``compute_limits`` / ``limit_pct`` /
    ``LIMIT_TOLERANCE`` 都是 ``is`` 同一个），因此**没有口径分叉** —— 但它是一条
    会误导人的路径：`tradability.py` 与 `market_breadth.py` 的文档都不得不专门
    写一句「不是这棵树」。2026-09-20 之前还有 5 个回测脚本从这里取
    ``compute_limits``，现已收口。

    这条断言钉住的是**没有新消费者**，不是「文件已删除」。
    """
    offenders: list[str] = []
    for p in _iter_py_files():
        rel = p.relative_to(_REPO_ROOT).as_posix()
        if rel.startswith("backend/services/trade/simulation/"):
            continue  # 旧树自身
        if "/tests/" in f"/{rel}" or rel.startswith("tests/"):
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(src.splitlines(), 1):
            code = line.split("#", 1)[0]
            if "services.trade.simulation" in code:
                offenders.append(f"{rel}:{i} — {line.strip()}")
    assert not offenders, (
        "以下位置仍从并行旧树取权威实现，请改为 "
        "backend.services.simulation.services.local_market_data：\n" + "\n".join(offenders)
    )


def test_legacy_shim_still_forwards_the_same_objects():
    """空壳若被仓库外的旧脚本引用，必须仍转发**同一对象**（不是同值副本）。"""
    import importlib

    old = importlib.import_module(
        "backend.services.trade.simulation.services.local_market_data"
    )
    new = importlib.import_module(
        "backend.services.simulation.services.local_market_data"
    )
    for name in ("compute_limits", "limit_pct", "LIMIT_TOLERANCE", "LIMIT_TOLERANCE_BSE"):
        assert getattr(old, name) is getattr(new, name), f"{name} 在旧树上不是同一对象"


def test_generated_template_body_carries_the_canonical_import():
    """出厂模板（用户会克隆走的那些）也必须不带自己的容差。"""
    import importlib.util

    path = _REPO_ROOT / "scripts" / "gen_ashare_strategy_templates.py"
    spec = importlib.util.spec_from_file_location("_gen_tpl_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    body = mod._LIMIT_UP_BODY
    ast.parse(body)  # 生成体必须是可解析的 Python
    assert "LIMIT_TOLERANCE" in body
    assert "_LIMIT_TOLERANCE" not in body, "生成体仍在自持容差副本"
    # 保守兜底保留：拿不到权威实现时按最严主板线判，宁少交易不放真涨停进来
    assert "return 0.095" in body  # fidelity: allow-limit-threshold — 断言语料：钉住生成体确实保留了「按最严主板线」的兜底字面量


# ---------------------------------------------------------------------------
# 「贴板」容差族：TOUCH_TOLERANCE（比例）与 PRICE_EPS_YUAN（元）
#
# 与上面的 LIMIT_TOLERANCE 是**三个不同的量**，别互相换算：
#   LIMIT_TOLERANCE    比例  判「涨幅够不够涨停」（回测/因子标注）
#   TOUCH_TOLERANCE    比例  判「成交价够不够贴涨停价」（实盘/模拟撮合）
#   PRICE_EPS_YUAN     元    判「两个分位价是不是同一个数」（日线封板分类）
# 2026-09-20 之前，前者的消费点自持 0.0015 共 8 处（2 个默认参数 + 6 处内联），
# 后者带一个被忽略的 symbol 参数散在 market_breadth 里。
# ---------------------------------------------------------------------------

_TOUCH_LITERAL = 0.0015  # fidelity: allow-limit-threshold — 期望值，钉住既有口径
_PRICE_EPS_LITERAL = 0.004  # fidelity: allow-limit-threshold — 期望值，钉住既有口径

#: 持有「成交价 vs 涨跌停价」比较逻辑的两个模块：模拟撮合栈与实盘服务里的纸面撮合。
#: 二者各有一份同名 ``_is_price_near``（同值同类）—— 本次只统一取值来源，
#: **不合并这两个函数**：跨服务合并属重构，不在口径收敛范围内。
#: 真单通道（QMTBroker / QmtExecBroker / RedisBroker / TdxBroker）不判贴板，
#: 涨跌停交给柜台，因此这组容差只影响模拟与纸面成交。
_TOUCH_CONSUMERS = (
    "backend/services/simulation/services/execution_engine.py",
    "backend/services/live_trading/services/broker_client.py",
)


def test_touch_tolerance_values_are_pinned():
    """三个容差各自的数值 —— 改它必须是一次有意识的改动。"""
    from backend.services.simulation.services.local_market_data import (
        PRICE_EPS_YUAN,
        TOUCH_TOLERANCE,
    )

    assert TOUCH_TOLERANCE == _TOUCH_LITERAL
    assert PRICE_EPS_YUAN == _PRICE_EPS_LITERAL
    # 单位不同、量级不同，不可互换；0.004 元 < 半分，吸不了 1 分的来源差异，
    # 所以它**不能**当比例容差用，反之亦然。
    assert PRICE_EPS_YUAN < 0.005
    assert TOUCH_TOLERANCE <= 0.005


def _float_literals(path: Path) -> list[float]:
    """模块里出现过的全部浮点字面量。

    用 AST 而不是正则：本次收敛在多处留了「本模块曾有 …… = 0.0015」的说明，
    正则会把 docstring 里的叙述也算成命中，AST 只认真正的常量节点。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    ]


@pytest.mark.parametrize("rel", _TOUCH_CONSUMERS)
def test_touch_consumers_hold_no_touch_tolerance_literal(rel):
    """消费点不得再自带 0.0015 —— 内联写法（``* (1 - 0.0015)``）正是这次漏网的形态。"""
    path = _REPO_ROOT / rel
    literals = _float_literals(path)
    # 防空转：扫到 0 个字面量说明文件读错了或路径失效，此时「没有 0.0015」毫无意义
    assert literals, f"{rel} 里一个浮点字面量都没有，扫描疑似失效"
    assert "TOUCH_TOLERANCE" in path.read_text(encoding="utf-8"), (
        f"{rel} 完全没有引用 TOUCH_TOLERANCE，本断言覆盖不到它"
    )
    assert _TOUCH_LITERAL not in literals, (
        f"{rel} 仍自带 {_TOUCH_LITERAL} 字面量，应改为导入 TOUCH_TOLERANCE"
    )


@pytest.mark.parametrize(
    "module_path,class_name",
    [
        (
            "backend.services.simulation.services.execution_engine",
            "SimulationExecutionEngine",
        ),
        ("backend.services.live_trading.services.broker_client", "PaperTradingBroker"),
    ],
)
def test_touch_helper_reads_the_authority_at_call_time(
    module_path, class_name, monkeypatch
):
    """两栈的 ``_is_price_near`` 必须**每次调用**读事实源。

    断言方式不是「值等于 0.0015」（同值不代表同源 —— 正是这样漂移出 8 份的），
    而是**改权威常量，消费点行为随之改变**。为此导入必须写在函数体内（函数内的
    ``from X import Y`` 每次调用都重新取值）；写成模块常量或默认参数就断不了。
    """
    import importlib
    import inspect

    from backend.services.simulation.services import local_market_data as lmd

    cls = getattr(importlib.import_module(module_path), class_name)
    near = cls._is_price_near

    # 默认参数曾是这个容差的第二份副本，且开了「调用方可传旧口径」的口子
    assert "tolerance" not in inspect.signature(near).parameters

    limit = 10.0
    assert near(limit, limit) is True
    assert near(limit * (1 - 0.001), limit) is True  # 偏差 0.1% < 0.15%
    assert near(limit * (1 - 0.003), limit) is False  # 偏差 0.3% > 0.15%
    # 取不到限价 / 价格非正 → 一律「不贴板」，不因缺数据误报
    assert near(limit, None) is False
    assert near(0.0, limit) is False

    monkeypatch.setattr(lmd, "TOUCH_TOLERANCE", 0.5)
    assert near(5.0, limit) is True, "贴板容差没有跟随唯一事实源（可能是模块期快照）"
    monkeypatch.setattr(lmd, "TOUCH_TOLERANCE", 0.0)
    assert near(5.0, limit) is False


def test_market_breadth_price_tolerance_forwards_the_authority():
    """``price_tolerance`` 的 0.004 同样只有一份来源。

    注意它与上面两条断言形态不同：这里 import 在**模块级**，是导入期快照，
    改事实源不会回写 —— 所以只能断言「是同一个对象」，不能断言运行期联动。
    """
    from backend.services.simulation.services.local_market_data import (
        PRICE_EPS_YUAN,
    )
    from backend.shared import market_breadth as mb

    assert mb.PRICE_EPS_YUAN is PRICE_EPS_YUAN
    assert mb.price_tolerance("600000.SH") == _PRICE_EPS_LITERAL
    # symbol 参数是历史遗留、被忽略；两个不同板别取到同一个值
    assert mb.price_tolerance("300750.SZ") == mb.price_tolerance("830799.BJ")
