#!/usr/bin/env python3
"""大盘情绪快照 —— 从广度面板生成前端「大盘情绪」面板所需的 JSON。

产出：``${QM_QUANTDB_DATA_DIR:-data/quantdb}/5_technical_derived/market_breadth/sentiment.json``

放在派生数据目录而不是 ``QM_MARKET_SNAPSHOT_DIR``：后者由容器以 root 创建，
宿主机上的定时任务没有写权限；派生产物与面板同目录，容器与宿主同路径可读。

设计取舍：
- 与市场分析模块既有模式一致：**预计算快照 + 只读 API**，API 进程零计算。
  广度分位是 O(n²) 的扩张窗口，放进请求路径会拖慢每一次刷新。
- 预测部分只给「历史同状态的条件分布」，不给点预测。广度对方向几乎没有
  稳定预测力（见 ``breadth_regime.md``），把它画成一条确定的预测线是骗人。
- 扇形图用的是**当前状态所在区间**的历史路径分位，而不是全样本 —— 换状态
  换形态，这一点必须在图上可见。
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

from backend.scripts.research_stats import expanding_pct_rank
from backend.scripts.index_forecast import forecast_index
from backend.scripts.breadth_regime import (
    INDICATORS,
    build_features,
    cluster_count,
    composite_scores,
    load_panel,
)

log = logging.getLogger("sentiment_snapshot")

#: QuantDB 根目录：容器内是 /data/quantdb，宿主机默认 data/quantdb。
#: 必须由 env 决定 —— 容器 cwd 是 /app，写死相对路径会找不到面板。
QUANTDB_DIR = Path(os.getenv("QM_QUANTDB_DATA_DIR", "data/quantdb"))
PANEL_PATH = QUANTDB_DIR / "5_technical_derived" / "market_breadth" / "trin_daily.parquet"
TARGETS = {"000300.SH": "沪深300", "000001.SH": "上证指数", "000852.SH": "中证1000"}
HORIZON = 20
HISTORY_DAYS = 250   # 供前端拖动回看；默认视窗仍是最近 60 日 + 20 日前瞻
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
    "breadth20", "breadth60", "trin_ema10", "adr",
    "udr", "mcclellan", "limit_net", "nh_nl",
)

ZONES = (
    (80.0, "extreme_pressure", "极端压力", "历史同状态偏多，但波动最大",
     "下跌后段 / 超卖区", "多数股票已经跌到低位，历史上其后 20 天上涨居多，但波动也最大"),
    (60.0, "pressure", "承压", "历史同状态无稳定方向",
     "弱势整理", "市场偏弱但没有极值信号，方向要等趋势确认"),
    (40.0, "neutral", "中性", "历史同状态接近基准",
     "震荡中段", "冷热都在中间，历史同状态没有稳定方向"),
    (20.0, "strong", "偏强", "历史同状态偏震荡",
     "上涨中段 / 偏强", "多数股票在上涨，但越往过热走，回撤概率越高"),
    (-1.0, "overheat", "过热", "大盘回撤概率高于基准",
     "上涨后段 / 过热", "普涨且成交放大，历史上其后大盘回撤概率高于平时"),
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
    """
    if as_of not in close.index:
        return []
    pos = close.index.get_loc(as_of)
    future = close.iloc[pos + 1 : pos + 1 + horizon]
    if len(future) < horizon:
        return []
    base = float(close.iloc[pos])
    if not np.isfinite(base) or base <= 0:
        return []
    return [{"k": 0, "pct": 0.0}] + [
        {"k": i + 1, "pct": round((float(v) / base - 1.0) * 100, 2)}
        for i, v in enumerate(future)
    ]


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


def build_payload(
    df: pd.DataFrame, feats: pd.DataFrame, as_of: pd.Timestamp | None = None
) -> dict:
    """构建情绪面板数据。``as_of`` 给定时，全部信号只用该日及以前的数据。

    无前视保证：切片后 stress / 分位 / 扇形 / 基准全部只在切片内计算；扇形的
    未来路径在切片内不足 20 天者自然被丢弃。真实走势另走 :func:`actual_path`
    从全量数据取，且只用于回看对照。
    """
    signal_df = df.loc[:as_of] if as_of is not None else df
    signal_feats = feats.loc[:as_of] if as_of is not None else feats
    if len(signal_df) < 600:
        raise ValueError("as_of 太早：可用历史不足 600 个交易日，无法计算分位")

    scores = composite_scores(signal_feats)
    score = float(scores["stress"].iloc[-1])
    score_pct = float(expanding_pct_rank(scores["stress"], min_periods=500).iloc[-1])
    zone_key, zone_label, zone_note, phase, phase_detail = stress_zone(score)
    mask = mask_from_zone(scores["stress"], score)

    indicators = []
    for ind in INDICATORS:
        if ind.name not in CORE_KEYS:
            continue
        unit, scale, digits = DISPLAY.get(ind.name, ("", 1.0, 3))
        raw = signal_df[ind.column].iloc[-1]
        pct = float(signal_feats[ind.name].iloc[-1])
        indicators.append(
            {
                "key": ind.name,
                "label": ind.note,
                "value": round(float(raw) * scale, digits) if np.isfinite(raw) else None,
                "unit": unit,
                "pct": round(pct, 3) if np.isfinite(pct) else None,
                "zone": "low" if pct <= 0.1 else ("high" if pct >= 0.9 else "normal"),
            }
        )

    history = []
    for d, s_val in scores["stress"].tail(STRESS_HISTORY_DAYS).items():
        history.append(
            {
                "date": d.strftime("%Y-%m-%d"),
                "stress": round(float(s_val), 1),
                "breadth20": round(float(signal_df.loc[d, "pct_above_ma20"]) * 100, 1),
            }
        )

    last_date = signal_df.index[-1]
    indices = {}
    for symbol, name in TARGETS.items():
        close = signal_df[f"close_{symbol}"]
        last = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        hist = [
            {"date": d.strftime("%Y-%m-%d"), "close": round(float(c), 2)}
            for d, c in close.tail(HISTORY_DAYS).items()
        ]
        indices[symbol] = {
            "name": name,
            "last": round(last, 2),
            "change_pct": round((last / prev - 1.0) * 100, 2) if prev else None,
            "history": hist,
            "fan": fan_bands(close, mask),
            "stats": index_stats(close, mask),
            "baseline": baseline_stats(close),
            "actual": (
                actual_path(df[f"close_{symbol}"], last_date) if as_of is not None else []
            ),
            "forecast": forecast_index(df, feats, symbol, last_date),
            "future_dates": [
                d.strftime("%Y-%m-%d")
                for d in df.index[df.index.get_loc(last_date) + 1 : df.index.get_loc(last_date) + 1 + HORIZON]
            ],
        }

    return {
        "trade_date": last_date.strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "QuantDB daily_backward · market_breadth",
        "horizon": HORIZON,
        "as_of": as_of is not None,
        "available_from": df.index[0].strftime("%Y-%m-%d"),
        "available_to": df.index[-1].strftime("%Y-%m-%d"),
        "dispersion": {
            "value": round(float(signal_df["ret_disp"].iloc[-1]), 2),
            "pct": (
                round(float(expanding_pct_rank(signal_df["ret_disp"], 500).iloc[-1]), 3)
                if np.isfinite(signal_df["ret_disp"].iloc[-1]) else None
            ),
        },
        "stress": {
            "score": round(score, 1),
            "zone": zone_key,
            "zone_label": zone_label,
            "zone_note": zone_note,
            "phase": phase,
            "phase_detail": phase_detail,
            "percentile": round(score_pct, 3) if np.isfinite(score_pct) else None,
        },
        "indicators": indicators,
        "stress_history": history,
        "factor_history": factor_history(signal_feats),
        "indices": indices,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="生成大盘情绪快照 JSON")
    p.add_argument("--panel", default=str(PANEL_PATH))
    p.add_argument("--out", default=None, help="输出路径（默认快照根/sentiment.json）")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    df = load_panel(Path(args.panel))
    feats = build_features(df)
    payload = build_payload(df, feats)

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
        "写出 %s（%s，压力分 %.1f %s，%d 个指标）",
        out, payload["trade_date"], payload["stress"]["score"],
        payload["stress"]["zone_label"], len(payload["indicators"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
