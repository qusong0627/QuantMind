#!/usr/bin/env python3
"""大盘情绪快照 —— 从广度面板生成前端「大盘情绪」面板所需的 JSON。

产出：``${QM_QUANTDB_DATA_DIR:-data/quantdb}/5_technical_derived/market_breadth/sentiment.json``

放在派生数据目录而不是 ``QM_MARKET_SNAPSHOT_DIR``：后者由容器以 root 创建，
宿主机上的定时任务没有写权限；派生产物与面板同目录，容器与宿主同路径可读。

设计取舍：
- 与市场分析模块既有模式一致：**预计算快照 + 只读 API**，API 进程零计算。
  广度分位是扩张窗口，放进请求路径会拖慢每一次刷新；分市场后 8 个宇宙
  × 13 个指标更不可能现算 —— 分位在 ``breadth_universe.py`` 建面板时一次算完，
  本模块只切片。
- 预测部分只给「历史同状态的条件分布」，不给点预测。广度对方向几乎没有
  稳定预测力（见 ``breadth_regime.md``），把它画成一条确定的预测线是骗人。
- 扇形图用的是**当前状态所在区间**的历史路径分位，而不是全样本 —— 换状态
  换形态，这一点必须在图上可见。
- **每个指数用自己宇宙的压力分**（2026-10-28）：全 A 分数掩盖了「指数涨、
  多数票跌」这类结构分歧（2024-02-07 创业板 82.7 vs 沪深300 29.8）。
  宇宙口径见 ``breadth_universe.UNIVERSES``，全部 PIT。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.breadth_regime import (
    INDICATORS,
    MIN_PERIODS,
    build_features,
    cluster_count,
    composite_scores,
    load_panel,
    stress_from_raw,
)
from backend.scripts.index_forecast import forecast_index
from backend.scripts.breadth_universe import UNIVERSE_BY_KEY
from backend.scripts.market_analysis.main_force import load_main_force, latest_block
from backend.scripts.market_analysis.retail_force import (
    latest_retail_block,
    load_retail_force,
)
from backend.scripts.research_stats import expanding_pct_rank

log = logging.getLogger("sentiment_snapshot")

#: QuantDB 根目录：容器内是 /data/quantdb，宿主机默认 data/quantdb。
#: 必须由 env 决定 —— 容器 cwd 是 /app，写死相对路径会找不到面板。
QUANTDB_DIR = Path(os.getenv("QM_QUANTDB_DATA_DIR", "data/quantdb"))
BREADTH_DIR = QUANTDB_DIR / "5_technical_derived" / "market_breadth"
PANEL_PATH = BREADTH_DIR / "trin_daily.parquet"
UNIVERSE_PANEL_PATH = BREADTH_DIR / "by_universe.parquet"
INDEX_OHLC_PATH = BREADTH_DIR / "index_ohlc.parquet"

#: 展示指数 → (名称, 广度宇宙, 价格来源)。顺序即前端 tab 顺序。
#: 价格一律用官方指数；只有全 A 没有官方口径（本地没有中证全指/万得全A），
#: 用 ``breadth_universe`` 的月频再平衡等权合成（见该模块口径说明）。
TARGETS: dict[str, tuple[str, str, str]] = {
    "000001.SH": ("上证指数", "sh", "official"),
    "399001.SZ": ("深证成指", "sz500", "official"),
    "399006.SZ": ("创业板指", "cyb100", "official"),
    "000300.SH": ("沪深300", "hs300", "official"),
    "000852.SH": ("中证1000", "zz1000", "official"),
    "ALL.A": ("A股指数（全A等权）", "all", "synthetic"),
}
HORIZON = 20
HISTORY_DAYS = 250  # 供前端拖动回看；默认视窗仍是最近 60 日 + 20 日前瞻
STRESS_HISTORY_DAYS = 250

#: 展示用：指标单位与缩放（值本身在面板里是小数或原始量）
DISPLAY = {
    "trin": ("", 1.0, 3),
    "trin_ema10": ("", 1.0, 3),
    "trin_sum10": ("", 1.0, 3),
    "adr": ("", 1.0, 2),
    "udr": ("", 1.0, 2),
    "breadth20": ("%", 100.0, 1),
    "breadth60": ("%", 100.0, 1),
    "mcclellan": ("", 1.0, 0),
    "limit_net": ("%", 100.0, 2),
    "amt_shock": ("σ", 1.0, 2),
    "nh_nl": ("%", 100.0, 2),
    "adl_slope": ("", 1.0, 2),
    "eqw_ret": ("%", 1.0, 2),
}

#: 前端默认展示的 8 个核心指标（顺序即展示顺序）
CORE_KEYS = (
    "breadth20",
    "breadth60",
    "trin_ema10",
    "adr",
    "udr",
    "mcclellan",
    "limit_net",
    "nh_nl",
)

ZONES = (
    (
        80.0,
        "extreme_pressure",
        "极端压力",
        "历史同状态偏多，但波动最大",
        "下跌后段 / 超卖区",
        "多数股票已经跌到低位，历史上其后 20 天上涨居多，但波动也最大",
    ),
    (
        60.0,
        "pressure",
        "承压",
        "历史同状态无稳定方向",
        "弱势整理",
        "市场偏弱但没有极值信号，方向要等趋势确认",
    ),
    (
        40.0,
        "neutral",
        "中性",
        "历史同状态接近基准",
        "震荡中段",
        "冷热都在中间，历史同状态没有稳定方向",
    ),
    (
        20.0,
        "strong",
        "偏强",
        "历史同状态偏震荡",
        "上涨中段 / 偏强",
        "多数股票在上涨，但越往过热走，回撤概率越高",
    ),
    (
        -1.0,
        "overheat",
        "过热",
        "大盘回撤概率高于基准",
        "上涨后段 / 过热",
        "普涨且成交放大，历史上其后大盘回撤概率高于平时",
    ),
)


def stress_zone(score: float) -> tuple[str, str, str, str, str]:
    for threshold, key, label, note, phase, detail in ZONES:
        if score >= threshold:
            return key, label, note, phase, detail
    return "neutral", "中性", "", "震荡中段", ""


def mask_from_zone(stress: pd.Series, score: float) -> np.ndarray:
    """按当前分数归档取历史同区间样本。"""
    if score >= 80:
        return (stress >= 80).to_numpy()
    if score >= 60:
        return ((stress >= 60) & (stress < 80)).to_numpy()
    if score >= 40:
        return ((stress >= 40) & (stress < 60)).to_numpy()
    if score >= 20:
        return ((stress >= 20) & (stress < 40)).to_numpy()
    return (stress < 20).to_numpy()


def fan_bands(close: pd.Series, mask: np.ndarray, horizon: int = HORIZON) -> list[dict]:
    """历史同状态下的路径分位（相对起点，单位 %）。k=0 固定在 0。"""
    vals = close.to_numpy(dtype=float)
    idx = np.flatnonzero(mask)
    paths = []
    for i in idx:
        if i + horizon >= len(vals) or not np.isfinite(vals[i]) or vals[i] <= 0:
            continue
        seg = vals[i + 1 : i + 1 + horizon] / vals[i] - 1.0
        if np.all(np.isfinite(seg)):
            paths.append(seg)
    out = [{"k": 0, "p10": 0.0, "p25": 0.0, "p50": 0.0, "p75": 0.0, "p90": 0.0}]
    if not paths:
        return out
    arr = np.vstack(paths)
    for k in range(horizon):
        col = arr[:, k] * 100.0
        q = np.percentile(col, [10, 25, 50, 75, 90])
        out.append(
            {
                "k": k + 1,
                "p10": round(float(q[0]), 2),
                "p25": round(float(q[1]), 2),
                "p50": round(float(q[2]), 2),
                "p75": round(float(q[3]), 2),
                "p90": round(float(q[4]), 2),
            }
        )
    return out


def index_stats(close: pd.Series, mask: np.ndarray, horizon: int = HORIZON) -> dict:
    """同状态下的终值收益与尾部风险。"""
    vals = close.to_numpy(dtype=float)
    rets, lows = [], []
    for i in np.flatnonzero(mask):
        if i + horizon >= len(vals) or not np.isfinite(vals[i]) or vals[i] <= 0:
            continue
        seg = vals[i + 1 : i + 1 + horizon] / vals[i] - 1.0
        if np.all(np.isfinite(seg)):
            rets.append(seg[-1])
            lows.append(seg.min())
    if not rets:
        return {}
    r = np.array(rets)
    lo = np.array(lows)
    return {
        "n_days": int(len(r)),
        "n_clusters": cluster_count(mask, horizon),
        "mean": round(float(r.mean()) * 100, 2),
        "median": round(float(np.median(r)) * 100, 2),
        "win": round(float((r > 0).mean()), 3),
        "p10": round(float(np.percentile(r, 10)) * 100, 2),
        "p90": round(float(np.percentile(r, 90)) * 100, 2),
        "p_drop5": round(float((lo <= -0.05).mean()), 3),
        "p_drop10": round(float((lo <= -0.10).mean()), 3),
    }


def baseline_stats(close: pd.Series, horizon: int = HORIZON) -> dict:
    vals = close.to_numpy(dtype=float)
    rets, lows = [], []
    for i in range(len(vals) - horizon):
        if not np.isfinite(vals[i]) or vals[i] <= 0:
            continue
        seg = vals[i + 1 : i + 1 + horizon] / vals[i] - 1.0
        if np.all(np.isfinite(seg)):
            rets.append(seg[-1])
            lows.append(seg.min())
    r = np.array(rets)
    lo = np.array(lows)
    return {
        "mean": round(float(r.mean()) * 100, 2),
        "win": round(float((r > 0).mean()), 3),
        "p_drop10": round(float((lo <= -0.10).mean()), 3),
    }


def actual_path(
    close: pd.Series, as_of: pd.Timestamp, horizon: int = HORIZON
) -> list[dict]:
    """as-of 之后**真实走出**的路径（相对 as-of 收盘，单位 %）。

    只用于「回看模式」把历史结果叠加到扇形上做对照；最新一期没有未来数据，
    返回空列表 —— 绝不把未来数据混进当时的信号。

    回看日期离今天不足 ``horizon`` 个交易日时返回**已走出的部分**（而不是空），
    调用方用 ``len(actual)-1 / horizon`` 标注「已走 N 天」；把不足 20 天说成
    「没有实际走势」会让人误以为回看坏了。
    """
    if as_of not in close.index:
        return []
    pos = close.index.get_loc(as_of)
    future = close.iloc[pos + 1 : pos + 1 + horizon]
    if len(future) == 0:
        return []
    base = float(close.iloc[pos])
    if not np.isfinite(base) or base <= 0:
        return []
    out: list[dict] = [{"k": 0, "pct": 0.0}]
    for i, v in enumerate(future):
        val = float(v)
        if not np.isfinite(val):
            break
        out.append({"k": i + 1, "pct": round((val / base - 1.0) * 100, 2)})
    return out


def factor_history(feats: pd.DataFrame, days: int = 60) -> dict:
    """近 60 个交易日的分项冷度（0=最热，100=最冷），供底部热力图。

    方向与前端一致：``trin_ema10`` 原始值越高越冷，其余越高越热。
    """
    tail = feats.tail(days)
    items = []
    for ind in INDICATORS:
        if ind.name not in CORE_KEYS:
            continue
        vals = []
        for v in tail[ind.name]:
            p = float(v)
            if not np.isfinite(p):
                vals.append(None)
                continue
            cold = p if ind.name == "trin_ema10" else 1.0 - p
            vals.append(round(cold * 100.0, 0))
        items.append({"key": ind.name, "label": ind.note, "cold": vals})
    return {"dates": [d.strftime("%Y-%m-%d") for d in tail.index], "items": items}


# --------------------------------------------------------------------------
# 分市场：面板装载与视图
# --------------------------------------------------------------------------
def load_universe_panel(path: Path | None = None) -> pd.DataFrame:
    p = path or UNIVERSE_PANEL_PATH
    if not p.exists():
        return pd.DataFrame()
    return pd.read_parquet(p)


def load_index_ohlc(path: Path | None = None) -> pd.DataFrame:
    p = path or INDEX_OHLC_PATH
    if not p.exists():
        return pd.DataFrame(
            columns=["date", "symbol", "open", "high", "low", "close", "amount"]
        )
    return pd.read_parquet(p)


def frame_by_universe(uni: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """长表 → {宇宙: 按日索引的帧}。分位/压力分由面板预计算，这里不重算。"""
    out: dict[str, pd.DataFrame] = {}
    for key, sub in uni.groupby("universe", sort=False):
        frame = sub.drop(columns=["universe"]).set_index("date").sort_index()
        out[str(key)] = frame
    return out


def feature_view(frame: pd.DataFrame) -> pd.DataFrame:
    """分项分位视图：优先读面板预计算的 ``pct_*``；旧面板/测试帧现算兜底。"""
    cols = {}
    for ind in INDICATORS:
        col = f"pct_{ind.name}"
        cols[ind.name] = (
            frame[col]
            if col in frame
            else expanding_pct_rank(frame[ind.column], MIN_PERIODS)
        )
    return pd.DataFrame(cols, index=frame.index)


def stress_series(frame: pd.DataFrame) -> pd.Series:
    if "stress" in frame:
        return frame["stress"].astype(float)
    return stress_from_raw(frame)


def stress_percentile(frame: pd.DataFrame, scores: pd.Series) -> pd.Series:
    if "stress_pct" in frame:
        return frame["stress_pct"]
    return expanding_pct_rank(scores, 500)


def indicator_list(frame: pd.DataFrame, feats: pd.DataFrame) -> list[dict]:
    out = []
    for ind in INDICATORS:
        if ind.name not in CORE_KEYS:
            continue
        unit, scale, digits = DISPLAY.get(ind.name, ("", 1.0, 3))
        raw = frame[ind.column].iloc[-1]
        pct = float(feats[ind.name].iloc[-1])
        out.append(
            {
                "key": ind.name,
                "label": ind.note,
                "value": round(float(raw) * scale, digits)
                if np.isfinite(raw)
                else None,
                "unit": unit,
                "pct": round(pct, 3) if np.isfinite(pct) else None,
                "zone": "low" if pct <= 0.1 else ("high" if pct >= 0.9 else "normal"),
            }
        )
    return out


def stress_block(scores: pd.Series, pct: pd.Series) -> dict:
    score = float(scores.iloc[-1])
    score_pct = float(pct.iloc[-1])
    zone_key, zone_label, zone_note, phase, phase_detail = stress_zone(score)
    return {
        "score": round(score, 1),
        "zone": zone_key,
        "zone_label": zone_label,
        "zone_note": zone_note,
        "phase": phase,
        "phase_detail": phase_detail,
        "percentile": round(score_pct, 3) if np.isfinite(score_pct) else None,
    }


def price_series(
    symbol: str, kind: str, frame: pd.DataFrame, ohlc: pd.DataFrame | None
) -> pd.Series:
    if kind == "synthetic":
        return frame["ew_close"].astype(float)
    if ohlc is None or ohlc.empty:
        return pd.Series(dtype=float)
    sub = ohlc[ohlc["symbol"] == symbol].set_index("date").sort_index()
    return sub["close"].astype(float).reindex(frame.index)


#: 七档涨跌分布（互斥），与 ``breadth_universe.distribution_counts`` 的键一致
DIST_KEYS = (
    "dist_limit_up",
    "dist_up_big",
    "dist_up_small",
    "dist_flat",
    "dist_down_small",
    "dist_down_big",
    "dist_limit_down",
)


def distribution_block(frame: pd.DataFrame) -> dict:
    """最新交易日的七档涨跌分布（只给当天计数，不搬 250 天序列）。

    每个指数用它自己宇宙的成分股；旧路径（无分市场面板）没有这些列，返回
    空 dict —— 前端按缺失隐藏，不回落成 0。
    """
    if not all(k in frame for k in DIST_KEYS):
        return {}
    row = frame.iloc[-1]
    return {k: int(row[k]) if np.isfinite(row[k]) else 0 for k in DIST_KEYS}


def index_volume_block(frame: pd.DataFrame, scope: str, days: int = 120) -> dict:
    """单指数/宇宙的成交额序列：与扇形主图日期逐日对齐。"""
    tail = frame.tail(days)
    amt = tail["amt_total"].astype(float)
    up = tail["amt_up"].astype(float)
    down = tail["amt_down"].astype(float)
    vw = (up - down) / amt.where(amt > 0)
    vw_pct = up / amt.where(amt > 0)
    return {
        "scope": scope,
        "dates": [d.strftime("%Y-%m-%d") for d in tail.index],
        "turnover_yi": [
            round(float(v) / 1e4, 1) if np.isfinite(float(v)) else None for v in amt
        ],
        "vw_breadth": [
            round(float(v), 4) if np.isfinite(float(v)) else None for v in vw
        ],
        "vw_breadth_pct": [
            round(float(v), 4) if np.isfinite(float(v)) else None for v in vw_pct
        ],
    }


def _index_block(
    symbol: str,
    name: str,
    frame: pd.DataFrame,
    feats: pd.DataFrame,
    scores: pd.Series,
    pct: pd.Series,
    close: pd.Series,
    full_close: pd.Series,
    df: pd.DataFrame,
    feats_global: pd.DataFrame,
    as_of: pd.Timestamp | None,
) -> dict:
    if len(close) == 0 or not np.isfinite(close.iloc[-1]):
        return {}
    score = float(scores.iloc[-1])
    mask = mask_from_zone(scores, score)
    last_date = close.index[-1]
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) > 1 else np.nan
    pos = (
        full_close.index.get_loc(last_date)
        if last_date in full_close.index
        else len(full_close) - 1
    )
    future_dates = [
        d.strftime("%Y-%m-%d") for d in full_close.index[pos + 1 : pos + 1 + HORIZON]
    ]
    # 预测模型沿用全市场广度特征（其 IC 是按这个口径验证的）；只有被解释变量
    # 换成该指数自己的价格序列。旧面板只带 4 个 close_*，统一用 full_close 覆盖。
    dfx = df.assign(**{f"close_{symbol}": full_close.reindex(df.index)})
    fc = forecast_index(dfx, feats_global, symbol, last_date)
    disp_pct = float(frame["disp_pct"].iloc[-1]) if "disp_pct" in frame else np.nan
    actual = actual_path(full_close, last_date) if as_of is not None else []
    return {
        "name": name,
        "last": round(last, 2),
        "change_pct": round((last / prev - 1.0) * 100, 2) if prev else None,
        "history": [
            {"date": d.strftime("%Y-%m-%d"), "close": round(float(c), 2)}
            for d, c in close.tail(HISTORY_DAYS).items()
        ],
        "fan": fan_bands(close, mask),
        "stats": index_stats(close, mask),
        "baseline": baseline_stats(close),
        "actual": actual,
        "actual_days": max(len(actual) - 1, 0),
        "actual_horizon": HORIZON,
        "forecast": fc,
        "future_dates": future_dates,
        "stress": stress_block(scores, pct),
        "indicators": indicator_list(frame, feats),
        "stress_history": [
            {
                "date": d.strftime("%Y-%m-%d"),
                "stress": round(float(s), 1),
                "breadth20": round(float(frame.loc[d, "pct_above_ma20"]) * 100, 1),
            }
            for d, s in scores.tail(STRESS_HISTORY_DAYS).items()
        ],
        "factor_history": factor_history(feats),
        "dispersion": {
            "value": round(float(frame["ret_disp"].iloc[-1]), 2),
            "pct": round(disp_pct, 3) if np.isfinite(disp_pct) else None,
        },
        "universe": _universe_meta(symbol, frame),
        "distribution": distribution_block(frame),
        "volume": index_volume_block(frame, name),
    }


def _universe_meta(symbol: str, frame: pd.DataFrame) -> dict:
    """这个指数用的是哪个广度宇宙（前端要如实标注「近似宇宙」）。"""
    _, ukey, _ = TARGETS[symbol]
    u = UNIVERSE_BY_KEY.get(ukey)
    return {
        "key": ukey,
        "label": u.label if u else ukey,
        "mode": u.mode if u else "unknown",
        "note": u.note if u else "",
        "n_last": int(frame["n_total"].iloc[-1]) if "n_total" in frame else None,
    }


def build_payload(
    df: pd.DataFrame,
    feats: pd.DataFrame,
    as_of: pd.Timestamp | None = None,
    uni: pd.DataFrame | None = None,
    ohlc: pd.DataFrame | None = None,
) -> dict:
    """构建情绪面板数据。``as_of`` 给定时，全部信号只用该日及以前的数据。

    无前视保证：切片后 stress / 分位 / 扇形 / 基准全部只在切片内计算；扇形的
    未来路径在切片内不足 20 天者自然被丢弃。真实走势另走 :func:`actual_path`
    从全量数据取，且只用于回看对照。

    ``uni`` 给定时，每个指数用**自己宇宙**的压力分与分项（分市场）；不给则
    退回旧的「全市场分数 + 官方指数价格」路径（向后兼容）。
    """
    signal_df = df.loc[:as_of] if as_of is not None else df
    signal_feats = feats.loc[:as_of] if as_of is not None else feats
    if len(signal_df) < 600:
        raise ValueError("as_of 太早：可用历史不足 600 个交易日，无法计算分位")

    scores = composite_scores(signal_feats)
    score_pct = float(expanding_pct_rank(scores["stress"], min_periods=500).iloc[-1])

    last_date = signal_df.index[-1]
    frames = frame_by_universe(uni) if uni is not None and not uni.empty else {}

    indices: dict[str, dict] = {}
    for symbol, (name, ukey, kind) in TARGETS.items():
        frame = frames.get(ukey)
        if frame is not None:
            sliced = frame.loc[:as_of] if as_of is not None else frame
            if len(sliced) < 600:
                continue
            feats_u = feature_view(sliced)
            scores_u = stress_series(sliced)
            pct_u = stress_percentile(sliced, scores_u)
            close = price_series(symbol, kind, sliced, ohlc).dropna()
            full_close = price_series(symbol, kind, frame, ohlc).dropna()
            block = _index_block(
                symbol,
                name,
                sliced,
                feats_u,
                scores_u,
                pct_u,
                close,
                full_close,
                df,
                feats,
                as_of,
            )
        else:
            # 旧路径（无分市场面板）：全市场分数 + 官方指数价格
            close = (
                signal_df[f"close_{symbol}"].dropna()
                if f"close_{symbol}" in signal_df
                else pd.Series(dtype=float)
            )
            if close.empty:
                continue
            block = _index_block(
                symbol,
                name,
                signal_df,
                signal_feats,
                scores["stress"].rename("stress"),
                pd.Series(1.0, index=scores.index),
                close,
                df[f"close_{symbol}"].dropna(),
                df,
                feats,
                as_of,
            )
            block["stress"] = stress_block(
                scores["stress"], pd.Series(1.0, index=scores.index)
            )
            block["stress"]["percentile"] = (
                round(score_pct, 3) if np.isfinite(score_pct) else None
            )
            block["indicators"] = indicator_list(signal_df, signal_feats)
            block["stress_history"] = [
                {
                    "date": d.strftime("%Y-%m-%d"),
                    "stress": round(float(s), 1),
                    "breadth20": round(
                        float(signal_df.loc[d, "pct_above_ma20"]) * 100, 1
                    ),
                }
                for d, s in scores["stress"].tail(STRESS_HISTORY_DAYS).items()
            ]
            block["factor_history"] = factor_history(signal_feats)
        if block:
            indices[symbol] = block

    history = []
    for d, s_val in scores["stress"].tail(STRESS_HISTORY_DAYS).items():
        history.append(
            {
                "date": d.strftime("%Y-%m-%d"),
                "stress": round(float(s_val), 1),
                "breadth20": round(float(signal_df.loc[d, "pct_above_ma20"]) * 100, 1),
            }
        )

    # 全 A 口径的宇宙块（顶栏卡片与旧字段共用）
    all_frame = frames.get("all")
    all_view = (
        all_frame.loc[:as_of]
        if (all_frame is not None and as_of is not None)
        else all_frame
    )
    if all_view is not None:
        all_sliced = all_view
        all_feats = feature_view(all_sliced)
        all_scores = stress_series(all_sliced)
        all_pct = stress_percentile(all_sliced, all_scores)
        dispersion = {
            "value": round(float(all_sliced["ret_disp"].iloc[-1]), 2),
            "pct": (
                round(float(all_sliced["disp_pct"].iloc[-1]), 3)
                if "disp_pct" in all_sliced
                else None
            ),
        }
        distribution_all = distribution_block(all_sliced)
    else:
        all_feats, all_scores, all_pct = (
            signal_feats,
            scores["stress"],
            pd.Series(1.0, index=scores.index),
        )
        dispersion = {
            "value": round(float(signal_df["ret_disp"].iloc[-1]), 2),
            "pct": (
                round(float(expanding_pct_rank(signal_df["ret_disp"], 500).iloc[-1]), 3)
                if np.isfinite(signal_df["ret_disp"].iloc[-1])
                else None
            ),
        }
        distribution_all = {}

    # 主力视角：L2 超大单+大单日频曲线 + 成交额加权涨跌系数（不参与评分，只做展示）
    base_view = all_view if all_view is not None else signal_df
    try:
        # L2 资金流自 2022 年起可用；取全量做主力分位，曲线只画最近 120 日
        mf = load_main_force(days=1600)
    except Exception:  # noqa: BLE001 —— 主力数据缺失不影响情绪主链
        mf = None
    mf_start = None
    if mf is not None and not mf.empty:
        mf_start = mf.index[0].strftime("%Y-%m-%d")
        mf = mf.reindex(base_view.index)
    try:
        rf = load_retail_force(days=1600)
    except Exception:  # noqa: BLE001
        rf = None
    rf_start = None
    if rf is not None and not rf.empty:
        rf_start = rf.index[0].strftime("%Y-%m-%d")
        rf = rf.reindex(base_view.index)
    vw_breadth = (
        (base_view["amt_up"] - base_view["amt_down"])
        / base_view["amt_total"].where(base_view["amt_total"] > 0)
    ).reindex(base_view.index)
    vw_pct = (
        base_view["amt_up"] / base_view["amt_total"].where(base_view["amt_total"] > 0)
    ).reindex(base_view.index)
    vol_tail = base_view.tail(120).index
    volume = {
        "dates": [d.strftime("%Y-%m-%d") for d in vol_tail],
        "turnover_yi": [
            round(float(base_view.loc[d, "amt_total"]) / 1e4, 1)
            if np.isfinite(float(base_view.loc[d, "amt_total"]))
            else None
            for d in vol_tail
        ],
        "vw_breadth": [
            None if not np.isfinite(float(vw_breadth.get(d, np.nan))) else round(float(vw_breadth.get(d)), 4)
            for d in vol_tail
        ],
        "vw_breadth_pct": [
            None if not np.isfinite(float(vw_pct.get(d, np.nan))) else round(float(vw_pct.get(d)), 4)
            for d in vol_tail
        ],
        "main_net_ratio": [
            None
            if mf is None or d not in mf.index or not np.isfinite(float(mf.loc[d, "main_net_ratio"]))
            else round(float(mf.loc[d, "main_net_ratio"]), 4)
            for d in vol_tail
        ],
        "main_net_yi": [
            None
            if mf is None or d not in mf.index or not np.isfinite(float(mf.loc[d, "main_net_yi"]))
            else round(float(mf.loc[d, "main_net_yi"]), 2)
            for d in vol_tail
        ],
        "main_breadth": [
            None
            if mf is None or d not in mf.index or not np.isfinite(float(mf.loc[d, "main_breadth"]))
            else round(float(mf.loc[d, "main_breadth"]), 4)
            for d in vol_tail
        ],
        "main_breadth_amt": [
            None
            if mf is None or d not in mf.index or not np.isfinite(float(mf.loc[d, "main_breadth_amt"]))
            else round(float(mf.loc[d, "main_breadth_amt"]), 4)
            for d in vol_tail
        ],
        "retail_net_yi": [
            None
            if rf is None or d not in rf.index or not np.isfinite(float(rf.loc[d, "retail_net_yi"]))
            else round(float(rf.loc[d, "retail_net_yi"]), 2)
            for d in vol_tail
        ],
        "latest": {
            "date": vol_tail[-1].strftime("%Y-%m-%d") if len(vol_tail) else None,
            "turnover_yi": (
                round(float(base_view.loc[vol_tail[-1], "amt_total"]) / 1e4, 1)
                if len(vol_tail)
                else None
            ),
            "vw_breadth": (
                round(float(vw_breadth.get(vol_tail[-1])), 4)
                if len(vol_tail) and np.isfinite(float(vw_breadth.get(vol_tail[-1], np.nan)))
                else None
            ),
            **(latest_block(mf) if mf is not None and not mf.empty else {}),
        },
        "conventions": {
            "turnover_yi": "全市场成交额（亿元）",
            "vw_breadth": "(上涨股成交额 − 下跌股成交额) / 全市场成交额，−1~1；>0=成交额加权偏涨",
            "main_net_ratio": "(超大单+大单)净额 / 全市场成交额；主力视角的资金强度",
            "main_breadth": "主力净流入为正的个股占比（等权）",
            "main_breadth_amt": "主力净流入为正的个股成交额占比（成交额加权，更接近主力视角）",
        },
        "disclosure": "主力=超大单+大单；主力视角只描述当前资金结构，不构成方向预测。",
    }

    # 主力视角冷热分项（与广度分项同 UI 组件）；分位用扩张窗口，不用未来数据
    main_force_indicators = []
    main_force_series = {
        "main_net_ratio": ("主力净额/成交额", "%", 100.0, 2),
        "main_breadth": ("主力广度（等权）", "%", 100.0, 1),
        "main_breadth_amt": ("主力广度（成交额加权）", "%", 100.0, 1),
    }
    for key, (label, unit, scale, digits) in main_force_series.items():
        series = (
            mf[key].reindex(base_view.index)
            if mf is not None and key in mf
            else pd.Series(np.nan, index=base_view.index)
        )
        raw = float(series.iloc[-1]) if len(series) else np.nan
        pct_series = expanding_pct_rank(series, min_periods=60)
        pct = float(pct_series.iloc[-1]) if len(pct_series) else np.nan
        main_force_indicators.append(
            {
                "key": key,
                "label": label,
                "value": round(raw * scale, digits) if np.isfinite(raw) else None,
                "unit": unit,
                "pct": round(pct, 3) if np.isfinite(pct) else None,
                "zone": "low" if pct <= 0.1 else ("high" if pct >= 0.9 else "normal")
                if np.isfinite(pct)
                else "normal",
            }
        )
    # 成交额加权涨跌系数：−1~1，>0=成交额加权偏涨；作为“成交量加权系数”曲线
    vw_series = vw_breadth.reindex(base_view.index)
    raw_vw = float(vw_series.iloc[-1]) if len(vw_series) else np.nan
    pct_vw = expanding_pct_rank(vw_series, min_periods=60)
    main_force_indicators.append(
        {
            "key": "vw_breadth",
            "label": "成交额加权涨跌系数",
            "value": round(raw_vw, 4) if np.isfinite(raw_vw) else None,
            "unit": "",
            "pct": round(float(pct_vw.iloc[-1]), 3) if len(pct_vw) and np.isfinite(pct_vw.iloc[-1]) else None,
            "zone": "normal",
        }
    )
    mf_available = bool(
        mf is not None
        and not mf.empty
        and np.isfinite(float(mf["main_net_ratio"].iloc[-1]))
    )
    main_score_series = pd.Series(np.nan, index=base_view.index)
    main_pressure_series = pd.Series(np.nan, index=base_view.index)
    if mf is not None and not mf.empty:
        parts = []
        for key in ("main_net_ratio", "main_breadth", "main_breadth_amt"):
            ser = mf[key].astype(float).reindex(base_view.index)
            parts.append(expanding_pct_rank(ser, min_periods=120))
        main_score_series = (sum(parts) / len(parts)) * 100.0
        main_pressure_series = 100.0 - main_score_series
    main_force = {
        "latest": latest_block(mf) if mf is not None and not mf.empty else {},
        "available": mf_available,
        "available_from": mf_start,
        "score": (
            round(float(main_score_series.iloc[-1]), 1)
            if len(main_score_series) and np.isfinite(main_score_series.iloc[-1])
            else None
        ),
        "pressure": (
            round(float(main_pressure_series.iloc[-1]), 1)
            if len(main_pressure_series) and np.isfinite(main_pressure_series.iloc[-1])
            else None
        ),
        "history": {
            "dates": [d.strftime("%Y-%m-%d") for d in base_view.tail(120).index],
            "score": [None if not np.isfinite(float(v)) else round(float(v), 1) for v in main_score_series.tail(120)],
            "pressure": [None if not np.isfinite(float(v)) else round(float(v), 1) for v in main_pressure_series.tail(120)],
        },
        "indicators": main_force_indicators,
        "disclosure": "主力=超大单+大单；主力视角描述当前资金结构，不构成方向预测。主力派发分=100−主力买入强度。",
    }

    # 散户视角：唯一经历史验证的反向指标是「散户追涨相关」
    retail = {}
    if rf is not None and not rf.empty:
        rf_view = rf.reindex(base_view.index)
        chase = rf_view["retail_chase_corr"].astype(float)
        retail_available = bool(len(chase) and np.isfinite(float(chase.iloc[-1])))
        # 分数=原始追涨相关的线性映射（0.05→0, 0.30→100），阈值来自 2022-2026 验证：
        # >0.25 过热（之后偏弱），<0.10 冷/不追（之后偏强）。
        score_series = ((chase - 0.05) / 0.25 * 100.0).clip(lower=0, upper=100)
        latest_r = latest_retail_block(rf_view)
        raw_chase = float(chase.iloc[-1]) if len(chase) else np.nan
        score = float(score_series.iloc[-1]) if len(score_series) else np.nan
        if np.isfinite(score):
            latest_r["retail_score"] = round(score, 1)
        if not retail_available:
            verdict = {"label": "该日期无散户 L2 数据", "tone": "neutral", "hint": f"L2 资金流自 {rf_start or '2022-06'} 起可用，之前的日期只显示成交额口径"}
        elif np.isfinite(raw_chase) and raw_chase >= 0.25:
            verdict = {"label": "散户追涨过热", "tone": "warn", "hint": "反向警惕：散户在追近期赢家，之后偏弱"}
        elif np.isfinite(raw_chase) and raw_chase <= 0.10:
            verdict = {"label": "散户不追/回避", "tone": "good", "hint": "反向关注：散户没有追涨，之后偏强"}
        else:
            verdict = {"label": "散户情绪中性", "tone": "neutral", "hint": "没有极端反向信号"}
        def _pct(key: str) -> float | None:
            ser = rf_view[key].astype(float)
            pv = expanding_pct_rank(ser, min_periods=120)
            v = float(pv.iloc[-1]) if len(pv) else np.nan
            return round(v, 3) if np.isfinite(v) else None
        retail_indicators = [
            {
                "key": "retail_chase_corr",
                "label": "散户追涨相关",
                "value": None if not np.isfinite(chase.iloc[-1]) else round(float(chase.iloc[-1]), 4),
                "unit": "",
                "pct": _pct("retail_chase_corr"),
                "zone": "low" if (_pct("retail_chase_corr") or 0) <= 0.1 else ("high" if (_pct("retail_chase_corr") or 0) >= 0.9 else "normal"),
            },
            {
                "key": "retail_ratio",
                "label": "散户净额/成交额",
                "value": round(float(rf_view["retail_ratio"].iloc[-1]) * 100, 2) if np.isfinite(rf_view["retail_ratio"].iloc[-1]) else None,
                "unit": "%",
                "pct": _pct("retail_ratio"),
                "zone": "normal",
            },
            {
                "key": "retail_breadth_amt",
                "label": "散户广度（成交额加权）",
                "value": round(float(rf_view["retail_breadth_amt"].iloc[-1]) * 100, 1) if np.isfinite(rf_view["retail_breadth_amt"].iloc[-1]) else None,
                "unit": "%",
                "pct": _pct("retail_breadth_amt"),
                "zone": "normal",
            },
            {
                "key": "retail_divergence",
                "label": "主力−散户背离",
                "value": round(float(rf_view["divergence"].iloc[-1]) * 100, 2) if np.isfinite(rf_view["divergence"].iloc[-1]) else None,
                "unit": "%",
                "pct": _pct("divergence"),
                "zone": "normal",
            },
        ]
        retail = {
            "latest": latest_r,
            "available": retail_available,
            "available_from": rf_start,
            "verdict": verdict,
            "score": round(score, 1) if np.isfinite(score) else None,
            "thresholds": {"hot": 0.25, "cold": 0.10},
            "indicators": retail_indicators,
            "history": {
                "dates": [d.strftime("%Y-%m-%d") for d in base_view.tail(120).index],
                "score": [None if not np.isfinite(float(v)) else round(float(v), 1) for v in score_series.tail(120)],
                "chase_corr": [None if not np.isfinite(float(v)) else round(float(v), 4) for v in chase.tail(120)],
            },
            "disclosure": "散户=中单+小单。只有「散户追涨相关」经 2022-2026 历史验证具反向价值：高=散户追近期赢家→之后偏弱，低=散户不追→之后偏强；散户净流入/广度本身更像同步动量，不单独做反向。",
        }

    return {
        "trade_date": (
            all_view.index[-1] if all_view is not None else last_date
        ).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "QuantDB daily_backward · market_breadth",
        "horizon": HORIZON,
        "as_of": as_of is not None,
        "available_from": df.index[0].strftime("%Y-%m-%d"),
        "available_to": (
            all_view.index[-1] if all_view is not None else df.index[-1]
        ).strftime("%Y-%m-%d"),
        "dispersion": dispersion,
        "stress": stress_block(all_scores, all_pct),
        "indicators": indicator_list(
            all_view if all_view is not None else signal_df, all_feats
        ),
        "stress_history": (
            [
                {
                    "date": d.strftime("%Y-%m-%d"),
                    "stress": round(float(s), 1),
                    "breadth20": round(
                        float(all_sliced.loc[d, "pct_above_ma20"]) * 100, 1
                    ),
                }
                for d, s in all_scores.tail(STRESS_HISTORY_DAYS).items()
            ]
            if all_view is not None
            else history
        ),
        "factor_history": factor_history(all_feats),
        "distribution": distribution_all,
        "volume": volume,
        "main_force": main_force,
        "retail": retail,
        "indices": indices,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="生成大盘情绪快照 JSON")
    p.add_argument("--panel", default=str(PANEL_PATH))
    p.add_argument("--universe-panel", default=str(UNIVERSE_PANEL_PATH))
    p.add_argument("--index-ohlc", default=str(INDEX_OHLC_PATH))
    p.add_argument("--out", default=None, help="输出路径（默认快照根/sentiment.json）")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args(argv)
    df = load_panel(Path(args.panel))
    feats = build_features(df)
    uni = load_universe_panel(Path(args.universe_panel))
    ohlc = load_index_ohlc(Path(args.index_ohlc))
    if uni.empty:
        log.warning(
            "缺少分市场面板 %s —— 退回全市场口径（分市场 tab 不可用）",
            args.universe_panel,
        )
    payload = build_payload(df, feats, uni=uni if not uni.empty else None, ohlc=ohlc)

    root = QUANTDB_DIR / "5_technical_derived" / "market_breadth"
    out = Path(args.out) if args.out else root / "sentiment.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # 原子写：容器（root）与宿主机（zbox）都会跑这个脚本，原地 truncate 会让
    # 先写的一方把文件属主改成自己，另一方从此写不进去。临时文件 + os.replace
    # 只需要目录写权限，两边都能覆盖，API 也不会读到半截 JSON。
    tmp = out.with_name(f"{out.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, out)
    log.info(
        "写出 %s（%s，全A压力 %.1f %s，%d 个指数，%d 个指标）",
        out,
        payload["trade_date"],
        payload["stress"]["score"],
        payload["stress"]["zone_label"],
        len(payload["indices"]),
        len(payload["indicators"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
