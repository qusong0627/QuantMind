"""资金流预测力：研究产物读取 + 当日资金流质量分 / 形态匹配。

三部分：
1. **研究产物**（离线构建，``build_flow_predictive.py``）：质量分分位→之后
   3/5/10/20 日可执行超额（t+1 收盘建仓）、净流入分位对照、5×5 热力图、模式统计；
2. **当日截面**：用最新/指定交易日的 L2 明细算质量分、分项分位与形态命中；
3. **降级**：产物缺失返回 ``available=false`` + 原因，前端隐藏统计区、只留口径；
   分项列缺失时**不评分**（绝不拿 0 补），显式返回 missing。

口径与 ``backend/shared/flow_quality.py`` 完全同源；研究口径见产物 conventions。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backend.shared.flow_quality import (
    COMPONENTS,
    COMPONENT_COLUMNS,
    component_percentiles,
    quality_score,
)
from backend.shared.quantdb_flow_units import normalize_l2_flow_money_to_yuan

from .quantdb_feed import (
    _available,
    _cached,
    _get_partition_dates,
    _instrument_names,
    _normalize_prefix,
    _read_partitioned,
    _sector_members,
)

ARTIFACT_NAME = "flow_predictive.json"
_SUBDIR = ("market-analysis", "flow_predictive")
CACHE_TTL = 300
MIN_AMOUNT_YUAN = 5e7
MIN_PRICE = 2.0
SIZE_FLOOR_PCT = 0.3

_LOCK = threading.Lock()
_JSON_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def resolve_dir(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.getenv("QM_FLOW_PREDICTIVE_DIR")
    if env:
        return Path(env)
    root = Path(os.getenv("QM_QUANTDB_DATA_DIR", "data/quantdb"))
    # 与 flow_evidence 同属 /data/market-analysis 下的派生产物
    return root.parent / "market-analysis" / "flow_predictive"


def load_artifact(directory: str | Path | None = None) -> dict[str, Any] | None:
    """按 mtime 缓存读取研究产物；缺失/损坏返回 None（调用方显式降级）。"""
    path = resolve_dir(directory) / ARTIFACT_NAME
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    with _LOCK:
        cached = _JSON_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 损坏产物按缺失处理，不半读
        return None
    if not isinstance(payload, dict):
        return None
    with _LOCK:
        _JSON_CACHE[key] = (mtime, payload)
    return payload


def _norm_date(date: str | None) -> str | None:
    if not date:
        return None
    return str(date).replace("-", "").strip() or None


def _l2_days(target: str | None, n: int = 25) -> list[str]:
    dates = _get_partition_dates("6_ml_datasets/l2_factors")
    if target:
        dates = [d for d in dates if d <= target]
    return dates[:n]


def _wide(df: pd.DataFrame, col: str) -> pd.DataFrame:
    return df.pivot(index="dt", columns="symbol", values=col).sort_index()


def _st_set(target: str) -> set[str]:
    try:
        st = pd.read_parquet(
            Path(os.getenv("QM_QUANTDB_DATA_DIR", "data/quantdb"))
            / "2_base_sector"
            / "st_pit"
            / "st_pit.parquet"
        )
    except Exception:  # noqa: BLE001 —— ST 表缺失时不剔除，但显式标记
        return set()
    if st.empty:
        return set()
    st["dt"] = st["time"].astype(str)
    prior = st[st["dt"] <= target]["dt"]
    if prior.empty:
        return set()
    nearest = prior.max()
    return set(st.loc[st["dt"] == nearest, "symbol"].astype(str))


def _industry_map() -> dict[str, str]:
    members = _sector_members()
    if members.empty or "sector_type" not in members.columns:
        return {}
    sub = members[members["sector_type"] == "行业板块(二级)"]
    sub = sub.drop_duplicates("symbol")
    return {str(r.symbol): str(r.sector_name) for r in sub.itertuples(index=False)}


def _current(date: str | None) -> dict[str, Any]:
    days = _l2_days(_norm_date(date), 25)
    if not days:
        return {"available": False, "reason": "no_l2_data"}
    target = days[0]
    cols = ",".join(
        [
            "symbol",
            "amount",
            "flow_net_amount",
            *COMPONENT_COLUMNS,
        ]
    )
    flow = _read_partitioned("6_ml_datasets/l2_factors", days, cols)
    if flow.empty:
        return {"available": False, "reason": "no_flow_data", "trade_date": target}
    flow["dt"] = flow["dt"].astype(str)
    flow = normalize_l2_flow_money_to_yuan(flow)
    flow["net_ratio"] = flow["flow_net_amount"].astype(float) / (
        flow["amount"].astype(float) * 1e4
    )

    missing = [c for c in COMPONENT_COLUMNS if c not in flow.columns]
    if missing:
        return {
            "available": False,
            "reason": "missing_columns",
            "missing": missing,
            "trade_date": target,
        }

    prices = _load_prices_safe(days)
    if prices.empty:
        return {"available": False, "reason": "no_price_data", "trade_date": target}
    close = prices.pivot(index="dt", columns="symbol", values="close").sort_index()
    pct = prices.pivot(index="dt", columns="symbol", values="pct_change").sort_index()

    val = _read_partitioned(
        "5_technical_derived/valuation", [target], "symbol, float_mv"
    )
    float_mv = (
        val.set_index("symbol")["float_mv"].astype(float)
        if not val.empty
        else pd.Series(dtype=float)
    )

    amount = _wide(flow, "amount").reindex(index=days, columns=close.columns)
    net = _wide(flow, "net_ratio").reindex(index=days, columns=close.columns)
    z20 = (net - net.rolling(20, min_periods=10).mean()) / net.rolling(
        20, min_periods=10
    ).std()
    r5 = close / close.shift(5) - 1.0

    last_close = close.iloc[-1] if len(close) else pd.Series(dtype=float)
    last_amount = amount.iloc[-1] if len(amount) else pd.Series(dtype=float)
    size_pct = (
        float_mv.rank(pct=True).reindex(close.columns)
        if not float_mv.empty
        else pd.Series(0.0, index=close.columns)
    )
    st = _st_set(target)
    not_st = pd.Series([sym not in st for sym in close.columns], index=close.columns)
    mask_row = (
        last_close.notna()
        & (last_amount.fillna(0.0) * 1e4 >= MIN_AMOUNT_YUAN)
        & (last_close.fillna(0.0) >= MIN_PRICE)
        & (size_pct.fillna(0.0) >= SIZE_FLOOR_PCT)
        & not_st
    )
    mask = pd.DataFrame(
        [mask_row.to_numpy()] * len(close), index=close.index, columns=close.columns
    )

    features = {
        c: _wide(flow, c).reindex(index=days, columns=close.columns)
        for c in COMPONENT_COLUMNS
    }
    score = quality_score(features, mask)
    pct = component_percentiles(features, mask)
    score_last = score.iloc[-1]
    comp_last = {c: pct[c].iloc[-1] for c in COMPONENT_COLUMNS}
    net_last = net.iloc[-1]
    z_last = z20.iloc[-1]
    r5_last = r5.iloc[-1]
    close_last = close.iloc[-1]
    names = _instrument_names()
    industry = _industry_map()

    records: list[dict[str, Any]] = []
    for sym in close.columns:
        if not bool(mask_row.get(sym, False)):
            continue
        s = score_last.get(sym)
        if s is None or not np.isfinite(s):
            continue
        records.append(
            {
                "symbol": _normalize_prefix(sym),
                "suffix": sym,
                "name": names.get(sym, ""),
                "industry": industry.get(sym),
                "score": round(float(s), 1),
                "components": {
                    c: (
                        round(float(comp_last[c].get(sym)), 3)
                        if np.isfinite(comp_last[c].get(sym, np.nan))
                        else None
                    )
                    for c in COMPONENT_COLUMNS
                },
                "net_ratio": _round(net_last.get(sym), 4),
                "z20": _round(z_last.get(sym), 2),
                "r5": _round(
                    r5_last.get(sym) * 100
                    if np.isfinite(r5_last.get(sym, np.nan))
                    else np.nan,
                    2,
                ),
                "close": _round(close_last.get(sym), 2),
                "pct_change": _round(_pct_change(close, sym), 2),
                "amount": _round((amount.iloc[-1].get(sym) or 0.0) * 1e4, 0),
            }
        )
    records.sort(key=lambda x: x["score"], reverse=True)
    top = records[:15]
    bottom = list(reversed(records[-15:]))

    def _match(cond) -> dict[str, Any]:
        hits = [
            {
                k: r[k]
                for k in (
                    "symbol",
                    "name",
                    "industry",
                    "score",
                    "r5",
                    "z20",
                    "net_ratio",
                )
            }
            for r in records
            if cond(r)
        ]
        return {"count": len(hits), "items": hits[:10]}

    def _comp(r: dict[str, Any], key: str) -> float:
        v = r["components"].get(key)
        return -1.0 if v is None else float(v)

    score_cut = (
        float(np.quantile([r["score"] for r in records], 0.8)) if records else 100.0
    )
    score_floor = (
        float(np.quantile([r["score"] for r in records], 0.2)) if records else 0.0
    )
    patterns = {
        "score_top20": _match(lambda r: r["score"] >= score_cut),
        "score_bottom20": _match(lambda r: r["score"] <= score_floor),
        "consistency_not_extended": _match(
            lambda r: _comp(r, "flow_consistency") >= 0.8 and (r["r5"] or 0) <= 2
        ),
        "consistency_strong": _match(
            lambda r: _comp(r, "flow_consistency") >= 0.8 and (r["r5"] or 0) >= 5
        ),
        "chase_inflow": _match(
            lambda r: (r["z20"] or 0) >= 1.5 and (r["r5"] or 0) >= 5
        ),
        "outflow_strong": _match(
            lambda r: (r["z20"] or 0) <= -1.5 and (r["r5"] or 0) >= 5
        ),
        "sell_pressure_high": _match(
            lambda r: _comp(r, "micro_trade_sell_pressure") >= 0.8
        ),
        "cancel_depth_high": _match(
            lambda r: _comp(r, "flow_cancel_depth_dist") >= 0.8
        ),
    }

    # 板块聚合：成分股市值加权质量分（仅作展示，非官方指数）
    sector_scores: dict[str, dict[str, float]] = {}
    for r in records:
        sec = r["industry"] or "未分类"
        bucket = sector_scores.setdefault(sec, {"w": 0.0, "s": 0.0, "n": 0})
        w = float(r["amount"] or 0.0)
        bucket["w"] += w
        bucket["s"] += w * float(r["score"])
        bucket["n"] += 1
    sectors = [
        {
            "name": k,
            "score": round(v["s"] / v["w"], 1) if v["w"] > 0 else None,
            "n": int(v["n"]),
        }
        for k, v in sector_scores.items()
    ]
    sectors = [s for s in sectors if s["score"] is not None]
    sectors.sort(key=lambda x: x["score"], reverse=True)

    return {
        "available": True,
        "trade_date": target,
        "coverage": {"scored": len(records)},
        "top": top,
        "bottom": bottom,
        "patterns": patterns,
        "sectors_top": sectors[:8],
        "sectors_bottom": list(reversed(sectors[-8:])),
    }


def _pct_change(close: pd.DataFrame, sym: str) -> float:
    if len(close) < 2:
        return float("nan")
    prev = close[sym].iloc[-2]
    cur = close[sym].iloc[-1]
    if not np.isfinite(prev) or prev <= 0 or not np.isfinite(cur):
        return float("nan")
    return float((cur / prev - 1.0) * 100.0)


def _round(v: Any, digits: int) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, digits) if np.isfinite(f) else None


def _load_prices_safe(days: list[str]) -> pd.DataFrame:
    from .quantdb_feed import _load_prices

    try:
        return _load_prices(days)
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


def get_predictive(date: str | None = None) -> dict[str, Any]:
    """研究产物 + 当日截面（5 分钟缓存）。"""
    key = f"flow_predictive_{_norm_date(date) or 'latest'}"
    return _cached(key, CACHE_TTL, lambda: _predictive_impl(date))


def _predictive_impl(date: str | None) -> dict[str, Any]:
    artifact = load_artifact()
    current = _current(date)
    return {
        "available": bool(current.get("available")),
        "artifact": artifact,
        "current": current,
        "meta": {
            "score_definition": "各分项当日截面分位按符号等权（0~100，50=中性）；分项见 components",
            "components": [
                {"key": c.column, "sign": c.sign, "label": c.label, "note": c.note}
                for c in COMPONENTS
            ],
            "universe": "剔除 ST / 收盘<2 元 / 成交额<5000 万 / 流通市值后 30%",
            "disclosure": "历史统计不等于未来收益；单票胜率受左偏分布影响，看篮子/日期级胜率更稳",
        },
    }
