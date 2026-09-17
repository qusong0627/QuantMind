"""实时推理共享核心（T-P6-08/09）：周期装配 + 摘要 + 账本条目——服务与回放器唯一实现。

**为什么独立成模块（结构优化）**：实时服务（在线）与回放验收器（离线）必须走**同一份**
周期装配与同一份摘要定义，否则"diff=0"的验收本身失去意义（两处各写=自证循环）。
服务逐周期把「输入锚（每标的快照水印 cuts）+ 矩阵摘要 + 分数摘要」落账本；
回放器按 cuts 从 L0.5 归档重建同样的引擎状态与矩阵，摘要逐周期比对。

**精确复现语义（2026-09-17 定稿）**：实时服务每周期对每标的只喂**当前快照键值一帧**
（不是喂帧流）——回放器同法：每周期取「最后一条 ts ≤ cut 的归档帧」喂入。
引擎状态逐周期累积路径两侧完全一致 → 摘要可逐周期严格相等（非近似）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from backend.shared.feature_incremental import TIER_COLUMNS


def digits(symbol: str) -> str:
    """后缀式/前缀式/纯数字 → 纯数字（快照 parquet 与 engine_signal_scores 口径）。"""
    s = str(symbol or "").strip().upper()
    for suf in (".SH", ".SZ", ".BJ"):
        if s.endswith(suf):
            return s[: -len(suf)]
    if s[:2] in ("SH", "SZ", "BJ") and s[2:].isdigit():
        return s[2:]
    return s


def snapshot_key(symbol: str) -> str | None:
    """任意形态 → ``market:snapshot:{prefix.lower()}``（与 collector 写入面同构）。"""
    s = str(symbol or "").strip().upper()
    if "." in s:
        code, _, mk = s.partition(".")
        if mk in ("SH", "SZ", "BJ") and code.isdigit():
            return f"market:snapshot:{mk.lower()}{code}"
        return None
    if s[:2] in ("SH", "SZ", "BJ") and s[2:].isdigit():
        return f"market:snapshot:{s[:2].lower()}{s[2:]}"
    if s.isdigit() and len(s) == 6:
        mk = "SH" if s[0] in "69" else ("BJ" if s[0] in "48" else "SZ")
        return f"market:snapshot:{mk.lower()}{s}"
    return None


def snapshot_watermark(snap: dict[str, Any] | None) -> float | None:
    """快照消费水印：快照键内的 timestamp/ts（回放锚）。"""
    if not snap:
        return None
    for key in ("timestamp", "ts"):
        raw = snap.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


@dataclass
class CycleResult:
    """单周期装配结果（含回放对账所需的全部摘要物质）。"""

    x: np.ndarray
    symbols: list[str]  # 纯数字，行序=矩阵行序（账本据此重放）
    cols: list[str]
    scores: np.ndarray
    ranks: np.ndarray
    ready: int
    missing: int
    overridden: int
    cuts: list[float | None] = field(default_factory=list)


def matrix_digest(x: np.ndarray, cols: list[str], model_version: str) -> str:
    """输入矩阵摘要：模型版本 + 列序 + float32 原始字节（行序由 symbols 锚定）。"""
    h = hashlib.sha256()
    h.update(str(model_version).encode("utf-8"))
    h.update(b"\x1f")
    h.update(",".join(cols).encode("utf-8"))
    h.update(b"\x1f")
    h.update(np.ascontiguousarray(x, dtype="<f4").tobytes())
    return h.hexdigest()


def scores_digest(scores: np.ndarray) -> str:
    """分数摘要：round(6) 后 float64 字节（消除无关浮点尾差噪声的伪差异）。"""
    arr = np.round(np.asarray(scores, dtype=float), 6)
    return hashlib.sha256(arr.astype("<f8").tobytes()).hexdigest()


def ledger_entry(
    result: CycleResult,
    *,
    ts: float,
    run_id: str,
    model_version: str,
    override: tuple[str, ...] | list[str] | set[str] = (),
) -> dict[str, Any]:
    """账本条目：输入锚（symbols 顺序 + 每标的 cuts）+ 双摘要 + 诊断量（回放对账唯一依据）。

    ``override`` 必须随账落盘——回放要用**当时**的覆盖白名单，而非当前配置（配置会变）。
    """
    return {
        "ts": round(float(ts), 3),
        "run_id": run_id,
        "model_version": model_version,
        "n": len(result.symbols),
        "ready": result.ready,
        "missing": result.missing,
        "overridden": result.overridden,
        "symbols": list(result.symbols),
        "cuts": result.cuts,
        "override": sorted(str(c) for c in override),
        "x_mean": round(float(np.mean(result.x)), 8),
        "scores_mean": round(float(np.mean(result.scores)), 8),
        "x_digest": matrix_digest(result.x, result.cols, model_version),
        "scores_digest": scores_digest(result.scores),
    }


def live_coverage(
    hot: list[str], snapshots: dict[str, dict[str, Any]], *, now: float
) -> float:
    """实时快照覆盖率（0~1）：**可用**快照的占比——ts 缺失或超 STALE 线一律不算。

    口径与消费方新鲜度同源（``shared/freshness.classify_age``；阈值仅在该模块读取）。
    用于发布闸门：覆盖率不足时**不发布伪实时信号**（基线 T-1 打分不等于实时）。
    """
    if not hot:
        return 0.0
    from backend.shared.freshness import UNAVAILABLE, classify_age, quote_policy

    policy = quote_policy()
    usable = 0
    for sym in hot:
        snap = snapshots.get(sym)
        if not snap:
            continue
        try:
            ts = float(snap.get("timestamp") or snap.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts > 1e12:
            ts /= 1000.0
        if ts <= 0:
            continue
        if (
            classify_age(
                now - ts,  # 不夹取负值：未来偏斜由 classify_age 的容差守卫裁定

                fresh_within_s=policy.fresh_within_s,
                stale_within_s=policy.stale_within_s,
            )
            != UNAVAILABLE
        ):
            usable += 1
    return usable / len(hot)


def compute_cycle(
    *,
    session: Any,
    input_name: str,
    cols: list[str],
    fill: dict[str, Any],
    model_version: str,
    hot: list[str],
    snapshots: dict[str, dict[str, Any]],
    baseline: dict[str, dict[str, Any]],
    histories: dict[str, Any],
    override: set[str],
    engine: Any,
    bootstrapped: set[str],
) -> CycleResult:
    """单周期装配：引导(每标的一次) → 喂当前快照一帧 → live 覆盖 → 矩阵 → 推理 → 排名。

    在线服务与离线回放共用本函数——任何装配逻辑改动两侧同时生效（结构纪律）。
    """
    x = np.empty((len(hot), len(cols)), dtype=np.float32)
    ready = missing = overridden = 0
    symbols_norm: list[str] = []
    cuts: list[float | None] = []
    for i, sym in enumerate(hot):
        norm = digits(sym)
        symbols_norm.append(norm)
        row = dict(baseline.get(norm) or {})
        if sym not in bootstrapped:
            hist = histories.get(norm)
            if hist is not None and len(hist):
                try:
                    engine.bootstrap(sym, hist)
                except Exception:  # noqa: BLE001 - 单标的引导失败不拖垮周期
                    pass
            bootstrapped.add(sym)
        snap = snapshots.get(sym)
        cuts.append(snapshot_watermark(snap))
        if snap:
            engine.on_snapshot(sym, snap)
        live = None
        if override:
            try:
                live = engine.compute(sym)
            except Exception:  # noqa: BLE001 - 单标的失败不拖垮周期
                live = None
        for j, col in enumerate(cols):
            val = row.get(col)
            if live is not None and col in override:
                lv = live.get(col)
                if lv is not None and np.isfinite(lv):
                    val = lv
                    overridden += 1
            if val is None or (isinstance(val, float) and np.isnan(val)):
                val = fill.get(col, 0.0)
                missing += 1
            x[i, j] = float(val)
        if row:
            ready += 1
    out = session.run(None, {input_name: x})[0]
    scores = np.asarray(out, dtype=float).reshape(-1)
    order = np.argsort(-scores)
    ranks = np.empty(len(scores), dtype=int)
    ranks[order] = np.arange(1, len(scores) + 1)
    return CycleResult(
        x=x, symbols=symbols_norm, cols=list(cols), scores=scores, ranks=ranks,
        ready=ready, missing=missing, overridden=overridden, cuts=cuts,
    )


# ── 基线加载（服务与回放共用；路径可注入）────────────────────────────


def load_baseline_bundle(
    symbols: list[str],
    day: date,
    *,
    parquet_path: str | Path,
    cols: list[str],
    history_len: int = 45,
) -> dict[str, Any]:
    """T-1 行 + 价格历史（单次 parquet 读取）。返回 {rows, history}；缺文件返回空。"""
    import pandas as pd

    path = Path(parquet_path)
    if not path.is_file() or not cols:
        return {"rows": {}, "history": {}}
    raw = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]
    # 列裁剪必须与 parquet **实际 schema 取交集**：模型 feature_columns 里的 parquet 未收录列
    # （如 JQ110_* ——2026-09-17 实测硬报 ArrowInvalid）不得进 read_parquet，缺失值统一走
    # compute_cycle 的 fill_values 兜底（与快照覆盖白名单同纪律：口径不符不硬来）。
    requested = raw + [c for c in cols if c not in raw]
    try:
        import pyarrow.parquet as _pq

        available = set(_pq.ParquetFile(path).schema_arrow.names)
    except Exception:  # noqa: BLE001 - schema 读取失败回落全量请求（由 read_parquet 报真错）
        available = set()
    if available:
        ordered = list(dict.fromkeys(requested))
        selected = [c for c in ordered if c in available]
    else:
        selected = list(dict.fromkeys(requested))
    df = pd.read_parquet(path, columns=selected)
    df = df[df["symbol"].isin([digits(s) for s in symbols])]
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    past = df[df["trade_date"].dt.date < day]
    if past.empty:
        return {"rows": {}, "history": {}}
    latest = past["trade_date"].max()
    rows: dict[str, dict[str, Any]] = {}
    history: dict[str, Any] = {}
    for sym, g in past.groupby("symbol"):
        g = g.sort_values("trade_date")
        history[str(sym)] = g[raw].tail(history_len).reset_index(drop=True)
        last = g[g["trade_date"] == latest]
        if not last.empty:
            row = last.iloc[-1]
            rows[str(sym)] = {c: row.get(c) for c in cols}
    return {"rows": rows, "history": history}


def effective_override(whitelist: tuple[str, ...] | list[str], cols: list[str]) -> set[str]:
    """白名单 ∩ TIER ∩ 模型列（唯一裁定入口）。"""
    return set(whitelist) & set(TIER_COLUMNS) & set(cols)


def ledger_json(entry: dict[str, Any]) -> str:
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
