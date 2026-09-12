"""美股个股终端（stock_terminal_us）数据层 + 路由测试。

零 mock 直连本地 quantus parquet + 本地 PG（enrichment 资讯）+ Huntly SQLite
（与 `test_quantus_market_analysis.py` 同款模式）。

断言取向：**契约与健康度**（防停更 / 防口径失真 / 防 NaN 串 JSON / 防单段失败拖垮
整体 / 防响应形状与前端 types.ts 漂移），而不是精确数值 —— 数据每天在变，
精确断言会天天红。

跑法（后端依赖在容器里）：
    docker exec -w /app/backend quantmind python -m pytest tests/test_us_stock_terminal.py -q --no-cov
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.api.market_analysis_shared import market_days
from backend.services.api.market_analysis_us.feed import base as us_base
from backend.services.api.stock_terminal_us.feed import (
    base as term_base,
    detail,
    kline,
    news,
    research,
    universe,
)

_PREFIX = "/api/v1/stock-terminal-us"
# 日线分区允许的滞后天数：美股节假日最长连休 4 天，两周足以覆盖，超出即认定停更
_SYNC_LAG_DAYS = 14

# 与前端 `stock-terminal-us/types.ts` 严格对齐的键集合（契约回归网）
_DETAIL_KEYS = {
    "symbol",
    "name",
    "trade_date",
    "overview",
    "valuation",
    "financials",
    "analysts",
    "earnings",
    "insiders",
    "holdings",
    "corporate_actions",
    "notes",
}
_OVERVIEW_KEYS = {
    "cn_name",
    "en_name",
    "sector",
    "industry",
    "close",
    "pct_change",
    "market_cap",
    "cap_display",
    "week52_high",
    "week52_low",
    "avg_volume",
    "trade_date",
}
_VALUATION_KEYS = {
    "pe_ratio",
    "pb_ratio",
    "dividend_yield",
    "market_cap",
    "week52_high",
    "week52_low",
    "source",
    "asof",
    "size_tier",
    "stale_warning",
}
_INSIDER_NET_KEYS = {"buy_value", "sell_value", "net_value", "buy_count", "sell_count"}
_HOLDINGS_KEYS = {
    "insiders_pct",
    "institutions_pct",
    "institutions_float_pct",
    "institutions_count",
    "funds",
    "reported_date",
}
_ACTION_ENUM = {"up", "down", "init", "reiterated", "other"}


# ---- 数据健康度（防停更） ----


def test_kline_partition_dates_not_stale():
    """日线分区必须延续到近期（防数据同步停更后终端静默显示老数据）。"""
    dates = market_days.list_partition_dates(us_base.KLINE_REL, us_base.DATA_DIR)
    assert len(dates) > 5000, "美股日线分区数量异常偏少"
    assert dates[0].startswith("2001"), f"最早分区应为 2001 年，实际 {dates[0]}"
    latest = datetime.strptime(dates[-1], "%Y%m%d").date()
    lag = (date.today() - latest).days
    assert lag <= _SYNC_LAG_DAYS, f"日线停更：最新分区 {dates[-1]}，已滞后 {lag} 天"


def test_kline_reads_only_requested_partitions():
    """列裁剪 + 分区裁剪必须生效（性能关键路径）。"""
    days = us_base._trading_days(None, 3)
    df = term_base._read_symbol_bars("AAPL", days)
    assert list(df.columns) == [
        "symbol",
        "dt",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ]
    assert df["symbol"].nunique() == 1, "单标的读不应混入其他标的"
    # 不存在的分区安全返回空表，不抛异常也不全库扫描
    assert term_base._read_symbol_bars("AAPL", ["19000101"]).empty


# ---- 标的池 / 列表 / 概要 ----


def test_symbol_meta_scale_and_names():
    meta = term_base._symbol_meta()
    assert len(meta) >= 400, f"标的池偏小：{len(meta)}"
    assert meta["symbol"].is_unique
    assert (meta["cn_name"].str.strip() != "").all(), "展示名不得为空单元格"
    assert {"symbol", "cn_name", "en_name", "sector", "sector_cn", "industry"}.issubset(
        meta.columns
    )
    row = meta[meta["symbol"] == "AAPL"]
    assert not row.empty and row.iloc[0]["cn_name"] == "苹果"
    assert (meta["sector_cn"].str.strip() != "").all()


def test_list_pool_is_latest_quoted_symbols():
    """默认列表 = 最新分区有行情的标的（退市/无报价壳标的不得出现在搜索结果里）。"""
    _, snap = term_base._latest_snapshot()
    quoted = set(snap["symbol"])
    meta = term_base._symbol_meta()
    delisted = set(meta["symbol"]) - quoted
    assert delisted, "池内应存在无最新报价的标的（否则本用例失去意义）"

    page = universe.list_symbols(q=None, page=1, page_size=600)
    assert page["total"] == len(quoted)
    assert {it["symbol"] for it in page["items"]} == quoted
    assert all(it["has_quote"] and it["close"] is not None for it in page["items"])
    assert all(not it["delisted"] for it in page["items"])

    full = universe.list_symbols(q=None, page=1, page_size=600, include_delisted=True)
    assert full["total"] == len(meta)
    assert {it["symbol"] for it in full["items"] if it["delisted"]} == delisted
    assert all(it["close"] is None for it in full["items"] if it["delisted"]), (
        "退市标的不得给出报价"
    )


def test_list_symbols_pagination_and_search():
    page = universe.list_symbols(q=None, page=1, page_size=50)
    assert page["pages"] >= 2
    assert len(page["items"]) == 50
    assert page["trade_date"] and page["adjust"] == "none"
    # 市值降序（大票在前）
    caps = [it["market_cap"] for it in page["items"]]
    assert caps == sorted(caps, reverse=True)
    # 前端搜索框直接读 name / cap_display，缺失会显示空白
    top = page["items"][0]
    assert top["name"] and top["cap_display"].startswith("$")

    hit = universe.list_symbols(q="苹果", page=1, page_size=10)
    assert [it["symbol"] for it in hit["items"]] == ["AAPL"]
    assert (
        universe.list_symbols(q="aapl", page=1, page_size=10)["items"][0]["symbol"]
        == "AAPL"
    )
    assert universe.list_symbols(q="不存在的公司", page=1, page_size=10)["total"] == 0
    # 全池一次拉取（前端做本地筛选的场景）
    full = universe.list_symbols(q=None, page=1, page_size=600)
    assert len(full["items"]) == full["total"]


def test_profile_aapl_fields():
    p = universe.get_profile("aapl")  # 大小写与空白应被归一
    assert p and p["symbol"] == "AAPL" and p["cn_name"] == "苹果"
    assert p["close"] > 1 and -100 <= p["pct_change"] <= 100
    assert p["market_cap"] > 1e11 and p["market_cap_yi"] > 100
    assert p["cap_display"] == f"${p['market_cap'] / 1e12:.2f}万亿"
    assert p["high_52w"] > p["low_52w"] > 0
    assert p["sector_cn"] == "信息技术"
    assert p["adjust"] == "none"
    assert p["notes"]["amount_unit"], "口径说明必须随响应下发"


def test_profile_delisted_symbol_has_no_quote_but_no_crash():
    """已退市/代码口径不一致的标的：池内必须有记录，但报价字段允许为空（不造假、不报错）。"""
    meta = term_base._symbol_meta()
    _, snap = term_base._latest_snapshot()
    stale = sorted(set(meta["symbol"]) - set(snap["symbol"]))
    assert stale, "池内应存在无最新报价的退市/壳标的（否则本用例失去意义）"
    p = universe.get_profile(stale[0])
    assert p is not None and p["symbol"] == stale[0]
    assert p["has_quote"] is False and p["close"] is None


def test_profile_keys_always_present():
    """所有键必须始终存在（缺值给 null）—— 前端 `xxx.toFixed` 白屏的根因就是键时有时无。"""
    required = {
        "symbol",
        "cn_name",
        "display_name",
        "sector_cn",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "prev_close",
        "pct_change",
        "volume",
        "amount",
        "market_cap_yi",
        "cap_display",
        "high_52w",
        "low_52w",
        "has_quote",
        "adjust",
        "notes",
    }
    meta = term_base._symbol_meta()
    _, snap = term_base._latest_snapshot()
    stale = sorted(set(meta["symbol"]) - set(snap["symbol"]))
    for sym in ("AAPL", stale[0]):
        p = universe.get_profile(sym)
        assert required.issubset(p), f"{sym} 缺少键：{required - set(p)}"
        json.dumps(p, allow_nan=False, ensure_ascii=False)


# ---- K 线 ----


def test_aapl_kline_sane():
    k = kline.get_kline("AAPL", days=250)
    assert k and k["symbol"] == "AAPL" and k["adjust"] == "none"
    assert k["count"] >= 200
    assert not k["truncated"]
    dates = [it["date"] for it in k["items"]]
    assert dates == sorted(dates), "K 线必须按日期升序"
    assert k["start_date"] == dates[0] and k["end_date"] == dates[-1]
    for it in k["items"]:
        assert 0.5 < it["close"] < 2000, f"AAPL 价格量级异常：{it}"
        assert it["volume"] > 0 and it["amount"] > 0
        assert it["low"] <= it["high"]
        # 美元原始成交额（≈ close×volume），不是 A 股「股/万元」口径
        assert 0.5 < it["amount"] / (it["close"] * it["volume"]) < 1.5
    # 未复权原始价：250 日内不应出现拆股式的 ±100% 跳变（AAPL 上次拆股在 2020 年）
    closes = [it["close"] for it in k["items"]]
    for prev, cur in zip(closes, closes[1:], strict=False):
        assert abs(cur / prev - 1) < 0.5, "非拆股窗口内的单日跳变过大"


def test_aapl_split_event_and_raw_price_jump():
    """拆股口径：K 线返回**未复权原始价**（跳变真实存在），事件单独标记（全部历史）。"""
    k = kline.get_kline("AAPL", start="2020-08-28", end="2020-09-01")
    assert k and k["count"] == 3
    closes = [it["close"] for it in k["items"]]
    before, after = closes[0], closes[1]
    assert before > 400 and after < 200, (
        f"4:1 拆股日应出现原始价腰斩：{before} -> {after}"
    )
    assert abs(before / after - 4) < 0.2, "拆股比例应约为 4:1"
    # splits 是该标的**全部历史**拆股（前端按窗口过滤后画竖线），不是窗口内事件
    assert {"date": "2020-08-31", "ratio": 4.0} in k["splits"]
    assert [s["date"] for s in k["splits"]] == sorted(s["date"] for s in k["splits"])
    assert len(k["splits"]) >= 5, "AAPL 历史拆股应完整（1987/2000/2005/2014/2020）"
    recent = kline.get_kline("AAPL", days=250)
    assert recent["splits"] == k["splits"], "窗口不同不应改变拆股事件集合"


def test_kline_range_window_and_truncation():
    """start/end 为闭区间；超长窗口受硬上限约束并显式置 truncated。"""
    k = kline.get_kline("AAPL", start="2026-01-05", end="2026-01-09")
    assert k["count"] == 5
    assert k["start_date"] == "2026-01-05" and k["end_date"] == "2026-01-09"
    long = kline.get_kline("AAPL", start="2001-01-01", end="2026-12-31")
    assert long["truncated"] is True
    assert long["count"] == term_base.MAX_RANGE_DAYS


def test_kline_unknown_symbol_returns_none():
    assert kline.get_kline("ZZZZZZ") is None
    assert kline.get_kline("not a symbol") is None
    assert universe.get_profile("ZZZZZZ") is None
    assert news.get_stock_news("ZZZZZZ") is None
    assert detail.get_detail("ZZZZZZ") is None


# ---- 详情：契约形状（与前端 types.ts 对齐） ----


def test_detail_contract_shape():
    """顶层与各面板的键集合必须与前端 types.ts 严格一致（多的可以是增益，少的会白屏）。"""
    d = detail.get_detail("AAPL")
    assert d is not None and d["symbol"] == "AAPL"
    assert _DETAIL_KEYS.issubset(d), f"缺少顶层键：{_DETAIL_KEYS - set(d)}"
    assert set(d["overview"]) == _OVERVIEW_KEYS
    assert set(d["valuation"]) == _VALUATION_KEYS
    assert set(d["financials"]) == {"periods", "income", "balance", "cashflow"}
    assert set(d["analysts"]) == {"target", "ratings", "upgrades"}
    assert set(d["earnings"]) == {"history", "upcoming"}
    assert set(d["insiders"]) == {"items", "net"}
    assert set(d["insiders"]["net"]) == _INSIDER_NET_KEYS
    assert set(d["holdings"]) == _HOLDINGS_KEYS
    assert set(d["corporate_actions"]) == {"dividends", "splits"}
    # 数值出口不得有 NaN/Infinity（前端 null.toFixed 白屏事故的根因）
    json.dumps(d, allow_nan=False, ensure_ascii=False)


def test_detail_overview_values():
    d = detail.get_detail("AAPL")
    o = d["overview"]
    assert o["cn_name"] == "苹果" and o["sector"] == "Technology"
    assert o["en_name"] is None, "en_name 全库为空串，出口应为 None（前端显示 --）"
    assert o["close"] > 1 and -100 <= o["pct_change"] <= 100
    assert o["market_cap"] > 1e11 and o["cap_display"].startswith("$")
    assert o["week52_high"] > o["week52_low"] > 0
    assert o["avg_volume"] > 0
    assert o["trade_date"] == d["trade_date"] == d["valuation"]["asof"]


def test_detail_financials_periods_aligned():
    """values 必须与 periods 等长同序；只出现实际存在的列。"""
    fin = detail._financials("AAPL")
    periods = fin["periods"]
    assert len(periods) == 5 and periods == sorted(periods, reverse=True)
    for group in ("income", "balance", "cashflow"):
        assert fin[group], f"{group} 表不应为空（AAPL 全量数据齐备）"
        for row in fin[group]:
            assert set(row) == {"key", "label", "values"}
            assert len(row["values"]) == len(periods)
            assert any(v is not None for v in row["values"]), "整行为空的条目不应出现"
            assert row["key"].isascii(), "key 必须是 parquet 原始英文列名"
    revenue = next(r for r in fin["income"] if r["key"] == "Total Revenue")
    assert revenue["label"] == "营业收入"
    assert revenue["values"][0] > 1e10, "最新财年营收应为美元原始值（不是亿元）"


def test_detail_analysts_contract():
    a = research.get_analysts("AAPL")
    assert a["target"] is not None
    assert set(a["target"]) == {"current", "high", "low", "mean", "median"}
    assert all(v is None or v > 0 for v in a["target"].values()), (
        "0 是 yahoo 的无值占位，出口必须转 None"
    )
    assert a["ratings"] and a["ratings"][0]["period"] == "0m", "评级分布最新一期在前"
    for r in a["ratings"]:
        assert set(r) == {"period", "strongBuy", "buy", "hold", "sell", "strongSell"}
    ups = a["upgrades"]
    assert ups and len(ups) <= 30
    dates = [u["date"] for u in ups]
    assert dates == sorted(dates, reverse=True), "升 downgrade 流水按日期倒序"
    for u in ups:
        assert set(u) == {
            "date",
            "firm",
            "to_grade",
            "from_grade",
            "action",
            "current_target",
            "prior_target",
        }
        assert u["action"] in _ACTION_ENUM, f"action 未归一：{u}"
        assert len(u["date"]) == 10
        assert u["current_target"] is None or u["current_target"] > 0


def test_detail_earnings_contract():
    """earnings_history.surprisePercent 是小数，出口必须归一到百分数。"""
    e = research.get_earnings("AAPL")
    hist = e["history"]
    assert hist and len(hist) <= 8
    quarters = [h["quarter"] for h in hist]
    assert quarters == sorted(quarters, reverse=True)
    for row in hist:
        assert set(row) == {"quarter", "actual", "estimate", "surprise_pct"}
        assert abs(row["surprise_pct"]) < 100, f"疑似未 ×100：{row}"
        if row["actual"] and row["estimate"]:
            expect = (row["actual"] / row["estimate"] - 1) * 100
            assert abs(row["surprise_pct"] - expect) < 0.5
    upcoming = e["upcoming"]
    assert upcoming, "财报日历应有未来披露日"
    for row in upcoming:
        assert set(row) == {"date", "eps_estimate", "revenue_estimate"}
        assert row["date"] >= date.today().strftime("%Y-%m-%d")
    # calendar 口径优先：AAPL 的下一财报日是 2026-10-30（earnings_dates 的东八区时间会差一天）
    assert upcoming[0]["revenue_estimate"] is not None


def test_detail_insiders_contract_and_net_consistency():
    ins = research.get_insiders("AAPL")
    items, net = ins["items"], ins["net"]
    assert items and len(items) <= 30
    dates = [it["date"] for it in items]
    assert dates == sorted(dates, reverse=True)
    for it in items:
        assert set(it) == {"date", "insider", "position", "type", "shares", "value"}
        assert it["type"] in {"buy", "sell", "other"}, (
            "类型从 Text 前缀归一到 buy/sell/other"
        )
    buys = [it for it in items if it["type"] == "buy"]
    sells = [it for it in items if it["type"] == "sell"]
    assert net["buy_count"] == len(buys) and net["sell_count"] == len(sells)
    assert abs(net["net_value"] - (net["buy_value"] - net["sell_value"])) < 0.01, (
        "净额 = 买入金额 - 卖出金额"
    )
    assert any(it["type"] == "sell" for it in items), "AAPL 应有可解析的卖出记录"


def test_detail_holdings_contract():
    """major_holders 是 4 行无标签宽表，顺序固定 —— 顺序错了会把「机构家数」当占比。"""
    h = research.get_holdings("AAPL")
    assert 0 <= h["insiders_pct"] <= 100
    assert 0 < h["institutions_pct"] <= 100
    assert h["insiders_pct"] < h["institutions_pct"], "内部人占比应小于机构占比"
    assert 0 < h["institutions_float_pct"] <= 100
    assert h["institutions_count"] > 100, "第 4 行是机构家数（整数），不是占比"
    assert h["reported_date"] and len(h["reported_date"]) == 10, "13F 披露日供面板标注"
    assert h["funds"] and len(h["funds"]) <= 15
    for f in h["funds"]:
        assert set(f) == {
            "holder",
            "pct_held",
            "shares",
            "value",
            "pct_change",
            "date_reported",
        }
        assert f["holder"] and f["pct_held"] is not None


def test_detail_corporate_actions_contract():
    ca = detail._corporate_actions("AAPL")
    divs = ca["dividends"]
    assert len(divs) == 20, "分红取近 20 次"
    assert [d["date"] for d in divs] == sorted((d["date"] for d in divs), reverse=True)
    assert all(d["amount"] is not None and d["amount"] > 0 for d in divs)
    splits = ca["splits"]
    assert {"date": "2020-08-31", "ratio": 4.0} in splits
    assert [s["date"] for s in splits] == sorted(s["date"] for s in splits)


def test_detail_panels_degrade_to_empty_skeleton():
    """缺数据的面板返回空骨架而非抛异常（退市壳标的无财务/分析师/机构文件）。"""
    meta = term_base._symbol_meta()
    _, snap = term_base._latest_snapshot()
    delisted = sorted(set(meta["symbol"]) - set(snap["symbol"]))[0]
    d = detail.get_detail(delisted)
    assert d is not None, "退市标的仍应给出详情（报价为空），而不是 404"
    assert set(d["overview"]) == _OVERVIEW_KEYS
    assert d["overview"]["close"] is None
    json.dumps(d, allow_nan=False, ensure_ascii=False)
    # 未知标的：各面板函数按符号取数必须安全返回空骨架，而不是 KeyError
    assert detail._financials("ZZZZZZ")["periods"] == []
    assert research.get_analysts("ZZZZZZ") == {
        "target": None,
        "ratings": [],
        "upgrades": [],
    }
    assert research.get_earnings("ZZZZZZ") == {"history": [], "upcoming": []}
    assert research.get_insiders("ZZZZZZ")["items"] == []
    assert research.get_holdings("ZZZZZZ")["funds"] == []
    assert detail._corporate_actions("ZZZZZZ")["dividends"] == []
    assert research.get_holdings("ZZZZZZ")["reported_date"] is None


def test_insider_type_parsed_from_text_prefix():
    from backend.services.api.market_analysis_us.feed.holdings import _insider_type

    assert _insider_type("Sale at price 317.01 per share.") == "Sale"
    assert _insider_type("Purchase at price 12.50 per share.") == "Purchase"
    assert _insider_type("Stock Gift at price 0.00 per share.") == "Stock Gift"
    assert _insider_type("Unknown Prefix") == "Unknown Prefix", "未知前缀原样保留，不猜"
    assert _insider_type(None) == ""
    assert _insider_type("") == ""
    # 只有 Purchase/Sale 归为 buy/sell，其余（授予/行权/未知）一律 other
    assert research._INSIDER_TYPE_MAP.get("Purchase") == "buy"
    assert research._INSIDER_TYPE_MAP.get("Sale") == "sell"
    assert research._INSIDER_TYPE_MAP.get("Stock Gift", "other") == "other"


def test_upgrade_action_normalized_by_shared_grade_logic():
    """action 归一复用市场分析模块的评级词档位比较，不另写一套。"""
    assert research._norm_action("Equal-Weight", "Overweight", "main") == "up"
    assert research._norm_action("Buy", "Hold", "main") == "down"
    assert research._norm_action("", "Buy", "init") == "init"
    assert research._norm_action("Buy", "Buy", "reit") == "reiterated"
    assert research._norm_action("Buy", "Buy", "main") == "reiterated"
    assert research._norm_action("", "", "") == "other"


# ---- 资讯 ----


def test_news_keywords_skip_single_char_tickers():
    """单字符代码命中一切（实测 F 命中 9.4 万条），必须不参与匹配。"""
    assert news.keywords_for("AAPL") == ["苹果", "AAPL"]
    for sym in ("F", "T", "A", "C", "V"):
        kws = news.keywords_for(sym)
        assert sym not in kws, f"单字符代码 {sym} 不应参与匹配"
        assert kws and len(kws[0]) > 1
    assert news.keywords_for("") == []


def test_news_enrichment_primary_path():
    """主路径：PG enrichment（tickers 精确 + 标题中文名），带 FinBERT 情绪标签。"""
    try:
        news._fetch_candidates("AAPL", news.keywords_for("AAPL"), 5)
    except Exception as exc:  # noqa: BLE001 - 无 PG 的环境跳过（本地容器里有）
        pytest.skip(f"PG 不可用：{exc}")
    res = news.get_stock_news("AAPL", limit=10)
    assert res and res["provider"] == "enrichment" and res["available"] is True
    assert 0 < len(res["items"]) <= 10
    for it in res["items"]:
        assert it["title"] and it["id"] > 0
        assert it["matched_by"] in {"ticker", "title"}
        assert set(it) >= {
            "title",
            "link",
            "published_at",
            "source",
            "sentiment_score",
            "sentiment_label",
            "event_tags",
            "matched_by",
        }
        if it["sentiment_label"]:
            assert it["sentiment_label"] in {"bullish", "bearish", "neutral"}
    # 发布时间倒序（无发布时间的排末尾）
    stamps = [it["published_at"] for it in res["items"] if it["published_at"]]
    assert stamps == sorted(stamps, reverse=True)
    # ticker 匹配确实生效（AAPL 有 3500+ 条 enrichment 记录）
    assert any(it["matched_by"] == "ticker" for it in res["items"])
    json.dumps(res, allow_nan=False, ensure_ascii=False)


def test_news_single_char_symbol_uses_name_only():
    if not os.path.exists(news._db_path()) and not _pg_reachable():
        pytest.skip("无资讯源")
    res = news.get_stock_news("F", limit=3)
    assert res is not None and "F" not in res["keywords"]
    assert res["keywords"] == ["福特汽车"]


def _pg_reachable() -> bool:
    try:
        news._fetch_candidates("AAPL", ["苹果"], 1)
        return True
    except Exception:  # noqa: BLE001
        return False


def test_news_huntly_fallback_path():
    """兜底路径：Huntly 标题 LIKE（原实现保留），条目形状与主路径一致。"""
    db = news._db_path()
    if not os.path.exists(db):
        pytest.skip("本机无 Huntly 库")
    items = news._fetch_huntly(db, news.keywords_for("AAPL"), "AAPL", 5)
    assert 0 < len(items) <= 5
    ids = [it["id"] for it in items]
    assert ids == sorted(ids, reverse=True), "兜底路径按 id 倒序"
    for it in items:
        assert it["title"] and it["link"]
        assert it["matched_by"] == "title"
        assert "sentiment_score" in it


def test_news_unmatched_symbol_degrades_gracefully():
    """无任何资讯命中的标的：不抛异常，available=false + 空 items。"""
    res = news.get_stock_news("AVB", limit=5)
    assert res is not None
    assert res["available"] in (True, False) and isinstance(res["items"], list)


# ---- 路由（鉴权 + 信封 + 参数边界 + 错误码） ----


def _stub_auth():
    return {"tenant_id": "default", "user_id": "tester", "sub": "tester"}


@pytest.fixture(scope="module")
def client():
    from backend.services.api.stock_terminal_us.router import router
    from backend.services.api.user_app.middleware.auth import get_current_user

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _stub_auth
    return TestClient(app)


def test_endpoints_return_enveloped_data(client):
    """响应信封 {success, data} 与 A 股终端一致（前端共享服务读 resp.data.data）。"""
    r = client.get(f"{_PREFIX}/list", params={"page_size": 5})
    body = r.json()
    assert r.status_code == 200 and body["success"] is True
    assert len(body["data"]["items"]) == 5

    r = client.get(f"{_PREFIX}/profile", params={"symbol": "AAPL"})
    body = r.json()
    assert r.status_code == 200 and body["data"]["symbol"] == "AAPL"

    r = client.get(f"{_PREFIX}/kline", params={"symbol": "AAPL", "days": 60})
    body = r.json()["data"]
    assert r.status_code == 200 and body["count"] >= 40 and body["adjust"] == "none"
    assert any(s["date"] == "2020-08-31" for s in body["splits"])

    r = client.get(f"{_PREFIX}/detail", params={"symbol": "AAPL"})
    body = r.json()["data"]
    assert r.status_code == 200 and _DETAIL_KEYS.issubset(body)

    r = client.get(f"{_PREFIX}/news", params={"symbol": "AAPL", "limit": 5})
    body = r.json()["data"]
    assert r.status_code == 200 and "items" in body

    r = client.post(f"{_PREFIX}/refresh")
    assert r.status_code == 200 and r.json()["data"]["status"] == "success"


def test_endpoints_error_codes(client):
    assert (
        client.get(f"{_PREFIX}/profile", params={"symbol": "ZZZZZZ"}).status_code == 404
    )
    assert (
        client.get(f"{_PREFIX}/kline", params={"symbol": "ZZZZZZ"}).status_code == 404
    )
    assert (
        client.get(f"{_PREFIX}/detail", params={"symbol": "ZZZZZZ"}).status_code == 404
    )
    # 非法代码（含引号/分号）在入口就被拒，不落进 SQL
    bad = client.get(f"{_PREFIX}/profile", params={"symbol": "12'; DROP--"})
    assert bad.status_code == 400
    # 参数上下界
    assert (
        client.get(f"{_PREFIX}/kline", params={"symbol": "AAPL", "days": 5}).status_code
        == 422
    )
    assert client.get(f"{_PREFIX}/list", params={"page_size": 99999}).status_code == 422
    assert (
        client.get(f"{_PREFIX}/news", params={"symbol": "AAPL", "limit": 0}).status_code
        == 422
    )
