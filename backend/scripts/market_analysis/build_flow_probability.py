#!/usr/bin/env python3
"""资金流概率模型（离线）：订单流 + 量价速度 + 板块状态 → 未来 h 日跑赢概率。

模型：LightGBM 二分类（label = 可执行行业中性超额 >0），walk-forward 留出
2025-2026 报 OOS 指标；最终用截至最新可得标签的全量样本重训，预测最新交易日。

纪律：
- 特征只用 t 日及以前；标签 t+1 收盘建仓 → t+1+h 收盘；
- 训练集与测试集之间 purge h+2 个交易日，避免重叠标签泄漏；
- 模型概率是**排序分**，不是校准后的真实胜率；页面同时给 OOS AUC / 十分位统计；
- 板块不做独立模型（板块层资金流实测无稳定 alpha）；板块概率 = 成分股概率按成交额加权，
  只作构成/风险聚合，不是板块预测。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import lightgbm as lgb
import numpy as np
from sklearn.isotonic import IsotonicRegression
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.shared.flow_quality import COMPONENT_COLUMNS  # noqa: E402
from backend.shared.stock_utils import StockCodeUtil  # noqa: E402
from backend.shared.quantdb_flow_units import normalize_l2_flow_money_to_yuan  # noqa: E402

log = logging.getLogger("build_flow_probability")

L2_REL = "6_ml_datasets/l2_factors"
PX_REL = "1_kline_data/daily_forward"
VAL_REL = "5_technical_derived/valuation"
MEMBERS_REL = "2_base_sector/sector_concept/sector_members.parquet"
ST_REL = "2_base_sector/st_pit/st_pit.parquet"
INST_REL = "2_base_sector/instrument_detail/instrument_list.parquet"
SECTOR_PANEL_REL = "market-analysis/flow_evidence/panel_sector.parquet"
HORIZONS = (3, 5, 10, 20)
START_DEFAULT = "20220101"
SAMPLE_STEP = 2
MIN_AMOUNT_YUAN = 5e7
MIN_PRICE = 2.0
SIZE_FLOOR_PCT = 0.3
PLACEHOLDER_SHARE = 25

PARAMS = {
    "objective": "binary",
    "num_leaves": 31,
    "learning_rate": 0.05,
    "n_estimators": 300,
    "min_child_samples": 200,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "n_jobs": 8,
    "verbose": -1,
}

CONVENTIONS = {
    "exec_definition": "t 日收盘信号 → t+1 收盘建仓 → t+1+h 收盘",
    "label": "行业中性后减当日过滤后宇宙均值 > 0（二分类）",
    "universe": "剔除 ST / 收盘<2 元 / 成交额<5000 万 / 流通市值后 30%",
    "features": "订单流结构 + 量价速度 + 板块状态（无未来数据）",
    "probability_note": "模型分是排序分，不是校准后的真实胜率；单票超额左偏，关注日期级篮子指标",
    "sector_note": "板块不做独立模型；板块概率＝成分股概率成交额加权，仅作构成/风险聚合",
    "sample": f"每 {SAMPLE_STEP} 个交易日取样训练；OOS=2025-2026",
}


def _dates(data_dir: Path, rel: str) -> list[str]:
    d = data_dir / rel
    if not d.is_dir():
        return []
    return sorted(
        e.name[3:]
        for e in d.iterdir()
        if e.is_dir() and e.name.startswith("dt=") and e.name[3:].isdigit()
    )


def _load_flow(data_dir: Path, days: list[str]) -> pd.DataFrame:
    files = [str(data_dir / L2_REL / f"dt={d}" / "data.parquet") for d in days]
    files = [f for f in files if Path(f).exists()]
    if not files:
        return pd.DataFrame()
    con = duckdb.connect()
    try:
        df = con.execute(
            f"""SELECT symbol, dt, amount, volume, open, high, low, close,
                       flow_net_amount, flow_order_imbalance,
                       {",".join(COMPONENT_COLUMNS)}
                FROM read_parquet({files!r})"""
        ).df()
    finally:
        con.close()
    if df.empty:
        return df
    df["dt"] = df["dt"].astype(str)
    df = normalize_l2_flow_money_to_yuan(df)
    dup = df.groupby(["dt", "flow_net_amount"])["symbol"].transform("size")
    df = df[dup < PLACEHOLDER_SHARE].copy()
    df["net_ratio"] = df["flow_net_amount"].astype(float) / (
        df["amount"].astype(float) * 1e4
    )
    return df


def _pivot(
    df: pd.DataFrame, col: str, dates: list[str], symbols: pd.Index
) -> pd.DataFrame:
    return (
        df.pivot(index="dt", columns="symbol", values=col)
        .reindex(index=dates, columns=symbols)
        .sort_index()
    )


def _load_prices(data_dir: Path, days: list[str]) -> pd.DataFrame:
    files = [str(data_dir / PX_REL / f"dt={d}" / "data.parquet") for d in days]
    files = [f for f in files if Path(f).exists()]
    if not files:
        return pd.DataFrame()
    con = duckdb.connect()
    try:
        df = con.execute(
            f"SELECT symbol, time, close FROM read_parquet({files!r})"
        ).df()
    finally:
        con.close()
    df["dt"] = df["time"].dt.strftime("%Y%m%d")
    return df.drop(columns=["time"])


def _load_float_mv(data_dir: Path, days: list[str]) -> pd.DataFrame:
    files = [str(data_dir / VAL_REL / f"dt={d}" / "data.parquet") for d in days]
    files = [f for f in files if Path(f).exists()]
    if not files:
        return pd.DataFrame()
    con = duckdb.connect()
    try:
        df = con.execute(
            f"SELECT symbol, time, float_mv FROM read_parquet({files!r})"
        ).df()
    finally:
        con.close()
    df["dt"] = df["time"].dt.strftime("%Y%m%d")
    return df.drop(columns=["time"])


def _industry(data_dir: Path, symbols: pd.Index) -> pd.Series:
    p = data_dir / MEMBERS_REL
    if not p.is_file():
        return pd.Series(index=symbols, dtype=object)
    df = pd.read_parquet(p)
    df = df[df["SectorType"] == "行业板块(二级)"].drop_duplicates("Symbol")
    return df.set_index("Symbol")["SectorName"].reindex(symbols)


def _st_mask(data_dir: Path, dates: list[str], symbols: pd.Index) -> pd.DataFrame:
    p = data_dir / ST_REL
    out = np.zeros((len(dates), len(symbols)), dtype=bool)
    if not p.is_file():
        return pd.DataFrame(out, index=dates, columns=symbols)
    st = pd.read_parquet(p)
    if st.empty:
        return pd.DataFrame(out, index=dates, columns=symbols)
    st["dt"] = st["time"].astype(str)
    by_date = {d: set(g["symbol"]) for d, g in st.groupby("dt")}
    pos = {s: i for i, s in enumerate(symbols)}
    keys = sorted(by_date)
    for di, d in enumerate(dates):
        prior = [k for k in keys if k <= d]
        if not prior:
            continue
        for sym in by_date[prior[-1]]:
            j = pos.get(sym)
            if j is not None:
                out[di, j] = True
    return pd.DataFrame(out, index=dates, columns=symbols)


def _ind_neutral(x: pd.DataFrame, industry: pd.Series) -> pd.DataFrame:
    out = x.sub(x.mean(axis=1, skipna=True), axis=0)
    for name, cols in x.T.groupby(industry.fillna("__NA__")):
        if name == "__NA__" or len(cols) < 5:
            continue
        sub = x[cols.index]
        out[cols.index] = sub.sub(sub.mean(axis=1, skipna=True), axis=0)
    return out


def _sector_features(
    data_dir: Path, dates: list[str], symbols: pd.Index, industry: pd.Series
) -> dict[str, pd.DataFrame]:
    candidates = [
        data_dir.parent / SECTOR_PANEL_REL,
        Path("/data") / SECTOR_PANEL_REL,
        Path("data") / SECTOR_PANEL_REL,
    ]
    p = next((c for c in candidates if c.is_file()), None)
    if p is None:
        return {}
    sp = pd.read_parquet(p)
    sp["dt"] = sp["dt"].astype(str)
    out: dict[str, pd.DataFrame] = {}
    for col, name in (
        ("net_ratio", "sec_net_ratio"),
        ("breadth", "sec_breadth"),
        ("mom3", "sec_mom3"),
        ("streak", "sec_streak"),
    ):
        piv = sp.pivot(index="dt", columns="sector", values=col).reindex(index=dates)
        arr = np.full((len(dates), len(symbols)), np.nan, dtype=np.float32)
        for j, sym in enumerate(symbols):
            sec_name = industry.get(sym)
            if isinstance(sec_name, str) and sec_name in piv.columns:
                arr[:, j] = piv[sec_name].to_numpy()
        out[name] = pd.DataFrame(arr, index=dates, columns=symbols)
    return out


def _build_panel(data_dir: Path, start: str, max_days: int | None):
    l2_all = [d for d in _dates(data_dir, L2_REL) if d >= start]
    if max_days:
        l2_all = l2_all[-max_days:]
    l2 = l2_all[::SAMPLE_STEP]
    # 最新 3 天必须进面板：取样步长可能跳过最新完整交易日
    for d in l2_all[-3:]:
        if d not in l2:
            l2.append(d)
    l2 = sorted(set(l2))
    px_dates = [d for d in _dates(data_dir, PX_REL) if d >= l2[0]]
    val_dates = [d for d in px_dates if (data_dir / VAL_REL / f"dt={d}").is_dir()]
    flow = _load_flow(data_dir, l2)
    px = _load_prices(data_dir, px_dates)
    val = _load_float_mv(data_dir, val_dates)
    if flow.empty or px.empty:
        raise RuntimeError("L2 或价格数据为空")
    close = (
        px.pivot(index="dt", columns="symbol", values="close")
        .reindex(index=l2)
        .sort_index()
    )
    symbols = close.columns
    dates = list(close.index)
    fmv = (
        val.pivot(index="dt", columns="symbol", values="float_mv").reindex(
            index=dates, columns=symbols
        )
        if not val.empty
        else pd.DataFrame(np.nan, index=dates, columns=symbols)
    )
    amount = _pivot(flow, "amount", dates, symbols) * 1e4
    volume = _pivot(flow, "volume", dates, symbols)
    op = _pivot(flow, "open", dates, symbols)
    hi = _pivot(flow, "high", dates, symbols)
    lo = _pivot(flow, "low", dates, symbols)
    l2close = _pivot(flow, "close", dates, symbols)
    net = _pivot(flow, "net_ratio", dates, symbols)
    z20 = (net - net.rolling(20, min_periods=10).mean()) / net.rolling(
        20, min_periods=10
    ).std()
    r1 = close / close.shift(1) - 1
    r3 = close / close.shift(3) - 1
    r5 = close / close.shift(5) - 1
    r20 = close / close.shift(20) - 1
    r60 = close / close.shift(60) - 1
    vol20 = ((close / close.shift(1) - 1) * 100).rolling(20, min_periods=10).std()
    vol5 = ((close / close.shift(1) - 1) * 100).rolling(5, min_periods=3).std()
    turn = amount / fmv
    features: dict[str, pd.DataFrame] = {
        "net_ratio": net,
        "z20": z20,
        "consistency": _pivot(flow, "flow_consistency", dates, symbols),
        "sellp": _pivot(flow, "micro_trade_sell_pressure", dates, symbols),
        "interval": _pivot(flow, "micro_trade_interval_mean", dates, symbols),
        "vpin_ratio": _pivot(flow, "micro_vpin_vol_ratio", dates, symbols),
        "vpin_z": _pivot(flow, "micro_vpin_zscore_20", dates, symbols),
        "cancel_depth": _pivot(flow, "flow_cancel_depth_dist", dates, symbols),
        "big_trade": _pivot(flow, "flow_big_trade_ratio", dates, symbols),
        "order_imb": _pivot(flow, "flow_order_imbalance", dates, symbols),
        "r1": r1,
        "r3": r3,
        "r5": r5,
        "r20": r20,
        "r60": r60,
        "amt_ratio20": amount / amount.rolling(20, min_periods=10).mean(),
        "amt_chg5": amount / amount.shift(5) - 1,
        "turn": turn,
        "turn_chg5": turn / turn.shift(5) - 1,
        "vol20": vol20,
        "vol_ratio5": vol5 / vol20,
        "range": (hi - lo) / close.shift(1),
        "close_pos": (l2close - lo) / (hi - lo).replace(0, np.nan),
        "gap": op / close.shift(1) - 1,
        "intraday_ret": l2close / op - 1,
        "amihud": np.log1p(np.abs(r1) * 1e12 / amount),
        "vp_corr20": r1.rolling(20, min_periods=10).corr(
            volume.pct_change(fill_method=None)
        ),
    }
    industry = _industry(data_dir, symbols)
    features.update(_sector_features(data_dir, dates, symbols, industry))
    st = _st_mask(data_dir, dates, symbols)
    mask = (
        close.notna()
        & (amount >= MIN_AMOUNT_YUAN)
        & (close >= MIN_PRICE)
        & (fmv.rank(axis=1, pct=True) >= SIZE_FLOOR_PCT)
        & (~st)
    )
    exec_ = {
        h: (close.shift(-(h + 1)) / close.shift(-1) - 1.0) * 100.0 for h in HORIZONS
    }
    rel = {}
    for h in HORIZONS:
        neutral = _ind_neutral(exec_[h], industry)
        rel[h] = neutral.sub(neutral.where(mask).mean(axis=1, skipna=True), axis=0)
    features = {k: v.replace([np.inf, -np.inf], np.nan) for k, v in features.items()}
    names = pd.read_parquet(data_dir / INST_REL)[["Symbol", "Name"]].drop_duplicates(
        "Symbol"
    )
    names = names.set_index("Symbol")["Name"]
    return {
        "dates": dates,
        "symbols": symbols,
        "features": features,
        "mask": mask,
        "rel": rel,
        "net": net,
        "z20": z20,
        "r5": r5,
        "amount": amount,
        "industry": industry,
        "names": names,
        "latest_flow": flow[flow["dt"] == dates[-1]][
            ["symbol", "flow_net_amount", "amount"]
        ],
    }


def _flatten(features: dict[str, pd.DataFrame], symbols: pd.Index):
    names = list(features)
    X = np.column_stack([features[k].to_numpy(dtype=np.float32).ravel() for k in names])
    return X, names


def _train_predict(
    X: np.ndarray,
    y: np.ndarray,
    mask_flat: np.ndarray,
    date_idx: np.ndarray,
    years: np.ndarray,
    h: int,
) -> tuple[np.ndarray, dict, lgb.LGBMClassifier | None, IsotonicRegression | None]:
    """2025-2026 留出指标 + 全量重训模型（用于最新预测）。"""
    ok = mask_flat & np.isfinite(y)
    preds = np.full(len(y), np.nan, dtype=np.float32)
    starts = np.where(years >= 2025)[0]
    train = np.array([], dtype=int)
    test = np.array([], dtype=int)
    if len(starts):
        cut = int(starts[0]) - h - 2
        train = np.where(ok & (date_idx < cut))[0]
        test = np.where(ok & (years[date_idx] >= 2025))[0]
    model = None
    calibrator = None
    metrics: dict = {}
    if len(train) and len(test):
        model = lgb.LGBMClassifier(**PARAMS)
        model.fit(X[train], (y[train] > 0).astype(np.int8))
        preds[test] = model.predict_proba(X[test])[:, 1]
        from sklearn.metrics import roc_auc_score

        label = (y[test] > 0).astype(np.int8)
        auc = float(roc_auc_score(label, preds[test]))
        top_means, bot_means, top_hits, base_hits = [], [], [], []
        for d in np.unique(date_idx[test]):
            ii = test[date_idx[test] == d]
            if len(ii) < 100:
                continue
            p = preds[ii]
            yy = y[ii]
            k = max(10, len(ii) // 10)
            order = np.argsort(-p)
            top_means.append(float(yy[order[:k]].mean()))
            bot_means.append(float(yy[order[-k:]].mean()))
            top_hits.append(float((yy[order[:k]] > 0).mean()))
            base_hits.append(float((yy > 0).mean()))
        calibrator = IsotonicRegression(out_of_bounds="clip").fit(preds[test], label)
        metrics = {
            "auc": round(auc, 4),
            "top_mean": round(float(np.mean(top_means)), 3),
            "bot_mean": round(float(np.mean(bot_means)), 3),
            "top_hit": round(float(np.mean(top_hits)), 3),
            "base_hit": round(float(np.mean(base_hits)), 3),
            "n_days": len(top_means),
        }
    # 全量重训（标签已走完的日期）
    latest = int(np.max(date_idx[ok])) if ok.any() else int(date_idx.max())
    train_all = np.where(ok & (date_idx < latest - h - 2))[0]
    final = None
    if len(train_all):
        final = lgb.LGBMClassifier(**PARAMS)
        final.fit(X[train_all], (y[train_all] > 0).astype(np.int8))
    return preds, metrics, final, calibrator


def build(
    data_dir: Path, out_dir: Path, start: str, max_days: int | None = None
) -> dict:
    started = time.time()
    panel = _build_panel(data_dir, start, max_days)
    dates = panel["dates"]
    symbols = panel["symbols"]
    mask = panel["mask"]
    X, feat_names = _flatten(panel["features"], symbols)
    mask_flat = mask.to_numpy().ravel()
    date_idx = np.repeat(np.arange(len(dates)), len(symbols))
    years = pd.to_datetime(pd.Index(dates), format="%Y%m%d").year
    log.info("面板 %d 日 × %d 标的，特征 %d", len(dates), len(symbols), len(feat_names))

    out_dir.mkdir(parents=True, exist_ok=True)
    metrics: dict[str, dict] = {}
    latest_probs: dict[int, np.ndarray] = {}
    latest_raw: dict[int, np.ndarray] = {}
    for h in HORIZONS:
        y = panel["rel"][h].to_numpy().ravel()
        _, met, final, calibrator = _train_predict(X, y, mask_flat, date_idx, years, h)
        metrics[str(h)] = met
        if final is not None:
            final.booster_.save_model(str(out_dir / f"model_h{h}.txt"))
            valid_dates = np.where(mask.sum(axis=1).to_numpy() > 0)[0]
            last = int(valid_dates[-1]) if len(valid_dates) else len(dates) - 1
            idx = np.where(mask_flat & (date_idx == last))[0]
            if len(idx) == 0:
                continue
            raw = final.predict_proba(X[idx])[:, 1]
            latest_raw[h] = raw
            latest_probs[h] = calibrator.predict(raw) if calibrator is not None else raw
        log.info("h=%d 指标=%s", h, met)

    valid_dates = np.where(mask.sum(axis=1).to_numpy() > 0)[0]
    last = int(valid_dates[-1]) if len(valid_dates) else len(dates) - 1
    latest_syms = symbols[mask.iloc[last].to_numpy()]

    # 最新日的原始行特征（用于展示）
    def _last(frame: pd.DataFrame) -> pd.Series:
        return frame.iloc[-1].reindex(latest_syms)

    net_last = _last(panel["net"])
    z_last = _last(panel["z20"])
    r5_last = _last(panel["r5"])
    amount_last = _last(panel["amount"])
    names = panel["names"]
    industry = panel["industry"]
    records = []
    for i, sym in enumerate(latest_syms):
        item = {
            "symbol": StockCodeUtil.to_prefix(str(sym)),
            "name": str(names.get(sym, "") or ""),
            "industry": industry.get(sym),
            "net_ratio": _r(net_last.get(sym), 4),
            "z20": _r(z_last.get(sym), 2),
            "r5": _r(
                r5_last.get(sym) * 100 if pd.notna(r5_last.get(sym)) else np.nan, 2
            ),
            "amount": _r(amount_last.get(sym), 0),
        }
        for h in HORIZONS:
            arr = latest_probs.get(h)
            raw = latest_raw.get(h)
            item[f"p{h}"] = round(float(arr[i]), 4) if arr is not None else None
            item[f"raw{h}"] = round(float(raw[i]), 4) if raw is not None else None
        records.append(item)

    thresholds = {}
    for h in HORIZONS:
        vals = [r[f"raw{h}"] for r in records if r.get(f"raw{h}") is not None]
        thresholds[str(h)] = {
            "hi": round(float(np.quantile(vals, 0.8)), 4) if vals else None,
            "lo": round(float(np.quantile(vals, 0.2)), 4) if vals else None,
        }

    def _rank_top(h: int, n: int = 20, reverse: bool = False):
        key = f"raw{h}"
        vals = [r for r in records if r.get(key) is not None]
        vals.sort(key=lambda r: r[key], reverse=not reverse)
        return vals[:n]

    stocks = {}
    for h in HORIZONS:
        ranked = _rank_top(h, n=len(records))
        pvals = [r[f"raw{h}"] for r in records if r.get(f"raw{h}") is not None]
        hi = float(np.quantile(pvals, 0.9)) if pvals else 1.0
        lo = float(np.quantile(pvals, 0.1)) if pvals else 0.0
        stocks[f"top_{h}"] = ranked[:20]
        stocks[f"bottom_{h}"] = list(reversed(ranked[-20:]))
        stocks[f"outflow_strong_{h}"] = [
            r for r in ranked if (r["net_ratio"] or 0) < 0 and r[f"raw{h}"] >= hi
        ][:20]
        stocks[f"inflow_weak_{h}"] = [
            r
            for r in reversed(ranked)
            if (r["net_ratio"] or 0) > 0 and r[f"raw{h}"] <= lo
        ][:20]
    prob_map = {
        r["symbol"]: {str(h): r.get(f"raw{h}") for h in HORIZONS} for r in records
    }
    stocks["prob_map"] = prob_map

    # 板块聚合：成交额加权概率 + 当前净额方向（不是独立板块模型）
    sectors: dict[str, dict] = {}
    for r in records:
        name = r["industry"] or "未分类"
        w = float(r["amount"] or 0.0)
        bucket = sectors.setdefault(
            name,
            {
                "name": name,
                "w": 0.0,
                "p3": 0.0,
                "p5": 0.0,
                "p10": 0.0,
                "p20": 0.0,
                "n": 0,
                "net": 0.0,
            },
        )
        bucket["w"] += w
        bucket["n"] += 1
        bucket["net"] += w * float(r["net_ratio"] or 0.0)
        for h in HORIZONS:
            p = r.get(f"p{h}")
            if p is not None:
                bucket[f"p{h}"] += w * float(p)
    sector_rows = []
    for b in sectors.values():
        if b["w"] <= 0:
            continue
        row = {"name": b["name"], "n": b["n"], "net_ratio": round(b["net"] / b["w"], 4)}
        for h in HORIZONS:
            row[f"p{h}"] = round(b[f"p{h}"] / b["w"], 4)
        # 板块内高质量股占比（p5 前 20%）
        sector_rows.append(row)
    sector_rows.sort(key=lambda r: r["p5"], reverse=True)

    artifact = {
        "schema": "flow_probability.v1",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "trade_date": dates[last],
        "window": [dates[0], dates[last]],
        "features": feat_names,
        "conventions": CONVENTIONS,
        "metrics": metrics,
        "thresholds": thresholds,
        "latest": {
            "stocks": stocks,
            "prob_map": prob_map,
            "sectors": sector_rows,
        },
    }
    path = out_dir / "flow_probability.json"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(artifact, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    log.info(
        "写出 %s（%d 只股票，%d 个板块，用时 %.0fs）",
        path,
        len(records),
        len(sector_rows),
        time.time() - started,
    )
    return artifact


def _r(v, digits: int):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, digits) if np.isfinite(f) else None


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(description="构建资金流概率模型产物")
    p.add_argument(
        "--data-dir", default=os.getenv("QM_QUANTDB_DATA_DIR", "data/quantdb")
    )
    p.add_argument("--out-dir", default=None)
    p.add_argument("--start", default=START_DEFAULT)
    p.add_argument("--max-days", type=int, default=None)
    args = p.parse_args(argv)
    out = args.out_dir
    if not out:
        out = str(Path(args.data_dir).parent / "market-analysis" / "flow_probability")
    build(Path(args.data_dir), Path(out), args.start, args.max_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
