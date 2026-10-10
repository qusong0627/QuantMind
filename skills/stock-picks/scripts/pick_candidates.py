#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多维度选股候选生成（每日复盘后）

把每日复盘产物（市场方向 facts + 新闻情绪 news.json）+ 模型推理信号 + 个股深分因子，
合成一个多维候选池，输出未来几天大概率走强的股票排名。

数据源（全部宿主机可直达）：
  - PG `engine_signal_scores`：融合分数 fusion_score / 信号方向 signal_side /
    quality(json: position.position_score / position.pct_industry /
    position.industry_top10_avg / consensus)
  - PG `stock_aliases`：symbol -> 股票名
  - `data/reports/daily_review/{date}_news.json`：当日新闻情绪（news_review.py 产物，
    缺失则该维度中性）
  - QuantDB `6_ml_datasets/l2_factors`：订单微结构截面分位（正 IC 高=资金活跃，
    负 IC 反转计入；缺失则跳过）

打分：L2 微观结构为主(40%)，模型融合分其次(30%)，趋势不纳入；
`--window N` 跨 N 个推理日聚合（每股取跨日复合分均值），`--window 1` 为严格单日。

用法：
  python3 pick_candidates.py --top 20
  python3 pick_candidates.py --data-date 20260819 --window 3 --top 20 --json
  python3 pick_candidates.py --data-date 20260819 --window 1 --top 20 --json   # 严格单日
  python3 pick_candidates.py --no-l2            # 跳过 L2 维度（更快）

输出：
  data/reports/stock_picks/{date}_picks.json   全量候选 + 各维度分解
  data/reports/stock_picks/{date}_picks.md     排名表 Markdown 骨架（供报告模板引用）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime

import duckdb
import pandas as pd
import psycopg2

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# 默认信号日覆盖阈值：distinct symbol >= 该值视为「全量推理日」（与 stock_terminal 一致）
_MIN_SIGNAL_COVERAGE = 1000

# 多维度权重（满分 1.0）：L2 微观结构为主(50%)，融合分其次(25%)，L1 动量(15%)，
# 仓位/板块少量确认，新闻极少有数据故权重 0；趋势不纳入
# 支持环境变量覆盖（调参用）：W_L2 / W_FUSION / W_L1 / W_POSITION / W_SECTOR / W_NEWS
W_FUSION = float(os.getenv("W_FUSION", "0.25"))
W_POSITION = float(os.getenv("W_POSITION", "0.05"))
W_TREND = 0.0
W_SECTOR = float(os.getenv("W_SECTOR", "0.05"))
W_NEWS = float(os.getenv("W_NEWS", "0.0"))
W_L2 = float(os.getenv("W_L2", "0.50"))
W_L1 = float(os.getenv("W_L1", "0.15"))

# 硬过滤：仓位门（position_score>0 或 行业百分位>=80%），避免大盘空仓日推满仓
_POSITION_GATE = 0.8

# L2 正 IC 因子（IC 方向经《L2 微观结构因子系统化分析报告》验证；列缺失则跳过该列）
L2_POS_FACTORS = [
    "micro_vpin_vol_ratio", "flow_order_duration_p90", "micro_vpin_amount_ratio",
    "flow_cancel_lifetime", "micro_trade_interval_mean", "micro_vpin_50",
    "micro_vpin_ma_20", "micro_informed_ratio",
]

# L2 负 IC 因子：高分位=利空（波动持续/毒性/追单/对倒密集），L2 健康分把其反转计入
L2_NEG_FACTORS = [
    "vol_persistence", "flow_buy_amount", "flow_sell_amount",
    "micro_toxicity_persistence", "flow_order_arrival_rate",
    "micro_trade_arrival_rate", "vol_tick_density", "vol_realized_jump",
]

# IC 检验通过的 14 个推荐因子（db/feature_snapshots/l2_recommended_factors.csv，
# 全部正 IC：因子值越高未来收益越高；权重=ICIR，2026-08-21 检验，主 horizon T+3）
RECOMMENDED_L2_FACTORS: dict[str, float] = {
    "micro_vpin_vol_ratio": 0.562,
    "micro_vpin_amount_ratio": 0.483,
    "micro_zone_distribution": 0.417,
    "micro_zone_vol_ratio_T4": 0.345,
    "micro_zone_vol_ratio_T6": 0.338,
    "vol_price_divergence": 0.332,
    "micro_zone_vol_ratio_T5": 0.316,
    "micro_open_gap": 0.273,
    "micro_impact_decay_half_life": 0.271,
    "micro_liquidity_daily_pattern": 0.237,
    "micro_zone_vol_ratio_T3": 0.198,
    "flow_imbalance_revert_speed": 0.161,
    "micro_pin": 0.159,
    "micro_zone_rv_ratio_close": 0.156,
}

# L1 动量/量能因子（正 IC 方向：高动量+温和换手 = 相对强势；用于动量确认维度）
L1_MOMENTUM_FACTORS = ["mom_ret_5d", "mom_ret_10d", "turn_ratio_1_5", "amt_ratio_1_5"]

# IC 检验报告路径候选（运行时动态读取，优于硬编码：重跑检验自动跟进）
_IC_REPORT_PATHS = [
    "db/feature_snapshots/l2_factor_eval_report.csv",
    "/app/db/feature_snapshots/l2_factor_eval_report.csv",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "db", "feature_snapshots", "l2_factor_eval_report.csv"),
]
# IC 通过阈值（与 evaluate_factors.py 一致）
_IC_MIN_ICIR = 0.15
_IC_MIN_POS_RATIO = 0.55
_ic_factor_cache: dict[str, float] | None = None
_ic_neg_factor_cache: dict[str, float] | None = None
# 因子集规模：None=全部 IC 通过；N=按 ICIR 取前 N（去冗余后 14 最优 vs 全 55 含噪音，
# 深度调参用；默认 None）
IC_TOP_N: int | None = None
# 反向（负 IC）因子：取 |ICIR| 最强的前 N 个做「微结构风险罚分」（值越高越利空）
IC_NEG_TOP = int(os.getenv("IC_NEG_TOP", "15"))
# 风险罚分权重（从综合分扣除）
W_RISK = float(os.getenv("W_RISK", "0.20"))


# 反向（负 IC）因子兜底：IC 检验报告缺失时用（值越高=波动持续/对倒/恐慌越强，越利空）
RECOMMENDED_L2_NEG_FACTORS: dict[str, float] = {
    "vol_persistence": 0.677,
    "flow_buy_amount": 0.524,
    "flow_sell_amount": 0.516,
    "micro_jump_count_1pct": 0.509,
    "micro_toxicity_persistence": 0.503,
    "micro_close_squeeze": 0.485,
    "flow_order_arrival_rate": 0.480,
    "micro_trade_interval_cv": 0.469,
    "micro_trade_arrival_rate": 0.469,
    "vol_tick_density": 0.468,
    "vol_realized_jump": 0.459,
    "vol_realized_rrv": 0.454,
    "micro_jump_count_05pct": 0.452,
    "micro_vpin_zscore_20": 0.450,
    "vol_realized_10min": 0.437,
}


def load_ic_neg_factors() -> dict[str, float]:
    """反向（负 IC）因子：{factor: |icir|}，取检验报告 |ICIR| 最强前 N。

    找不到报告时回退硬编码 RECOMMENDED_L2_NEG_FACTORS。这些因子高分位=利空，
    用于「微结构风险罚分」。
    """
    global _ic_neg_factor_cache
    if _ic_neg_factor_cache is not None:
        return _ic_neg_factor_cache
    import csv
    for path in _IC_REPORT_PATHS:
        if os.path.exists(path):
            try:
                factors: dict[str, float] = {}
                for r in csv.DictReader(open(path)):
                    try:
                        ic = float(r["ic_mean"])
                        icir = float(r["icir"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    if ic < 0 and icir < -_IC_MIN_ICIR:
                        factors[r["factor"]] = -icir
                if factors:
                    factors = dict(sorted(factors.items(), key=lambda kv: -kv[1])[: IC_NEG_TOP])
                    _ic_neg_factor_cache = factors
                    return factors
            except Exception:
                continue
    _ic_neg_factor_cache = dict(RECOMMENDED_L2_NEG_FACTORS)
    return _ic_neg_factor_cache


def load_ic_factors() -> dict[str, float]:
    """L2 因子集（{factor: icir}）。

    默认返回去冗余 14 个（RECOMMENDED_L2_FACTORS，14 天深度调参实测最优，见 backtest）。
    显式设 IC_TOP_N>0 时读 IC 检验报告取 ICIR 最高的前 N（深挖/实验用）。
    """
    global _ic_factor_cache
    if _ic_factor_cache is not None:
        return _ic_factor_cache
    if IC_TOP_N is None:
        _ic_factor_cache = dict(RECOMMENDED_L2_FACTORS)
        return _ic_factor_cache
    import csv
    for path in _IC_REPORT_PATHS:
        if os.path.exists(path):
            try:
                factors: dict[str, float] = {}
                for r in csv.DictReader(open(path)):
                    try:
                        icir = float(r["icir"])
                        net = float(r["net_ic"])
                        pos = float(r.get("ic_positive_ratio") or 0)
                    except (KeyError, ValueError, TypeError):
                        continue
                    if icir > _IC_MIN_ICIR and net > 0 and pos > _IC_MIN_POS_RATIO:
                        factors[r["factor"]] = icir
                if factors:
                    factors = dict(sorted(factors.items(), key=lambda kv: -kv[1])[: IC_TOP_N])
                    _ic_factor_cache = factors
                    return factors
            except Exception:
                continue
    _ic_factor_cache = dict(RECOMMENDED_L2_FACTORS)
    return _ic_factor_cache

# 默认推理模型：L2 单模型（CatBoost T+5，a44568f2）——按用户要求用 L2 单个模型推理分，
# 不用融合分；该模型覆盖 2026 年 1-5 月 + 8 月全量。无其 run 的日期回退覆盖最大模型。
_DEFAULT_DAILY_MODEL = "mdl_cn_train_20260820100348_a44568f2_f81f5685"


def pg_connect() -> psycopg2.extensions.connection:
    """连接推理信号 PG。宿主机默认 127.0.0.1:5432（quantmind-db 端口映射）。"""
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "quantmind"),
        password=os.getenv("POSTGRES_PASSWORD", "quantmind2026"),
        dbname=os.getenv("POSTGRES_DB", "quantmind"),
        connect_timeout=5,
    )


def latest_full_signal_date(cur) -> date | None:
    """最近一个覆盖充分的推理日；无达标日回退最近任意有分数日。"""
    cur.execute(
        "SELECT trade_date FROM engine_signal_scores "
        "WHERE tenant_id='default' GROUP BY trade_date "
        "HAVING COUNT(DISTINCT symbol) >= %s "
        "ORDER BY trade_date DESC LIMIT 1",
        (_MIN_SIGNAL_COVERAGE,),
    )
    d = cur.fetchone()
    if d:
        return d[0]
    cur.execute(
        "SELECT MAX(trade_date) FROM engine_signal_scores WHERE tenant_id='default'"
    )
    d = cur.fetchone()
    return d[0] if d and d[0] else None


def resolve_primary_run(cur, data_date: date, model_id: str | None = None) -> str | None:
    """按推理数据日取单一主 run（默认日推模型优先，保证单模型、可复现）。"""
    return _resolve_run(cur, "data_trade_date", data_date, model_id)


def resolve_run_by_prediction(cur, buy_date: date, model_id: str | None = None) -> str | None:
    """按信号日（预测日）取默认日推模型的单一 run（默认模式用，避免混多模型）。"""
    return _resolve_run(cur, "prediction_trade_date", buy_date, model_id)


def _resolve_run(cur, col: str, d: date, model_id: str | None) -> str | None:
    if model_id:
        cur.execute(
            f"SELECT run_id FROM qm_model_inference_runs "
            f"WHERE {col}=%s AND model_id=%s AND status='completed' "
            f"ORDER BY created_at DESC LIMIT 1",
            (d, model_id),
        )
        r = cur.fetchone()
        if r:
            return r[0]
    cur.execute(
        f"SELECT run_id FROM qm_model_inference_runs "
        f"WHERE {col}=%s AND model_id=%s AND status='completed' "
        f"ORDER BY created_at DESC LIMIT 1",
        (d, _DEFAULT_DAILY_MODEL),
    )
    r = cur.fetchone()
    if r:
        return r[0]
    cur.execute(
        f"SELECT run_id FROM qm_model_inference_runs "
        f"WHERE {col}=%s AND status='completed' "
        f"ORDER BY (SELECT COUNT(*) FROM engine_signal_scores e WHERE e.run_id=qm_model_inference_runs.run_id) DESC "
        f"LIMIT 1",
        (d,),
    )
    r = cur.fetchone()
    return r[0] if r else None


def load_scores(cur, data_date: date | None, buy_date: date, model_id: str | None = None) -> dict[str, dict]:
    """当日全部有融合分数的股票：fusion / side / position / consensus / industry_top10_avg。

    始终取单一主 run（默认日推模型），杜绝同一天多模型 run 混用：
    data_date 指定时按推理数据日过滤（无泄露）；否则按信号日（预测日）过滤。
    """
    if data_date is not None:
        run_id = resolve_primary_run(cur, data_date, model_id)
    else:
        run_id = resolve_run_by_prediction(cur, buy_date, model_id)
    if run_id is None:
        return {}
    cur.execute(
        "SELECT symbol, fusion_score, signal_side, quality "
        "FROM engine_signal_scores "
        "WHERE tenant_id='default' AND run_id=%s",
        (run_id,),
    )
    out: dict[str, dict] = {}
    for sym, fusion, side, quality in cur.fetchall():
        sym = str(sym)
        q = quality or {}
        pos = (q.get("position") or {}) if isinstance(q, dict) else {}
        out[sym] = {
            "fusion": float(fusion),
            "side": str(side or "HOLD"),
            "position_score": _f(pos.get("position_score")),
            "pct_industry": _f(pos.get("pct_industry")),
            "industry_top10_avg": _f(pos.get("industry_top10_avg")),
            "consensus": int(q.get("consensus") or 0) if isinstance(q, dict) else 0,
        }
    return out




def load_names(cur) -> dict[str, str]:
    """股票名：QuantDB instrument_detail 优先（完整 universe），PG stock_aliases 兜底。
    engine_signal_scores.symbol 是纯代码(600884)，两种 key 都建索引。"""
    out: dict[str, str] = {}
    qs = _find_quantdb()
    if qs:
        p = os.path.join(qs, "2_base_sector", "instrument_detail", "instrument_detail.parquet")
        if os.path.exists(p):
            try:
                df = duckdb.connect().execute(
                    f"SELECT Symbol, Name FROM read_parquet('{p}')"
                ).df()
                for ticker, name in df.itertuples(index=False):
                    if name:
                        out[str(ticker)] = str(name)
                        out[str(ticker).split(".")[0]] = str(name)
            except Exception:
                pass
    try:
        cur.execute("SELECT ticker, alias FROM stock_aliases WHERE alias_type='name'")
        for ticker, name in cur.fetchall():
            if not name:
                continue
            out.setdefault(str(ticker), str(name))
            out.setdefault(str(ticker).split(".")[0], str(name))
    except Exception:
        pass
    return out


def load_sector_flow(data_date: date | None, buy_date: date) -> dict[str, float]:
    """复盘 stats 的板块超级大单净额（亿）：{行业: net_yi}。缺失返回空。
    用于反推：候选所处行业当日有无聪明钱净流入。"""
    base = os.getenv("QM_REPORT_DIR") or os.path.join(os.getenv("QM_REPORTS_DIR", "data/reports"), "daily_review")
    want = data_date or buy_date
    path = os.path.join(base, f"{want.strftime('%Y-%m-%d')}_stats.json")
    if not os.path.exists(path):
        return {}
    try:
        d = json.load(open(path))
        flows = d.get("factors", {}).get("sector_flow", [])
        return {str(x.get("industry")): float(x.get("net_yi") or 0.0) for x in flows}
    except Exception:
        return {}


def load_industries() -> dict[str, str]:
    """symbol(纯代码) -> 行业（申万一级，instrument_detail.rs_hyname）。"""
    qs = _find_quantdb()
    if not qs:
        return {}
    p = os.path.join(qs, "2_base_sector", "instrument_detail", "instrument_detail.parquet")
    if not os.path.exists(p):
        return {}
    try:
        df = duckdb.connect().execute(
            f"SELECT Symbol, rs_hyname FROM read_parquet('{p}')"
        ).df()
        out: dict[str, str] = {}
        for ticker, ind in df.itertuples(index=False):
            if ind:
                out[str(ticker).split(".")[0]] = str(ind)
        return out
    except Exception:
        return {}


def load_news(data_date: date | None, buy_date: date) -> dict[str, dict]:
    """新闻情绪（news_review.py 产物）。数据日指定时用数据日（无泄露：只用买入日前
    已知的新闻）；否则用信号日。缺失时返回空（新闻维度中性）。"""
    base = os.getenv("QM_REPORT_DIR") or os.path.join(os.getenv("QM_REPORTS_DIR", "data/reports"), "daily_review")
    want = data_date or buy_date
    path = os.path.join(base, f"{want.strftime('%Y-%m-%d')}_news.json")
    alt = os.path.join(base, f"{want.strftime('%Y%m%d')}_news.json")
    for p in (path, alt):
        if os.path.exists(p):
            try:
                d = json.load(open(p))
                return {
                    str(s.get("symbol")): {
                        "news_count": int(s.get("news_count") or 0),
                        "net_ratio": float(s.get("net_ratio") or 0.0),
                        "score_mean": float(s.get("score_mean") or 0.0),
                    }
                    for s in d.get("stocks", []) if s.get("symbol")
                }
            except Exception:
                return {}
    return {}


def load_market_direction(data_date: date | None, buy_date: date) -> dict | None:
    """复盘六维方向：{direction, total_score, data_completeness}；无复盘返回 None。

    当日无复盘时回退到 ≤ 该日的最近复盘（往前最多找 7 天），保证看空门不因
    复盘缺跑而漏掉暴跌日。
    """
    base = os.getenv("QM_REPORT_DIR") or os.path.join(os.getenv("QM_REPORTS_DIR", "data/reports"), "daily_review")
    want = data_date or buy_date
    from datetime import timedelta
    for back in range(0, 8):
        d = want - timedelta(days=back)
        for fmt in (d.strftime("%Y-%m-%d"), d.strftime("%Y%m%d")):
            path = os.path.join(base, f"{fmt}_stats.json")
            if os.path.exists(path):
                try:
                    dr = (json.load(open(path)).get("direction") or {})
                    if isinstance(dr, dict) and dr.get("direction"):
                        return {
                            "direction": dr["direction"],
                            "total_score": dr.get("total_score"),
                            # 旧键 `confidence` 仅作存量 stats JSON 的过渡回退（T6-4 改名 数据完整度）
                            "data_completeness": dr.get("data_completeness", dr.get("confidence")),
                            "review_date": fmt,
                        }
                except Exception:
                    continue
    return None


def load_l1(data_date: date | None, buy_date: date) -> dict[str, dict]:
    """L1 动量/量能因子截面分位（正 IC 方向：高动量+温和量能=相对强势）。"""
    qs = _find_quantdb()
    if not qs:
        return {}
    ds = os.path.join(qs, "6_ml_datasets", "l1_factors")
    if not os.path.isdir(ds):
        return {}
    pats = sorted(os.path.join(ds, p) for p in os.listdir(ds) if p.startswith("dt="))
    if not pats:
        return {}
    if data_date is not None:
        want = os.path.join(ds, f"dt={data_date.strftime('%Y%m%d')}")
        part = want if os.path.isdir(want) else _latest_before(pats, data_date)
    else:
        part = _latest_before(pats, buy_date)
    part = part or pats[-1]
    try:
        p = os.path.join(part, "data.parquet")
        df = duckdb.connect().execute(f"SELECT * FROM read_parquet('{p}') LIMIT 0").df()
        avail = [c for c in L1_MOMENTUM_FACTORS if c in df.columns]
        if not avail:
            return {}
        df = duckdb.connect().execute(
            f"SELECT symbol, {', '.join(avail)} FROM read_parquet('{p}')"
        ).df()
    except Exception:
        return {}
    out: dict[str, dict] = {}
    if df.empty:
        return out
    df = df.set_index("symbol")
    for col in avail:
        df[col + "_pct"] = df[col].astype(float).rank(pct=True)
    pct_cols = [c + "_pct" for c in avail]
    for sym, row in df[pct_cols].iterrows():
        vals = [v for v in row if pd.notna(v)]
        out[str(sym).split(".")[0]] = {"l1_score": float(sum(vals) / len(vals)) if vals else 0.5}
    return out


def load_l2(data_date: date | None, buy_date: date) -> dict[str, dict]:
    """订单微结构截面分位（正 IC 因子均值百分位）。分区缺失/读失败返回空。

    无泄露：data_date 指定时用 data_date 当日分区（周三分区，买入日前已存在）；
    未指定时用严格早于 buy_date 的最新分区（信号日当天的 L2 是买入日盘中数据，不能用）。
    """
    qs = _find_quantdb()
    if not qs:
        return {}
    ds = os.path.join(qs, "6_ml_datasets", "l2_factors")
    if not os.path.isdir(ds):
        return {}
    pats = sorted(os.path.join(ds, p) for p in os.listdir(ds) if p.startswith("dt="))
    if not pats:
        return {}
    # 选择分区：优先 data_date 当日；否则最接近且 < buy_date；再退最新
    if data_date is not None:
        want = os.path.join(ds, f"dt={data_date.strftime('%Y%m%d')}")
        part = want if os.path.isdir(want) else _latest_before(pats, data_date)
    else:
        part = _latest_before(pats, buy_date)
    part = part or pats[-1]
    return _read_l2_partition(part)


def _latest_before(pats: list[str], d: date) -> str | None:
    """分区列表里最晚一个 dt < d 的（无泄露：不能用 >= d 的分区）。"""
    dstr = d.strftime("%Y%m%d")
    prev = [p for p in pats if os.path.basename(p)[3:] < dstr]
    return prev[-1] if prev else None


def _read_l2_partition(part: str) -> dict[str, dict]:
    """按 IC 检验因子算微观结构分。

    l2_score = 正向（正 IC）因子 ICIR 加权截面分位（高=强）；
    l2_risk = 反向（负 IC）因子 |ICIR| 加权截面分位（高=波动持续/对倒/恐慌=利空，作风险罚分）。
    """
    ic_factors = load_ic_factors()
    ic_neg = load_ic_neg_factors()
    try:
        p = os.path.join(part, "data.parquet")
        df = duckdb.connect().execute(f"SELECT * FROM read_parquet('{p}') LIMIT 0").df()
        rec = [c for c in ic_factors if c in df.columns]
        rec_neg = [c for c in ic_neg if c in df.columns]
        pos_cols = [c for c in L2_POS_FACTORS if c in df.columns]
        neg_cols = [c for c in L2_NEG_FACTORS if c in df.columns]
        all_cols = list(dict.fromkeys(rec + rec_neg + pos_cols + neg_cols))
        if not all_cols:
            return {}
        df = duckdb.connect().execute(
            f"SELECT symbol, {', '.join(all_cols)} FROM read_parquet('{p}')"
        ).df()
    except Exception:
        return {}
    out: dict[str, dict] = {}
    if df.empty:
        return out
    df = df.set_index("symbol")
    for col in all_cols:
        df[col + "_pct"] = df[col].astype(float).rank(pct=True)
    rec_pcts = [c + "_pct" for c in rec]
    rec_neg_pcts = [c + "_pct" for c in rec_neg]
    pos_pcts = [c + "_pct" for c in pos_cols]
    neg_pcts = [c + "_pct" for c in neg_cols]
    rec_weights = [ic_factors[c] for c in rec]
    neg_weights = [ic_neg[c] for c in rec_neg]
    for sym, row in df.iterrows():
        code = str(sym).split(".")[0]
        # 主分：IC 推荐因子 ICIR 加权截面分位（全正 IC，高=强）
        rv = [row[c] for c in rec_pcts if pd.notna(row[c])]
        rw = [w for c, w in zip(rec_pcts, rec_weights) if pd.notna(row[c])]
        l2_factor = float(sum(v * w for v, w in zip(rv, rw)) / (sum(rw) or 1.0)) if rv else 0.5
        # 风险分：反向因子 |ICIR| 加权截面分位（高=利空）
        nv = [row[c] for c in rec_neg_pcts if pd.notna(row[c])]
        nw = [w for c, w in zip(rec_neg_pcts, neg_weights) if pd.notna(row[c])]
        l2_risk = float(sum(v * w for v, w in zip(nv, nw)) / (sum(nw) or 1.0)) if nv else 0.5
        pos_v = [row[c] for c in pos_pcts if pd.notna(row[c])]
        neg_v = [row[c] for c in neg_pcts if pd.notna(row[c])]
        l2_pos = float(sum(pos_v) / len(pos_v)) if pos_v else 0.5
        l2_neg = float(sum(neg_v) / len(neg_v)) if neg_v else 0.5
        out[code] = {
            "l2_score": round(l2_factor, 3),
            "l2_risk": round(l2_risk, 3),
            "l2_pos": round(l2_pos, 3),
            "l2_neg": round(l2_neg, 3),
        }
    return out


def _find_quantdb() -> str | None:
    here = os.path.dirname(os.path.abspath(__file__))
    # here = <repo>/skills/stock-picks/scripts，仓库根 = 上溯 4 级
    for cand in (
        os.path.realpath(os.path.join(here, "..", "..", "..", "..", "data", "quantdb")),
        os.path.realpath(os.path.join(here, "..", "..", "..", "data", "quantdb")),
        "/data/quantdb",
    ):
        if os.path.isdir(os.path.join(cand, "6_ml_datasets")):
            return cand
    return None


def _f(x, default: float | None = None):
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def _per_day_scores(scores: dict[str, dict], news: dict[str, dict], l2: dict[str, dict],
                    l1: dict[str, dict], industries: dict[str, str],
                    sector_flow: dict[str, float]) -> dict[str, dict]:
    """单日各维度复合分（不排名、不过滤）。趋势不纳入；L2(IC加权) > 融合 > L1 动量。

    返回 {symbol: {composite, d_*, fusion, side, position_score, l2_pos, l2_neg, ...}}。
    """
    pos_fusions = [s["fusion"] for s in scores.values() if s["fusion"] > 0]
    if pos_fusions:
        _fmin, _fmax = min(pos_fusions), max(pos_fusions)
        _fspan = (_fmax - _fmin) or 1.0
    else:
        _fmin, _fspan = 0.0, 1.0
    sec_series = pd.Series({
        sym: s["industry_top10_avg"] for sym, s in scores.items()
        if s["industry_top10_avg"] is not None
    })
    sec_pct = sec_series.rank(pct=True).to_dict() if not sec_series.empty else {}

    def _flow_score(net_yi: float | None) -> float:
        """板块超级大单净额 -> 0-1（+10亿 封顶=1，-10亿 触底=0，中性 0.5）。"""
        if net_yi is None:
            return 0.5
        return min(max(0.5 + net_yi / 20.0, 0.0), 1.0)

    out: dict[str, dict] = {}
    for sym, s in scores.items():
        industry = industries.get(sym, "")
        sector_net = sector_flow.get(industry)
        position_score = s["position_score"]
        d_fusion = max((s["fusion"] - _fmin) / _fspan, 0.0)
        d_position = min(max(position_score, 0.0), 1.0) if position_score is not None else 0.0
        d_sector = 0.7 * sec_pct.get(sym, 0.5) + 0.3 * _flow_score(sector_net)
        nws = news.get(sym, {})
        d_news = 0.5
        if nws:
            d_news = min(max(0.5 + nws["net_ratio"] * 2 + nws["score_mean"] * 0.5, 0.0), 1.0)
        l2s = l2.get(sym, {})
        d_l2 = l2s.get("l2_score", 0.5)
        d_risk = l2s.get("l2_risk", 0.5)
        l1s = l1.get(sym, {})
        d_l1 = l1s.get("l1_score", 0.5)
        # 正向维度加权 - 反向因子风险罚分（高波动持续/对倒/恐慌扣分）
        composite = (
            W_L2 * d_l2 + W_FUSION * d_fusion + W_L1 * d_l1
            + W_POSITION * d_position + W_SECTOR * d_sector + W_NEWS * d_news
            - W_RISK * d_risk
        )
        out[sym] = {
            "composite": composite,
            "d_fusion": d_fusion,
            "d_position": d_position,
            "d_sector": d_sector,
            "d_sector_flow": _flow_score(sector_net),
            "d_news": d_news,
            "d_l2": d_l2,
            "d_risk": d_risk,
            "d_l1": d_l1,
            "fusion": s["fusion"],
            "side": s["side"],
            "position_score": position_score,
            "pct_industry": s["pct_industry"],
            "industry_top10_avg": s["industry_top10_avg"],
            "consensus": s["consensus"],
            "industry": industry,
            "sector_net": sector_net,
            "l2_pos": l2s.get("l2_pos"),
            "l2_neg": l2s.get("l2_neg"),
        }
    return out


def score_candidates(day_scores: list[dict[str, dict]], names: dict[str, str],
                     exclude_st: bool = True) -> list[dict]:
    """跨日聚合复合分 + 过滤 + 排名（按分数降序）。

    day_scores: 每个元素是某天的 _per_day_scores 结果（时间顺序，越晚越靠后）。
    每股取跨日复合分均值；过滤：ST 排除 / 最近日融合>0 / 仓位门（任一天 position>0 或行业分位≥80%）。
    """
    # 跨日聚合
    syms: set[str] = set()
    for d in day_scores:
        syms.update(d.keys())
    agg: dict[str, dict] = {}
    for sym in syms:
        days = [d[sym] for d in day_scores if sym in d]
        if not days:
            continue
        agg[sym] = {
            "composite": sum(x["composite"] for x in days) / len(days),
            "n_days": len(days),
            "days": days,
        }

    cands: list[dict] = []
    for sym, a in agg.items():
        name = names.get(sym, "")
        if exclude_st and "ST" in name.upper():
            continue
        latest = a["days"][-1]
        # 硬过滤：最近日模型预测为负的不推
        if latest["fusion"] <= 0:
            continue
        # 仓位门：position 数据缺失不拦；有数据时需 position>0 或 行业分位>=80%
        gate = any(
            (d["position_score"] is None and d["pct_industry"] is None)
            or (d["position_score"] is not None and d["position_score"] > 0)
            or (d["pct_industry"] is not None and d["pct_industry"] >= _POSITION_GATE)
            for d in a["days"]
        )
        if not gate:
            continue
        cand = {
            "symbol": sym,
            "name": name,
            "industry": latest["industry"],
            "sector_net": round(latest["sector_net"], 2) if latest["sector_net"] is not None else None,
            "fusion": round(latest["fusion"], 4),
            "side": latest["side"],
            "position_score": round(latest["position_score"], 3) if latest["position_score"] is not None else None,
            "pct_industry": round(latest["pct_industry"], 3) if latest["pct_industry"] is not None else None,
            "consensus": latest["consensus"],
            "n_days": a["n_days"],
            "d_fusion": round(latest["d_fusion"], 3),
            "d_position": round(latest["d_position"], 3),
            "d_sector": round(latest["d_sector"], 3),
            "d_news": round(latest["d_news"], 3),
            "d_l2": round(latest["d_l2"], 3),
            "d_risk": round(latest.get("d_risk", 0.5), 3),
            "d_l1": round(latest.get("d_l1", 0.5), 3),
            "l2_pos": latest["l2_pos"],
            "l2_neg": latest["l2_neg"],
            "news_net": None,
            "score": round(a["composite"], 4),
        }
        cands.append(cand)
    cands.sort(key=lambda c: c["score"], reverse=True)
    for i, c in enumerate(cands, 1):
        c["rank"] = i
    return cands


def render_md(date_str: str, cands: list[dict], meta: dict) -> str:
    lines = [
        f"# 多维度选股候选 {date_str}",
        "",
        f"> 数据截止日 **{meta['data_date']}**；买入/信号日 **{meta['signal_date']}**"
        f"（window={meta['window']}：跨 {meta['window']} 个推理日聚合，每股取跨日复合分均值）",
        f"> 维度权重：L2 {W_L2:.0%} / 融合 {W_FUSION:.0%} / L1动量 {W_L1:.0%} / 仓位 {W_POSITION:.0%} / 板块 {W_SECTOR:.0%} / 新闻 {W_NEWS:.0%}（趋势不纳入）",
        f"> 候选 {meta['candidate_count']} 只；未含：{meta['missing']}",
        (f"> ⚠️ {meta['gate']}" if meta.get('gate') else ""),
        "",
        "## Top 候选（按跨日复合分降序）",
        "",
        "| # | 代码 | 名称 | 得分 | L2 | 融合 | 方向 | 仓位 | 行业分位 | 覆盖日 | 行业 | 板块大单(亿) | L2正 | L2负 |",
        "|---|------|------|------|----|------|------|------|----------|--------|------|------|------|------|",
    ]
    for c in cands:
        l2p = c.get("l2_pos")
        l2n = c.get("l2_neg")
        lines.append(
            f"| {c['rank']} | {c['symbol']} | {c['name']} | {c['score']:.3f} | "
            f"{c['d_l2']:.2f} | {c['fusion']} | {c['side']} | {c['position_score'] or '--'} | "
            f"{_pct(c['pct_industry'])} | {c['n_days']} | {c['industry'] or '--'} | "
            f"{c['sector_net'] if c['sector_net'] is not None else '--'} | "
            f"{l2p if l2p is not None else '--'} | {l2n if l2n is not None else '--'} |"
        )
    lines += ["", "## 各维度分解（Top 20）", "",
              "| # | 代码 | L2 | 融合 | L1动量 | 仓位 | 板块 | 新闻 |", "|---|------|----|------|-------|------|------|------|"]
    for c in cands[:20]:
        lines.append(
            f"| {c['rank']} | {c['symbol']} | {c['d_l2']:.2f} | {c['d_fusion']:.2f} | "
            f"{c.get('d_l1', 0.5):.2f} | {c['d_position']:.2f} | {c['d_sector']:.2f} | {c['d_news']:.2f} |"
        )
    return "\n".join(lines)


def _pct(x):
    if x is None:
        return "--"
    return f"{x * 100:.0f}%"


def resolve_data_window(cur, start: date, window: int) -> list[date]:
    """从 start 起往后 window 个有已完成推理批次的数据日（跨多日聚合用）。"""
    cur.execute(
        "SELECT DISTINCT data_trade_date FROM qm_model_inference_runs "
        "WHERE status='completed' AND data_trade_date >= %s "
        "ORDER BY data_trade_date ASC LIMIT %s",
        (start, window),
    )
    return [r[0] for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="信号日 YYYYMMDD（缺省=最近全量推理日）")
    ap.add_argument("--data-date", help="推理数据截止日 YYYYMMDD——无泄露回测用，"
                                        "只用该日收盘前数据推的信号（L2/新闻同口径）")
    ap.add_argument("--window", type=int, default=3,
                    help="跨日聚合窗口（默认3）：从数据日往前 N 个推理日，取每股跨日复合分均值。"
                         "window=1 为严格无未来视觉单日模式")
    ap.add_argument("--top", type=int, default=5, help="候选上限（默认精选 5 只）")
    ap.add_argument("--no-l2", action="store_true", help="跳过 L2 维度")
    ap.add_argument("--model", help="指定推理模型（同批次多模型时用单一主 run，默认日推模型）")
    ap.add_argument("--keep-st", action="store_true", help="保留 ST 股（默认排除）")
    ap.add_argument("--json", action="store_true", help="同时输出 JSON 全量")
    args = ap.parse_args()

    conn = pg_connect()
    cur = conn.cursor()

    data_date: date | None = None
    buy_date: date | None = None
    if args.data_date:
        data_date = datetime.strptime(args.data_date, "%Y%m%d").date()
        cur.execute(
            "SELECT MAX(prediction_trade_date) FROM qm_model_inference_runs "
            "WHERE status='completed' AND data_trade_date=%s",
            (data_date,),
        )
        buy_date = cur.fetchone()[0]
        if buy_date is None:
            print(f"无 {data_date} 的已完成推理批次（qm_model_inference_runs 无 data_trade_date={data_date}）")
            sys.exit(1)
    elif args.date:
        buy_date = datetime.strptime(args.date, "%Y%m%d").date()
    else:
        buy_date = latest_full_signal_date(cur)
    if buy_date is None:
        print("无可用信号日（PG 无 engine_signal_scores 数据）")
        sys.exit(1)
    date_str = buy_date.strftime("%Y%m%d")

    names = load_names(cur)
    industries = load_industries()

    # 跨日聚合：每个数据日算一套单日复合分，最后按跨日均值排名
    window_dates: list[date] = [data_date] if data_date else [buy_date]
    if data_date is not None and args.window > 1:
        window_dates = resolve_data_window(cur, data_date, args.window)
    print(f"[info] 数据截止日: {data_date or '默认'}  | 买入/信号日: {buy_date}  | "
          f"聚合窗口 {len(window_dates)} 日: {[str(d) for d in window_dates]}")

    day_scores: list[dict[str, dict]] = []
    for wd in window_dates:
        scores = load_scores(cur, wd, buy_date, model_id=args.model)
        if not scores:
            continue
        sector_flow = load_sector_flow(wd, buy_date)
        news = load_news(wd, buy_date)
        l2 = {} if args.no_l2 else load_l2(wd, buy_date)
        l1 = {} if args.no_l2 else load_l1(wd, buy_date)
        day_scores.append(_per_day_scores(scores, news, l2, l1, industries, sector_flow))
        print(f"[info]  {wd}: 融合 {len(scores)} / L2 {len(l2)} / L1 {len(l1)} / 新闻 {len(news)} / 板块资金 {len(sector_flow)}")
    cur.close()
    conn.close()

    if not day_scores:
        print("无任何推理批次可用")
        sys.exit(1)
    cands = score_candidates(day_scores, names, exclude_st=not args.keep_st)[: args.top]

    # 复盘方向门：判「看空/强烈看空」则清空选股（大盘空仓日不推荐，避免暴跌日逆势抄底）
    market_dir = None
    for wd in window_dates:
        md = load_market_direction(wd, buy_date)
        if md:
            market_dir = md
            break
    gate_note = None
    if market_dir and market_dir.get("direction") in ("看空", "强烈看空"):
        cands = []
        gate_note = f"复盘判{market_dir['direction']}（{market_dir.get('total_score')}/11，数据完整度{'★' * (market_dir.get('data_completeness') or 0)}）→ 清空选股，不推荐"
        print(f"[info] ⚠️ {gate_note}")
    elif market_dir:
        gate_note = f"复盘判{market_dir['direction']}（{market_dir.get('total_score')}/11）"

    missing = ", ".join(
        x for x, has in (
            ("L2", all(not s for s in [any(v.get("d_l2", 0) > 0 for v in d.values()) for d in day_scores])),
            ("新闻", not any(d for d in day_scores if any(v.get("d_news", 0.5) != 0.5 for v in d.values()))),
        ) if not has
    ) or "无"

    # 落盘
    out_dir = os.getenv("QM_PICKS_DIR") or os.path.join(os.getenv("QM_REPORTS_DIR", "data/reports"), "stock_picks")
    os.makedirs(out_dir, exist_ok=True)
    md = render_md(date_str, cands, {
        "signal_date": str(buy_date),
        "data_date": str(data_date) if data_date else "≤信号日前",
        "window": len(window_dates),
        "candidate_count": len(cands), "missing": missing,
        "gate": gate_note or "",
    })
    md_path = os.path.join(out_dir, f"{date_str}_picks.md")
    with open(md_path, "w") as f:
        f.write(md)
    print(f"[info] Markdown 骨架: {md_path}")

    if args.json:
        payload = {
            "date": date_str,
            "buy_date": str(buy_date),
            "data_date": str(data_date) if data_date else None,
            "window_days": [str(d) for d in window_dates],
            "market_direction": market_dir,
            "gate": gate_note,
            "weights": {"l2": W_L2, "fusion": W_FUSION, "l1": W_L1, "position": W_POSITION,
                        "sector": W_SECTOR, "news": W_NEWS},
            "missing": missing,
            "candidates": cands,
        }
        json_path = os.path.join(out_dir, f"{date_str}_picks.json")
        with open(json_path, "w") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[info] JSON 全量: {json_path}")

    # 控制台 Top 10
    print("\n=== Top 10 候选（跨日聚合，L2 主导） ===")
    for c in cands[:10]:
        print(
            f"{c['rank']:>3}. {c['symbol']:<12}{c['name']:<10} score={c['score']:.3f} "
            f"fusion={c['fusion']} L2={c['d_l2']:.2f} 覆盖{c['n_days']}日 "
            f"industry={c['industry']}"
        )


if __name__ == "__main__":
    main()
