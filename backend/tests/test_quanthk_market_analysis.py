"""Unit and integration tests for QuantHK Market Analysis feed.

直连本地 quanthk parquet（与 A 股 test_quantdb_market_analysis.py 同款模式）。
"""

import pytest

from backend.services.api.market_analysis_hk import quanthk_feed
from backend.services.api.market_analysis_shared import display, market_days


# ---- 共享层：交易日历 ----

def test_partition_dates_has_recent_dates():
    """港股日线分区应从 1980 年延续到近期（>=2026-01-01）。"""
    dates = market_days.list_partition_dates(quanthk_feed.KLINE_REL, quanthk_feed.DATA_DIR)
    assert len(dates) > 1000, "港股日线分区数量异常偏少"
    assert dates[0].startswith("1980"), f"最早分区应为 1980 年，实际 {dates[0]}"
    assert dates[-1] >= "20260101", f"最新分区 {dates[-1]} 应晚于 2026-01-01"


def test_south_partition_dates_recent():
    """南向主力布局（dt= 分区式）应当日频更新到近期。"""
    dates = market_days.list_partition_dates(quanthk_feed.SOUTH_REL, quanthk_feed.DATA_DIR)
    assert dates, "南向 dt= 分区不存在"
    assert dates[-1] >= "20260101", f"南向数据停更：最新分区 {dates[-1]}"


def test_ccass_factors_dates_recent():
    """CCASS 因子应当日频更新到近期。"""
    dates = market_days.list_partition_dates(quanthk_feed.CCASS_FACTORS_REL, quanthk_feed.DATA_DIR)
    assert dates, "CCASS 因子分区不存在"
    assert dates[-1] >= "20260101", f"CCASS 因子停更：最新分区 {dates[-1]}"


def test_to_iso_and_ymd_roundtrip():
    assert market_days.to_iso("20260827") == "2026-08-27"
    assert market_days.to_ymd("2026-08-27") == "20260827"
    assert market_days.partition_dates_to_sql(["20260827", "2026-08-26"]) == "20260827,20260826"


# ---- 共享层：展示口径 ----

def test_display_units():
    assert display.fmt_yi(5.2e10) == 520.0  # 520 亿
    assert display.amount_in_display(1e9) == 100000.0  # 10 亿 -> 万 口径
    assert display.pct(None) == 0.0
    assert display.pct(float("nan"), ndigits=2) == 0.0
    assert display.safe_float("abc") == 0.0


# ---- 港股大盘 ----

def test_hk_indices_overview():
    """四大恒指系列指数快照。"""
    indices = quanthk_feed.get_indices_overview()
    assert isinstance(indices, list)
    assert len(indices) == 4

    symbols = {idx["symbol"] for idx in indices}
    assert {"HSI.HK", "HSCEI.HK", "HSTECH.HK", "HSCCI.HK"} <= symbols
    for idx in indices:
        assert idx["price"] > 0
        assert idx["name"]
        assert idx["trade_date"].startswith("20")
        assert len(idx["trend"]) > 0


def test_hk_market_breadth():
    """市场温度计：涨跌家数/成交额/大涨大跌分布（港股 ±5% 口径）。"""
    data = quanthk_feed.get_market_breadth()
    assert isinstance(data, dict)
    for key in ("total_stocks", "advance_count", "decline_count", "flat_count",
                "big_up_count", "big_down_count", "total_turnover_yi",
                "profit_effect", "sentiment_score"):
        assert key in data

    total = data["advance_count"] + data["decline_count"] + data["flat_count"]
    # 港股全市场 ~2800 只，剔除停牌/无前收后应仍有 1500+ 只
    assert total >= 1500, f"港股可统计股票数 {total} 异常偏少"
    assert data["total_turnover_yi"] > 500, "港股日成交额应大于 500 亿港元"
    assert 0 <= data["profit_effect"] <= 100
    assert data["big_up_count"] + data["big_down_count"] <= total


def test_hk_sector_heatmap():
    """恒生行业热力图（全市场行业覆盖，非 141 只子集）。"""
    sectors = quanthk_feed.get_sector_heatmap(limit=40)
    assert isinstance(sectors, list)
    assert len(sectors) >= 15, f"恒生行业数 {len(sectors)} 异常偏少"
    for s in sectors:
        assert s["name"]
        assert "value" in s and s["value"] > 0
        assert s["leader"]
        assert isinstance(s["pct_change"], float)
        assert -100 <= s["pct_change"] <= 100  # 公司行动已裁剪


# ---- 南方资金 ----

def test_south_overview():
    """南向总览：持仓市值/覆盖股票/增减持家数。"""
    data = quanthk_feed.get_south_overview()
    assert isinstance(data, dict)
    assert data["covered_stocks"] >= 500, "南向覆盖股票数异常偏少"
    assert data["total_hold_value_yi"] > 1000, "南向总持仓市值应显著 > 0"
    assert data["trade_date"].startswith("20")
    assert 0 <= data.get("up_days_change", 0) <= data["covered_stocks"]


def test_south_stock_flow():
    """南向增持/减持榜：5 日窗口。"""
    data = quanthk_feed.get_south_stock_flow(period=5, limit=20)
    assert data["period"] == 5
    inc, dec = data["increase"], data["decrease"]
    assert len(inc) > 0 and len(dec) > 0
    for rows in (inc, dec):
        for item in rows:
            assert item["symbol"].endswith(".HK")
            assert "name" in item
            # 假清仓防御：holding_pct 不应为 0 且 quantity 大
            assert item["holding_pct"] > 0, f"{item['symbol']} 疑似披露缺失未剔除"
    # 增持榜按变化量降序
    vals = [i["pct_change_abs"] for i in inc]
    assert vals == sorted(vals, reverse=True)


def test_south_sector_flow():
    """南向板块配置。"""
    sectors = quanthk_feed.get_south_sector_flow(limit=10)
    assert isinstance(sectors, list)
    assert len(sectors) >= 5
    sums = sum(s["hold_value_yi"] for s in sectors)
    assert sums > 0


# ---- CCASS 席位 ----

def test_ccass_rankings():
    """全市场 CCASS 集中度榜。"""
    data = quanthk_feed.get_ccass_rankings(limit=30)
    assert data["trade_date"].startswith("20")
    items = data["items"]
    assert len(items) > 10
    for it in items[:5]:
        assert it["symbol"].endswith(".HK")
        assert 0 <= it["top10_pct"] <= 100
        assert 0 <= it["hhi"] <= 1.0


def test_ccass_holding_tencent():
    """腾讯（0700.HK）席位穿透：汇丰/中登/花旗应在前列。"""
    data = quanthk_feed.get_ccass_holding("0700.HK", limit=30)
    assert data["symbol"] == "0700.HK"
    assert data["name"] == "腾讯控股"
    items = data["items"]
    assert len(items) >= 10
    names = "".join(i["participant_name"] for i in items)
    assert "匯豐" in names or "汇丰" in names, "汇丰应在前十大席位"
    assert "花旗" in names
    total_pct = sum(i["holding_pct"] for i in items)
    assert 0 < total_pct <= 100


def test_ccass_holding_symbol_normalization():
    """无 .HK 后缀的输入应被归一化。"""
    data = quanthk_feed.get_ccass_holding("0700", limit=5)
    assert data["symbol"] == "0700.HK"


def test_ccass_movers():
    """席位新进/退出异动。"""
    data = quanthk_feed.get_ccass_movers(limit=20)
    assert data["trade_date"].startswith("20")
    for rows in (data["new_entrants"], data["exits"]):
        for it in rows[:3]:
            assert it["symbol"].endswith(".HK")


# ---- AH 对应 ----

def test_ah_pairs():
    """AH 对应股联动表。"""
    data = quanthk_feed.get_ah_pairs(limit=50)
    assert data["trade_date"].startswith("20")
    items = data["items"]
    assert len(items) >= 20, "AH 对应股数量异常偏少"
    for it in items[:5]:
        assert it["h_symbol"].endswith(".HK")
        assert it["a_symbol"]
        assert "h_pct_change" in it


# ---- 估值主题 ----

def test_valuation_dividend():
    """高股息榜默认过滤小市值噪声。"""
    data = quanthk_feed.get_valuation_rankings(kind="dividend", limit=20)
    assert data["titlename"] == "高股息率"
    items = data["items"]
    assert len(items) > 0
    for it in items[:5]:
        assert it["value"] > 0
        if "total_market_cap_yi" in it:
            assert it["total_market_cap_yi"] >= 10, "== 不应出现 <10 亿港元的小市值股 =="


def test_valuation_pe_pb():
    """低 PE / 低 PB 榜。"""
    for kind in ("pe", "pb"):
        data = quanthk_feed.get_valuation_rankings(kind=kind, limit=20)
        assert data["items"], f"{kind} 榜为空"
        vals = [i["value"] for i in data["items"]]
        assert vals == sorted(vals), f"{kind} 榜应按指标升序"


# ---- 行业轮动 ----

def test_sector_rotation():
    """恒生行业 1/5/20 日强弱。"""
    data = quanthk_feed.get_sector_rotation(limit=24)
    assert data["trade_date"].startswith("20")
    items = data["items"]
    assert len(items) >= 10
    for it in items[:5]:
        assert "ret_1d" in it and "ret_5d" in it and "ret_20d" in it
    # 按 5 日涨幅降序
    r5 = [i["ret_5d"] or -999 for i in items]
    assert r5 == sorted(r5, reverse=True)


# ---- 状态 ----

def test_feed_status():
    st = quanthk_feed.feed_status()
    assert st["available"] is True
    assert st["latest_kline_date"].startswith("20")
    assert st["latest_ccass_date"].startswith("20")
    assert st["industry_count"] > 500, "全市场行业覆盖应远大于 141 只子集"


# ---- 缓存 ----

def test_cache_clear():
    quanthk_feed.get_market_breadth()
    quanthk_feed.clear_cache_hk()
    data = quanthk_feed.get_market_breadth()  # 清缓存后应能重新计算
    assert data["total_stocks"] > 0


# ---- 容错：健康代码路径（模拟损坏文件场景） ----

def test_south_legacy_corrupt_files_skipped(monkeypatch):
    """旧每股文件布局含损坏 parquet 时，主分区布局读取不受影响。"""
    # 主布局存在 → 走 dt= 分区路径
    from backend.services.api.market_analysis_shared import market_days
    has_parts = bool(market_days.list_partition_dates(quanthk_feed.SOUTH_REL, quanthk_feed.DATA_DIR))
    assert has_parts
    df = quanthk_feed._read_south_safe()
    assert not df.empty
    assert len(df["symbol"].unique()) > 500
