"""institutional_classifier 单元测试 — CCASS 参与者分类规则。

分类是名称关键词启发式，这些测试即消歧回归护栏：
桶检查顺序（apac → us_eu → cn → hk 白名单 → 默认）每变必查。
"""

import pandas as pd
import pytest

from backend.services.api.market_analysis_hk.institutional_classifier import (
    CATEGORY_APAC,
    CATEGORY_CN_BROKER,
    CATEGORY_HK,
    CATEGORY_HKSCC,
    CATEGORY_OTHER,
    CATEGORY_SOUTHBOUND,
    CATEGORY_US_EU,
    KIND_BROKER,
    KIND_CUSTODIAN,
    KIND_OTHER,
    KIND_SETTLEMENT,
    category_label,
    classify,
    load_overrides,
    recognize,
)


def _c(pid: str, name: str) -> tuple[str, str]:
    return classify(pid, name)


# ---- 特例识别 ----


def test_southbound_a_seats():
    assert _c("A00003", "中國証券登記結算有限責任公司") == (
        CATEGORY_SOUTHBOUND,
        KIND_SETTLEMENT,
    )


def test_csdc_hk_is_cn_not_southbound():
    # A00005 中国结算(香港)：非港股通通道（H 股大宗存量），归内资桶、性质=结算
    assert _c("A00005", "中國證券登記結算(香港)有限公司") == (
        CATEGORY_CN_BROKER,
        KIND_SETTLEMENT,
    )


def test_southbound_name_only():
    # ID 缺失/变化时仍按登记结算名称识别（南向池防口漂移；
    # 注意非 A00005 名下仍按名称识别为南向，仅 香港 实体按 ID 特判）
    assert _c(None, "中國證券登記結算有限責任公司") == (
        CATEGORY_SOUTHBOUND,
        KIND_SETTLEMENT,
    )


def test_hkscc_defensive():
    assert _c("B00001", "香港中央結算(代理人)有限公司") == (CATEGORY_HKSCC, KIND_OTHER)
    assert _c("B99999", "HKSCC NOMINEES LIMITED") == (CATEGORY_HKSCC, KIND_OTHER)


def test_personal_retail():
    assert _c("P00013", "周大福珠寶金行有限公司") == (CATEGORY_OTHER, KIND_OTHER)
    assert _c(None, "LAU SUK HAN *") == (CATEGORY_OTHER, KIND_OTHER)


# ---- 分类命中（繁/简/英三形态） ----


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("中國國際金融香港證券有限公司", CATEGORY_CN_BROKER),
        ("國泰君安證券(香港)有限公司", CATEGORY_CN_BROKER),
        ("中信建投(國際)証券有限公司", CATEGORY_CN_BROKER),
        ("中國銀河國際證券(香港)有限公司", CATEGORY_CN_BROKER),
        ("富途證券國際(香港)有限公司", CATEGORY_CN_BROKER),
        ("華泰金融控股(香港)有限公司", CATEGORY_CN_BROKER),
        ("中國銀行(香港)有限公司", CATEGORY_CN_BROKER),
        ("中國北方證券集團有限公司", CATEGORY_CN_BROKER),  # 通用「中國」兜底
    ],
)
def test_cn_broker_hits(name, category):
    assert _c("B01955", name)[0] == category


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("輝立証券(香港)有限公司", CATEGORY_APAC),  # Phillip — 新加坡
        ("星展銀行(香港)有限公司", CATEGORY_APAC),
        ("DBS BANK LTD", CATEGORY_APAC),
        ("中國信託綜合證券(香港)有限公司", CATEGORY_APAC),
        ("國泰證券(香港)有限公司", CATEGORY_APAC),
        ("元大證券(香港)有限公司", CATEGORY_APAC),
        ("瑞穗證券亞洲有限公司", CATEGORY_APAC),
        ("MUFG BANK, LTD.", CATEGORY_APAC),
        ("盤谷銀行", CATEGORY_APAC),
        ("大眾銀行(香港)有限公司", CATEGORY_APAC),
    ],
)
def test_apac_hits(name, category):
    assert _c("B01345", name)[0] == category


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("UBS SECURITIES HONG KONG LTD", CATEGORY_US_EU),
        ("MORGAN STANLEY HONG KONG SECURITIES LTD", CATEGORY_US_EU),
        ("J.P. MORGAN BROKING (HONG KONG) LTD", CATEGORY_US_EU),
        ("高盛(亞洲)證券有限公司", CATEGORY_US_EU),
        ("花旗銀行", CATEGORY_US_EU),
        ("渣打銀行(香港)有限公司", CATEGORY_US_EU),
        ("德意志銀行", CATEGORY_US_EU),
        ("盈透證券香港有限公司", CATEGORY_US_EU),
        ("SOFI SECURITIES (HONG KONG) LTD", CATEGORY_US_EU),
        ("CREDIT AGRICOLE CORPORATE AND INVESTMENT BANK", CATEGORY_US_EU),
    ],
)
def test_us_eu_hits(name, category):
    assert _c("B01161", name)[0] == category


# ---- 消歧回归（桶检查顺序护栏） ----


def test_substring_disambiguation():
    # 國泰君安 = 中资券商；國泰證券 = 台湾（亚太）——「國泰」子串必须在 cn 检查前消歧
    assert _c("B01565", "國泰君安證券(香港)有限公司")[0] == CATEGORY_CN_BROKER
    assert _c("B01848", "國泰證券(香港)有限公司")[0] == CATEGORY_APAC
    # 中國信託 = 台湾；通用「中國」不吞它
    assert _c("B01833", "中國信託綜合證券(香港)有限公司")[0] == CATEGORY_APAC
    assert _c("C00033", "中國銀行(香港)有限公司")[0] == CATEGORY_CN_BROKER
    # 兴证国际 vs 万兴/荣兴/粤兴/创兴（興證 子串误撞）
    assert _c("B01938", "興證國際證券有限公司")[0] == CATEGORY_CN_BROKER
    assert _c("B01328", "萬興證券有限公司")[0] == CATEGORY_HK
    assert _c("B01350", "榮興證券有限公司")[0] == CATEGORY_HK
    assert _c("B01183", "創興證券有限公司")[0] == CATEGORY_HK
    # 東方證券(香港) vs 智易東方
    assert _c("B01900", "東方證券(香港)有限公司")[0] == CATEGORY_CN_BROKER
    assert _c("B02019", "智易東方證券有限公司")[0] == CATEGORY_HK
    # 山證國際 vs 藍山
    assert _c("B01980", "山證國際證券有限公司")[0] == CATEGORY_CN_BROKER
    assert _c("B02054", "藍山證券有限公司")[0] == CATEGORY_HK
    # 上海商業銀行 = 港资；上海浦東發展銀行 = 中资
    assert _c("C00037", "上海商業銀行有限公司")[0] == CATEGORY_HK
    assert _c("C00104", "上海浦東發展銀行股份有限公司")[0] == CATEGORY_CN_BROKER
    # 盈透 = 欧美；盈寶 = 港资（默认）
    assert _c("B01590", "盈透證券香港有限公司")[0] == CATEGORY_US_EU
    assert _c("B02214", "盈寶證券國際(香港)有限公司")[0] == CATEGORY_HK
    # 恒生 = 港资（恒昇 不误配）
    assert _c("C00018", "恒生銀行有限公司")[0] == CATEGORY_HK
    assert _c("B01782", "恒昇證券有限公司")[0] == CATEGORY_HK


# ---- 默认桶 ----


def test_default_bucket_hk_for_cb_prefix():
    assert _c("B01231", "騰達證券有限公司")[0] == CATEGORY_HK  # B 前缀小券商
    assert _c("C00003", "東亞銀行有限公司")[0] == CATEGORY_HK  # C 前缀银行
    assert _c("B02223", "都會金融香港有限公司")[0] == CATEGORY_HK


def test_default_other_for_unknown_prefix():
    assert _c("X00001", "SOME NEW PARTICIPANT")[0] == CATEGORY_OTHER


# ---- kind 判定 ----


def test_kind_by_participant_type():
    assert _c("C00019", "香港上海匯豐銀行有限公司")[1] == KIND_CUSTODIAN  # 银行托管
    assert _c("C00002", "交通銀行信託有限公司")[1] == KIND_CUSTODIAN
    assert _c("B01130", "中銀國際證券有限公司")[1] == KIND_BROKER
    # 名称含「信託」但实为券商（中国信托综合证券）
    assert _c("B01833", "中國信託綜合證券(香港)有限公司")[1] == KIND_BROKER
    assert _c("A00003", "中國証券登記結算有限責任公司")[1] == KIND_SETTLEMENT
    assert _c("P00013", "周大福珠寶金行有限公司")[1] == KIND_OTHER


# ---- 人工覆盖表 ----


def test_overrides_force_category():
    overrides = pd.DataFrame(
        [{"participant_id": "B01231", "participant_name": "騰達證券有限公司",
          "category": CATEGORY_OTHER, "kind": KIND_OTHER, "reason": "test", "updated_at": "now"}]
    )
    assert classify("B01231", "騰達證券有限公司", overrides)[0] == CATEGORY_OTHER
    # 未覆盖的 ID 不受影响
    assert classify("B01230", "高裕證券有限公司", overrides)[0] == CATEGORY_HK


def test_overrides_empty_df_not_used():
    empty = pd.DataFrame(columns=["participant_id", "participant_name", "category", "kind"])
    assert classify("B01231", "騰達證券有限公司", empty)[0] == CATEGORY_HK


def test_load_overrides_missing_file(tmp_path):
    df = load_overrides(tmp_path)
    assert list(df.columns) == ["participant_id", "participant_name", "category",
                                "kind", "reason", "updated_at"]
    assert len(df) == 0


# ---- 工具 ----


def test_category_label_mapping():
    assert category_label(CATEGORY_CN_BROKER) == "内资·中資券商"
    assert category_label("未知") == "未知"


def test_recognize_returns_none_for_ordinary_broker():
    assert recognize("B01231", "騰達證券有限公司") is None
