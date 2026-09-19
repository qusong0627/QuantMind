"""因子报告（Alphalens 式）——快照读取 + 单因子明细。

数据来源与口径见 `backend/scripts/build_factor_report.py`（快照构建脚本）与
`datasets.py`（数据集注册表，构建脚本与本服务共用）：
  - 快照 JSON：<数据集>/report/factor_report.json —— 因子排行（IC/ICIR/分位收益/换手）+ 秩相关矩阵
  - 明细序列：<数据集>/report/factor_series.parquet —— 逐日 × 逐因子（IC/十分位收益/换手/覆盖率），
    detail 接口靠它把「扫 250 个分区 ~2.4s」变成毫秒级切片
  - 兜底：序列文件缺失时按需扫分区（仅 labels_table 模式；close_fwd 模式无标签表，需要序列文件）

支持数据集：alpha_library / l1_factors / l2_factors / l1_l2_factors（见 datasets.py）
"""

from __future__ import annotations

import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from . import blocks as BLK
from .datasets import DATASETS, DEFAULT_DATASET, dataset_dir, label_dir

log = logging.getLogger(__name__)

N_QUANTILES = 10
HORIZONS = ("fwd_ret_1", "fwd_ret_2", "fwd_ret_3", "fwd_ret_5", "fwd_ret_10", "fwd_ret_20")

# 明细结果的进程内缓存（{dataset:factor:horizon:lookback:分组:成本:基准} → payload）
#
# ⚠️ 分组/成本/基准**必须在键里**：它们改的是多空曲线本身，不进键就会出现
# 「把 G3/G9 改成 G1/G10 后页面还是旧曲线」——静默错数，且刷新页面也不消失。
_DETAIL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_DETAIL_TTL_SECONDS = 600

DEFAULT_LONG_GROUP = 3
DEFAULT_SHORT_GROUP = 9
DEFAULT_COST_BPS = 20.0


def normalize_dataset(dataset: str | None) -> str:
    key = str(dataset or "").strip()
    return key if key in DATASETS else DEFAULT_DATASET


def snapshot_path(dataset: str = DEFAULT_DATASET) -> Path:
    return dataset_dir(normalize_dataset(dataset)) / "report" / "factor_report.json"


def series_path(dataset: str = DEFAULT_DATASET) -> Path:
    """单因子逐日明细序列（快照构建脚本一并产出）。"""
    return dataset_dir(normalize_dataset(dataset)) / "report" / "factor_series.parquet"


def load_snapshot(dataset: str = DEFAULT_DATASET) -> dict[str, Any] | None:
    """读取快照；不存在返回 None（页面据此提示「尚未生成」）。"""
    path = snapshot_path(dataset)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        log.warning(f"因子报告快照解析失败({dataset}): {e}")
        return None


def _partition_dates(dataset: str, limit: int | None = None) -> list[str]:
    """可用的分区日期（升序）。limit 表示只取最近 N 个。"""
    root = dataset_dir(normalize_dataset(dataset))
    dts = sorted(p.name.split("=")[1] for p in root.glob("dt=*") if p.is_dir())
    if limit and limit > 0:
        dts = dts[-limit:]
    return dts


def _read_factor_column(dataset: str, dt: str, factor: str) -> Any:
    path = dataset_dir(normalize_dataset(dataset)) / f"dt={dt}" / "data.parquet"
    if not path.exists():
        return None
    try:
        return pq.read_table(path, columns=["symbol", factor]).to_pandas()
    except Exception:  # noqa: BLE001 — 该因子在此分区不存在（不同数据集列不同）
        return None


def _read_label_column(dataset: str, dt: str, horizon: str) -> Any:
    root = label_dir(normalize_dataset(dataset))
    if root is None:
        return None
    path = root / f"dt={dt}" / "data.parquet"
    if not path.exists():
        return None
    try:
        return pq.read_table(path, columns=["symbol", horizon]).to_pandas()
    except Exception:  # noqa: BLE001
        return None


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    """秩相关（各自转秩后 Pearson）。样本过少或任一序列无变化时返回 NaN。

    注意：常量序列的秩相关在数学上是未定义的 —— 必须在**转秩之前**判方差，
    否则稳定排序会给常量列安排上 1..n 的假秩，算出一个看似正常的相关性。
    """
    if a.size < 20:
        return float("nan")
    if np.nanstd(a) <= 0 or np.nanstd(b) <= 0:
        return float("nan")
    ra = _rank(a)
    rb = _rank(b)
    sa, sb = ra.std(), rb.std()
    if sa <= 0 or sb <= 0:
        return float("nan")
    return float(((ra - ra.mean()) * (rb - rb.mean())).mean() / (sa * sb))


def _rank(x: np.ndarray) -> np.ndarray:
    order = np.argsort(x, kind="stable")
    r = np.empty(x.size, dtype=np.float64)
    r[order] = np.arange(1, x.size + 1, dtype=np.float64)
    return r


def _detail_from_series(
    dataset: str, factor: str, horizon: str, lookback: int,
    *, long_group: int, short_group: int, cost_bps: float, bench: str | None,
) -> dict[str, Any] | None:
    """从预计算序列 parquet 取明细（毫秒级）。

    返回 None 表示序列文件不可用（或快照的前瞻期与请求不一致），由调用方回退按需扫描。

    ⚠️ 读的是**全窗口**再切片，不是直接 tail：累计型指标（累计 IC、净值、超额累计）
    必须全窗口才有意义，而噪声型的日序列只需 ``lookback`` 个点。整列读进来
    也就几百 KB，不做两次 IO。
    """
    path = series_path(dataset)
    if not path.exists():
        return None
    snap = load_snapshot(dataset) or {}
    meta = snap.get("meta") or {}
    snap_horizon = str(meta.get("horizon") or "")
    if snap_horizon and snap_horizon != horizon:
        return None
    try:
        tbl = pq.read_table(path, filters=[("factor", "=", factor)])
    except Exception as e:  # noqa: BLE001 — 文件损坏/字段不符时回退扫描
        log.warning(f"读取因子序列失败，回退按需扫描: {e}")
        return None
    full = tbl.to_pandas()
    if full.empty:
        return None
    # lookback <= 0 = 全窗口（与下方按需扫描路径 :302 同义）；否则至少 20 天
    full = full.sort_values("date")
    df = full if lookback <= 0 else full.tail(max(lookback, 20))
    dates = [str(int(d)) for d in df["date"].tolist()]
    q_cols = [f"q{i}" for i in range(1, N_QUANTILES + 1)]
    q_mat = df[q_cols].to_numpy(dtype=np.float64)          # T × 10
    ic_arr = df["ic"].to_numpy(dtype=np.float64)
    k = int(horizon.split("_")[-1])
    daily = q_mat / k
    curves = np.cumprod(1.0 + np.nan_to_num(daily, nan=0.0), axis=0)
    roll = 20
    ic_roll: list[float | None] = []
    for i in range(len(ic_arr)):
        if i + 1 < roll:
            ic_roll.append(None)
            continue
        seg = ic_arr[i + 1 - roll : i + 1]
        ic_roll.append(None if not np.isfinite(seg).any() else float(np.nanmean(seg)))
    turnover = df["turnover"].to_numpy(dtype=np.float64)
    # 机构级九个块（读时派生）；任一数据源缺失时内部降级为 available=False + reason
    try:
        blocks = BLK.build_blocks(
            full, factor=factor, horizon=horizon, lookback=lookback,
            long_group=long_group, short_group=short_group, cost_bps=cost_bps,
            snapshot=snap, bench_symbol=bench,
        )
    except Exception as e:  # noqa: BLE001 — 新块失败不该让既有明细整页打不开
        log.exception("因子明细块派生失败(%s/%s)", dataset, factor)
        blocks = {"error": f"{type(e).__name__}: {e}"}
    return {
        "dataset": normalize_dataset(dataset),
        "factor": factor,
        "horizon": horizon,
        "empty": False,
        "source": "series_snapshot",
        "schema_version": meta.get("schema_version"),
        "blocks": blocks,
        "dates": dates,
        "quantile_mean": [float(v) for v in np.nanmean(q_mat, axis=0)],
        "quantile_curves": [[float(v) for v in curves[:, j]] for j in range(N_QUANTILES)],
        "ls_curve": [float(v) for v in curves[:, -1] / np.maximum(curves[:, 0], 1e-9)],
        "ic_series": [None if not np.isfinite(v) else float(v) for v in ic_arr],
        "ic_rolling": ic_roll,
        "ic_mean": float(np.nanmean(ic_arr)) if np.isfinite(ic_arr).any() else None,
        "ic_std": float(np.nanstd(ic_arr)) if np.isfinite(ic_arr).any() else None,
        "turnover_dates": dates,
        "turnover_series": [float(v) for v in turnover],
        "turnover_mean": float(np.nanmean(turnover)) if np.isfinite(turnover).any() else None,
        "coverage_mean": float(df["coverage"].mean()),
        "n_dates": len(dates),
        "start": dates[0],
        "end": dates[-1],
    }


def _load_pairs(dataset: str, dt: str, factor: str, horizon: str, label_mode: str, all_dts: list[str]) -> tuple[str, Any, Any] | None:
    """读一个分区的因子列与标签列（供线程池并行调用）。

    label_mode=labels_table：标签读同 dt 的标签表；
    label_mode=close_fwd：标签=未来第 k 个交易日的 close（同一数据集内取）。
    """
    fac = _read_factor_column(dataset, dt, factor)
    if fac is None:
        return None
    k = int(horizon.split("_")[-1])
    if label_mode == "labels_table":
        lab = _read_label_column(dataset, dt, horizon)
        if lab is None:
            return None
        return dt, fac, lab
    # close_fwd：本分区 close 与 T+k 分区 close
    try:
        idx = all_dts.index(dt)
    except ValueError:
        return None
    j = idx + k
    if j >= len(all_dts):
        return None
    root = dataset_dir(normalize_dataset(dataset))
    try:
        fut = pq.read_table(f"{root}/dt={all_dts[j]}/data.parquet", columns=["symbol", "close"]).to_pandas()
    except Exception:  # noqa: BLE001
        return None
    base = _read_factor_column(dataset, dt, "close")
    if base is None:
        return None
    merged = base.merge(fut, on="symbol", how="inner", suffixes=("_t", "_tk"))
    col_t = "close_t" if "close_t" in merged.columns else "close"
    col_k = "close_tk" if "close_tk" in merged.columns else "close"
    c0 = merged[col_t].to_numpy(dtype=np.float64)
    c1 = merged[col_k].to_numpy(dtype=np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        lab = merged[["symbol"]].copy()
        lab[horizon] = np.where((c0 > 0) & np.isfinite(c0) & np.isfinite(c1), c1 / c0 - 1.0, np.nan)
    return dt, fac, lab


def compute_detail(
    dataset: str, factor: str, horizon: str = "fwd_ret_5", lookback: int = 250,
    *, long_group: int = DEFAULT_LONG_GROUP, short_group: int = DEFAULT_SHORT_GROUP,
    cost_bps: float = DEFAULT_COST_BPS, bench: str | None = None,
) -> dict[str, Any]:
    """单因子明细：分位净值曲线、分位平均收益、IC 序列、换手序列、覆盖率 + 九个机构级块。

    优先读预计算序列（毫秒级）；缺失时回退扫分区（回退路径不产新块）。
    """
    ds = normalize_dataset(dataset)
    if horizon not in HORIZONS:
        raise ValueError(f"不支持的前瞻期: {horizon}")
    if not factor or not factor.replace("_", "").isalnum():
        raise ValueError("非法因子名")
    long_group, short_group = int(long_group), int(short_group)
    if not (1 <= long_group <= N_QUANTILES and 1 <= short_group <= N_QUANTILES):
        raise ValueError(f"分组下标越界（合法 1..{N_QUANTILES}）：长 {long_group} / 空 {short_group}")
    if long_group == short_group:
        raise ValueError("多空不能是同一组（曲线会恒等于 0 且看起来像「没行情」）")
    cost_bps = float(cost_bps)
    if not (0.0 <= cost_bps <= 1000.0):
        raise ValueError(f"成本 bps 超出合理范围 0..1000：{cost_bps}")

    key = f"{ds}:{factor}:{horizon}:{lookback}:{long_group}:{short_group}:{cost_bps:g}:{bench or ''}"
    hit = _DETAIL_CACHE.get(key)
    if hit and time.time() - hit[0] < _DETAIL_TTL_SECONDS:
        return hit[1]

    fast = _detail_from_series(ds, factor, horizon, lookback, long_group=long_group,
                               short_group=short_group, cost_bps=cost_bps, bench=bench)
    if fast is not None:
        _DETAIL_CACHE[key] = (time.time(), fast)
        return fast

    label_mode = str((DATASETS.get(ds) or {}).get("label_mode") or "labels_table")
    if label_mode != "labels_table":
        # close_fwd 数据集没有独立标签表，明细只能来自序列快照
        payload = {
            "dataset": ds,
            "factor": factor,
            "horizon": horizon,
            "empty": True,
            "reason": "该数据集的明细序列尚未生成，请先运行 backend/scripts/build_factor_report.py --dataset " + ds,
            "dates": [],
        }
        _DETAIL_CACHE[key] = (time.time(), payload)
        return payload

    all_dts = _partition_dates(ds)
    dts = all_dts[-lookback:] if lookback and lookback > 0 else all_dts
    loaded: list[tuple[str, Any, Any]] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_load_pairs, ds, dt, factor, horizon, label_mode, all_dts) for dt in dts]
        for fut in futures:
            try:
                res = fut.result()
            except Exception:  # noqa: BLE001 — 单个分区读失败不影响整体
                continue
            if res:
                loaded.append(res)
    loaded.sort(key=lambda x: x[0])

    dates: list[str] = []
    q_returns: list[np.ndarray] = []
    ic_series: list[float] = []
    coverage: list[float] = []
    members_prev: np.ndarray | None = None
    prev_syms: np.ndarray | None = None
    turnover_series: list[float] = []

    for dt, fac, lab in loaded:
        merged = fac.merge(lab, on="symbol", how="inner")
        if horizon not in merged.columns or len(merged) < 50:
            continue
        x = merged[factor].to_numpy(dtype=np.float64) if factor in merged.columns else None
        if x is None:
            continue
        y = merged[horizon].to_numpy(dtype=np.float64)
        ok = np.isfinite(x) & np.isfinite(y)
        if ok.sum() < 50:
            continue
        xv, yv = x[ok], y[ok]
        nq = max(1, math.ceil(xv.size / N_QUANTILES))
        order = np.argsort(xv, kind="stable")
        group = np.empty(xv.size, dtype=np.int16)
        group[order] = np.minimum(np.arange(xv.size, dtype=np.int64) // nq, N_QUANTILES - 1)

        cnt = np.bincount(group, minlength=N_QUANTILES)
        ssum = np.bincount(group, weights=yv, minlength=N_QUANTILES)
        q_ret = np.divide(ssum, np.maximum(cnt, 1), out=np.zeros(N_QUANTILES), where=cnt > 0)

        dates.append(dt)
        q_returns.append(q_ret)
        ic_series.append(_spearman(xv, yv))
        coverage.append(float(ok.sum() / max(len(merged), 1)))

        # 换手按 symbol 对齐再比（行序逐日可能变，按位置比会算成噪声；见 build_factor_report.py 同款注释）
        syms_valid = merged["symbol"].to_numpy()[ok]
        if members_prev is not None and prev_syms is not None:
            _, ia, ib = np.intersect1d(prev_syms, syms_valid, return_indices=True)
            if ia.size:
                turnover_series.append(float((members_prev[ia] != group[ib]).mean()))
        members_prev, prev_syms = group, syms_valid

    if not dates:
        payload = {
            "dataset": ds, "factor": factor, "horizon": horizon, "empty": True,
            "dates": [], "reason": "窗口内没有可用的因子/标签分区",
        }
        _DETAIL_CACHE[key] = (time.time(), payload)
        return payload

    q_mat = np.vstack(q_returns)
    k = int(horizon.split("_")[-1])
    daily = q_mat / k
    curves = np.cumprod(1.0 + daily, axis=0)

    ic_arr = np.array(ic_series, dtype=np.float64)
    roll = 20
    ic_roll: list[float | None] = []
    for i in range(len(ic_arr)):
        if i + 1 < roll:
            ic_roll.append(None)
        else:
            seg = ic_arr[i + 1 - roll : i + 1]
            ic_roll.append(None if not np.isfinite(seg).any() else float(np.nanmean(seg)))

    payload = {
        "dataset": ds,
        "factor": factor,
        "horizon": horizon,
        "empty": False,
        "source": "partition_scan",
        "dates": dates,
        "quantile_mean": [float(v) for v in np.nanmean(q_mat, axis=0)],
        "quantile_curves": [[float(v) for v in curves[:, j]] for j in range(N_QUANTILES)],
        "ls_curve": [float(v) for v in curves[:, -1] / np.maximum(curves[:, 0], 1e-9)],
        "ic_series": [None if not np.isfinite(v) else float(v) for v in ic_arr],
        "ic_rolling": ic_roll,
        "ic_mean": float(np.nanmean(ic_arr)) if np.isfinite(ic_arr).any() else None,
        "ic_std": float(np.nanstd(ic_arr)) if np.isfinite(ic_arr).any() else None,
        "turnover_dates": dates[1 : 1 + len(turnover_series)],
        "turnover_series": turnover_series,
        "turnover_mean": float(np.mean(turnover_series)) if turnover_series else None,
        "coverage_mean": float(np.mean(coverage)) if coverage else None,
        "n_dates": len(dates),
        "start": dates[0],
        "end": dates[-1],
    }
    _DETAIL_CACHE[key] = (time.time(), payload)
    return payload


def correlation_slice(dataset: str, names: list[str]) -> dict[str, Any]:
    """从快照里取子矩阵（保持请求顺序，忽略不存在的因子）。"""
    ds = normalize_dataset(dataset)
    snap = load_snapshot(ds)
    if not snap:
        return {"available": False, "reason": f"数据集 {ds} 的快照尚未生成，请运行 backend/scripts/build_factor_report.py --dataset {ds}"}
    corr = snap.get("correlation") or {}
    all_names: list[str] = list(corr.get("factors") or [])
    matrix: list[list[float]] = list(corr.get("matrix") or [])
    idx = {n: i for i, n in enumerate(all_names)}
    picked = [n for n in names if n in idx]
    if not picked:
        return {"available": True, "dataset": ds, "factors": [], "matrix": []}
    rows = [[float(matrix[idx[a]][idx[b]]) for b in picked] for a in picked]
    return {"available": True, "dataset": ds, "factors": picked, "matrix": rows}


def top_correlated(dataset: str, factor: str, top: int = 8) -> list[dict[str, Any]]:
    """与某因子相关性最高（含负相关）的其它因子，用于明细页「相关因子」区。"""
    ds = normalize_dataset(dataset)
    snap = load_snapshot(ds)
    if not snap:
        return []
    corr = snap.get("correlation") or {}
    all_names: list[str] = list(corr.get("factors") or [])
    matrix: list[list[float]] = list(corr.get("matrix") or [])
    if factor not in all_names:
        return []
    i = all_names.index(factor)
    pairs = [
        {"name": n, "corr": round(float(matrix[i][j]), 3)}
        for j, n in enumerate(all_names)
        if j != i
    ]
    pairs.sort(key=lambda p: abs(p["corr"]), reverse=True)
    return pairs[: max(1, top)]


def _diversity_before_after(
    names: list[str], matrix: list, clusters: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """全库 vs 去重后（每簇留代表）的有效因子数 / 多样性熵。

    回答「按 |ρ| 聚类去重会不会把多样性也削掉」：N_eff = exp(熵)，
    相当于几个独立因子。实现复用训练侧 factor_quality（单一公式来源）。
    """
    from backend.shared.factor_quality import load_factor_quality

    fq = load_factor_quality()
    if fq is None or not names or not matrix:
        return None
    try:
        arr = np.asarray(matrix, dtype=np.float64)
        if arr.shape[0] != len(names):
            return None
        dup = {m["name"] for c in clusters for m in c["members"] if not m["is_rep"]}
        keep_idx = [i for i, n in enumerate(names) if n not in dup]
        n_eff_all = fq.effective_factors(arr)
        entropy_all = fq.diversity_entropy(arr)
        n_eff_keep = None
        entropy_keep = None
        if keep_idx:
            sub = arr[np.ix_(keep_idx, keep_idx)]
            n_eff_keep = fq.effective_factors(sub)
            entropy_keep = fq.diversity_entropy(sub)
        return {
            "n_total": len(names),
            "n_eff": round(float(n_eff_all), 2) if n_eff_all is not None else None,
            "entropy": round(float(entropy_all), 4) if entropy_all is not None else None,
            "n_keep": len(keep_idx),
            "n_eff_after": round(float(n_eff_keep), 2) if n_eff_keep is not None else None,
            "entropy_after": round(float(entropy_keep), 4) if entropy_keep is not None else None,
        }
    except Exception:  # noqa: BLE001 — 多样性度量失败不影响去重结果
        return None


def correlation_clusters(dataset: str, threshold: float = 0.9, keep: str = "icir") -> dict[str, Any]:
    """因子去重簇：|ρ| ≥ 阈值 的同源因子聚成一簇，每簇留一个代表。"""
    from .clusters import cluster_by_correlation, summarize

    ds = normalize_dataset(dataset)
    snap = load_snapshot(ds)
    if not snap:
        return {"available": False, "dataset": ds, "reason":
                f"数据集 {ds} 的快照尚未生成，请运行 backend/scripts/build_factor_report.py --dataset {ds}"}
    corr = snap.get("correlation") or {}
    names: list[str] = list(corr.get("factors") or [])
    matrix = list(corr.get("matrix") or [])
    metrics = {f.get("name"): f for f in (snap.get("factors") or []) if f.get("name")}
    clusters = cluster_by_correlation(names, matrix, metrics, threshold=threshold, keep=keep)
    return {
        "available": True,
        "dataset": ds,
        "threshold": threshold,
        "keep": keep,
        "summary": summarize(len(names), clusters),
        "diversity": _diversity_before_after(names, matrix, clusters),
        "clusters": clusters,
    }
