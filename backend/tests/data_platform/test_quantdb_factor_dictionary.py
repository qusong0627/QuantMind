from backend.services.engine.data_platform.quantdb_factor_dictionary import definition_for


def test_v5_database_definition_wins_over_heuristics():
    """库内 V5 字典优先：中文名/分组/公式与 SDK/官网同源。"""
    definition = definition_for("mom_ret_20d")

    assert definition["category_id"] == "L1 C 动量"
    assert definition["category_name"] == "L1 C 动量"
    assert definition["display_name"] == "20 日收益率"
    assert "close / close[-20]" in str(definition["formula"])
    assert definition["confidence"] == "database"


def test_v5_microstructure_uses_database_names():
    definition = definition_for("micro_aesp")

    assert definition["display_name"] == "绝对有效价差（元）"
    assert "price" in str(definition["formula"])
    assert definition["confidence"] == "database"


def test_v5_f500_carries_description_and_direction():
    definition = definition_for("Alpha101_020")

    assert definition["display_name"] == "Alpha101_20"
    assert "跳空" in str(definition["explanation"])
    assert "反向" in str(definition["direction"])
    assert definition["confidence"] == "database"


def test_unknown_factor_stays_reviewable():
    definition = definition_for("vendor_extension_signal")

    assert definition["category_id"] == "other"
    assert definition["confidence"] == "needs_review"


def test_non_v5_market_columns_keep_heuristics():
    """快照未覆盖的列（南向/CCASS/自定义）仍走启发式兜底。"""
    definition = definition_for("sb_pct")

    assert definition["confidence"] in ("documented", "needs_review")
    assert str(definition["display_name"])
