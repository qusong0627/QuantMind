"""QuantUS（美股）市场分析的数据层单元 + 集成测试。

直连本地 quantus parquet（与 `test_quanthk_market_analysis.py`、
`test_quantdb_market_analysis.py` 同款零 mock 模式）。

断言取向：**量级与健康度**（防停更 / 防排序回归 / 防口径失真），
而不是精确数值 —— 数据每天在变，精确断言会天天红。
"""

import pytest

from backend.services.api.market_analysis_shared import market_days
from backend.services.api.market_analysis_us.feed import (
    analysts,
    breadth,
    earnings,
    holdings,
    indices,
    sectors,
    valuation,
)
from backend.services.api.market_analysis_us.feed import base as us_base


# ---- 共享层：交易日历（数据健康度） ----


def test_kline_partition_dates_recent():
    """美股日线分区应从 2001 年延续到近期。"""
    dates = market_days.list_partition_dates(us_base.KLINE_REL, us_base.DATA_DIR)
    assert len(dates) > 5000, "美股日线分区数量异常偏少"
    assert dates[0].startswith("2001"), f"最早分区应为 2001 年，实际 {dates[0]}"
    assert dates[-1] >= "20260101", f"最新分区 {dates[-1]} 应晚于 2026-01-01"


def test_index_partition_dates_present():
    """指数分区必须存在（历史上曾因全量分支漏同步而长期停滞）。"""
    dates = market_days.list_partition_dates(us_base.INDEX_REL, us_base.DATA_DIR)
    assert dates, "指数分区不存在"
    assert dates[-1] >= "20260101", f"指数数据停更：最新分区 {dates[-1]}"


def test_index_and_kline_use_separate_calendars():
    """指数与个股的分区集合不同，交易日序列不可互相替代。"""
    kline = us_base._trading_days(None, 5)
    index = us_base._index_trading_days(None, 5)
    assert kline and index
    # 各自都必须是降序且无重复
    for seq in (kline, index):
        assert seq == sorted(seq, reverse=True)
        assert len(set(seq)) == len(seq)


def test_market_days_helpers():
    assert market_days.to_iso("20260910") == "2026-09-10"
    assert market_days.to_ymd("2026-09-10") == "20260910"
    assert market_days.partition_dates_to_sql(["20260910", "2026-09-09"]) == "20260910,20260909"


# ---- 基座：标的池 / 行业 / 快照 ----


def test_universe_and_sector_map_shapes():
    uni = us_base._universes()
    smap = us_base._sector_map()
    assert len(uni) >= 400, f"标的池偏小：{len(uni)}"
    assert {"symbol", "cn_name", "en_name"}.issubset(uni.columns)
    assert len(smap) >= 400, f"行业映射覆盖偏小：{len(smap)}"
    assert {"symbol", "sector", "industry"}.issubset(smap.columns)
    # sector 空值必须被归一为「未分类」，否则热力图/轮动会出现 NaN 分组
    assert smap["sector"].notna().all()
    assert not (smap["sector"] == "").any()


def test_f10_snapshot_has_core_columns():
    f10 = us_base._f10_snapshot()
    assert len(f10) >= 400
    for col in ("market_cap", "pe_ratio", "pb_ratio", "dividend_yield", "52w_high", "52w_low"):
        assert col in f10.columns, f"f10 缺少 {col}"


def test_market_pct_snapshot_clips_extremes():
    """涨跌幅必须裁剪在 ±100% 内（拆股造成的价格跳变不得污染统计）。"""
    latest, snap = us_base._market_pct_snapshot()
    assert latest, "取不到最新交易日"
    assert not snap.empty
    assert snap["close"].fillna(0).gt(0).all(), "快照应剔除停牌（close<=0）标的"
    assert snap["pct_change"].between(-100.0, 100.0).all()


# ---- Tab1 大盘脉搏 ----


def test_indices_overview_shape_and_null_semantics():
    """指数快照：5 个指数齐备；amount 恒 0 故 turnover 为 None；SOX 无成交量。"""
    items = indices.get_indices_overview()
    assert len(items) == 5, f"应有 5 个指数，实际 {len(items)}"
    by_symbol = {i["symbol"]: i for i in items}
    for sym in ("SPX.US", "NDX.US", "IXIC.US", "DJI.US", "SOX.US"):
        assert sym in by_symbol, f"缺少指数 {sym}"
        it = by_symbol[sym]
        assert it["price"] > 0
        assert -100 <= it["pct_change"] <= 100
        assert it["trade_date"], "指数必须带自己的数据日期（与个股不同）"
        assert len(it["trend"]) <= 5
        # amount 恒为 0 → 必须输出 None 而不是 0（0 会被前端当成真实成交额）
        assert it["turnover_yi"] is None
    # SOX 的 volume 也是 0 → 必须为 None
    assert by_symbol["SOX.US"]["volume"] is None
    assert by_symbol["SPX.US"]["volume"] is None or by_symbol["SPX.US"]["volume"] > 0


def test_index_spread_pairs():
    spread = indices.get_index_spread()
    assert spread["pairs"], "指数相对强弱为空"
    for p in spread["pairs"]:
        assert p["left_symbol"] and p["right_symbol"]
        assert abs(p["spread"] - (p["left_return"] - p["right_return"])) < 0.01


def test_market_breadth_consistency():
    b = breadth.get_market_breadth()
    assert b["trade_date"], "温度计缺少交易日"
    assert b["total_stocks"] >= 400, f"截面标的数偏少：{b['total_stocks']}"
    # 家数自洽
    assert b["advance_count"] + b["decline_count"] + b["flat_count"] == b["total_stocks"]
    assert 0 <= b["profit_effect"] <= 100
    assert 0 <= b["sentiment_score"] <= 100
    # 异动家数不能超过涨跌家数
    assert b["big_up_count"] <= b["advance_count"]
    assert b["big_down_count"] <= b["decline_count"]
    assert -100 <= b["median_pct"] <= 100
    assert b["total_turnover_yi"] > 0, "全市场成交额应为正（美元原始值）"


def test_profit_leaders_sorted_and_named():
    res = breadth.get_profit_leaders(10)
    assert res["items"]
    scores = [i["score"] for i in res["items"]]
    assert scores == sorted(scores, reverse=True), "赚钱效应榜必须按评分降序"
    for it in res["items"]:
        assert it["name"], "每条榜单项都应有名称（中文名或代码兜底）"
        assert -100 <= it["pct_change"] <= 100


# ---- Tab2 市场宽度 ----


def test_breadth_history_series():
    h = breadth.get_breadth_history(60)
    points = h["points"]
    assert len(points) >= 30, f"宽度序列过短：{len(points)}"
    prev_cum = None
    for p in points:
        assert 0 <= p["pct_above_ma50"] <= 100
        assert 0 <= p["pct_above_ma200"] <= 100
        assert p["new_highs"] >= 0 and p["new_lows"] >= 0
        # A-D 线是累计值：必须单调可回溯（不允许出现未累计的原始日差）
        if prev_cum is not None:
            assert abs(p["ad_line"] - prev_cum) <= 600, "A-D 线日变化超出合理范围"
        prev_cum = p["ad_line"]
    assert h["summary"]["pct_above_ma50"] == points[-1]["pct_above_ma50"]


def test_breadth_highlights_are_genuine_extremes():
    """创新高/新低的判定必须自洽：新高项的回撤 ≤ 0，新低项的价格贴近 52 周低点。"""
    hl = breadth.get_breadth_highlights(20)
    counts = hl["high_low_counts"]
    assert counts, "52 周计数缺失（预热窗口不足时会全空）"
    assert counts["total"] >= 400
    for row in hl["new_highs"]:
        assert row["drawdown_pct"] >= -0.01, "创新高的回撤不应为负"
        assert row["high_52w"] >= row["close"] * 0.999
    for row in hl["new_lows"]:
        assert row["low_52w"] <= row["close"] * 1.001
    if hl["near_high"]:
        dd = [r["drawdown_pct"] for r in hl["near_high"]]
        assert dd == sorted(dd, reverse=True), "贴近高点榜必须按回撤降序"


# ---- Tab3 板块轮动 ----


def test_sector_heatmap_items():
    items = sectors.get_sector_heatmap(40)
    assert len(items) >= 8, f"板块数异常偏少：{len(items)}"
    for it in items:
        assert it["name"], "板块必须有中文名"
        assert it["value"] >= 0
        assert -100 <= it["pct_change"] <= 100
        assert it["stock_count"] > 0
        assert it["advance_count"] >= 0
    values = [i["value"] for i in items]
    assert values == sorted(values, reverse=True), "热力图必须按成交额降序"


def test_sector_rotation_sorted_and_nullable():
    rot = sectors.get_sector_rotation(24)
    assert rot["sectors"]
    # 指数日期与个股日期分开标注（指数通常滞后）
    assert rot["trade_date"]
    assert "index_date" in rot
    rets = [r["ret_20d"] for r in rot["sectors"] if r["ret_20d"] is not None]
    assert rets == sorted(rets, reverse=True), "轮动榜必须按 20 日收益降序"
    for r in rot["sectors"]:
        for key in ("ret_1d", "ret_5d", "ret_20d", "ret_60d", "rs_20d"):
            val = r[key]
            assert val is None or -100 <= val <= 500, f"{r['name']}.{key} 口径异常：{val}"
        b = r["breadth_20d"]
        assert b is None or 0 <= b <= 100


def test_sector_valuation_medians():
    rows = sectors.get_sector_valuation(24)
    assert rows
    for r in rows:
        assert r["stock_count"] > 0
        # 中位数无效时必须为 None，不能落 0（0 会被前端当成真实估值）
        for key in ("pe_median", "pb_median", "dividend_yield_median"):
            assert r[key] is None or r[key] != 0


# ---- Tab4 财报季 ----


def test_earnings_calendar_window():
    cal = earnings.get_earnings_calendar(30, 50)
    assert cal["as_of"]
    for it in cal["items"]:
        assert 0 <= it["days_until"] <= 30, f"{it['symbol']} 越出窗口：{it['days_until']}"
        assert it["earnings_date"] >= cal["as_of"]
    dates = [i["earnings_date"] for i in cal["items"]]
    assert dates == sorted(dates), "财报日历必须按日期升序"


def test_earnings_surprises_only_reported():
    res = earnings.get_earnings_surprises(20, 120)
    for it in res["items"]:
        assert it["reported_eps"] is not None, "超预期榜只能包含已披露标的"
        assert it["report_date"]
    vals = [i["surprise_pct"] for i in res["items"]]
    assert vals == sorted(vals, reverse=True), "超预期榜必须降序"
    # 同一标的只保留最近一期
    syms = [i["symbol"] for i in res["items"]]
    assert len(syms) == len(set(syms))


def test_earnings_revisions_sorted():
    res = earnings.get_earnings_revisions(20)
    for it in res["items"]:
        assert it["analyst_count"] >= 0
        assert it["eps_growth_pct"] is not None
    vals = [i["eps_growth_pct"] for i in res["items"]]
    assert vals == sorted(vals, reverse=True)


# ---- Tab5 分析师 ----


@pytest.mark.parametrize(
    "from_g,to_g,action,expected",
    [
        ("Hold", "Buy", "up", "up"),
        ("Buy", "Sell", "down", "down"),
        ("Overweight", "Overweight", "main", "neutral"),
        ("", "Outperform", "init", "up"),  # 首次覆盖给看多档位 → 判为 up
        ("", "Underperform", "init", "down"),
    ],
)
def test_grade_direction(from_g, to_g, action, expected):
    """评级方向判定（纯函数，覆盖升级/降级/重申/首次覆盖）。"""
    assert analysts._grade_direction(from_g, to_g, action) == expected


def test_analyst_upgrades_rows():
    res = analysts.get_analyst_upgrades(30, 40)
    for it in res["items"]:
        assert it["direction"] in ("up", "down", "neutral")
        assert it["grade_date"] <= res["as_of"]
        assert it["symbol"] and it["firm"]
        chg = it["price_target_change_pct"]
        assert chg is None or -100 <= chg <= 1000


def test_analyst_targets_capped():
    """目标价隐含空间必须剔除离群值（退市/并购残留会造成几十倍失真）。"""
    res = analysts.get_analyst_targets(30)
    for it in res["items"]:
        assert -200 <= it["upside_pct"] <= 200, f"{it['symbol']} 离群未剔除：{it['upside_pct']}"
        assert it["close"] > 0 and it["target_mean"] > 0
    vals = [i["upside_pct"] for i in res["items"]]
    assert vals == sorted(vals, reverse=True)


def test_analyst_ratings_totals():
    r = analysts.get_analyst_ratings()
    total = r["strong_buy"] + r["buy"] + r["hold"] + r["sell"] + r["strong_sell"]
    assert total > 0
    assert total == r["total_coverage"], "分档之和必须等于覆盖总数"
    assert 0 <= r["bull_ratio"] <= 100
    if r["top_rated"]:
        vals = [x["bull_ratio"] for x in r["top_rated"]]
        assert vals == sorted(vals, reverse=True)


# ---- Tab6 资金与筹码 ----


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Sale at price 135.42 per share.", "Sale"),
        ("Purchase at price 12.00 per share.", "Purchase"),
        ("Stock Award(Grant) at price 0.00 per share.", "Stock Award(Grant)"),
        ("", ""),
        (None, ""),
    ],
)
def test_insider_type_parse(text, expected):
    """交易类型必须从 Text 前缀解析（`Transaction` 列在本数据源恒为空）。"""
    assert holdings._insider_type(text) == expected


def test_insider_movers_only_signal_types():
    res = holdings.get_insider_movers(90, 20)
    assert res["as_of"]
    for key in ("top_buys", "top_sells"):
        for it in res[key]:
            assert it["value_yi"] > 0, "榜单金额应为正（卖出金额同样取绝对值口径）"
            assert it["last_date"] <= res["as_of"]
            assert it["trades"] >= 1
    buys = [i["value_yi"] for i in res["top_buys"]]
    assert buys == sorted(buys, reverse=True)


def test_institutional_holders_shape():
    res = holdings.get_institutional_holders(20)
    assert res["coverage"] >= 400, f"major_holders 覆盖偏低：{res['coverage']}"
    assert 0 <= res["institutions_pct_median"] <= 200
    for key in ("top_increases", "top_decreases"):
        vals = [i["delta_value_yi"] for i in res[key]]
        if key == "top_increases":
            assert vals == sorted(vals, reverse=True)
        else:
            assert vals == sorted(vals)


def test_dividend_calendar_future_only():
    cal = holdings.get_dividend_calendar(60, 40)
    for it in cal["items"]:
        assert it["ex_dividend_date"] >= cal["as_of"], "除息日历不得包含已过去的日期"
        assert 0 <= it["days_until"] <= 60
    dates = [i["ex_dividend_date"] for i in cal["items"]]
    assert dates == sorted(dates)


def test_recent_splits_positive_ratio():
    res = holdings.get_recent_splits(365, 30)
    for it in res["items"]:
        assert it["ratio"] > 0
        assert it["split_date"] <= res["as_of"]
    dates = [i["split_date"] for i in res["items"]]
    assert dates == sorted(dates, reverse=True)


# ---- Tab7 估值 ----


@pytest.mark.parametrize("kind", ["dividend", "pe", "pb"])
def test_valuation_rankings_thresholds(kind):
    res = valuation.get_valuation_rankings(kind, 20)
    assert res["kind"] == kind
    for it in res["items"]:
        # 健全性门槛：剔除快照陈旧的空壳标的
        assert it["market_cap_yi"] >= 20, f"{it['symbol']} 低于市值门槛"
        if kind == "pe":
            assert it["pe_ratio"] is not None and it["pe_ratio"] >= 3
        if kind == "pb":
            assert it["pb_ratio"] is not None and it["pb_ratio"] >= 0.3
        if kind == "dividend":
            assert it["dividend_yield"] is not None and it["dividend_yield"] >= 0.5
        assert it["sector"], "榜单项应带上所属板块"
    vals = [i["value"] for i in res["items"]]
    # 低 PE / 低 PB 升序，高股息降序
    assert vals == sorted(vals, reverse=(kind == "dividend"))


def test_size_tiers_partition():
    res = valuation.get_size_tiers()
    assert res["tiers"], "市值分层为空"
    assert res["total_market_cap_yi"] > 0
    total = sum(t["market_cap_yi"] for t in res["tiers"])
    # 分层为全量划分，各层合计应接近总量（保留两位小数的误差）
    assert abs(total - res["total_market_cap_yi"]) < max(1.0, res["total_market_cap_yi"] * 0.01)
    for t in res["tiers"]:
        assert t["count"] > 0
        assert t["pe_median"] is None or t["pe_median"] >= 3


def test_valuation_overview_quantiles():
    ov = valuation.get_valuation_overview()
    assert ov["coverage"] >= 400
    assert ov["pe_median"] is not None
    if ov["pe_p25"] and ov["pe_p75"]:
        assert ov["pe_p25"] <= ov["pe_median"] <= ov["pe_p75"]


# ---- 状态 / 缓存 / 容错 ----


def test_feed_status_reports_dates_and_notes():
    st = us_base.feed_status()
    assert st["available"] is True
    assert st["kline_latest"] and st["kline_latest"] >= "20260101"
    assert st["index_latest"], "状态接口必须暴露指数最新日期（用于页面标注滞后）"
    assert st["universe_size"] >= 400
    assert st["sector_covered"] >= 400
    # 口径提示必须随状态一起下发，前端据此向用户说明数据边界
    for key in ("universe", "price_adjust", "no_vix", "index_lag"):
        assert key in st["notes"], f"status.notes 缺少 {key}"


def test_cache_roundtrip():
    """同一 key 二次调用命中缓存（数值应完全一致）。"""
    a = breadth.get_market_breadth()
    b = breadth.get_market_breadth()
    assert a == b
    us_base.clear_cache_us()
    c = breadth.get_market_breadth()
    assert c["trade_date"] == a["trade_date"]


def test_read_partitioned_missing_partition_returns_empty():
    """不存在的分区必须安全返回空表，而不是抛异常或全库扫描。"""
    df = us_base._read_partitioned(us_base.KLINE_REL, ["19000101"])
    assert df.empty


def test_read_partitioned_column_pruning():
    """列裁剪必须生效：只取 3 列时结果列数固定（性能关键路径）。"""
    days = us_base._trading_days(None, 3)
    df = us_base._read_partitioned(us_base.KLINE_REL, days, columns="symbol, dt, close")
    assert list(df.columns) == ["symbol", "dt", "close"]


def test_sector_cn_mapping_and_fallback():
    assert us_base._sector_cn("Technology") == "信息技术"
    assert us_base._sector_cn("Unknown") == "未分类"
    assert us_base._sector_cn(None) == "未分类"
    # 未收录的类名原样返回，不丢信息
    assert us_base._sector_cn("Some New Sector") == "Some New Sector"
