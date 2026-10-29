"""板块方向概率：大盘情绪先验 + 板块状态表相对概率 + 资金流全景状态。

口径（与 ``build_sector_direction.py`` 产物一致）：
- 大盘先验：情绪快照 ALL.A 的 T+5/10/20 模型 ``prob_up``（缺失时退同状态 20 日胜率）；
- 板块相对：历史同状态（压力区 × 3 日净额比加速五分 × 5 日涨幅五分）
  的 P(板块等权超额 > 0)，来自离线状态表；
- 组合：``P(板块上涨) ≈ P(大盘上涨) + (P(板块超额) − 0.5)``，截断到 5%~95%；
  这是可解释的一阶组合，不是联合分布模型；
- 历史频率不是未来保证；45%~55% 视为无方向，``n`` / ``date_win`` 一并返回。

数据缺失/非行业二级/历史快照一律返回空，不补 0。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import quantdb_feed
from . import sentiment as _sentiment

ARTIFACT_NAME = "sector_direction.json"
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_STATE_CACHE: dict[str, Any] = {"ts": 0.0, "flow": {}, "price": {}}
_STATE_TTL = 300.0
HORIZONS = (5, 10, 20)
NEUTRAL_BAND = (0.45, 0.55)

_ZONE_MAP = {
    "extreme_pressure": "extreme",
    "pressure": "pressure",
    "neutral": "neutral",
    "strong": "strong",
    "overheat": "overheat",
}


def resolve_dir(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.getenv("QM_SECTOR_DIRECTION_DIR")
    if env:
        return Path(env)
    root = Path(os.getenv("QM_QUANTDB_DATA_DIR", "data/quantdb"))
    return root.parent / "market-analysis" / "sector_direction"


def load_artifact(directory: str | Path | None = None) -> dict[str, Any] | None:
    path = resolve_dir(directory) / ARTIFACT_NAME
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    with _LOCK:
        cached = _CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    with _LOCK:
        _CACHE[key] = (mtime, payload)
    return payload


def _zone_of(payload: dict) -> str:
    zone = str((payload.get("stress") or {}).get("zone") or "")
    return _ZONE_MAP.get(zone, "neutral")


def _buckets(values: dict[str, float]) -> dict[str, str]:
    """按当日板块截面五分（q1..q5，与离线产物同口径）；不足 6 个板块不硬分。"""
    items = sorted((v, k) for k, v in values.items() if v is not None)
    n = len(items)
    if n < 6:
        return {}
    out: dict[str, str] = {}
    for rank, (_v, name) in enumerate(items):
        pct = (rank + 1) / n  # 对齐 pandas rank(pct=True)
        idx = min(max(int(pct * 5 - 1e-9), 0), 4)
        out[name] = f"q{idx + 1}"
    return out


def _state_maps() -> tuple[dict[str, str], dict[str, str]]:
    now = time.time()
    with _LOCK:
        if now - float(_STATE_CACHE["ts"]) < _STATE_TTL:
            return dict(_STATE_CACHE["flow"]), dict(_STATE_CACHE["price"])
    flow_items = quantdb_feed.get_sector_heatmap("shenwan", "3d")
    price_items = quantdb_feed.get_sector_heatmap("shenwan", "5d")
    flow_vals = {
        str(it.get("name")): float(
            it["flow_ratio_accel"]
            if isinstance(it.get("flow_ratio_accel"), (int, float))
            else it["flow_accel"]
        )
        for it in flow_items
        if isinstance(it.get("flow_ratio_accel"), (int, float))
        or isinstance(it.get("flow_accel"), (int, float))
    }
    price_vals = {
        str(it.get("name")): float(
            it["ret_period_sum"]
            if isinstance(it.get("ret_period_sum"), (int, float))
            else it["pct_period"]
        )
        for it in price_items
        if isinstance(it.get("ret_period_sum"), (int, float))
        or isinstance(it.get("pct_period"), (int, float))
    }
    flow, price = _buckets(flow_vals), _buckets(price_vals)
    with _LOCK:
        _STATE_CACHE.update({"ts": now, "flow": flow, "price": price})
    return flow, price


def _market_block(payload: dict, zone: str) -> dict[str, Any]:
    block = (payload.get("indices") or {}).get("ALL.A") or {}
    stats = block.get("stats") or {}
    forecast = block.get("forecast") or {}
    priors: dict[str, float | None] = {}
    for h in HORIZONS:
        f = forecast.get(str(h)) or {}
        p = f.get("prob_up")
        priors[str(h)] = float(p) if isinstance(p, (int, float)) else None
    return {
        "trade_date": payload.get("trade_date"),
        "stress_score": (payload.get("stress") or {}).get("score"),
        "stress_zone": zone,
        "stress_label": (payload.get("stress") or {}).get("zone_label"),
        "stress_note": (payload.get("stress") or {}).get("zone_note"),
        "phase": (payload.get("stress") or {}).get("phase"),
        "win20": stats.get("win"),
        "median20": stats.get("median"),
        "p10_20": stats.get("p10"),
        "p90_20": stats.get("p90"),
        "n_days": stats.get("n_days"),
        "market_prob": priors,
        "indices": [
            {
                "key": key,
                "name": (idx or {}).get("name") or key,
                "change_pct": (idx or {}).get("change_pct"),
                "prob_up": {
                    str(h): (((idx or {}).get("forecast") or {}).get(str(h)) or {}).get(
                        "prob_up"
                    )
                    for h in HORIZONS
                },
            }
            for key, idx in (payload.get("indices") or {}).items()
        ],
        "note": "大盘先验=全A模型 T+5/10/20 prob_up；同状态统计见 win20",
    }


def market(date: str | None = None) -> dict[str, Any] | None:
    """只取大盘情绪基准块（供非行业二级分类的情绪热度使用；不含板块方向）。"""
    if date:
        return None
    try:
        payload = _sentiment.load_sentiment()
    except Exception:  # noqa: BLE001
        return None
    zone = _zone_of(payload)
    block = _market_block(payload, zone)
    block["available"] = False
    block["neutral_band"] = list(NEUTRAL_BAND)
    return block


def attach(items: list[dict[str, Any]], date: str | None = None) -> dict[str, Any]:
    """给行业二级热力图 items 附加 direction（其余分类/历史日期返回空）。

    复制 items 再写：``quantdb_feed.get_sector_heatmap`` 走进程内缓存，直接改
    缓存对象会把最新方向概率泄漏到历史快照/其它分类的响应里。
    """
    out_items = [dict(it) for it in items]
    if not out_items or date:
        return {"items": out_items, "market": None}
    artifact = load_artifact()
    if artifact is None:
        return {"items": out_items, "market": None}
    try:
        payload = _sentiment.load_sentiment()
    except Exception:  # noqa: BLE001 —— 情绪快照缺失时方向不可用
        return {"items": out_items, "market": None}
    zone = _zone_of(payload)
    market = _market_block(payload, zone)
    priors = market.get("market_prob") or {}
    fallback_prior = market.get("win20")
    try:
        flow_bucket, price_bucket = _state_maps()
    except Exception:  # noqa: BLE001
        return {"items": out_items, "market": market}
    cells = {
        (int(c["h"]), str(c["stress"]), str(c["flow"]), str(c["price"])): c
        for c in artifact.get("cells") or []
    }
    available = False
    lo, hi = NEUTRAL_BAND
    for it in out_items:
        name = str(it.get("name"))
        fb = flow_bucket.get(name)
        pb = price_bucket.get(name)
        if fb is None or pb is None:
            it["direction"] = None
            continue
        direction: dict[str, Any] = {}
        for h in HORIZONS:
            cell = cells.get((h, zone, fb, pb))
            if not cell:
                continue
            rel = cell.get("win")
            if not isinstance(rel, (int, float)):
                continue
            mp = priors.get(str(h))
            if not isinstance(mp, (int, float)):
                mp = fallback_prior
            if not isinstance(mp, (int, float)):
                continue
            prob = max(0.05, min(0.95, float(mp) + (float(rel) - 0.5)))
            direction[str(h)] = {
                "prob": round(prob, 3),
                "label": "up" if prob >= hi else "down" if prob <= lo else "neutral",
                "market_prob": round(float(mp), 3),
                "rel_prob": rel,
                "mean": cell.get("mean"),
                "n": cell.get("n"),
                "date_win": cell.get("date_win"),
                "n_dates": cell.get("n_dates"),
                "state": {"stress": zone, "flow": fb, "price": pb},
            }
        it["direction"] = direction or None
        available = available or bool(direction)
    market["available"] = available
    market["neutral_band"] = list(NEUTRAL_BAND)
    market["disclosure"] = artifact.get("disclosure")
    return {"items": out_items, "market": market}
