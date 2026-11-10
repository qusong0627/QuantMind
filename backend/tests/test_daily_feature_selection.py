"""日频特征选择的配额分组回归测试。

重点锁住 2026-11-10 的误分类：``_category_group`` 曾把 ``"pe"`` 当基本面
关键词，``OPEN55``/``peak``/``type`` 都命中 ``pe`` 子串，导致 Alpha360
量价因子被塞进基本面配额。分组优先走因子研究内置分类，兜底才用来源库/关键词。
"""

from __future__ import annotations

import pytest

from backend.scripts.stock_classifier.select_daily_features import _category_group


@pytest.mark.unit
def test_open55_alpha360_is_price_volume_not_fundamental():
    # Arrange / Act
    group = _category_group(
        "OPEN55", "alpha360", "Alpha360 量价", "alpha360", "量价基础", "price_volume"
    )
    # Assert
    assert group == "量价/挖掘"


@pytest.mark.unit
def test_alpha360_fallback_uses_l1_price_volume_label():
    # 分类 id 缺失时，l1 的「量价」也必须压过 pe 子串
    group = _category_group(
        "OPEN55", "alpha360", "Alpha360 量价", "alpha360", "量价基础", ""
    )
    assert group == "量价/挖掘"


@pytest.mark.unit
def test_quality_factor_is_fundamental():
    group = _category_group(
        "f13_1128", "factor1300", "factor1300", "factor1300", "财务质量", "quality"
    )
    assert group == "基本面"


@pytest.mark.unit
def test_style_factor_is_style_group():
    group = _category_group(
        "ind_dispersion_20",
        "l1_factors",
        "L1 因子",
        "l1_factors",
        "风格与行业",
        "style",
    )
    assert group == "风格/风险"


@pytest.mark.unit
def test_l2_library_wins_even_with_empty_category():
    group = _category_group(
        "micro_spread_vol_ratio", "l2_factors", "", "l2_factors", "", "other"
    )
    assert group == "L2/微观"


@pytest.mark.unit
def test_jkp_accounting_factor_is_fundamental():
    group = _category_group(
        "be_mev",
        "jkp_ashare_daily",
        "JKP A股因子（日频投影）",
        "jkp_ashare_daily",
        "",
        "other",
    )
    assert group == "基本面"


@pytest.mark.unit
def test_unknown_factor_falls_back_to_other():
    group = _category_group(
        "mystery_001", "unknown_lib", "未知", "unknown_lib", "", "other"
    )
    assert group == "其他"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("feat_vcmk_pead", "基本面"),  # PEAD 显式关键词，不能靠 pe 子串
        ("feat_plv_peak_n", "量价/挖掘"),  # 价量峰谷特征
    ],
)
def test_other_category_fallbacks(code: str, expected: str):
    group = _category_group(
        code, "factor_defs", "factor_defs", "factor_defs", "", "other"
    )
    assert group == expected


@pytest.mark.unit
def test_size_excess_removes_bucket_mean():
    import numpy as np

    from backend.scripts.stock_classifier.select_daily_features import _size_excess

    y = np.array([1.0, 2.0, 3.0, 10.0, 20.0, 30.0])
    mv = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    out = _size_excess(y, mv, n_buckets=3)
    # 三桶 [1,2] [3,4] [5,6] 的均值 1.5/6.5/25 → 桶内超额
    assert out.tolist() == pytest.approx([-0.5, 0.5, -3.5, 3.5, -5.0, 5.0])
