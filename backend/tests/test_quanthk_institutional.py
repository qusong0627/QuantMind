"""机构持仓分析集成测试（直连本地 quanthk parquet）。

核心见证：
- overview 分类占比自洽（口径 D1：ccass_top50 单源）
- 交叉验证闸门：内资·港股通（A 席）与 hsgt_south 在**同一股票集合**上
  持仓量一致（±2%）—— ccass_top50 只披露前 50 大参与方，全量分母会凭空
  少约 3%（2026-11-12 有 86 只股票的中国结算席位在 50 名外）
- 窗口增减持/个股详情/简繁搜索

与 test_quanthk_market_analysis.py 同款直连模式，无网络依赖。
"""

import duckdb
import pandas as pd
import pytest

from backend.services.api.market_analysis_hk import quanthk_feed as feed
from backend.services.api.market_analysis_hk.institutional_classifier import (
    CATEGORY_ORDER,
    CATEGORY_SOUTHBOUND,
    classify,
    load_overrides,
)
from backend.services.api.market_analysis_shared import market_days
from backend.shared.hk_smart_money import SOUTHBOUND_SEAT_IDS


def _latest_ccass_dt() -> str:
    d = feed._latest_ccass_top_date()
    assert d, "ccass_top50 无数据"
    return d


# ---- 市场概览 ----


def test_institutional_overview_shape():
    ov = feed.get_institutional_overview()
    assert ov["trade_date"].startswith("20")
    assert ov["stock_count"] > 2000
    assert ov["disclosed_value_yi"] > 0
    cats = {c["category"] for c in ov["categories"]}
    assert cats == set(CATEGORY_ORDER)
    # 分类占比求和 ≈ 100（hkscc 防御类不参与）
    total_pct = sum(c["pct_of_disclosed"] for c in ov["categories"])
    assert abs(total_pct - 100.0) < 1.0, f"分类占比之和 {total_pct} 偏离 100"
    assert all(c["value_yi"] >= 0 for c in ov["categories"])
    assert ov["change_stats"]["window"] == 5
    assert ov["change_stats"]["increased"] + ov["change_stats"]["decreased"] > 0
    assert ov["hkscc_nominees"]["noted"] is False  # 当前数据源不含 HKSCC 代理人席


def test_institutional_overview_southbound_cross_check():
    """交叉验证闸门：A 席 vs hsgt_south，在**两边都可见的股票交集**上 ±2%。

    ccass_top50 只披露前 50 大参与方；部分股票的中国结算席位排在第 50 名
    之外（2026-11-12 有 86 只），拿全量南向做分母会凭空少约 3%。
    """
    ov = feed.get_institutional_overview()
    dt = ov["trade_date"].replace("-", "")
    seats = ",".join(f"'{s}'" for s in SOUTHBOUND_SEAT_IDS)
    cc_path = feed.DATA_DIR / feed.CCASS_TOP50_REL / f"dt={dt}" / "data.parquet"
    south_path = feed.DATA_DIR / feed.SOUTH_REL / f"dt={dt}" / "data.parquet"
    a_qty, s_qty, n, south_n = (
        duckdb.connect()
        .execute(
            f"""
            WITH a AS (
              SELECT stock_code AS symbol, sum(holding_quantity) AS q
              FROM read_parquet('{cc_path}')
              WHERE participant_id IN ({seats})
              GROUP BY stock_code
            ), s AS (
              SELECT symbol, sum(holding_quantity) AS q
              FROM read_parquet('{south_path}')
              GROUP BY symbol
            )
            SELECT sum(a.q), sum(s.q), count(*), (SELECT count(*) FROM s)
            FROM a JOIN s USING(symbol)
            """
        )
        .fetchone()
    )
    assert a_qty and s_qty and n
    # 覆盖率门：交集至少 90% 南向股票（防代码映射断裂/整库缺失）
    assert n >= 0.9 * south_n, f"CCASS/南向代码交集仅 {n}/{south_n}"
    ratio = a_qty / s_qty
    assert abs(ratio - 1.0) < 0.02, (
        f"A席/南向比值 {ratio:.4f} 偏离 1.0（A={a_qty} S={s_qty} n={n}/{south_n}）"
    )


# ---- 增减持榜 ----


def test_institutional_movers_increase_sorted():
    m = feed.get_institutional_movers(category="cn_broker", window=5, direction="increase", limit=10)
    assert m["trade_date"].startswith("20")
    assert m["base_date"] < m["trade_date"]
    assert m["window"] == 5
    items = m["items"]
    assert 0 < len(items) <= 10
    keys = {"symbol", "name", "price", "hold_yi", "hold_pct", "delta_qty",
            "delta_yi", "delta_pct_abs", "first_seen"}
    assert all(keys <= set(it) for it in items)
    assert all(it["delta_qty"] > 0 for it in items)
    deltas = [abs(it["delta_yi"]) for it in items]
    assert deltas == sorted(deltas, reverse=True)


def test_institutional_movers_decrease_sorted():
    m = feed.get_institutional_movers(category="hk", window=20, direction="decrease", limit=10)
    assert all(it["delta_qty"] < 0 for it in m["items"])
    deltas = [it["delta_yi"] for it in m["items"]]
    assert deltas == sorted(deltas)  # 负值升序 = 减持最多在前


def test_institutional_movers_all_categories_excludes_hkscc():
    m = feed.get_institutional_movers(category="all", window=5, direction="increase", limit=10)
    assert len(m["items"]) > 0
    # 全量榜按任意单一分类应互不重复加总（hkscc 除外），抽查不出现 0 持股噪音
    assert all(it["hold_yi"] > 0 for it in m["items"])


# ---- 个股详情 ----


def test_institutional_stock_symbol_normalization():
    a = feed.get_institutional_stock("0700")
    b = feed.get_institutional_stock("0700.HK")
    assert a["symbol"] == b["symbol"] == "0700.HK"
    assert a["name"] == b["name"] == "腾讯控股"
    assert a["trend"]["dates"] == b["trend"]["dates"]
    assert abs(a["south_pct"] - b["south_pct"]) < 1e-9


def test_institutional_stock_tencent_detail():
    s = feed.get_institutional_stock("0700.HK")
    assert s["trade_date"].startswith("20")
    assert s["price"] > 0
    assert 0 < s["disclosed_pct"] <= 100
    assert s["south_pct"] is not None and s["south_pct"] > 0
    cats = {c["category"] for c in s["categories"]}
    # 腾讯应有沪深 A 席（南向）+ 汇丰托管 + 中资券商
    assert CATEGORY_SOUTHBOUND in cats
    assert "hk" in cats
    # 解禁区：2700 家股票中应有披露席位
    assert len(s["participants"]) > 10
    # 每分类提供 5/20/60 窗口（数据足够时 3 个）
    for c in s["categories"]:
        assert {d["window"] for d in c["deltas"]} == {5, 20, 60}
    # 趋势序列长度 = min(61, 分区数)
    assert len(s["trend"]["dates"]) == min(61, len(market_days.list_partition_dates(
        feed.CCASS_TOP50_REL, feed.DATA_DIR)))
    for ser in s["trend"]["series"]:
        assert len(ser["values"]) == len(s["trend"]["dates"])
    assert any(len(ser["values"]) > 0 and max(ser["values"]) > 0 for ser in s["trend"]["series"])


def test_institutional_stock_participant_classification():
    """0700 应有 匯豐=港资托管、A席=内资·港股通、UBS=欧美（分类抽核）。"""
    s = feed.get_institutional_stock("0700.HK")
    cats = {p["participant_name"]: (p["category"], p["kind"]) for p in s["participants"]}
    assert cats.get("香港上海匯豐銀行有限公司") == ("hk", "custodian")
    assert any(k == (CATEGORY_SOUTHBOUND, "settlement") for k in cats.values())
    for nm, (cat, _kind) in cats.items():
        if "UBS" in nm.upper():
            assert cat == "us_eu"


# ---- 搜索 ----


def test_institutional_suggest_traditional_and_simplified():
    assert "0700.HK" in {x["symbol"] for x in feed.get_institutional_stock_suggest("騰訊")}
    assert "0700.HK" in {x["symbol"] for x in feed.get_institutional_stock_suggest("腾讯")}
    assert "0005.HK" in {x["symbol"] for x in feed.get_institutional_stock_suggest("汇丰")}


def test_institutional_suggest_symbol_prefix():
    res = feed.get_institutional_stock_suggest("0700")
    assert res and res[0]["symbol"] == "0700.HK"


# ---- 参与者审计 ----


def test_institutional_participants_filter():
    us = feed.get_institutional_participants(category="us_eu", limit=100)
    assert us["total"] > 10
    assert all(p["category"] == "us_eu" for p in us["items"])
    assert all(p["hold_yi"] >= 0 for p in us["items"])
    # 花旗（C00010 托管）应排前
    assert any(p["participant_id"] == "C00010" for p in us["items"])
    cn = feed.get_institutional_participants(category="cn_broker", limit=100)
    assert any(p["participant_id"] == "B01955" for p in cn["items"])  # 富途


def test_institutional_participants_keyword():
    res = feed.get_institutional_participants(q="汇丰", limit=20)
    assert res["total"] >= 2
    assert any("香港上海匯豐銀行" in p["participant_name"] for p in res["items"])


# ---- 分类器全库覆盖 ----
# 规则表应能对真实名册全覆盖且分布合理（默认桶只应承担「港资小券商」与「其他」）


def test_classifier_full_registry_coverage():
    dates = market_days.list_partition_dates(feed.CCASS_TOP50_REL, feed.DATA_DIR)
    # 用最新**完整**分区：夜间抓取中的半成品只有几百只，会随机缺席位
    dt = feed._latest_ccass_top_date() or dates[-1]
    df = duckdb.connect().execute(
        "SELECT DISTINCT participant_id, participant_name FROM read_parquet("
        f"'{feed.DATA_DIR / feed.CCASS_TOP50_REL}/dt=*/data.parquet', hive_partitioning=1) "
        f"WHERE dt = {dt}"
    ).fetchdf()
    assert len(df) > 300
    overrides = load_overrides(feed.DATA_DIR)
    counts: dict[str, int] = {}
    for r in df.itertuples(index=False):
        pid = None if pd.isna(r.participant_id) else str(r.participant_id)
        nm = "" if pd.isna(r.participant_name) else str(r.participant_name)
        cat, _ = classify(pid, nm, overrides)
        counts[cat] = counts.get(cat, 0) + 1
    assert counts.get(CATEGORY_SOUTHBOUND, 0) == 4  # A00002/03/04/06（A00005 归内资桶）
    for solid in ("cn_broker", "hk", "us_eu", "apac"):
        assert counts.get(solid, 0) > 10, f"分类 {solid} 覆盖过少: {counts}"
    # 未知前缀默认桶只应极小（当前全库仅 P00013 一类零售）
    assert counts.get("other", 0) <= 3
