"""因子研究 API 路由（/api/v1/factor-research）。

挂载：backend/services/engine/main.py 中 include_router；
网关放行：backend/services/api/routers/engine_proxy.py 白名单前缀。

数据集：dataset=classic（demo 复刻 82 因子，**A 股专属**，默认）| private
（筛选最终保留的私人因子库，当前 292）。
市场：全部端点带 ``market``（缺省 ``"CN"``，存量调用零变化）；非法市场在本层
即 4xx 拒绝（绝不静默落 CN —— 要查港股却读到 A 股数据是数据污染）。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.services.engine.factor_research import service
from backend.services.engine.factor_research.store import (
    DedupMatrixMissing,
    FactorDataset,
    MarketUnsupported,
    SnapshotMissing,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/factor-research", tags=["Factor Research"])


def _market(market: str) -> str:
    """市场参数收口：规范化 + 非法值**请求级 422**。

    不把校验留给服务深处：深层 ``validate_market`` 抛的裸 ``ValueError`` 会一路
    冒泡成 500（文案对，状态码错，前端还得按错误正文猜是参数问题还是服务坏了）。
    """
    from backend.shared.feature_source import validate_market

    try:
        return validate_market(market)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


def _snapshot_guard(exc: FileNotFoundError) -> None:
    """快照缺失（如未计算/重建中）→ 503 可读提示，而非 500。

    按**类型**判，不按文案判：数据目录缺失（``6_ml_datasets`` 没挂上 / 没生成）
    的报错文案里也有「缺失」，按文案判会把它翻译成「请点一键计算」—— 指错路，
    且用户照着点只会白等一次几分钟的重建。
    """
    if isinstance(exc, SnapshotMissing):
        raise HTTPException(
            status_code=503,
            detail="因子快照不完整（可能未生成或正在重建）：请在页面右上角「快照」中一键计算",
        )
    raise exc


async def _run(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except MarketUnsupported as e:
        # 422：请求本身合法，但「市场 × 数据集」组合不供给（如 classic 经典库
        # 为 A 股专属）—— 与「未知数据集」（400）、「快照缺失」（503）指引互不串门。
        raise HTTPException(status_code=422, detail=str(e)) from e
    except FileNotFoundError as e:
        _snapshot_guard(e)
        raise  # pragma: no cover - _snapshot_guard 必然抛出
    except DedupMatrixMissing as e:
        # 409 + 可操作文案：这是离线矩阵没建（与快照无关，不是「一键计算」能修的）
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.get("/catalog")
async def get_catalog(dataset: FactorDataset = "classic", market: str = "CN"):
    """因子目录（大类/小类/方向/公式/数据源/可用性）+ 快照元信息 + 本市场基准清单。"""
    return await _run(service.catalog, dataset, _market(market))


@router.get("/leaderboard")
async def get_leaderboard(
    start: str | None = None,
    end: str | None = None,
    n: int = 30,
    dataset: FactorDataset = "classic",
    dedup: float = 0.0,
    market: str = "CN",
):
    """排行榜：区间内重算 KPI/超额/RankIC + 持仓画像 + 双标签 + 综合分。

    n = 业绩 KPI 的持仓数（默认 30）；标签恒按 top-30 判定；全列返回，前端排序。
    dedup > 0：同源去重（|ρ| ≥ dedup 只留榜单更靠前者，口径同筛选页）+ rank 重编号；
    相关矩阵缺失 → 409（先去运行 build_factor_panel_corr.py）。
    超额按本市场基准映射返回（``excess_by_bench``；CN 另带 excess_300/800/500 旧键）。
    """
    return await _run(
        service.leaderboard, start, end, n, dataset, dedup, _market(market)
    )


@router.get("/factor/{code}")
async def get_factor(
    code: str,
    ns: str = "30",
    start: str | None = None,
    end: str | None = None,
    stocks_n: int = 30,
    dataset: FactorDataset = "classic",
    market: str = "CN",
):
    """单因子：区间内多档持仓数（ns=5,10,30）净值/KPI/超额 + N 扫描 + 个股表 + 分布。"""
    try:
        wanted = [int(x) for x in str(ns).split(",") if x.strip()]
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"ns 需为逗号分隔整数: {e}") from e
    d = await _run(
        service.factor_detail,
        code,
        wanted,
        start,
        end,
        stocks_n,
        dataset,
        _market(market),
    )
    if d is None:
        raise HTTPException(status_code=404, detail=f"未知因子: {code}")
    return d


class CompareItem(BaseModel):
    code: str
    n: int = Field(30, ge=1, le=100)


class CompareRequest(BaseModel):
    items: list[CompareItem]
    start: str | None = None
    end: str | None = None
    dataset: FactorDataset = "classic"
    market: str = "CN"


@router.get("/compare")
async def get_compare(
    codes: str,
    start: str | None = None,
    end: str | None = None,
    dataset: FactorDataset = "classic",
    market: str = "CN",
):
    """多因子对比（GET 兼容形态：codes=逗号分隔，每因子 Top-30）。"""
    lst = [c.strip() for c in codes.split(",") if c.strip()]
    if not lst:
        raise HTTPException(status_code=422, detail="codes 不能为空")
    return await _run(service.compare, lst, start, end, dataset, _market(market))


@router.post("/compare")
async def post_compare(req: CompareRequest):
    """多因子对比（每因子可单独设持仓数 n，≤12 个）。"""
    if not req.items:
        raise HTTPException(status_code=422, detail="items 不能为空")
    items = [it.model_dump() for it in req.items]
    return await _run(
        service.compare, items, req.start, req.end, req.dataset, _market(req.market)
    )


@router.get("/screening")
async def get_screening(market: str = "CN"):
    """因子筛选：质量门槛 + 同源去重后的保留清单与剔除明细（训练特征选择用）。

    非 CN 诚实缺席（available:false + reason）：该产品为 A 股口径的七库筛选。
    """
    return await _run(service.screening, _market(market))


@router.get("/scan")
async def get_scan(dataset: FactorDataset = "private", market: str = "CN"):
    """扫描本市场数据目录，列出与快照目录的差异（新增/消失）。

    **只读**：不写 factors.json、不触发重算（重算要 5~15 分钟并重写 5.5GB 宽表，
    值不值得付由用户看完差异再定）。dataset 只支持 private —— classic 的目录来自
    静态 catalog.py，没有「盘上有什么」可对，故明确 400 而不是回一份空差异。
    「哪个 dataset 能扫」这条规则只在 ``service.scan_sources`` 里写一次，这里只
    把它的 ``ValueError`` 翻成状态码（避免两处各判一次、日后改一处漏一处）。
    """
    try:
        return await _run(service.scan_sources, dataset, _market(market))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        # 能走到这里的 FileNotFoundError 都不是快照缺失（那些在 `_run` 里已翻成
        # 「请点一键计算」了），而是数据目录本身的问题 —— 指路要指到「数据」。
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/snapshot-status")
async def get_snapshot_status(dataset: FactorDataset = "classic", market: str = "CN"):
    """快照状态（是否存在/构建中/进度日志）—— 供前端「一键计算」入口。"""
    return await _run(service.snapshot_status, dataset, _market(market))


@router.post("/build")
async def post_build(
    dataset: FactorDataset = "classic",
    market: str = "CN",
    start: str | None = None,
    end: str | None = None,
    append: bool = False,
):
    """一键计算快照（后台子进程，全本地 QuantDB 计算、不上传任何数据）。

    private 市场感知：非 CN 会以 ``--market`` 装配构建脚本（构建落本市场根目录）。
    ``start``/``end`` 只重建该区间；``append=true`` 为增量续拉（保留旧年分区，
    私有库须已有 direction.json）。classic 不支持 append（全量重建约 6 分钟）。
    """
    out = await _run(service.start_build, dataset, _market(market), start, end, append)
    if out.get("error"):
        raise HTTPException(status_code=500, detail=out["error"])
    return out


@router.post("/promote")
async def post_promote():
    """一键毕业：CUSTOM 挖掘因子库 →（镜像 + 字段注册）→ CN 盘面。

    刻意不发布 CN 训练目录（--publish 留给人/训练页）：这个按钮只让因子
    到达「因子研究/报告」的取数面。跑完重建快照即可见。

    **CN 专属端点**（毕业桥目标 = CN rd_mined 目录，不带 market 参数；
    非 CN 前端隐藏该入口，扫描响应里 pipeline 亦为 None）。
    """
    out = await _run(service.start_promote)
    if out.get("error"):
        raise HTTPException(status_code=500, detail=out["error"])
    return out


@router.get("/promote-status")
async def get_promote_status():
    """毕业桥状态（是否运行中 / 日志最新一行）—— 供扫描面板一键毕业入口轮询。**CN 专属**。"""
    return await _run(service.promote_status)


@router.get("/correlation")
async def get_correlation(codes: str | None = None, market: str = "CN"):
    """全量因子相关对（classic 产物）。非 CN 无此产物 → 422（不拿 A 股矩阵冒充）。"""
    lst = [c.strip() for c in codes.split(",") if c.strip()] if codes else None
    return await _run(service.correlation, lst, _market(market))


class ComposeRequest(BaseModel):
    weights: dict[str, float] = Field(..., description="因子代码 → 权重（可负）")
    top_n: int = Field(30, ge=1, le=200)
    threshold: float | None = Field(
        None, description="合成打分下限（z 刻度），None=不过滤"
    )
    thresholds: dict[str, float] | None = Field(
        None, description="每因子过滤下限（z 刻度，选股前先筛）"
    )
    cost_rate: float | None = Field(None, ge=0, le=0.05)
    start: str | None = None
    end: str | None = None
    dataset: FactorDataset = "classic"
    market: str = "CN"


@router.post("/compose")
async def post_compose(req: ComposeRequest):
    """多因子合成：权重 +（逐因子/合成）阈值 → 区间内实时回测。"""
    out = await _run(
        service.compose,
        req.weights,
        req.top_n,
        req.threshold,
        req.thresholds,
        req.cost_rate,
        req.start,
        req.end,
        req.dataset,
        _market(req.market),
    )
    if out.get("error"):
        raise HTTPException(status_code=422, detail=out["error"])
    return out


class OptimalRequest(BaseModel):
    codes: list[str] = Field(..., description="参与搜索的因子（≤10）")
    top_n: int = Field(30, ge=1, le=100)
    start: str | None = None
    end: str | None = None
    dataset: FactorDataset = "classic"
    market: str = "CN"


@router.post("/optimal-weights")
async def post_optimal(req: OptimalRequest):
    """网格搜索最优权重（非负、和为 1）：夏普 / 年化 / 超额各一组。

    「超额」目标 = 本市场主基准（响应 ``bench_primary`` 说明是谁；
    FUTURES 无基准 → 该目标恒 None）。
    """
    if not req.codes:
        raise HTTPException(status_code=422, detail="codes 不能为空")
    out = await _run(
        service.optimal_weights,
        req.codes,
        req.top_n,
        req.start,
        req.end,
        req.dataset,
        _market(req.market),
    )
    if out.get("error"):
        raise HTTPException(status_code=422, detail=out["error"])
    return out
