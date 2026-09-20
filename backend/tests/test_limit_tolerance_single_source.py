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

#: 自持容差的写法：`_LIMIT_TOLERANCE = ...` / `_LIMIT_SLACK_PCT = ...`。
#: 只认**赋值**，不认注释里的叙述 —— 本次收敛在多处留了「本模块曾有 X」的说明。
_ASSIGN_RE = re.compile(r"^\s*_?LIMIT_(?:TOLERANCE\w*|SLACK\w*)\s*=\s*[0-9]")


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
        expected = float(limit_pct(symbol, is_st=False, trade_date=td)) - LIMIT_TOLERANCE
        assert CnExchange._get_limit_threshold(symbol, trade_date=td) == expected


def test_trading_cost_and_simulation_thresholds_agree_with_authority():
    """回测成本模型 / 模拟撮合的兜底阈值同样恒等于「权威口径 − 容差」。"""
    from backend.services.engine.inference.trading_cost import limit_threshold
    from backend.services.simulation.services.execution_engine import (
        SimulationExecutionEngine,
    )

    # trading_cost 按 listing_market 字符串取板别，主板 = 10%
    assert limit_threshold("沪市主板") == pytest.approx(0.10 - LIMIT_TOLERANCE)

    for symbol in ("SH600036", "SZ300750", "SH688981"):
        expected = (
            float(limit_pct(symbol, is_st=False, trade_date=date.today()))
            - LIMIT_TOLERANCE
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
    expected = float(limit_pct("600036.SH", is_st=False, trade_date=date(2026, 9, 18)))
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
    up, _ = compute_limits("000639.SZ", 1.44, is_st=False, trade_date=date(2026, 9, 18))
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
    assert "return 0.095" in body
