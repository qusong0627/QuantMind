"""T-P6-09 可回放复现验收器：账本 + L0.5 归档 → 逐周期摘要对账（diff=0 为通过）。

**验收语义（P6 硬验收）**：同数据（L0.5 归档帧）+ 同特征（共享核心 compute_cycle）+
同模型（ONNX 摘要口径）→ 同信号（矩阵/分数双摘要逐周期严格相等）。

**信号源**：实时服务的周期账本（``qm:realtime:infer:ledger:{YYYYMMDD}``）——每周期落
{输入锚 cuts（每标的快照水印）+ symbols 行序 + 覆盖白名单 + x_digest + scores_digest
+ 诊断量}。回放器按 cuts 从归档取「每标的最后一条 ts ≤ cut 的帧」喂同一引擎（与在线
逐周期只喂当前键值一帧的路径完全一致），重建矩阵并逐周期比对。

**对不上的三种典型原因**（报告会点名）：归档缺口（帧未落盘）/ 账本与归档不同源（篡改）/
代码或模型版本漂移（digest 覆盖 model_version + 列序）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from backend.services.engine.inference.realtime_core import (
    compute_cycle,
    digits,
    effective_override,
    load_baseline_for_model,
    matrix_digest,
    scores_digest,
)

logger = logging.getLogger(__name__)

LEDGER_KEY_PREFIX = "qm:realtime:infer:ledger"


def load_ledger(day_key: str, *, redis_client: Any | None = None) -> list[dict[str, Any]]:
    """读某日账本（Redis 列表，顺序即时间序）；不可用返回空表。"""
    import json
    import os

    try:
        client = redis_client
        if client is None:
            import redis as redis_lib

            client = redis_lib.Redis(
                host=os.getenv("REDIS_HOST") or "redis",
                port=int(os.getenv("REDIS_PORT", "6379")),
                db=int(os.getenv("REDIS_DB", "0")),
                password=os.getenv("REDIS_PASSWORD") or None,
                decode_responses=True, socket_connect_timeout=2, socket_timeout=5,
            )
        raw = client.lrange(f"{LEDGER_KEY_PREFIX}:{day_key}", 0, -1) or []
        out = []
        for item in raw:
            try:
                entry = json.loads(item)
            except (TypeError, ValueError):
                continue
            if isinstance(entry, dict):
                out.append(entry)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("读账本失败 %s: %s", day_key, exc)
        return []


def group_frames(frames: Any) -> dict[str, list[dict[str, Any]]]:
    """L0.5 归档行 → {纯数字: [帧dict 按 ts 升序]}（回放指针的依据）。"""
    if frames is None or len(frames) == 0:
        return {}
    df = frames.sort_values(["symbol", "ts"]) if hasattr(frames, "sort_values") else frames
    out: dict[str, list[dict[str, Any]]] = {}
    for _, row in df.iterrows():
        sym = digits(str(row.get("symbol")))
        record = {k: row.get(k) for k in df.columns}
        out.setdefault(sym, []).append(record)
    return out


def verify_entry(
    entry: dict[str, Any],
    *,
    session: Any,
    input_name: str,
    cols: list[str],
    fill: dict[str, Any],
    baseline: dict[str, dict[str, Any]],
    histories: dict[str, Any],
    frames_by_symbol: dict[str, list[dict[str, Any]]],
    pointers: dict[str, int],
    engine: Any,
    bootstrapped: set[str],
) -> dict[str, Any]:
    """单周期对账：按 cuts 推进指针 → 共享装配 → 双摘要比对。"""
    symbols = list(entry.get("symbols") or [])
    cuts = list(entry.get("cuts") or [])
    snapshots: dict[str, dict[str, Any]] = {}
    for i, sym in enumerate(symbols):
        cut = cuts[i] if i < len(cuts) else None
        if cut is None:
            continue
        frames = frames_by_symbol.get(sym) or []
        ptr = pointers.get(sym, 0)
        while ptr < len(frames) and float(frames[ptr].get("ts") or 0) <= float(cut):
            ptr += 1
        pointers[sym] = ptr
        if ptr > 0:
            snapshots[sym] = frames[ptr - 1]
    override = set(entry.get("override") or []) & set(cols)
    result = compute_cycle(
        session=session,
        input_name=input_name,
        cols=cols,
        fill=fill,
        model_version=str(entry.get("model_version") or ""),
        hot=symbols,
        snapshots=snapshots,
        baseline=baseline,
        histories=histories,
        override=override,
        engine=engine,
        bootstrapped=bootstrapped,
    )
    x_match = matrix_digest(result.x, cols, str(entry.get("model_version") or "")) == entry.get("x_digest")
    scores_match = scores_digest(result.scores) == entry.get("scores_digest")
    out: dict[str, Any] = {
        "ts": entry.get("ts"),
        "run_id": entry.get("run_id"),
        "n": len(symbols),
        "snapshots_used": len(snapshots),
        "x_match": bool(x_match),
        "scores_match": bool(scores_match),
        "matched": bool(x_match and scores_match),
    }
    if not out["matched"]:
        out["diagnostics"] = {
            "x_mean_live": entry.get("x_mean"),
            "x_mean_replay": round(float(np.mean(result.x)), 8),
            "scores_mean_live": entry.get("scores_mean"),
            "scores_mean_replay": round(float(np.mean(result.scores)), 8),
            "overridden_live": entry.get("overridden"),
            "overridden_replay": result.overridden,
            "ready_live": entry.get("ready"),
            "ready_replay": result.ready,
        }
    return out


def verify_day(
    *,
    day: date,
    model_dir: str | Path,
    ledger: list[dict[str, Any]],
    frames: Any,
    baseline_bundle: dict[str, Any] | None = None,
    session_factory: Callable[[Path, int], Any] | None = None,
    detail_limit: int = 20,
) -> dict[str, Any]:
    """整日回放对账。frames = 归档 DataFrame（l05_store.read_day）；baseline 缺省从快照加载。"""
    import json

    model_dir = Path(model_dir)
    if not ledger:
        return {"ok": False, "reason": "no_ledger", "day": day.isoformat(), "entries": 0}
    if frames is None or len(frames) == 0:
        return {"ok": False, "reason": "no_frames", "day": day.isoformat(), "entries": len(ledger)}
    meta = json.loads((model_dir / "metadata.json").read_text(encoding="utf-8"))
    cols = list(meta.get("feature_columns") or [])
    fill = meta.get("fill_values") or {}
    if not cols:
        return {"ok": False, "reason": "no_feature_columns", "day": day.isoformat()}

    frames_by_symbol = group_frames(frames)
    symbols_union = sorted({s for entry in ledger for s in (entry.get("symbols") or [])})
    if baseline_bundle is None:
        from backend.services.engine.inference.realtime_service import DEFAULT_SNAPSHOT_PARQUET

        # 与在线服务同一分派（quantdb 绑定 → 直读；遗留模型 → 快照），保证两侧同源可比
        baseline_bundle = load_baseline_for_model(
            symbols_union, day, meta=meta, cols=cols, parquet_path=DEFAULT_SNAPSHOT_PARQUET
        )
    baseline = baseline_bundle.get("rows") or {}
    histories = baseline_bundle.get("history") or {}

    if session_factory is None:
        def session_factory(model_path: Path, n_features: int):
            import onnxruntime as ort

            onnx_path = model_path / "model.onnx"
            if not onnx_path.is_file():
                from backend.services.engine.inference.onnx_exporter import export_model_to_onnx

                report = export_model_to_onnx(model_path, output_path=onnx_path)
                if not report.get("ok"):
                    raise RuntimeError(f"ONNX 导出失败: {report.get('reason')}")
            return ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])

    session = session_factory(model_dir, len(cols))
    input_name = session.get_inputs()[0].name

    from backend.services.engine.inference.incremental_features import IncrementalFeatureEngine

    engine = IncrementalFeatureEngine()
    bootstrapped: set[str] = set()
    pointers: dict[str, int] = {}

    ordered = sorted(ledger, key=lambda e: float(e.get("ts") or 0))
    results: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for entry in ordered:
        outcome = verify_entry(
            entry,
            session=session, input_name=input_name, cols=cols, fill=fill,
            baseline=baseline, histories=histories, frames_by_symbol=frames_by_symbol,
            pointers=pointers, engine=engine, bootstrapped=bootstrapped,
        )
        results.append(outcome)
        if not outcome["matched"] and len(mismatches) < detail_limit:
            mismatches.append(outcome)

    matched = sum(1 for r in results if r["matched"])
    return {
        "ok": matched == len(results),
        "diff_zero": matched == len(results),
        "day": day.isoformat(),
        "entries": len(results),
        "matched": matched,
        "mismatched": len(results) - matched,
        "model_dir": str(model_dir),
        "mismatch_details": mismatches,
        "engine_stats": (
            dict(engine.stats().get("details") or {})
            if len(engine.stats().get("details") or {}) <= 8
            else "(>8 symbols)"
        ),
    }
