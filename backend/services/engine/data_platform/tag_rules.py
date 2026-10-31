"""个股终端智能标签规则引擎

11 大类 + 25 个常用组合预设。基于 QuantDB 本地最新交易日快照
（instrument_detail + technical_indicators + valuation + l1_factors + l2_factors + margin_trading），
按 symbol 合并后向量化匹配。进程内缓存 5 分钟。

规则 = {id, name, category, desc, cond: (metric, op, threshold), sort_key, sort_asc}
metric 在 _METRICS 中定义；op ∈ {gt, lt, ge, le, between}。
preset 引用多个 tag id，logic ∈ {any, all}。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# 指标定义（快照 DataFrame 的列 -> 业务指标）
# ---------------------------------------------------------------------------

def _build_metrics() -> pd.DataFrame:
    """全市场最新快照（按 symbol 合并），返回指标 DataFrame。"""
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    hub = QuantDBDataHub.get_instance()
    conn = hub._get_duck_conn()

    def latest_rows(view: str, cols: list[str]) -> pd.DataFrame:
        try:
            mx = conn.execute(f"SELECT MAX(dt) FROM {view}").fetchone()[0]
            if mx is None:
                return pd.DataFrame()
            col_list = ", ".join(cols)
            return conn.execute(
                f"SELECT symbol, {col_list} FROM {view} WHERE dt = {mx}"
            ).fetchdf()
        except Exception:  # noqa: BLE001
            return pd.DataFrame()

    ti = latest_rows("qdb_technical_indicators", [
        "close", "pct_change", "vol_std_20", "vol_atr_14", "return_5d", "return_20d",
        "return_60d", "ma_gap_20", "rsi_14", "vol_to_ma5", "vol_to_ma20",
        "volume_trend_3d", "macd_hist", "beta_20",
    ])
    # 注：schema_adapter 已将 parquet 的 future_return_Nd 别名为 return_Nd，
    # 规则阈值仍按百分数口径（与改名前一致）。
    val = latest_rows("qdb_valuation", [
        "pe_ttm", "pe_static", "pb", "ps_ttm", "dividend_rate", "total_mv",
        "float_mv", "net_profit_ttm", "revenue_ttm", "equity",
    ])
    l1 = latest_rows("qdb_l1_factors", [
        "chip_profit_ratio_20", "chip_profit_ratio_60", "chip_concentration_20",
        "chip_cost_90_width", "concept_hot_score", "concept_rotation_score",
        "concept_crowding_max", "ind_strength_20", "ind_rotation_speed_20",
        "ind_relative_momentum_20", "ind_crowding_20", "fun_roe", "fun_peg",
        "turn_20", "fun_mv_rank", "amt_ratio_5_20", "mom_ret_5d",
        "mom_ret_20d", "mom_ret_60d", "style_beta_20",
        "tech_bb_width", "tech_bb_pos", "tech_cci_20", "tech_adx_14",
        "vol_parkinson_20",
    ])
    senti = latest_rows("qdb_market_sentiment", [
        "buy_pressure", "sell_pressure", "liquidity_score",
    ])
    margin = latest_rows("qdb_margin_trading", ["finance_balance", "finance_net"])
    l2 = latest_rows("qdb_l1_l2_factors", [
        "flow_net_amount", "flow_net_ratio", "flow_super_net",
    ])

    def merge(dfs: list[pd.DataFrame]) -> pd.DataFrame:
        out = pd.DataFrame()
        for df in dfs:
            if df is None or df.empty:
                continue
            sym_col = "symbol" if "symbol" in df.columns else "Symbol"
            df = df.rename(columns={sym_col: "symbol"})
            out = df if out.empty else out.merge(df, on="symbol", how="outer")
        return out

    df = merge([ti, val, l1, senti, margin, l2])
    if df.empty:
        return df

    # instrument_detail 静态列（行业/板块/标识）
    d = hub.data_dir
    detail_file = d / "2_base_sector" / "instrument_detail" / "instrument_list.parquet"
    if not detail_file.exists():
        detail_file = d / "2_base_sector" / "instrument_detail" / "instrument_detail.parquet"
    if detail_file.exists():
        try:
            det_cols = ["Symbol", "Name", "rs_hyname", "BelongHS300", "Zsz", "Ltsz", "DynaPE", "PB_MRQ"]
            _all = pd.read_parquet(detail_file)
            det = _all[[c for c in det_cols if c in _all.columns]]
            det = det.rename(columns={"Symbol": "symbol"})
            df = df.merge(det, on="symbol", how="left")
        except Exception:  # noqa: BLE001
            pass

    # 宽基指数归属列
    index_map = {"000300.SH": "hs300_idx", "000905.SH": "zz500_idx",
                 "000852.SH": "zz1000_idx", "000016.SH": "sz50_idx",
                 "000688.SH": "kc50_idx", "399006.SZ": "cyb_idx"}
    weights_dir = d / "2_base_sector" / "index_weights"
    if weights_dir.exists():
        for code, col in index_map.items():
            f = weights_dir / f"{code}.parquet"
            if not f.exists():
                df[col] = 0
                continue
            try:
                w = pd.read_parquet(f)
                sym_col = "Symbol" if "Symbol" in w.columns else "symbol"
                members = set(w[sym_col].astype(str))
                df[col] = df["symbol"].astype(str).isin(members).astype(int)
            except Exception:  # noqa: BLE001
                df[col] = 0

    # 派生指标
    def num(s: pd.Series) -> pd.Series:
        return pd.to_numeric(s, errors="coerce")

    _mv = df["total_mv"] if "total_mv" in df.columns else df.get("Zsz")
    _fiv = df["float_mv"] if "float_mv" in df.columns else df.get("Ltsz")
    df["total_mv_yi"] = num(_mv) / 1e8
    df["float_mv_yi"] = num(_fiv) / 1e8
    df["margin_ratio"] = num(df.get("finance_balance")) / df["float_mv_yi"] * 100  # 融资余额/流通市值 %
    df["_st"] = df.get("Name", pd.Series(index=df.index)).astype(str).str.contains("ST", na=False)
    df["sentiment"] = num(df.get("buy_pressure")) - num(df.get("sell_pressure"))
    df["growth_score"] = (num(df.get("mom_ret_20d")) + num(df.get("mom_ret_60d"))) / 2 * 100
    df["roe_ok"] = num(df.get("fun_roe")) > 8
    df["peg_ok"] = num(df.get("fun_peg")) < 1.5

    return df


# ---------------------------------------------------------------------------
# 标签定义
# ---------------------------------------------------------------------------

_metrics_cache: dict[str, Any] = {"df": None, "ts": 0.0}
# 标签命中结果缓存：stocks_for_tag 每次调用都 df.apply 全市场逐行（1-3s 同步），
# 在列表接口中被反复调用会拖垮并发——按 (tag_id, limit) 缓存结果，TTL 与指标缓存一致
_tag_stocks_cache: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}
# 最近信号日全市场分数缓存：并发 tag 请求共用一次 psycopg2 查询
_scores_cache: dict[str, Any] = {"v": None, "ts": 0.0}
# 指标重建锁：_build_metrics 读 8+ parquet，并发过期时只重建一次
_metrics_lock = threading.Lock()
# 标签命中锁：stocks_for_tag 的 apply 全市场逐行昂贵，并发时只算一次
_tag_stocks_lock = threading.Lock()
_TTL = 300.0


def _get_metrics() -> pd.DataFrame:
    now = time.time()
    if _metrics_cache["df"] is not None and now - _metrics_cache["ts"] < _TTL:
        return _metrics_cache["df"]
    with _metrics_lock:
        # 双重检查：并发请求同时过期时只重建一次（读 8+ parquet 很贵）
        now = time.time()
        if _metrics_cache["df"] is not None and now - _metrics_cache["ts"] < _TTL:
            return _metrics_cache["df"]
        df = _build_metrics()
        _metrics_cache.update({"df": df, "ts": now})
        return df


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


class TagRule:
    def __init__(self, tid: str, name: str, category: str, desc: str,
                 cond: tuple[str, str, float] | None,
                 sort_key: str | None = None, sort_asc: bool = False):
        self.id = tid
        self.name = name
        self.category = category
        self.desc = desc
        self.cond = cond  # (metric, op, threshold)
        self.sort_key = sort_key
        self.sort_asc = sort_asc

    def match(self, r: pd.Series) -> tuple[bool, float | None]:
        if self.cond is None:
            return True, None
        metric, op, th = self.cond
        v = _num(r.get(metric))
        if v is None:
            return False, None
        if op == "gt":
            ok = v > th
        elif op == "lt":
            ok = v < th
        elif op == "ge":
            ok = v >= th
        elif op == "le":
            ok = v <= th
        elif op == "between":
            ok = th <= v <= th + 1
        else:
            ok = False
        return ok, v


TAGS: list[TagRule] = [
    # ---- 宽基指数 ----
    TagRule("hs300", "沪深300成分", "宽基指数", "沪深300指数成分股", ("hs300_idx", "gt", 0), sort_key="total_mv_yi", sort_asc=False),
    TagRule("zz500", "中证500成分", "宽基指数", "中证500指数成分股", ("zz500_idx", "gt", 0), sort_key="total_mv_yi", sort_asc=False),
    TagRule("zz1000", "中证1000成分", "宽基指数", "中证1000指数成分股", ("zz1000_idx", "gt", 0), sort_key="total_mv_yi", sort_asc=False),
    TagRule("sz50", "上证50成分", "宽基指数", "上证50指数成分股", ("sz50_idx", "gt", 0), sort_key="total_mv_yi", sort_asc=False),
    TagRule("kc50", "科创50成分", "宽基指数", "科创50指数成分股", ("kc50_idx", "gt", 0), sort_key="total_mv_yi", sort_asc=False),
    TagRule("cyb", "创业板指成分", "宽基指数", "创业板指成分股", ("cyb_idx", "gt", 0), sort_key="total_mv_yi", sort_asc=False),

    # ---- 规模与流动性 ----
    TagRule("large_cap", "超大盘蓝筹", "规模与流动性", "总市值>3000亿", ("total_mv_yi", "gt", 3000), sort_key="total_mv_yi"),
    TagRule("mid_cap", "中盘股", "规模与流动性", "总市值300~3000亿", ("total_mv_yi", "between", 300), sort_key="total_mv_yi"),
    TagRule("small_cap", "小盘股", "规模与流动性", "总市值<100亿", ("total_mv_yi", "lt", 100), sort_key="total_mv_yi", sort_asc=True),
    TagRule("active_small", "小盘活跃", "规模与流动性", "小市值+高换手", ("turn_20", "gt", 5), sort_key="fun_turnover_20"),
    TagRule("liquid", "流动性筛选", "规模与流动性", "流动性评分靠前", ("amt_ratio_5_20", "gt", 1.2), sort_key="liq_amount_ratio_5"),

    # ---- 行业板块 ----
    TagRule("industry_strong", "行业强势", "行业板块", "行业强度高", ("ind_strength_20", "gt", 60), sort_key="ind_strength_20"),
    TagRule("industry_rotation", "行业轮动", "行业板块", "行业轮动加速", ("ind_rotation_speed_20", "gt", 55), sort_key="ind_rotation_speed_20"),
    TagRule("sector_hot", "行业热度", "行业板块", "行业拥挤度高", ("ind_crowding_20", "gt", 55), sort_key="ind_crowding_20"),

    # ---- 价值与成长 ----
    TagRule("low_pe_bluechip", "低估值蓝筹", "价值与成长", "PE(TTM)<15 且大市值", ("pe_ttm", "lt", 15), sort_key="pe_ttm", sort_asc=True),
    TagRule("high_roe_growth", "高ROE成长", "价值与成长", "ROE>15 且 PEG<1.5", ("fun_roe", "gt", 15), sort_key="fun_roe"),
    TagRule("pb_broken", "破净价值", "价值与成长", "PB<1 破净", ("pb", "between", 0), sort_key="pb", sort_asc=True),
    TagRule("high_dividend", "高股息价值", "价值与成长", "股息率>3%", ("dividend_rate", "gt", 0.03), sort_key="dividend_rate"),
    TagRule("beta_style", "高Beta风格", "价值与成长", "Beta>1.1", ("style_beta_20", "gt", 1.1), sort_key="style_value_20"),
    TagRule("low_vol_quality", "低波质量", "价值与成长", "低波动+高ROE", ("vol_parkinson_20", "lt", 0.03), sort_key="vol_parkinson_20", sort_asc=True),
    TagRule("gross_margin_quality", "高毛利稳健", "价值与成长", "高毛利稳健", ("gross_margin", "gt", 40), sort_key="gross_margin"),

    # ---- 技术形态 ----
    TagRule("breakout", "突破强势", "技术形态", "20日动量强", ("return_20d", "gt", 10), sort_key="return_20d"),
    TagRule("oversold_rebound", "超跌反弹", "技术形态", "60日跌深+RSI低", ("return_60d", "lt", -10), sort_key="return_60d", sort_asc=True),
    TagRule("momentum", "动量强势", "技术形态", "5日动量强", ("mom_ret_5d", "gt", 5), sort_key="mom_ret_5d"),
    TagRule("trend_break", "趋势突破", "技术形态", "站上MA20且放量", ("ma_gap_20", "gt", 0), sort_key="ma_gap_20"),
    TagRule("boll_tight", "布林收口", "技术形态", "布林带收口(低波动蓄势)", ("tech_bb_width", "lt", 0.04), sort_key="tech_bb_width", sort_asc=True),
    TagRule("volume_breakout", "放量突破", "技术形态", "量能放大", ("vol_to_ma5", "gt", 1.5), sort_key="vol_to_ma5"),
    TagRule("rsi_oversold", "RSI超卖", "技术形态", "RSI14<30", ("rsi_14", "lt", 30), sort_key="rsi_14", sort_asc=True),

    # ---- 资金趋势 ----
    TagRule("main_flow_in", "主力净流入", "资金趋势", "主力资金净流入", ("flow_net_amount", "gt", 0), sort_key="flow_net_amount"),
    TagRule("fund_inflow", "资金流入", "资金趋势", "资金净流入比例高", ("flow_net_ratio", "gt", 0.05), sort_key="flow_net_ratio"),

    # ---- 主题热点 ----
    TagRule("concept_hot", "概念热点", "主题热点", "概念热度高", ("concept_hot_score", "gt", 55), sort_key="concept_hot_score"),
    TagRule("concept_rotation", "概念轮动", "主题热点", "概念轮动加速", ("concept_rotation_score", "gt", 55), sort_key="concept_rotation_score"),
    TagRule("concept_crowded", "概念拥挤", "主题热点", "概念拥挤度高", ("concept_crowding_max", "gt", 60), sort_key="concept_crowding_max"),

    # ---- 筹码分析 ----
    TagRule("chip_concentrated", "筹码集中", "筹码分析", "筹码集中度高", ("chip_concentration_20", "lt", 0.5), sort_key="chip_concentration_20", sort_asc=True),
    TagRule("chip_profit_high", "获利盘多", "筹码分析", "获利盘比例高", ("chip_profit_ratio_20", "gt", 80), sort_key="chip_profit_ratio_20"),

    # ---- 市场情绪 ----
    TagRule("sentiment_hot", "情绪回暖", "市场情绪", "市场情绪指标上行", ("sentiment", "gt", 0), sort_key="sentiment"),

    # ---- 融资融券 ----
    TagRule("margin_net_in", "融资加仓", "融资融券", "融资净买入为正", ("finance_net", "gt", 0), sort_key="finance_net"),
    TagRule("margin_high", "高杠杆持仓", "融资融券", "融资余额占比高", ("margin_ratio", "gt", 5), sort_key="margin_ratio"),

    # ---- 因子选股 ----
    TagRule("alpha112", "因子Alpha112", "因子选股", "Alpha112 因子筛选", ("alpha112", "gt", 0), sort_key="alpha112"),
]


# 25 个常用组合预设（引用 tag id；logic=all 全部满足 / any 任一满足）
PRESETS: list[dict[str, Any]] = [
    {"id": "preset_low_pe", "name": "低估值蓝筹", "tags": ["low_pe_bluechip"], "logic": "all"},
    {"id": "preset_roe_growth", "name": "高ROE成长", "tags": ["high_roe_growth"], "logic": "all"},
    {"id": "preset_small_active", "name": "小盘活跃", "tags": ["active_small", "momentum"], "logic": "any"},
    {"id": "preset_mega_cap", "name": "超大盘蓝筹", "tags": ["large_cap"], "logic": "all"},
    {"id": "preset_pb_broken", "name": "破净价值", "tags": ["pb_broken"], "logic": "all"},
    {"id": "preset_breakout", "name": "突破强势", "tags": ["breakout", "trend_break"], "logic": "all"},
    {"id": "preset_oversold", "name": "超跌反弹", "tags": ["oversold_rebound", "rsi_oversold"], "logic": "any"},
    {"id": "preset_hs300_lowpe", "name": "沪深300+低PE", "tags": ["hs300", "low_pe_bluechip"], "logic": "all"},
    {"id": "preset_vol_breakout", "name": "放量突破", "tags": ["volume_breakout", "trend_break"], "logic": "all"},
    {"id": "preset_main_flow", "name": "主力净流入", "tags": ["main_flow_in"], "logic": "all"},
    {"id": "preset_margin_add", "name": "融资加仓", "tags": ["margin_net_in"], "logic": "all"},
    {"id": "preset_fund_inflow", "name": "资金流入", "tags": ["fund_inflow"], "logic": "all"},
    {"id": "preset_dividend", "name": "高股息价值", "tags": ["high_dividend"], "logic": "all"},
    {"id": "preset_gross_margin", "name": "高毛利稳健", "tags": ["gross_margin_quality"], "logic": "all"},
    {"id": "preset_chip", "name": "筹码集中", "tags": ["chip_concentrated"], "logic": "all"},
    {"id": "preset_main_accum", "name": "主力吸筹", "tags": ["main_flow_in", "chip_profit_high"], "logic": "all"},
    {"id": "preset_ind_strong", "name": "行业强势", "tags": ["industry_strong"], "logic": "all"},
    {"id": "preset_ind_rotation", "name": "行业轮动", "tags": ["industry_rotation"], "logic": "all"},
    {"id": "preset_beta_style", "name": "价值风格", "tags": ["beta_style"], "logic": "all"},
    {"id": "preset_low_vol", "name": "低波质量", "tags": ["low_vol_quality"], "logic": "all"},
    {"id": "preset_liquid", "name": "流动性筛选", "tags": ["liquid"], "logic": "all"},
    {"id": "preset_momentum", "name": "动量强势", "tags": ["momentum"], "logic": "all"},
    {"id": "preset_concept", "name": "概念热点", "tags": ["concept_hot"], "logic": "all"},
    {"id": "preset_trend", "name": "趋势突破", "tags": ["trend_break", "volume_breakout"], "logic": "any"},
    {"id": "preset_boll", "name": "布林收口", "tags": ["boll_tight"], "logic": "all"},
]


def get_tag_by_id(tid: str) -> TagRule | None:
    return next((t for t in TAGS if t.id == tid), None)


# ST 排除标签集（基本面/技术/资金类），宽基指数归属不受影响
_EXCLUDE_ST_CATEGORIES = {"宽基指数", "规模与流动性"}


def match_tags_for_symbol(symbol: str, max_tags: int = 24) -> list[dict[str, Any]]:
    """个股命中标签（含命中值+阈值）。"""
    df = _get_metrics()
    if df.empty:
        return []
    r = df[df["symbol"] == symbol]
    if r.empty:
        return []
    row = r.iloc[0]
    out: list[dict[str, Any]] = []
    is_st = bool(row.get("_st"))
    for t in TAGS:
        if is_st and t.category not in _EXCLUDE_ST_CATEGORIES:
            continue
        ok, v = t.match(row)
        if ok:
            out.append({"id": t.id, "name": t.name, "category": t.category,
                        "desc": t.desc, "value": v})
    return out[:max_tags]


def preset_matched(symbol: str) -> list[dict[str, Any]]:
    """命中的组合预设。"""
    df = _get_metrics()
    if df.empty:
        return []
    r = df[df["symbol"] == symbol]
    if r.empty:
        return []
    row = r.iloc[0]
    out: list[dict[str, Any]] = []
    for p in PRESETS:
        hits = [t for t in p["tags"] if get_tag_by_id(t) and get_tag_by_id(t).match(row)[0]]
        ok = len(hits) == len(p["tags"]) if p["logic"] == "all" else len(hits) > 0
        if ok:
            out.append({"id": p["id"], "name": p["name"], "matched": len(hits), "total": len(p["tags"])})
    return out


def _latest_signal_scores() -> tuple[dict[str, dict], float, float]:
    """默认模型最近交易日 全市场 fusion/side（纯数字 symbol -> {fusion, side, date}）。

    返回 (scores, min, max)：min/max 为该模型当日全市场分数极值，供前端
    按当前模型动态归一化显示（固定 ×100 对分数量级不同的模型失真）。
    同步 psycopg2 直连（本函数在 to_thread 中调用，不可用 async 会话）。
    结果缓存 _TTL：并发 tag 请求共用一次查询，避免连接风暴。
    """
    now = time.time()
    cached = _scores_cache.get("v")
    if cached is not None and now - _scores_cache["ts"] < _TTL:
        return cached

    import os
    import urllib.parse

    import psycopg2
    import psycopg2.extras

    db_url = os.getenv("DATABASE_URL", "")
    if db_url:
        # psycopg2 不认 asyncpg 协议前缀
        db_url = db_url.replace("postgresql+asyncpg", "postgresql").replace("postgresql+psycopg2", "postgresql")
    if not db_url:
        db_url = (
            f"postgresql://{os.getenv('DB_USER')}:{urllib.parse.quote_plus(os.getenv('DB_PASSWORD',''))}"
            f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
        )
    model_join = (
        "JOIN qm_model_inference_runs r ON r.run_id = e.run_id "
        "JOIN qm_user_models u ON u.model_id = r.model_id AND u.is_default = TRUE"
    )
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT MAX(e.trade_date) FROM engine_signal_scores e {model_join} "
                "WHERE e.tenant_id='default'"
            )
            d0 = cur.fetchone()
            if d0 is None or d0[0] is None:
                return {}, 0.0, 0.0
            d0 = d0[0]
            cur.execute(
                f"SELECT e.symbol, e.fusion_score, e.signal_side "
                f"FROM engine_signal_scores e {model_join} "
                "WHERE e.tenant_id='default' AND e.trade_date=%s",
                (d0,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    out: dict[str, dict] = {}
    vals: list[float] = []
    for r in rows:
        fv = float(r[1]) if r[1] is not None else None
        out[str(r[0])] = {"fusion": fv, "side": str(r[2] or "HOLD"), "date": str(d0)[:10]}
        if fv is not None:
            vals.append(fv)
    lo = min(vals) if vals else 0.0
    hi = max(vals) if vals else 0.0
    result = (out, lo, hi)
    _scores_cache["v"] = result
    _scores_cache["ts"] = time.time()
    return result


def stocks_for_tag(tag_id: str, limit: int = 50) -> dict[str, Any]:
    """标签同类股票（全市场匹配后按 sort_key 排序，叠加默认模型推理分数）。

    返回 {"items": [...], "score_min": lo, "score_max": hi}：min/max 为
    默认模型当日全市场分数极值，前端按当前模型动态归一化显示分数。
    结果按 (tag_id, limit) 缓存 _TTL 秒——df.apply 逐行匹配开销大且列表接口
    每次筛选都调用，不缓存会让连续筛选重复全市场扫描。锁内双重检查：
    并发请求同时过期时只执行一次 apply，其余等待后直接取缓存。
    """
    now = time.time()
    cached = _tag_stocks_cache.get((tag_id, limit))
    if cached is not None and now - cached[0] < _TTL:
        return cached[1]

    with _tag_stocks_lock:
        now = time.time()
        cached = _tag_stocks_cache.get((tag_id, limit))
        if cached is not None and now - cached[0] < _TTL:
            return cached[1]

        tag = get_tag_by_id(tag_id)
        if tag is None:
            raise ValueError(f"未知标签 {tag_id}")
        df = _get_metrics()
        if df.empty:
            return {"items": [], "score_min": None, "score_max": None}
        mask = df.apply(lambda r: tag.match(r)[0], axis=1)
        hit = df[mask].copy()
        if tag.category not in _EXCLUDE_ST_CATEGORIES and "_st" in hit.columns:
            hit = hit[~hit["_st"].fillna(False)]
        if tag.sort_key and tag.sort_key in hit.columns:
            hit = hit.sort_values(tag.sort_key, ascending=tag.sort_asc)
        scores, lo, hi = _latest_signal_scores()
        out: list[dict[str, Any]] = []
        for _, r in hit.head(limit).iterrows():
            _, v = tag.match(r)
            sym = str(r.get("symbol") or "")
            code = sym.split(".")[0]
            sc = scores.get(sym) or scores.get(code) or {}
            out.append({
                "symbol": sym,
                "name": r.get("Name"),
                "industry": r.get("rs_hyname"),
                "close": _num(r.get("close")),
                "pct_change": _num(r.get("pct_change")),
                "total_mv": _num(r.get("total_mv_yi")) or _num(r.get("Zsz")),
                "metric": v,
                "fusion": sc.get("fusion"),
                "side": sc.get("side"),
                "signal_date": sc.get("date"),
            })
        result: dict[str, Any] = {"items": out, "score_min": lo, "score_max": hi}
        _tag_stocks_cache[(tag_id, limit)] = (time.time(), result)
        return result
