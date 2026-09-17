from backend.services.engine.data_platform.quantdb_factor_dictionary import definition_for


def test_documented_factor_definition_uses_documented_category():
    definition = definition_for("mom_ret_20d")

    assert definition["category_id"] == "momentum"
    assert definition["category_name"] == "动量"
    assert "收益率" in str(definition["display_name"])
    assert "官方帮助文档" in str(definition["explanation"])
    assert "300_factors_lightgbm_design_v2.md" not in str(definition["explanation"])
    assert definition["confidence"] == "documented"


def test_unknown_factor_stays_reviewable():
    definition = definition_for("vendor_extension_signal")

    assert definition["category_id"] == "other"
    assert definition["confidence"] == "needs_review"


def test_microstructure_names_are_compact_and_readable():
    definition = definition_for("micro_aesp")

    assert definition["display_name"] == "AESP 有效价差"
    assert "价差与微观结构因子" not in str(definition["display_name"])


def test_alpha_library_names_keep_factor_number_and_no_control_chars():
    """Alpha 库三个前缀的显示名必须唯一且带编号。

    回归：模板曾写成非 raw 字符串的 "\\1"（控制符 \\x01）且没有 {} 占位符，
    导致 101 个 a101 与 170 个 gtja 因子的显示名全部退化成同一个字符串。
    """
    a101 = definition_for("a101_028")["display_name"]
    gtja = definition_for("gtja_125")["display_name"]
    a158 = definition_for("a158_MA20")["display_name"]

    assert a101 == "Alpha101 #028（Kakushadze 101 Formulaic Alphas）"
    assert gtja == "GTJA191 #125（国泰君安短周期价量因子）"
    assert "20" in str(a158)
    for name in (a101, gtja, a158):
        assert "\x01" not in str(name)
        assert "#" not in str(name) or "#0" in str(name) or "#1" in str(name)

    # 不同因子的显示名必须可区分
    assert definition_for("a101_001")["display_name"] != a101
    assert definition_for("gtja_001")["display_name"] != gtja


def test_alpha158_window_family_uses_chinese_names():
    """a158_{TOKEN}{N} 窗口族必须渲染为中文语义名（不再是「VMA 因子（30 日窗口）」）。

    评估中心/特征目录直接消费这些显示名，退化模板会被用户当成"没有中文名"。
    """
    names = {
        code: definition_for(code)["display_name"]
        for code in (
            "a158_MIN5",
            "a158_MIN10",
            "a158_QTLD30",
            "a158_VMA30",
            "a158_VMA60",
            "a158_VSUMN60",
            "a158_VSUMD60",
        )
    }
    for code, name in names.items():
        assert "因子（" not in str(name), f"{code} 仍是通用模板: {name}"
        assert any("一" <= ch <= "鿿" for ch in str(name)), f"{code} 无中文: {name}"
    # 同族不同窗口必须可区分
    assert names["a158_VMA30"] != names["a158_VMA60"]
    assert names["a158_MIN5"] != names["a158_MIN10"]
    assert len(set(names.values())) == len(names)
    # 未知 token 仍回退通用渲染（不吞代码）
    assert "ZZZ9" in str(definition_for("a158_ZZZ9")["display_name"])
