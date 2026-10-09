"""T-FB-02 单测：因子代码列依赖静态提取与跨市场兼容性分类。

这是「因子 × 市场适配矩阵」的前置闸门——误报 ``portable`` 会把因子送进
必然缺列的跑批（白烧算力、污染矩阵），误报 ``data_unsupported`` 会把能跑的
因子藏起来。两个方向都要钉死。

分类只做**静态**判据（AST 字符串常量里的 ``$`` 列 token ⊆ 市场列集），
不做执行、不读数据文件；动态写法（$ 拼接/f-string）判不穿时归 ``unknown``
交由实跑裁决，绝不猜。
"""

import pytest

from backend.services.engine.factor_backtest.compat import (
    BASE_COLUMNS,
    CN_MINING_COLUMNS,
    classify_factor,
    extract_column_tokens,
)

pytestmark = pytest.mark.unit


# ── extract_column_tokens ────────────────────────────────────────────


def test_extracts_dollar_tokens_from_subscript_strings():
    """df["$close"] / df['$volume'] 这类下标字符串是列依赖的规范写法。"""
    # Arrange
    code = 'x = df["$close"] / df["$volume"]'
    # Act
    scan = extract_column_tokens(code)
    # Assert
    assert scan.values == {"$close", "$volume"}
    assert scan.dynamic is False


def test_ignores_plain_names_without_dollar():
    """不带 $ 的标识符不是列依赖（挖掘契约列一律 $ 前缀引用）。"""
    # Arrange
    code = "close = data['close']\ntotal = 5\nx = total + 1"
    # Act / Assert
    assert extract_column_tokens(code).values == set()


def test_ignores_dollar_tokens_inside_comments():
    """注释里提到 $turn_5 不算依赖——否则注释会伪造缺列、误杀可跑因子。"""
    # Arrange
    code = '# 本想用 $turn_5，最终用 $close\nx = df["$close"]'
    # Act / Assert
    assert extract_column_tokens(code).values == {"$close"}


def test_ignores_dollar_tokens_inside_docstring():
    """docstring 里的 $ 引用是说明文字，不是依赖。"""
    # Arrange
    code = '"""使用 $netflow_5 与 $close 的说明。"""\nx = df["$close"]'
    # Act / Assert
    assert extract_column_tokens(code).values == {"$close"}


def test_reports_dynamic_dollar_concat():
    """``"$" + name`` 是动态列名——token 集不可信，必须判 dynamic。"""
    # Arrange
    code = 'col = "$" + suffix\nx = df[col]'
    # Act / Assert
    assert extract_column_tokens(code).dynamic is True


def test_reports_dynamic_fstring():
    """f"$turn_{n}" 是动态列名。"""
    # Arrange
    code = 'x = df[f"$turn_{n}"]'
    # Act / Assert
    assert extract_column_tokens(code).dynamic is True


def test_partial_token_concat_is_dynamic_not_token():
    """``"$turn_" + str(n)`` 里的半截 token 不是列名，不得被当成真实依赖。"""
    # Arrange
    code = 'x = df["$turn_" + str(n)]'
    # Act
    scan = extract_column_tokens(code)
    # Assert
    assert scan.dynamic is True
    assert "$turn_" not in scan.values


def test_percent_format_with_dollar_is_dynamic():
    """``"$%s_5" % name`` 这类格式化同样判动态。"""
    # Arrange
    code = 'x = df["$%s_5" % name]'
    # Act / Assert
    assert extract_column_tokens(code).dynamic is True


def test_syntax_error_raises_value_error():
    """语法错误的代码无法静态判依赖：抛 ValueError（调用方归 unknown）。"""
    # Arrange
    code = "def broken(:\n    pass"
    # Act / Assert
    with pytest.raises(ValueError):
        extract_column_tokens(code)


# ── classify_factor ──────────────────────────────────────────────────


def test_base_factor_portable_on_us():
    """只用基础列的因子在美股可移植。"""
    # Arrange
    code = 'x = df["$close"].pct_change() * df["$volume"]'
    # Act
    res = classify_factor(code, BASE_COLUMNS)
    # Assert
    assert res["status"] == "portable"
    assert res["missing"] == []


def test_amount_factor_portable_on_us():
    """$amount 在五市场 bin 均有（实测 7 列同构）——amount 档全市场可跑。"""
    # Arrange
    code = 'illiq = (df["$close"].pct_change().abs() / df["$amount"])'
    # Act
    res = classify_factor(code, BASE_COLUMNS)
    # Assert
    assert res["status"] == "portable"


def test_enriched_factor_unsupported_on_us_with_missing_list():
    """富化列（$netflow_5）在非 CN 不存在：data_unsupported + 缺列清单。"""
    # Arrange
    code = 'x = df["$netflow_5"] + df["$close"]'
    # Act
    res = classify_factor(code, BASE_COLUMNS)
    # Assert
    assert res["status"] == "data_unsupported"
    assert res["missing"] == ["$netflow_5"]


def test_enriched_factor_portable_on_cn():
    """同一因子在 CN（39 列挖掘契约）可跑。"""
    # Arrange
    code = 'x = df["$netflow_5"] + df["$close"]'
    # Act
    res = classify_factor(code, CN_MINING_COLUMNS)
    # Assert
    assert res["status"] == "portable"


def test_missing_list_sorted_and_deduplicated():
    """缺列清单排序去重——直接用于界面展示。"""
    # Arrange
    code = 'a = df["$netflow_5"] + df["$turn_5"] + df["$netflow_5"]'
    # Act
    res = classify_factor(code, BASE_COLUMNS)
    # Assert
    assert res["missing"] == ["$netflow_5", "$turn_5"]


def test_dynamic_code_is_unknown_not_unsupported():
    """动态写法判不穿 → unknown（照跑裁决），不得误杀成 data_unsupported。"""
    # Arrange
    code = 'x = df["$" + suffix]'
    # Act
    res = classify_factor(code, BASE_COLUMNS)
    # Assert
    assert res["status"] == "unknown"
    assert res["reason"] == "dynamic_column_reference"


def test_no_token_code_is_unknown():
    """无任何 $ 引用的代码判不穿 → unknown。"""
    # Arrange
    code = "x = 1 + 2"
    # Act
    res = classify_factor(code, BASE_COLUMNS)
    # Assert
    assert res["status"] == "unknown"
    assert res["reason"] == "no_column_reference"


def test_static_missing_beats_dynamic():
    """静态缺列已成事实（$netflow_5 ∉ US）时，即便代码里另有动态写法，
    也判 data_unsupported——缺列证据比动态疑点更硬。"""
    # Arrange
    code = 'a = df["$netflow_5"]\nb = df["$" + x]'
    # Act
    res = classify_factor(code, BASE_COLUMNS)
    # Assert
    assert res["status"] == "data_unsupported"


def test_syntax_error_classified_unknown():
    """语法错误归 unknown（执行侧会以真实报错收口，不在这里下结论）。"""
    # Arrange
    code = "def broken(:\n  pass"
    # Act
    res = classify_factor(code, BASE_COLUMNS)
    # Assert
    assert res["status"] == "unknown"
    assert res["reason"] == "syntax_error"


def test_tokens_returned_sorted_for_stable_ui():
    """返回的 tokens 排序稳定（矩阵悬浮卡直接渲染）。"""
    # Arrange
    code = 'x = df["$volume"] + df["$close"]'
    # Act
    res = classify_factor(code, BASE_COLUMNS)
    # Assert
    assert res["tokens"] == ["$close", "$volume"]


# ── 列集金样 ─────────────────────────────────────────────────────────


def test_base_columns_match_live_bin_layout():
    """五市场 bin 实测 7 列（2026-10-09 ls features/<标的>/）：OHLCV+amount+factor。"""
    assert BASE_COLUMNS == {
        "$open",
        "$high",
        "$low",
        "$close",
        "$volume",
        "$amount",
        "$factor",
    }


def test_cn_mining_contract_is_39_columns():
    """CN 挖掘同源契约 = 39 列（daily_pv_all.h5 实测列头，含 7 基础列）。"""
    assert len(CN_MINING_COLUMNS) == 39
    assert BASE_COLUMNS <= CN_MINING_COLUMNS
    # 富化代表列抽查（换手/资金流/概念热度——富化档因子的典型依赖）
    for col in ("$turn_5", "$turn_20", "$idio_vol_20", "$netflow_5", "$concept_hot"):
        assert col in CN_MINING_COLUMNS
