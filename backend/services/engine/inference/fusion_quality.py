"""融合质量适配层：成员 IC 序列 / 相关矩阵 / OOS 回放（现场回算）。

为什么现场回算（2026-10-09 取证）：`qm_model_inference_quality` 只按
`qm_model_inference_runs` 定位分数且天然滞后 H 天，实测最大 trade_date=09-23、
近 12 天零行；而成员分数桶（`engine_signal_scores` 的 `script_v1_<slug>`）天天在长。
所以权重引擎的输入以「分数桶 × QuantDB 已实现收益」现场回算为准，质量表仅作补充。

数学与生产融合模板（templates/inference_ensemble_src.py）逐位对齐：
- L1 截面 pct 秩：pandas ``rank(method="average", pct=True)``；
- L3 线性合成：``Σ w_i·rank_i / Σ w_i``（按当日实际存在的成员归一）；
- 回放**不含** L4 共识调节（其逐符号放大因子不可逆，属二阶项，UI 已注明）。

防前视：label 只取「未来 H 日收益已兑现」的日期 —— 由 QuantDB 分区自然截断
（尾部分区不足即无 label），无需额外日历推算。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from collections.abc import Mapping, Sequence

import pandas as pd
from sqlalchemy import bindparam, text

from backend.services.engine.inference.inference_quality_backfill import (
    _rank_ic_from_scores,
    _load_real_returns,
)
from backend.shared.database_manager_v2 import get_session
from backend.shared.signal_buckets import resolve_feature_version
from backend.shared.stock_utils import StockCodeUtil

__all__ = [
    "DEFAULT_HORIZON",
    "MIN_CROSS_SECTION",
    "FusionEvidence",
    "FusionReplay",
    "MemberEvidence",
    "MemberSpec",
    "compute_replay",
    "daily_rank_ic",
    "extract_horizon",
    "forward_returns_from_closes",
    "fuse_rank_frames",
    "gather_fusion_evidence",
    "pairwise_corr",
    "read_model_horizon",
    "replay_fusion",
]

DEFAULT_HORIZON = 5
MIN_CROSS_SECTION = 30          # 单日截面 IC 最少标的数（与回填口径一致）
IC_WINDOW_DAYS = 120            # 成员 IC 窗口（交易日）
CORR_WINDOW_DAYS = 60           # 相关矩阵窗口（交易日）
_SNAPSHOT_DIR = "/app/db/feature_snapshots"
_QUANTDB_SUBDIR = ("1_kline_data", "daily_forward")  # 只读 qfq；坏复权序列宁缺毋滥


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数（数学契约，TDD 覆盖）
# ─────────────────────────────────────────────────────────────────────────────


def _norm_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    return s[:10] if len(s) >= 10 else None


def extract_horizon(
    config: Mapping[str, Any] | None, metadata: Mapping[str, Any] | None
) -> int:
    """成员训练标签周期：config.label.target_horizon_days → metadata 兼容字段 → 5。"""
    candidates: list[Any] = []
    if config:
        label = config.get("label")
        if isinstance(label, Mapping):
            candidates.append(label.get("target_horizon_days"))
        candidates.append(config.get("target_horizon_days"))
    if metadata:
        candidates.extend(
            metadata.get(k) for k in ("horizon_days", "horizon", "prediction_horizon")
        )
    for v in candidates:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv > 0:
            return iv
    return DEFAULT_HORIZON


def read_model_horizon(model_dir: str | os.PathLike | None) -> int:
    """从模型目录读标签周期（config.yaml 优先，metadata.json 兜底）。"""
    if not model_dir:
        return DEFAULT_HORIZON
    base = Path(model_dir)
    config: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] | None = None
    try:
        import yaml

        cfg_path = base / "config.yaml"
        if cfg_path.exists():
            with open(cfg_path, encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001 - 缺 config 不该阻塞预览
        config = None
    try:
        import json

        meta_path = base / "metadata.json"
        if meta_path.exists():
            with open(meta_path, encoding="utf-8") as f:
                metadata = json.load(f) or {}
    except Exception:  # noqa: BLE001
        metadata = None
    return extract_horizon(config, metadata)


def forward_returns_from_closes(closes: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """长表收盘价 → 未来 H 日收益（按 symbol 组内位移，尾部不足即弃 = 防前视）。

    入参 closes: [trade_date, symbol, close]（顺序无关，可含全窗）。
    出参: [trade_date(str), symbol, label]，label = close_{t+H}/close_t − 1。
    """
    df = closes[["trade_date", "symbol", "close"]].copy()
    df["trade_date"] = df["trade_date"].map(_norm_date)
    df = df.dropna(subset=["trade_date", "close"])
    piv = df.pivot_table(
        index="symbol", columns="trade_date", values="close", aggfunc="last"
    ).sort_index(axis=1)
    fut = piv.shift(-int(horizon), axis=1)
    lab = (fut / piv) - 1.0
    out = lab.stack().rename("label").reset_index()
    out = out.dropna(subset=["label"])
    return out[["trade_date", "symbol", "label"]]


def daily_rank_ic(
    scores: pd.DataFrame, labels: pd.DataFrame, min_symbols: int = MIN_CROSS_SECTION
) -> dict[str, float]:
    """逐日截面 Rank IC。入参 [trade_date, symbol, score|label]，返回 {日期: ic}。

    仅返回有效日（共存符号数 ≥ min_symbols 且截面有区分度）；NaN 标签剔除后计数。
    """
    s = scores[["trade_date", "symbol", "score"]].copy()
    lab = labels[["trade_date", "symbol", "label"]].copy()
    for frame in (s, lab):
        frame["trade_date"] = frame["trade_date"].map(_norm_date)
        frame["symbol"] = frame["symbol"].astype(str)
    merged = s.merge(lab, on=["trade_date", "symbol"], how="inner")
    merged = merged.dropna(subset=["trade_date", "score", "label"])
    out: dict[str, float] = {}
    for d, g in merged.groupby("trade_date"):
        if len(g) < min_symbols:
            continue
        ic = _rank_ic_from_scores(g)
        if ic == ic:  # 非 NaN
            out[str(d)] = float(ic)
    return out


def _panel_series(frame: pd.DataFrame) -> dict[str, pd.Series]:
    """面板 → {日期: Series(symbol→score)}。"""
    out: dict[str, pd.Series] = {}
    f = frame[["trade_date", "symbol", "score"]].copy()
    f["trade_date"] = f["trade_date"].map(_norm_date)
    f["symbol"] = f["symbol"].astype(str)
    f = f.dropna(subset=["trade_date", "score"])
    for d, g in f.groupby("trade_date"):
        out[str(d)] = g.set_index("symbol")["score"]
    return out


def pairwise_corr(
    panels: Mapping[str, pd.DataFrame],
    min_symbols: int = MIN_CROSS_SECTION,
    min_days: int = 3,
) -> dict[str, dict[str, float]]:
    """成员两两「同日共同符号截面 Spearman」的跨日平均。证据不足的对不出现（引擎按无信息处理）。"""
    series = {m: _panel_series(p) for m, p in panels.items()}
    ids = sorted(series)
    out: dict[str, dict[str, float]] = {}
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            common_days = sorted(set(series[a]) & set(series[b]))
            vals: list[float] = []
            for d in common_days:
                joined = pd.DataFrame(
                    {"score": series[a][d], "label": series[b][d]}
                ).dropna()
                if len(joined) < min_symbols:
                    continue
                c = _rank_ic_from_scores(joined)
                if c == c:
                    vals.append(float(c))
            if len(vals) >= min_days:
                mean = float(sum(vals) / len(vals))
                out.setdefault(a, {})[b] = mean
                out.setdefault(b, {})[a] = mean
    return out


def fuse_rank_frames(
    day_ranks: Mapping[str, pd.Series], weights: Mapping[str, float]
) -> pd.Series:
    """生产模板 L1+L3 的合成数学：Σ w·rank_pct / Σ w（按当日存在成员归一）。

    成员不在权重表时取均匀缺省 1/n（与模板 ``eff_weights.get(mid, 1.0/n_models)``
    同款）；权重全为 0 的符号返回 NaN（调用方 dropna）。
    """
    rdf = pd.DataFrame(day_ranks)
    if rdf.empty:
        return pd.Series(dtype=float)
    n_models = rdf.shape[1]
    w = pd.Series({m: float(weights.get(m, 1.0 / n_models)) for m in rdf.columns})
    mask = rdf.notna()
    num = rdf.fillna(0.0).mul(w, axis=1).sum(axis=1)
    den = mask.mul(w, axis=1).sum(axis=1)
    return (num / den).replace([float("inf"), float("-inf")], float("nan"))


@dataclass(frozen=True)
class FusionReplay:
    """创建前 OOS 回放结论（线性主通道）。"""

    dates: tuple[str, ...]
    fused_ic: dict[str, float]
    member_ic: dict[str, dict[str, float]]
    summary: dict[str, dict[str, float]]  # key: 成员 id 或 "fused" → {ic_mean, icir, n_days}


def _series_summary(values: Sequence[float]) -> dict[str, float]:
    clean = [float(v) for v in values if v == v]
    n = len(clean)
    if n == 0:
        return {"ic_mean": float("nan"), "icir": float("nan"), "n_days": 0.0}
    mean = sum(clean) / n
    if n > 1:
        var = sum((x - mean) ** 2 for x in clean) / (n - 1)
        std = math.sqrt(var)
    else:
        std = 0.0
    icir = mean / std if std > 1e-9 else 0.0
    return {"ic_mean": mean, "icir": icir, "n_days": float(n)}


def replay_fusion(
    panels: Mapping[str, pd.DataFrame],
    weights: Mapping[str, float],
    labels: pd.DataFrame,
    min_symbols: int = MIN_CROSS_SECTION,
) -> FusionReplay:
    """按生产模板 L1+L3 数学回放融合：成员当日 pct 秩 → 权重线性合成 → 逐日 rank IC。

    与成员各自 IC（同窗同标签周期）并列，供「融合是否真的更强」裁决。
    """
    series = {m: _panel_series(p) for m, p in panels.items()}
    ranks_cache: dict[str, dict[str, pd.Series]] = {
        m: {d: s.rank(method="average", pct=True) for d, s in per_day.items()}
        for m, per_day in series.items()
    }
    lab = labels[["trade_date", "symbol", "label"]].copy()
    lab["trade_date"] = lab["trade_date"].map(_norm_date)
    lab["symbol"] = lab["symbol"].astype(str)
    lab = lab.dropna(subset=["trade_date", "label"])
    labels_by_date = {
        str(d): g.set_index("symbol")["label"] for d, g in lab.groupby("trade_date")
    }

    all_dates = sorted({d for per_day in ranks_cache.values() for d in per_day})
    fused_ic: dict[str, float] = {}
    member_ic: dict[str, dict[str, float]] = {m: {} for m in series}
    for d in all_dates:
        label_d = labels_by_date.get(d)
        if label_d is None or label_d.empty:
            continue
        day_ranks = {
            m: ranks_cache[m][d]
            for m in sorted(ranks_cache)
            if d in ranks_cache[m] and not ranks_cache[m][d].empty
        }
        if not day_ranks:
            continue
        # 成员各自 IC（各自全量符号 ∩ 当日标签）
        for m, r in day_ranks.items():
            joined_m = pd.DataFrame({"score": r, "label": label_d}).dropna()
            if len(joined_m) < min_symbols:
                continue
            ic_m = _rank_ic_from_scores(joined_m)
            if ic_m == ic_m:
                member_ic[m][d] = float(ic_m)
        # 融合：Σ w·rank / Σ w（缺失成员按存在集合归一）
        fused = fuse_rank_frames(day_ranks, weights).dropna()
        joined = pd.DataFrame({"fused": fused, "label": label_d}).dropna()
        if len(joined) < min_symbols:
            continue
        ic = _rank_ic_from_scores(joined, pred_col="fused")
        if ic == ic:
            fused_ic[d] = float(ic)

    summary = {
        "fused": _series_summary([fused_ic[d] for d in sorted(fused_ic)]),
    }
    for m in sorted(member_ic):
        summary[m] = _series_summary([member_ic[m][d] for d in sorted(member_ic[m])])
    return FusionReplay(
        dates=tuple(sorted(fused_ic)),
        fused_ic=fused_ic,
        member_ic=member_ic,
        summary=summary,
    )


# ─────────────────────────────────────────────────────────────────────────────
# IO 适配（分数桶 / QuantDB 收益 / 证据组装）
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MemberSpec:
    """成员入参：model_id 必填；horizon/market 缺省时由调用方或模型目录补。"""

    model_id: str
    horizon_days: int | None = None
    market: str = "CN"
    model_dir: str | None = None


@dataclass(frozen=True)
class MemberEvidence:
    model_id: str
    horizon_days: int
    market: str
    ic_by_date: dict[str, float]
    score_days: int

    @property
    def ic_series(self) -> list[float]:
        """时间升序 IC 序列（权重引擎入参）。"""
        return [self.ic_by_date[d] for d in sorted(self.ic_by_date)]


@dataclass
class FusionEvidence:
    members: tuple[MemberEvidence, ...]
    corr: dict[str, dict[str, float]]
    panels: dict[str, pd.DataFrame] = field(repr=False, default_factory=dict)
    labels_by_horizon: dict[int, pd.DataFrame] = field(repr=False, default_factory=dict)
    warnings: tuple[str, ...] = ()

    def member_ic_series(self) -> dict[str, list[float]]:
        return {m.model_id: m.ic_series for m in self.members}


def _quantdb_forward_dir(quantdb_dir: str | None) -> Path:
    base = quantdb_dir or os.getenv("QM_QUANTDB_DATA_DIR") or "/data/quantdb"
    return Path(base).joinpath(*_QUANTDB_SUBDIR)


def load_closes_quantdb(
    dates: Sequence[str], horizon: int, quantdb_dir: str | None = None
) -> pd.DataFrame:
    """批量读 QuantDB qfq 收盘价：覆盖 [min(dates), max(dates)+horizon 个交易日]。"""
    import glob as _glob

    fwd = _quantdb_forward_dir(quantdb_dir)
    if not fwd.is_dir():
        return pd.DataFrame(columns=["trade_date", "symbol", "close"])
    wanted = sorted({d for d in (_norm_date(x) for x in dates) if d})
    if not wanted:
        return pd.DataFrame(columns=["trade_date", "symbol", "close"])
    parts = sorted(
        p.split("=", 1)[1]
        for p in os.listdir(fwd)
        if p.startswith("dt=") and p.split("=", 1)[1].isdigit()
    )
    start = wanted[0].replace("-", "")
    end = wanted[-1].replace("-", "")
    idx = [k for k, d in enumerate(parts) if start <= d <= end]
    if not idx:
        return pd.DataFrame(columns=["trade_date", "symbol", "close"])
    take = parts[idx[0] : idx[-1] + horizon + 1]  # 尾部再留 horizon 个交易日供 label
    frames = []
    for d in take:
        files = sorted(_glob.glob(str(fwd / f"dt={d}" / "*.parquet")))
        for f in files:
            frames.append(
                pd.read_parquet(f, columns=["symbol", "close"]).assign(trade_date=d)
            )
    if not frames:
        return pd.DataFrame(columns=["trade_date", "symbol", "close"])
    out = pd.concat(frames, ignore_index=True)
    out["symbol"] = out["symbol"].map(lambda s: StockCodeUtil.to_prefix(str(s)))
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y-%m-%d")
    return out[["trade_date", "symbol", "close"]]


def load_labels(
    dates: Sequence[str], horizon: int, market: str, quantdb_dir: str | None = None
) -> pd.DataFrame:
    """已实现未来 H 日收益（CN 走 QuantDB 批量；其他市场逐日特征快照）。"""
    mkt = str(market or "CN").upper()
    if mkt in ("CN", "A", "CUSTOM", ""):
        closes = load_closes_quantdb(dates, horizon, quantdb_dir)
        if closes.empty:
            return pd.DataFrame(columns=["trade_date", "symbol", "label"])
        labels = forward_returns_from_closes(closes, horizon)
        wanted = {d for d in (_norm_date(x) for x in dates) if d}
        return labels[labels["trade_date"].isin(wanted)].reset_index(drop=True)
    frames = []
    for d in sorted({x for x in (_norm_date(v) for v in dates) if x}):
        df = _load_real_returns(_SNAPSHOT_DIR, d, mkt, horizon, quantdb_dir=quantdb_dir)
        if not df.empty:
            frames.append(df.assign(trade_date=d))
    if not frames:
        return pd.DataFrame(columns=["trade_date", "symbol", "label"])
    return pd.concat(frames, ignore_index=True)[["trade_date", "symbol", "label"]]


async def load_score_panels(
    model_ids: Sequence[str],
    *,
    tenant_id: str = "default",
    user_id: str = "",
    start_date: str,
    end_date: str | None = None,
) -> dict[str, pd.DataFrame]:
    """从各成员分数桶读 [trade_date, symbol, score]（realtime 行不参与证据）。"""
    buckets = {resolve_feature_version(m): m for m in model_ids}
    # end_date 条件拼进 SQL：裸 :end_date IS NULL 会让 asyncpg 无法推断参数类型
    end_clause = "AND trade_date <= :end_date" if end_date else ""
    stmt = text(
        f"""
        SELECT feature_version, trade_date, symbol, fusion_score
        FROM engine_signal_scores
        WHERE feature_version IN :buckets
          AND tenant_id = :tenant_id
          AND user_id = :user_id
          AND trade_date >= :start_date
          {end_clause}
          AND fusion_score IS NOT NULL
          AND COALESCE(source, '') <> 'realtime'
        """
    ).bindparams(bindparam("buckets", expanding=True))
    params: dict[str, Any] = {
        "buckets": list(buckets),
        "tenant_id": tenant_id,
        "user_id": user_id,
        "start_date": _as_date(start_date),
    }
    if end_date:
        params["end_date"] = _as_date(end_date)
    async with get_session(read_only=True) as session:
        rows = (await session.execute(stmt, params)).mappings().all()
    out: dict[str, pd.DataFrame] = {m: pd.DataFrame() for m in model_ids}
    if rows:
        df = pd.DataFrame([dict(r) for r in rows])
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        df["symbol"] = df["symbol"].map(lambda s: StockCodeUtil.to_prefix(str(s)))
        df = df.rename(columns={"fusion_score": "score"})
        for bucket, m in buckets.items():
            part = df[df["feature_version"] == bucket][
                ["trade_date", "symbol", "score"]
            ].reset_index(drop=True)
            out[m] = part
    return out


def _as_date(value: str | date) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _slice_last_days(panel: pd.DataFrame, days: int) -> pd.DataFrame:
    if panel.empty:
        return panel
    dates = sorted(panel["trade_date"].unique())[-days:]
    return panel[panel["trade_date"].isin(dates)].reset_index(drop=True)


async def gather_fusion_evidence(
    specs: Sequence[MemberSpec],
    *,
    tenant_id: str = "default",
    user_id: str = "",
    window_days: int = IC_WINDOW_DAYS,
    corr_window_days: int = CORR_WINDOW_DAYS,
    quantdb_dir: str | None = None,
) -> FusionEvidence:
    """组装证据：成员 IC 序列 + 相关矩阵（+ 面板/标签缓存供 compute_replay 复用）。"""
    warnings: list[str] = []
    model_ids = [s.model_id for s in specs]
    start = (datetime.now() - timedelta(days=int(window_days * 1.9) + 10)).strftime(
        "%Y-%m-%d"
    )
    panels = await load_score_panels(
        model_ids, tenant_id=tenant_id, user_id=user_id, start_date=start
    )

    horizons: dict[str, int] = {}
    markets: dict[str, str] = {}
    for s in specs:
        h = s.horizon_days
        if h is None:
            h = read_model_horizon(s.model_dir)
        horizons[s.model_id] = int(h)
        markets[s.model_id] = str(s.market or "CN").upper()

    labels_cache: dict[tuple[int, str], pd.DataFrame] = {}
    members: list[MemberEvidence] = []
    for s in specs:
        panel = _slice_last_days(panels.get(s.model_id, pd.DataFrame()), window_days)
        h = horizons[s.model_id]
        mkt = markets[s.model_id]
        if panel.empty:
            warnings.append(f"成员 {s.model_id} 窗口内无分数桶数据")
            members.append(
                MemberEvidence(s.model_id, h, mkt, {}, 0)
            )
            continue
        key = (h, mkt)
        if key not in labels_cache:
            dates = sorted(panel["trade_date"].unique())
            labels_cache[key] = load_labels(dates, h, mkt, quantdb_dir)
        labels = labels_cache[key]
        ic = daily_rank_ic(panel, labels)
        members.append(
            MemberEvidence(
                s.model_id, h, mkt, ic, int(panel["trade_date"].nunique())
            )
        )
        if not ic:
            warnings.append(
                f"成员 {s.model_id} 无已实现收益可算 IC（标签周期 {h} 日，需等收益兑现）"
            )

    corr_panels = {
        s.model_id: _slice_last_days(
            panels.get(s.model_id, pd.DataFrame()), corr_window_days
        )
        for s in specs
    }
    corr = pairwise_corr({m: p for m, p in corr_panels.items() if not p.empty})

    labels_by_horizon: dict[int, pd.DataFrame] = {}
    for (h, _mkt), frame in labels_cache.items():
        if frame.empty:
            continue
        labels_by_horizon[h] = (
            pd.concat([labels_by_horizon[h], frame], ignore_index=True)
            if h in labels_by_horizon
            else frame
        )

    return FusionEvidence(
        members=tuple(members),
        corr=corr,
        panels=panels,
        labels_by_horizon=labels_by_horizon,
        warnings=tuple(warnings),
    )


def compute_replay(
    evidence: FusionEvidence,
    weights: Mapping[str, float],
    target_horizon: int,
    *,
    min_symbols: int = MIN_CROSS_SECTION,
) -> FusionReplay:
    """用证据缓存回放融合（线性主通道），与成员 IC 并列。"""
    labels = evidence.labels_by_horizon.get(int(target_horizon))
    if labels is None or labels.empty:
        return FusionReplay(dates=(), fused_ic={}, member_ic={}, summary={})
    # 所有权重表内成员都参与回放（0 权成员仍计入生产模板的 n_models 共识分母）
    panels = {m: p for m, p in evidence.panels.items() if not p.empty}
    if not panels:
        return FusionReplay(dates=(), fused_ic={}, member_ic={}, summary={})
    return replay_fusion(panels, weights, labels, min_symbols=min_symbols)
