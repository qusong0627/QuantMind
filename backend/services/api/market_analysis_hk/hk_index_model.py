"""港股宽基指数 · 模型排序分聚合。

口径（只做「位置」描述，不输出买卖字眼）：
- 读取用户港股模型的最新 ``pred.parquet`` 截面（0–1 排序分）；
- 按最新指数成分快照（``2_base_sector/index_membership/dt=*``）聚合；
- 每个指数给出成分数/覆盖、均分/中位分、宽度（>0.5 占比）、
  前/后五分位占比、前五与后三成分股；
- 期货/美股等市场不在此模块，缺失数据返回 ``available=false`` 与原因。

排名字段是模型输出分，不是涨跌预测；展示面只用「靠前/居中/靠后」。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backend.services.api.market_analysis_shared.caching import cached
from backend.services.api.market_analysis_shared.names import build_name_map
from backend.services.engine.data_platform.quanthk_hub import _resolve_quanthk_data_dir

logger = logging.getLogger(__name__)

DATA_DIR = _resolve_quanthk_data_dir()
MEMBERSHIP_REL = "2_base_sector/index_membership"
SECURITY_MASTER_REL = "2_base_sector/security_master/data.parquet"

#: 展示顺序与指数元信息（HSCCI 无成分快照时如实置 unavailable）
INDEXES: tuple[dict[str, str], ...] = (
    {"code": "HSI", "name": "恒生指数", "symbol": "HSI.HK"},
    {"code": "HSCEI", "name": "恒生国企", "symbol": "HSCEI.HK"},
    {"code": "HSTECH", "name": "恒生科技", "symbol": "HSTECH.HK"},
    {"code": "HSCCI", "name": "恒生红筹", "symbol": "HSCCI.HK"},
)

POSITION_STRONG = 0.52
POSITION_WEAK = 0.48
TOP_CONSTITUENTS = 5
BOTTOM_CONSTITUENTS = 3
_QUERY_TTL = 300.0

_PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _resolve_model_dir(storage_path: str) -> Path | None:
    """模型目录：容器内 ``/app/models/...`` 直接存在；本机运行时映射到仓库根。"""
    raw = str(storage_path or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.exists():
        return path
    if raw.startswith("/app/"):
        candidate = _PROJECT_ROOT / raw[len("/app/") :]
        if candidate.exists():
            return candidate
    if not path.is_absolute():
        candidate = _PROJECT_ROOT / path
        if candidate.exists():
            return candidate
    return None


def _latest_membership_dir() -> Path | None:
    root = DATA_DIR / MEMBERSHIP_REL
    if not root.is_dir():
        return None
    for d in sorted(root.glob("dt=*"), reverse=True):
        if d.is_dir() and any(d.glob("*.parquet")):
            return d
    return None


def _load_membership() -> tuple[str | None, dict[str, pd.DataFrame]]:
    d = _latest_membership_dir()
    if d is None:
        return None, {}
    out: dict[str, pd.DataFrame] = {}
    for f in sorted(d.glob("*.parquet")):
        try:
            df = pd.read_parquet(f)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[hk-index-model] 成分文件读取失败 %s: %s", f, exc)
            continue
        code = str(df["index_name"].iloc[0]).upper() if "index_name" in df else f.stem.upper()
        if "stock_code" not in df.columns:
            continue
        sub = df.copy()
        sub["stock_code"] = sub["stock_code"].astype(str).str.upper().str.strip()
        if "weight" in sub.columns:
            sub["weight"] = pd.to_numeric(sub["weight"], errors="coerce")
        out[code] = sub
    return d.name[3:] if d.name.startswith("dt=") else d.name, out


def _read_latest_scores(model_dir: Path) -> tuple[str, pd.DataFrame]:
    pred_path = model_dir / "pred.parquet"
    if not pred_path.exists():
        raise FileNotFoundError("模型尚未产出 pred.parquet（请先运行推理）")
    df = pd.read_parquet(pred_path, columns=["symbol", "trade_date", "pred"])
    if df.empty:
        raise ValueError("pred.parquet 无有效分数")
    df["symbol"] = df["symbol"].astype(str).str.upper().str.strip()
    dates = pd.to_datetime(df["trade_date"], errors="coerce")
    df = df.assign(_date=dates).dropna(subset=["_date", "pred"])
    if df.empty:
        raise ValueError("pred.parquet 无有效分数")
    last = df["_date"].max()
    out = (
        df[df["_date"] == last][["symbol", "pred"]]
        .drop_duplicates("symbol", keep="last")
        .reset_index(drop=True)
    )
    return str(last.date()), out


def _names() -> dict[str, str]:
    return build_name_map(str(DATA_DIR / SECURITY_MASTER_REL))


def _position_label(mean_score: float | None) -> str | None:
    if mean_score is None or not np.isfinite(mean_score):
        return None
    if mean_score >= POSITION_STRONG:
        return "靠前"
    if mean_score <= POSITION_WEAK:
        return "靠后"
    return "居中"


def _safe_float(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def _constituents(
    sub: pd.DataFrame, names: dict[str, str], limit: int, *, ascending: bool
) -> list[dict]:
    ordered = sub.sort_values("pred", ascending=ascending).head(limit)
    out: list[dict] = []
    for row in ordered.itertuples(index=False):
        out.append(
            {
                "symbol": row.symbol,
                "name": names.get(row.symbol, row.symbol),
                "score": round(float(row.pred), 4),
                "weight": _safe_float(getattr(row, "weight", None)),
            }
        )
    return out


def _aggregate_index(
    meta: dict[str, str],
    members: pd.DataFrame | None,
    scores: pd.DataFrame,
    names: dict[str, str],
    cuts: dict[str, float | None],
) -> dict:
    base = {
        "code": meta["code"],
        "name": meta["name"],
        "index_symbol": meta["symbol"],
        "available": False,
        "reason": None,
        "n_members": 0,
        "n_scored": 0,
        "coverage": None,
        "mean_score": None,
        "median_score": None,
        "weighted_score": None,
        "breadth": None,
        "top_quintile_share": None,
        "bottom_quintile_share": None,
        "position": None,
        "top_constituents": [],
        "bottom_constituents": [],
    }
    if members is None or members.empty:
        base["reason"] = "暂无成分快照"
        return base
    base["n_members"] = int(len(members))
    merged = members.merge(scores, left_on="stock_code", right_on="symbol", how="inner")
    if merged.empty:
        base["reason"] = "成分股暂无模型分数"
        return base
    merged = merged.drop_duplicates("stock_code", keep="last")
    base["n_scored"] = int(len(merged))
    base["coverage"] = round(len(merged) / max(len(members), 1), 4)
    base["available"] = True
    mean_score = float(merged["pred"].mean())
    base["mean_score"] = round(mean_score, 4)
    base["median_score"] = round(float(merged["pred"].median()), 4)
    base["breadth"] = round(float((merged["pred"] > 0.5).mean()), 4)
    base["position"] = _position_label(mean_score)
    top_cut = cuts.get("top_quintile_cut")
    bottom_cut = cuts.get("bottom_quintile_cut")
    if top_cut is not None:
        base["top_quintile_share"] = round(float((merged["pred"] >= top_cut).mean()), 4)
    if bottom_cut is not None:
        base["bottom_quintile_share"] = round(
            float((merged["pred"] <= bottom_cut).mean()), 4
        )
    if "weight" in merged.columns and merged["weight"].notna().any():
        weights = merged["weight"].fillna(0.0).to_numpy(dtype=float)
        if weights.sum() > 0:
            base["weighted_score"] = round(
                float(np.average(merged["pred"].to_numpy(dtype=float), weights=weights)),
                4,
            )
    merged = merged.rename(columns={"stock_code": "symbol"})
    base["top_constituents"] = _constituents(
        merged, names, TOP_CONSTITUENTS, ascending=False
    )
    base["bottom_constituents"] = _constituents(
        merged, names, BOTTOM_CONSTITUENTS, ascending=True
    )
    return base


def _model_payload(model: dict) -> dict:
    meta = model.get("metadata_json") or {}
    if isinstance(meta, str):
        meta = {}
    metrics = model.get("metrics_json") or {}
    if isinstance(metrics, str):
        metrics = {}
    if not isinstance(metrics, dict) or not metrics:
        metrics = meta.get("metrics") if isinstance(meta, dict) else {}
    if not isinstance(metrics, dict):
        metrics = {}
    keys = (
        "ic",
        "rank_ic",
        "ic_t",
        "ic_win",
        "top_ann",
        "bottom_ann",
        "long_short",
        "long_short_sharpe",
    )
    return {
        "model_id": str(model.get("model_id") or ""),
        "display_name": str(
            meta.get("display_name")
            or meta.get("model_name")
            or model.get("model_id")
            or ""
        ),
        "model_type": str(meta.get("model_type") or meta.get("framework") or ""),
        "status": str(model.get("status") or ""),
        "feature_count": meta.get("feature_count"),
        "horizon_days": meta.get("target_horizon_days"),
        "metrics": {k: _safe_float(metrics.get(k)) for k in keys},
    }


def _compute(model: dict) -> dict:
    model_info = _model_payload(model)
    model_dir = _resolve_model_dir(str(model.get("storage_path") or ""))
    if model_dir is None:
        return {
            "available": False,
            "reason": "模型存储目录不存在（可能尚未落盘或路径已失效）",
            "model": model_info,
            "indices": [],
        }
    try:
        score_date, scores = _read_latest_scores(model_dir)
    except FileNotFoundError as exc:
        return {
            "available": False,
            "reason": str(exc),
            "model": model_info,
            "indices": [],
        }
    except (ValueError, OSError) as exc:
        return {
            "available": False,
            "reason": f"模型分数读取失败: {exc}",
            "model": model_info,
            "indices": [],
        }
    preds = scores["pred"].to_numpy(dtype=float)
    cuts = {
        "top_quintile_cut": round(float(np.nanquantile(preds, 0.8)), 4)
        if len(preds)
        else None,
        "bottom_quintile_cut": round(float(np.nanquantile(preds, 0.2)), 4)
        if len(preds)
        else None,
    }
    membership_date, memberships = _load_membership()
    names = _names()
    indices = [
        _aggregate_index(meta, memberships.get(meta["code"]), scores, names, cuts)
        for meta in INDEXES
    ]
    model_info.update(
        {
            "score_date": score_date,
            "universe_size": int(len(scores)),
        }
    )
    return {
        "available": True,
        "reason": None,
        "model": model_info,
        "membership_date": membership_date,
        "universe": {
            "n_scored": int(len(scores)),
            "mean_score": round(float(np.mean(preds)), 4),
            "median_score": round(float(np.median(preds)), 4),
            **cuts,
        },
        "indices": indices,
        "wording": "研究排序分（0–1 截面分位）；靠前/居中/靠后只描述位置，不是涨跌预测或交易指令。",
    }


def get_index_model_scores(model: dict) -> dict:
    """聚合一个港股模型对 HSI/HSCEI/HSTECH/HSCCI 的排序分（带 5 分钟缓存）。"""
    model_id = str(model.get("model_id") or "")
    model_dir = _resolve_model_dir(str(model.get("storage_path") or ""))
    pred = model_dir / "pred.parquet" if model_dir else None
    try:
        stamp = int(pred.stat().st_mtime) if pred is not None and pred.exists() else 0
    except OSError:
        stamp = 0
    key = f"hk.index_model:{model_id}:{stamp}"
    return cached(key, lambda: _compute(model), ttl=_QUERY_TTL)
