"""因子报告 REST 端点（引擎服务，经 api 网关 /api/v1/factor-report/* 转发）。

- GET /datasets    可选数据集清单（报告页顶部切换器用）
- GET /summary     快照摘要：因子排行（IC/ICIR/分位价差/单调性/换手）+ 元数据
- GET /detail      单因子明细：分位净值、分位平均收益、IC 序列、换手序列
- GET /correlation 相关性子矩阵（从快照取，按请求顺序）
- GET /related     某因子的高相关因子 TopN

所有端点都接受 dataset 参数（alpha_library / l1_factors / l2_factors / l1_l2_factors），
缺省 alpha_library。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from . import service
from .datasets import DATASETS, DEFAULT_DATASET

router = APIRouter(prefix="/api/v1/factor-report", tags=["Factor Report"])

_DATASET_DESC = "数据集：alpha_library / l1_factors / l2_factors / l1_l2_factors"


@router.get("/datasets")
async def list_datasets():
    """可选数据集及其快照状态（页面据此禁用尚未生成的数据集）。"""
    items = []
    for name, cfg in DATASETS.items():
        snap = service.load_snapshot(name)
        meta = (snap or {}).get("meta") or {}
        items.append({
            "dataset": name,
            "label": cfg.get("label") or name,
            "available": bool(snap),
            "horizon": meta.get("horizon"),
            "n_factors": meta.get("n_factors"),
            "start": meta.get("start"),
            "end": meta.get("end"),
            "generated_at": meta.get("generated_at"),
        })
    return {"default": DEFAULT_DATASET, "items": items}


@router.get("/summary")
async def factor_summary(
    dataset: str = Query(default=DEFAULT_DATASET, description=_DATASET_DESC),
    library: str | None = Query(default=None, description="按库过滤：alpha158 / alpha101 / gtja191 / L1 / L2"),
    sort: str = Query(default="abs_ic", description="排序：abs_ic | icir | turnover | ls_mean | name"),
    limit: int = Query(default=0, ge=0, le=1000, description="0 = 全部"),
):
    ds = service.normalize_dataset(dataset)
    snap = service.load_snapshot(ds)
    if not snap:
        return {
            "available": False,
            "dataset": ds,
            "reason": f"数据集 {ds} 的因子报告快照尚未生成；在服务器执行 "
                      f"python backend/scripts/build_factor_report.py --dataset {ds} 后刷新",
        }
    items = list(snap.get("factors") or [])
    if library:
        items = [x for x in items if x.get("library") == library]

    def _key(x: dict):
        if sort == "icir":
            return abs(x.get("icir") or 0)
        if sort == "turnover":
            return -(x.get("turnover") or 0)
        if sort == "ls_mean":
            return abs(x.get("ls_mean") or 0)
        if sort == "name":
            return x.get("name") or ""
        return abs(x.get("ic_mean") or 0)

    reverse = sort != "name"
    if reverse:
        items.sort(key=_key, reverse=True)
    else:
        items.sort(key=_key)
    total = len(items)
    if limit:
        items = items[:limit]
    return {"available": True, "dataset": ds, "meta": snap.get("meta") or {}, "total": total, "factors": items}


@router.get("/detail")
async def factor_detail(
    factor: str = Query(..., description="因子名，如 a158_ROC20 / turn_20 / micro_vpin_20"),
    dataset: str = Query(default=DEFAULT_DATASET, description=_DATASET_DESC),
    horizon: str = Query(default="fwd_ret_5", description="前瞻期：fwd_ret_1/2/3/5/10/20"),
    lookback: int = Query(default=250, ge=20, le=1200, description="回看交易日数"),
    long_group: int = Query(default=service.DEFAULT_LONG_GROUP, ge=1, le=10, description="多头组（G1=因子值最小）"),
    short_group: int = Query(default=service.DEFAULT_SHORT_GROUP, ge=1, le=10, description="空头组"),
    cost_bps: float = Query(default=service.DEFAULT_COST_BPS, ge=0, le=1000, description="双边成本（bps）"),
    bench: str | None = Query(default=None, description="主基准指数代码，如 000300.SH（默认沪深300）"),
):
    try:
        return service.compute_detail(
            dataset, factor, horizon=horizon, lookback=lookback,
            long_group=long_group, short_group=short_group, cost_bps=cost_bps, bench=bench,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/correlation")
async def factor_correlation(
    factors: str = Query(..., description="逗号分隔的因子名（2-30 个）"),
    dataset: str = Query(default=DEFAULT_DATASET, description=_DATASET_DESC),
):
    names = [n.strip() for n in factors.split(",") if n.strip()]
    if not names:
        raise HTTPException(status_code=400, detail="factors 不能为空")
    if len(names) > 30:
        raise HTTPException(status_code=400, detail="一次最多比较 30 个因子")
    return service.correlation_slice(dataset, names)


@router.get("/related")
async def factor_related(
    factor: str = Query(..., description="因子名"),
    dataset: str = Query(default=DEFAULT_DATASET, description=_DATASET_DESC),
    top: int = Query(default=8, ge=1, le=50),
):
    return {
        "dataset": service.normalize_dataset(dataset),
        "factor": factor,
        "related": service.top_correlated(dataset, factor, top=top),
    }


@router.get("/portfolio")
async def factor_portfolio(
    dataset: str = Query(default=DEFAULT_DATASET, description=_DATASET_DESC),
    recompute: bool = Query(default=False, description="忽略已落盘的 JSON，按当前快照重算"),
    n_top: int = Query(default=30, ge=5, le=200),
):
    """推荐因子组合：入选集 + 权重 + 淘汰理由（训练页勾选的数据来源）。"""
    from .portfolio import build_portfolio, portfolio_path

    ds = service.normalize_dataset(dataset)
    if not recompute:
        path = portfolio_path(ds)
        if path.exists():
            import json

            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:  # noqa: BLE001 — 落盘损坏则重算
                pass
    return build_portfolio(ds, n_top=n_top, persist=False)


@router.get("/clusters")
async def factor_clusters(
    dataset: str = Query(default=DEFAULT_DATASET, description=_DATASET_DESC),
    threshold: float = Query(default=0.9, ge=0.5, le=0.999, description="|ρ| 阈值，≥ 即视为同源"),
    keep: str = Query(default="icir", description="每簇保留口径：icir | abs_ic | ls"),
):
    """因子去重清单：同源因子簇 + 每簇代表（其余为重复项）。"""
    return service.correlation_clusters(dataset, threshold=threshold, keep=keep)
