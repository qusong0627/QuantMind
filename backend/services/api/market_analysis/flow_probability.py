"""资金流概率模型产物只读层（/money-flow/probability）。

产物由 ``backend/scripts/market_analysis/build_flow_probability.py`` 离线生成
（LightGBM，walk-forward 2025-2026 OOS 指标）；API 零计算，按 mtime 缓存。

模型分是**排序分**（isotonic 校准后仍不等于真实胜率），且只覆盖产物交易日；
指定历史日期时返回 ``as_of_latest=false``，前端应禁用概率着色而不是拿最新概率冒充历史。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backend.shared.stock_utils import StockCodeUtil

ARTIFACT_NAME = "flow_probability.json"
_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_INTENT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def resolve_dir(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.getenv("QM_FLOW_PROBABILITY_DIR")
    if env:
        return Path(env)
    root = Path(os.getenv("QM_QUANTDB_DATA_DIR", "data/quantdb"))
    return root.parent / "market-analysis" / "flow_probability"


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


def _norm_date(date: str | None) -> str | None:
    if not date:
        return None
    return str(date).replace("-", "").strip() or None


def _data_root() -> Path:
    return Path(os.getenv("QM_QUANTDB_DATA_DIR", "data/quantdb"))


def _news_path() -> Path | None:
    root = _data_root().parent
    for p in (
        root / "market-analysis" / "news_daily_sentiment.csv",
        Path("/data/market-analysis/news_daily_sentiment.csv"),
        Path("data/market-analysis/news_daily_sentiment.csv"),
    ):
        if p.is_file():
            return p
    return None


def _news_intent(trade_date: str) -> dict[str, dict[str, Any]]:
    """最近 3 个自然日的新闻情绪（0.6^age 加权）；只覆盖 2026 起有 enrichment 的日期。"""
    path = _news_path()
    if path is None or not trade_date:
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    key = f"news:{path}"
    with _CACHE_LOCK:
        cached = _INTENT_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        df = pd.read_csv(path)
    except Exception:  # noqa: BLE001
        return {}
    if df.empty or "signal_date" not in df.columns:
        return {}
    df["dt"] = pd.to_datetime(df["signal_date"], errors="coerce").dt.strftime("%Y%m%d")
    df = df[df["dt"].notna() & (df["dt"] <= trade_date)]
    if df.empty:
        return {}
    last = df["dt"].max()
    cutoff = str(int(last) - 7)  # 字面减法只作粗筛，下面按日差重算
    df = df[df["dt"] >= cutoff].copy()
    try:
        day = pd.to_datetime(df["dt"], format="%Y%m%d")
        ref = pd.to_datetime(trade_date, format="%Y%m%d")
        df["w"] = np.power(0.6, (ref - day).dt.days.clip(lower=0))
    except Exception:  # noqa: BLE001
        df["w"] = 1.0
    out: dict[str, dict[str, Any]] = {}
    for r in df.itertuples(index=False):
        ticker = str(getattr(r, "ticker", "") or "")
        if not ticker.upper().endswith((".SH", ".SZ", ".BJ")):
            continue
        sym = StockCodeUtil.to_prefix(ticker)
        bucket = out.setdefault(
            sym, {"w": 0.0, "score": 0.0, "n": 0, "pos": 0, "neg": 0}
        )
        w = float(getattr(r, "w", 1.0))
        bucket["w"] += w
        bucket["score"] += float(getattr(r, "score", 0.0) or 0.0) * w
        bucket["n"] += int(getattr(r, "n", 0) or 0)
        bucket["pos"] += int(getattr(r, "pos", 0) or 0)
        bucket["neg"] += int(getattr(r, "neg", 0) or 0)
    return {
        sym: {
            "news_score": round(v["score"] / v["w"], 3) if v["w"] else None,
            "news_n": int(v["n"]),
            "news_pos": int(v["pos"]),
            "news_neg": int(v["neg"]),
        }
        for sym, v in out.items()
    }


def _latest_rows(df: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    """取不超过 trade_date 的最近一个交易日（产物日可能是 T-1）。"""
    df["dt"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y%m%d")
    valid = df[df["dt"].notna() & (df["dt"] <= trade_date)]
    if valid.empty:
        return valid
    latest = valid["dt"].max()
    return valid[valid["dt"] == latest]


def _smart_intent(trade_date: str) -> dict[str, dict[str, Any]]:
    """龙虎榜/大宗交易（2023-10 起）按最近交易日聚合；调研（2025-10 起）跳过。"""
    root = _data_root() / "2_base_sector" / "smart_money"
    out: dict[str, dict[str, Any]] = {}
    try:
        lhb = _latest_rows(pd.read_parquet(root / "lhb_detail.parquet"), trade_date)
        for sym, g in lhb.groupby("symbol"):
            prefix = StockCodeUtil.to_prefix(str(sym))
            out.setdefault(prefix, {})["lhb_net"] = round(
                float(g["lhb_net_buy"].sum()), 2
            )
    except Exception:  # noqa: BLE001
        pass
    try:
        inst = _latest_rows(pd.read_parquet(root / "lhb_inst.parquet"), trade_date)
        for sym, g in inst.groupby("symbol"):
            prefix = StockCodeUtil.to_prefix(str(sym))
            out.setdefault(prefix, {})["lhb_inst_net"] = round(
                float(g["inst_net_buy"].sum()), 2
            )
    except Exception:  # noqa: BLE001
        pass
    try:
        block = _latest_rows(pd.read_parquet(root / "block_trade.parquet"), trade_date)
        for sym, g in block.groupby("symbol"):
            prefix = StockCodeUtil.to_prefix(str(sym))
            amt = float(g["amount"].sum()) if "amount" in g else 0.0
            prem = float((g["premium"] * g["amount"]).sum() / amt) if amt > 0 else None
            out.setdefault(prefix, {})["block_amount"] = round(amt, 2)
            out[prefix]["block_premium"] = round(prem, 4) if prem is not None else None
    except Exception:  # noqa: BLE001
        pass
    return out


def _intent(trade_date: str) -> dict[str, Any]:
    key = f"intent:{trade_date}"
    with _CACHE_LOCK:
        cached = _INTENT_CACHE.get(key)
    if cached:
        return cached[1]
    news = _news_intent(trade_date)
    smart = _smart_intent(trade_date)
    merged: dict[str, dict[str, Any]] = {}
    for sym in set(news) | set(smart):
        merged[sym] = {**news.get(sym, {}), **smart.get(sym, {})}
    payload = {
        "trade_date": trade_date,
        "stocks": merged,
        "news_coverage": "2026 起（Huntly enrichment）",
        "smart_coverage": "龙虎榜/大宗 2023-10 起",
    }
    with _CACHE_LOCK:
        _INTENT_CACHE[key] = (0.0, payload)
    return payload


def get_probability(date: str | None = None) -> dict[str, Any]:
    artifact = load_artifact()
    if artifact is None:
        return {
            "available": False,
            "reason": "not_built",
            "hint": "python -m backend.scripts.market_analysis.build_flow_probability",
        }
    trade_date = str(artifact.get("trade_date") or "")
    req = _norm_date(date)
    return {
        "available": True,
        "trade_date": trade_date,
        "requested_date": req,
        # 页面日期可能比产物晚 1 个交易日（forward 价格滞后）；只要不早于产物日就可用
        "as_of_latest": req is None or req >= trade_date,
        "artifact": artifact,
        "intent": _intent(trade_date),
    }
