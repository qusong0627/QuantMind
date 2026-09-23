"""对外任务面 `/task` —— 提交长任务、轮询状态。

这一面要解决的不是「多一个转发」，而是**上游五个任务的契约互不相同**，
而外部节点只想写一份代码。

上游实际的样子（全部实测，不是推测）
------------------------------------
把五种任务摆在一起看，就知道为什么不能直接转发：

| 种类 | 提交入参 | 作业 id | 进度 | 状态字段 | 取消 |
|------|---------|--------|------|---------|------|
| 训练 | JSON body（自由 dict） | `train_{时间}_{hex8}` | int 0-100 | `status` | 有 |
| 回测 | JSON body（大） | `uuid4().hex`（另有一个 Celery `task_id`） | 只有 0 或 1 | `status` | 键是 task_id |
| 因子演化 | **只有 query，没有 body** | `uuid4().hex[:16]` | `progress_pct` int | `status` | 有 |
| TradingAgents | JSON body | `str(uuid4())[:8]` | **完全没有** | 两个 bool + error | 尽力而为 |
| 数据同步 | 只有市场路径参数 | **没有** | 没有 | 没有 | 没有 |

五种信封、四种轮询形状、三种 id 形状。直接转发等于把这份差异摊给每个调用方。

于是本面做三件归一，且**只做这三件**：

1. **一个状态词表。** `queued / running / succeeded / failed / cancelled / unknown`。
   上游同时存在 `pending`、`provisioning`、`waiting_callback`、`completed`、
   `is_running`、`is_complete` 六种说法，外部节点的分支逻辑不该长成一张对照表。
2. **一个 id 字段名**（`ref`）与一种轮询路径（`GET /task/{kind}/{ref}`）。
3. **一处能力发现**（`GET /task/kinds`）：每个种类要什么入参、能不能轮询，
   由服务端自述，不由客户端猜。

⚠️ **归一不等于抹平**：`upstream_status` 原样保留。把 `waiting_callback` 压成
`running` 之后，一个「卡在等回调」的任务与一个「正在跑」的任务在归一后的词表里
无法区分，而这两种状态的处置方式完全不同（前者要去看回调面，后者等着就行）。
归一字段用于**分支**，原始字段用于**诊断**，两个都给。

为什么是轮询，不是 SSE
----------------------
规划时 `task` 面的 `transport` 写的是「202 + task_id + SSE」。落地时改成轮询，
理由不是省事：

* 上游五个任务里**只有训练有日志流**（Redis `_training_log_stream`），回测的
  `progress` 只有 0 和 1 两个值、TradingAgents 连百分比都没有。做一条 SSE
  通道去推一个「进度永远是 0 或 100」的数字，是在为一个不存在的实时性付复杂度。
* api 服务是**单 worker 单事件循环**（见 `upstream.py` 的说明）。SSE 会把一条
  连接连同它的心跳长期挂在唯一的事件循环上；外部节点重连一次泄漏一条，
  是个没有上限的资源占用点，而它的收益是「少发几次 GET」。
* 轮询的退避是**调用方**的事，且天然抗断线：断开就是不发下一个请求，
  没有需要服务端感知的会话状态。

所以 `capabilities` 里那一行要如实改成轮询描述——它现在写的是 SSE，是假的。

提交/轮询之外的三条边界
------------------------
* **不实现取消（本版）。** 五种取消是三种不同形状，回测那种还**不可能**实现：
  它的取消键是 Celery `task_id`，而轮询键是 `backtest_id`，两者之间上游没有
  映射端点——提交响应里两个 id 都给，但状态响应里没有 `task_id`，所以拿到
  `ref` 之后无法推出该停哪个。更重要的是 TradingAgents 的取消是**尽力而为**的
  daemon 线程（stop 只返回一句话，连状态都不给）。一条「点了取消但其实还在跑」
  的接口比没有取消更糟：调用方会据此认为资源已释放。宁可如实缺席。
* **不返回结果体。** 五种结果五种形状，塞进一个端点等于把刚归一掉的信封差异
  从「状态」挪到「结果」。本轮只给 `result_available` 这个布尔，取结果走
  各自的数据面端点（下一批）。
* **不重校验训练入参。** 训练 body 是自由 dict，其校验已收敛在
  `backend/shared/training/request.py::TrainingRequest`（上游 `_normalize_payload`
  就是调它）。本面**不**再声明一份字段表：那份表和上游会漂，而漂的表现是
  「客户端发了配置，服务端静默用了默认值」——最坏的一类失败。本面只把 body
  原样转发，422 由上游逐字返回。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.services.api.routers.external.auth import (
    ExternalPrincipal,
    require_external_principal,
)
from backend.services.api.routers.external.upstream import (
    WRITE_TIMEOUT_SECONDS,
    fetch_json,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["External API · Task"])

#: 422。**不写 `status.HTTP_422_UNPROCESSABLE_ENTITY`**：本仓的 starlette 已把
#: 那个名字标为 deprecated（发 StarletteDeprecationWarning），而换成它建议的
#: `HTTP_422_UNPROCESSABLE_CONTENT` 又会在旧版本上 AttributeError。
#: 状态码是 HTTP 规范的一部分，写数字没有这个版本问题。
_HTTP_422_UNPROCESSABLE = 422

# ---------------------------------------------------------------------------
# 归一状态词表（本面存在的理由，见模块 docstring）
# ---------------------------------------------------------------------------

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_UNKNOWN = "unknown"

#: 上游状态 → 归一状态。**唯一出处。**
#:
#: 未知值一律 `unknown` 而不是猜一个：`unknown` 会让调用方去读
#: `upstream_status`（那里面是原文），而猜错会让它对一个已失败的任务继续等。
_STATUS_MAP: dict[str, str] = {
    # 排队中
    "pending": STATUS_QUEUED,
    "queued": STATUS_QUEUED,
    "created": STATUS_QUEUED,
    # 进行中。`provisioning`（拉起训练容器/远端节点）与 `waiting_callback`
    # （等回调）都归这里——它们对调用方是同一件事：还没好，别停手。
    "running": STATUS_RUNNING,
    "provisioning": STATUS_RUNNING,
    "waiting_callback": STATUS_RUNNING,
    "started": STATUS_RUNNING,
    # 终态
    "completed": STATUS_SUCCEEDED,
    "succeeded": STATUS_SUCCEEDED,
    "success": STATUS_SUCCEEDED,
    "finished": STATUS_SUCCEEDED,
    "failed": STATUS_FAILED,
    "error": STATUS_FAILED,
    "cancelled": STATUS_CANCELLED,
    "canceled": STATUS_CANCELLED,
    "stopped": STATUS_CANCELLED,
}


def normalize_status(raw: Any) -> str:
    """上游状态字符串 → 归一状态。空/未知 → `unknown`（不猜）。"""
    return _STATUS_MAP.get(str(raw or "").strip().lower(), STATUS_UNKNOWN)


# ---------------------------------------------------------------------------
# 种类
# ---------------------------------------------------------------------------

KIND_TRAINING = "training"
KIND_BACKTEST = "backtest"
KIND_ALPHA_EVOLVE = "alpha_evolve"
KIND_TRADING_AGENTS = "trading_agents"
KIND_DATA_SYNC = "data_sync"

TaskKind = Literal["training", "backtest", "alpha_evolve", "trading_agents", "data_sync"]

#: 对外市场词表。**与交易面 `SimOrderRequest.market` 同一套**——外部节点
#: 只该学一套市场写法。上游内部另有两套（同步码 `A/BC`、适配器 id
#: `a_share/crypto`），翻译在本面内部完成，不出现在契约里。
Market = Literal["CN", "HK", "US", "CRYPTO", "FUTURES"]

#: 数据同步多一个 `CUSTOM`：它不拉上游行情，而是重建训练的输入因子集
#: （`MARKETS` 里有它，且它是「更新训练数据」的一部分）。
SyncMarket = Literal["CN", "HK", "US", "CRYPTO", "FUTURES", "CUSTOM"]

#: 对外市场 → RD-Agent 适配器 id（因子演化用）。
#:
#: ⚠️ 这是**唯一一份**这个方向的映射。`shared/market_sessions.normalize_market_key`
#: 只做单向归一（`a_share` → `CN`），反方向没有单源可引。
#: `test_external_api_task.py` 拿实际的 `list_markets()` 与这张表对表——
#: 上游加一个市场而这里没跟，测试会红，而不是让调用方收到一个
#: 带着 `a_share` 字样的 400（那是它被要求不要使用的词表）。
_MARKET_TO_ADAPTER_ID: dict[str, str] = {
    "CN": "a_share",
    "HK": "hong_kong",
    "US": "us_stock",
    "CRYPTO": "crypto",
    "FUTURES": "futures",
}


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------


class TaskSubmitResponse(BaseModel):
    """提交受理。**202 语义**：任务已受理，不代表已开始。"""

    kind: str
    #: 上游作业 id。轮询用 `GET /task/{kind}/{ref}`。
    #: ⚠️ `data_sync` 为 **null**——上游没有作业 id（见 `submit_data_sync`）。
    ref: str | None = None
    status: str = Field(..., description="归一状态，提交后通常是 queued")
    upstream_status: str | None = Field(
        None, description="上游原样状态，诊断用；分支请用 status"
    )
    pollable: bool = Field(
        ...,
        description=(
            "false = 该种类没有可轮询的作业 id，ref 为 null，别再调状态端点"
            "（本部署不会给你一个假的 id）。"
        ),
    )
    note: str | None = Field(None, description="该种类的额外说明，如轮询不可用时的替代做法")


class TaskStatusResponse(BaseModel):
    """轮询结果。字段跨种类恒定；不适用的一律 null，**不用 0 或空串顶替**。"""

    kind: str
    ref: str
    status: str
    upstream_status: str | None = None
    progress_pct: int | None = Field(
        None,
        ge=0,
        le=100,
        description=(
            "0-100。**null 表示上游根本没有这个量**，不要当 0 处理。"
            "回测只会上报 0 或 100；TradingAgents 恒为 null。"
        ),
    )
    stage: str | None = Field(None, description="当前阶段标识（仅 TradingAgents 提供）")
    stages_completed: int | None = Field(
        None,
        description=(
            "已完成阶段数（仅 TradingAgents）。**没有总数**——上游的阶段表"
            "不在响应里，本面不写死一个会在加阶段后过期的常量。"
        ),
    )
    error: str | None = None
    result_available: bool = Field(
        False,
        description="true = 上游有结果可取了。本版不在此返回结果体（见模块 docstring）。",
    )


class KindInfo(BaseModel):
    kind: str
    description: str
    pollable: bool
    request: dict[str, Any] = Field(
        ...,
        description=(
            "该种类的请求体 JSON Schema。由服务端实际用于校验的模型生成，"
            "不是手写的文档副本（手写的会漂）。"
        ),
    )
    notes: list[str] = Field(default_factory=list)


class KindsResponse(BaseModel):
    kinds: list[KindInfo] = Field(..., description="本部署当前支持的任务种类")
    market_vocabulary: dict[str, str] = Field(
        ...,
        description=(
            "对外统一的市场写法 → 各上游各自的写法。调用方**只用左边**；"
            "右边是服务端内部翻译目标，列出来是为了排查时能对上号。"
        ),
    )
    status_vocabulary: list[str] = Field(
        ..., description="归一状态的全部取值。上游原文见响应里的 upstream_status"
    )


# ---------------------------------------------------------------------------
# 请求体模型（每个种类一份；校验 + 生成 /task/kinds 的 schema，两用）
# ---------------------------------------------------------------------------


class TrainingSubmit(BaseModel):
    """训练提交。

    ⚠️ **本模型只声明了少数关键字段，但 `extra` 是 allow**：body 会**原样**
    转发给上游，其完整校验（含特征名、模型类型枚举、日期窗口）由上游的
    `TrainingRequest` 负责。这里声明字段是为了给调用方一份可发现的 schema 和
    尽早的 422，不是为了当第二份契约——上游新增字段不需要改这里。
    """

    model_config = ConfigDict(extra="allow")

    model_type: str | None = Field(
        None, description="模型类型。取值与上游同源，非法值由上游 422"
    )
    context: dict[str, Any] | None = Field(
        None, description="训练上下文，如 {market, benchmark}"
    )
    features: list[str] | None = Field(None, description="特征名列表；留空用默认")
    node_id: str | None = Field(
        None, description='训练节点："local" 或远端节点 id；默认 "local"'
    )


class BacktestSubmit(BaseModel):
    """回测提交。

    故意只开上游 `QlibBacktestRequest` 的一个**子集**并 `extra="forbid"`：
    上游那个模型有 20+ 字段（含策略源码、模板、费用模型），把全部字段透出去
    等于把一个内部模型的稳定性承诺给外部。这里的子集是「跑一次标准回测」所需，
    写错字段名会 422 而不是被静默忽略。
    """

    model_config = ConfigDict(extra="forbid")

    strategy_type: str = Field("TopkDropout", description="策略类型")
    model_id: str | None = Field(None, description="要用的模型 id；不传则用默认")
    start_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    benchmark: str | None = Field(None, description="基准，如 SH000300")
    universe: str | None = Field(None, description="股票池")
    pool_id: str | None = Field(None, description="全局股票池 id")
    initial_capital: float | None = Field(None, gt=0)
    strategy_params: dict[str, Any] | None = Field(
        None, description="策略参数；形状由上游校验（此处不做二次声明）"
    )


class AlphaEvolveSubmit(BaseModel):
    """因子演化提交。

    上游这个端点**只收 query 参数、没有 body**。本面统一收 JSON body，
    在内部转成 query——这是本面替调用方吸收的又一处上游怪癖。
    """

    model_config = ConfigDict(extra="forbid")

    market: Market = Field("CN", description="市场（对外词表）")
    universe: str = Field("csi300", description="股票池，如 csi300/csi500/all_a")
    loop_n: int = Field(5, ge=1, le=20, description="演化轮数")
    direction: str = Field("", description="因子挖掘方向/假设")
    directions: list[str] = Field(default_factory=list, description="因子类别方向列表")
    direction_mode: Literal["selected", "random"] = Field(
        "selected", description="类别选择模式"
    )
    data_source: str = Field("", description="数据源；留空用默认")


class TradingAgentsSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(..., min_length=1, max_length=32, description="标的代码")
    trade_date: str | None = Field(
        None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="分析基准日；留空用最近交易日"
    )
    market: Market | None = Field(None, description="市场（对外词表）")
    llm_provider: str | None = None
    deep_think_llm: str | None = None
    quick_think_llm: str | None = None


class DataSyncSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: SyncMarket = Field(..., description="要触发同步的市场（对外词表）")


# ---------------------------------------------------------------------------
# 上游响应薄模型
# ---------------------------------------------------------------------------
#
# `extra="ignore"`：上游响应里有大字段（训练的 `logs` 最多 600 行合并文本、
# TradingAgents 的 `stage_reports` 是几份完整 LLM 报告）。它们**必须**
# 不被解析进来——api 服务是单进程，让一个轮询端点把几 MB 的报告搬进内存
# 再丢掉，是把内存和延迟花在没人要的数据上。


class _TrainingRun(BaseModel):
    """`GET /api/v1/models/training-runs/{id}`。

    ⚠️ `error` 不在顶层：上游把失败原因放在 `result.error`（见
    `admin_training_utils.get_training_run_for_owner` 的收尾段），
    顶层只有 status/progress/logs/result/isCompleted。
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    status: str = ""
    progress: int | None = None
    result: dict[str, Any] | None = None
    is_completed: bool = Field(False, alias="isCompleted")


class _BacktestStatus(BaseModel):
    """`GET /api/v1/qlib/backtest/{id}/status`。

    `progress` 是 0.0-1.0 的浮点，且上游只写 0.0 或 1.0 两个值。
    """

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    progress: float | None = None
    error_message: str | None = None


class _AlphaTask(BaseModel):
    """`GET /api/v1/alpha-agent/tasks/{id}` 的 `data`。

    ⚠️ 两个进度字段容易拿错：`progress` 是**字符串**（给人看的短语），
    `progress_pct` 才是 int 0-100。取错不会报错，只会得到一个 0。
    """

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    progress_pct: int | None = None
    phase: str | None = None
    error_message: str | None = None
    result: Any = None


class _TradingAgentsProgress(BaseModel):
    """`GET /api/v1/trading-agents/progress/{id}` 的 `data`。

    进度要靠两个布尔推：上游**没有**百分比字段。`stage_reports` 被
    `extra="ignore"` 挡在门外（几份完整报告）。
    """

    model_config = ConfigDict(extra="ignore")

    is_running: bool = False
    is_complete: bool = False
    error: str | None = None
    current_stage: str | None = None
    completed_stages: list[str] = Field(default_factory=list)


class _UpstreamEnvelope(BaseModel):
    """`alpha-agent` 与 `trading-agents` 共用的 `{code, data}` 信封。"""

    model_config = ConfigDict(extra="ignore")

    code: int = 0
    data: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# 各种类的提交
# ---------------------------------------------------------------------------


async def _submit_training(
    principal: ExternalPrincipal, body: dict[str, Any]
) -> TaskSubmitResponse:
    raw = await fetch_json(
        "api",
        "POST",
        "/api/v1/models/run-training",
        principal=principal,
        json_body=body,
        timeout=WRITE_TIMEOUT_SECONDS,
    )
    run_id = raw.get("runId") if isinstance(raw, dict) else None
    if not run_id:
        # 上游 200 但没给 id：不能编一个，否则轮询必然 404 而调用方以为提交成功。
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="upstream_contract_changed",
        )
    upstream_status = str(raw.get("status") or "")
    return TaskSubmitResponse(
        kind=KIND_TRAINING,
        ref=str(run_id),
        status=normalize_status(upstream_status),
        upstream_status=upstream_status or None,
        pollable=True,
        note=_training_note(raw),
    )


def _training_note(raw: dict[str, Any]) -> str | None:
    """把上游的特征预检结果转述给调用方。

    上游在受理时就会比对特征目录，并在 `missingFeatures` 里列出**它没找到**的
    特征名。这些特征会被静默丢弃（训练照跑，只是少了几个因子）——不告诉调用方
    的话，「我传了 20 个特征」与「实际用了 12 个」在外部看起来完全一样。
    """
    missing = raw.get("missingFeatureCount")
    if not isinstance(missing, int) or missing <= 0:
        return None
    sample = raw.get("missingFeatures") or []
    shown = ", ".join(str(x) for x in sample[:10])
    return (
        f"上游有 {missing} 个特征在特征目录中不存在，已被丢弃（训练仍会开始）：{shown}"
        + ("…" if missing > len(sample[:10]) else "")
    )


async def _submit_backtest(
    principal: ExternalPrincipal, body: dict[str, Any]
) -> TaskSubmitResponse:
    # ⚠️ `async_mode=true` 是**必须的 query 参数**，且上游默认是 False
    # （同步阻塞跑完整个回测才返回）。漏了它，这个请求会在上游线程里阻塞到
    # 回测结束，然后我们的 30s 超时先到——调用方拿到 504，而上游还在跑。
    raw = await fetch_json(
        "engine",
        "POST",
        "/api/v1/qlib/backtest",
        principal=principal,
        json_body=body,
        params={"async_mode": "true"},
        timeout=WRITE_TIMEOUT_SECONDS,
    )
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="upstream_contract_changed"
        )
    backtest_id = raw.get("backtest_id")
    if not backtest_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="upstream_contract_changed"
        )
    upstream_status = str(raw.get("status") or "")
    return TaskSubmitResponse(
        kind=KIND_BACKTEST,
        ref=str(backtest_id),
        status=normalize_status(upstream_status),
        upstream_status=upstream_status or None,
        pollable=True,
        note=(
            "回测的取消键是上游 Celery task_id，与轮询键 backtest_id 之间上游没有"
            "映射端点，因此本版不提供取消。"
        ),
    )


async def _submit_alpha_evolve(
    principal: ExternalPrincipal, body: dict[str, Any]
) -> TaskSubmitResponse:
    market = _MARKET_TO_ADAPTER_ID[str(body.get("market") or "CN")]
    raw = await fetch_json(
        "engine",
        "POST",
        "/api/v1/alpha-agent/evolve",
        principal=principal,
        # 上游只收 query。`user_id` 不传：它是已废弃的防伪参数，身份一律来自
        # 我们签的委托 JWT（上游 `assert_identity_not_spoofed` 会核对）。
        params={
            "market": market,
            "universe": body.get("universe") or "csi300",
            "loop_n": body.get("loop_n") or 5,
            "direction": body.get("direction") or "",
            "directions": body.get("directions") or [],
            "direction_mode": body.get("direction_mode") or "selected",
            "data_source": body.get("data_source") or "",
        },
        timeout=WRITE_TIMEOUT_SECONDS,
    )
    data = _unwrap_alpha(raw)
    task_id = data.get("task_id")
    if not task_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="upstream_contract_changed"
        )
    upstream_status = str(data.get("status") or "")
    return TaskSubmitResponse(
        kind=KIND_ALPHA_EVOLVE,
        ref=str(task_id),
        status=normalize_status(upstream_status),
        upstream_status=upstream_status or None,
        pollable=True,
        note=None,
    )


async def _submit_trading_agents(
    principal: ExternalPrincipal, body: dict[str, Any]
) -> TaskSubmitResponse:
    # 上游的 `market` 走它自己的适配器/市场词表；本面收到的是对外词表，
    # 归一后再交给上游（`normalize_market_key` 是唯一归一实现）。
    payload = dict(body)
    if payload.get("market"):
        payload["market"] = _normalize_market(payload["market"])
    raw = await fetch_json(
        "engine",
        "POST",
        "/api/v1/trading-agents/analyze",
        principal=principal,
        json_body=payload,
        timeout=WRITE_TIMEOUT_SECONDS,
    )
    data = _unwrap_alpha(raw)
    analysis_id = data.get("analysis_id")
    if not analysis_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="upstream_contract_changed"
        )
    return TaskSubmitResponse(
        kind=KIND_TRADING_AGENTS,
        ref=str(analysis_id),
        status=STATUS_QUEUED,
        upstream_status=None,  # 上游受理响应里没有状态字段
        pollable=True,
        note=(
            "分析在上游的守护线程里跑。取消了也无法验证，故本版不提供取消。"
        ),
    )


async def _submit_data_sync(
    principal: ExternalPrincipal, body: dict[str, Any]
) -> TaskSubmitResponse:
    """触发一次市场数据同步。

    ⚠️ **两条上游事实决定了这个端点的形状，都不是本面能改的**：

    1. **没有作业 id。** 上游 handler 里 `celery_app.send_task(...)` 的返回值
       被丢弃（没接 `AsyncResult`），所以没有可轮询的句柄。本面因此
       `ref=null`、`pollable=false`——这是**如实**，不是功能缺失。
       想知道同步好没好，看数据面的 `as_of` 有没有推进。
    2. **上游要求该市场「定时同步」已启用**，否则 400。这是**有意的产品约束**
       （见 CLAUDE.md：是否开启同步一律以用户在前端保存的配置为准，5 个市场
       默认全部 `enabled=false`）。本面**不**替调用方去写那份配置——那会让
       机器侧多出一条绕过前端开关的路径，正好破坏那条约束。未启用时上游的
       400 原样返回给调用方，让它自己去问运维。
    """
    sync_market = _to_sync_market(str(body.get("market") or ""))
    await fetch_json(
        "api",
        "POST",
        f"/api/v1/admin/data-platform/sync-schedule/{sync_market}/run",
        principal=principal,
        timeout=WRITE_TIMEOUT_SECONDS,
        # ⚠️ 上游这个 router 是 `dependencies=[Depends(require_admin)]`。
        # 只有这一处提权（理由见 upstream.py 模块 docstring「例外」）。
        admin=True,
    )
    return TaskSubmitResponse(
        kind=KIND_DATA_SYNC,
        ref=None,
        status=STATUS_QUEUED,
        upstream_status="dispatched",
        pollable=False,
        note=(
            "上游已把任务派发到队列，但没有返回作业句柄（内部丢弃了 Celery 的 "
            "AsyncResult），因此没有可轮询的 ref。请改为轮询数据面的数据集清单，"
            "看 as_of 是否推进。另外：该市场必须已在平台内启用定时同步，"
            "否则上游会 400。"
        ),
    )


_SUBMITTERS = {
    KIND_TRAINING: (_submit_training, TrainingSubmit),
    KIND_BACKTEST: (_submit_backtest, BacktestSubmit),
    KIND_ALPHA_EVOLVE: (_submit_alpha_evolve, AlphaEvolveSubmit),
    KIND_TRADING_AGENTS: (_submit_trading_agents, TradingAgentsSubmit),
    KIND_DATA_SYNC: (_submit_data_sync, DataSyncSubmit),
}


# ---------------------------------------------------------------------------
# 各种类的轮询
# ---------------------------------------------------------------------------


async def _poll_training(
    principal: ExternalPrincipal, ref: str
) -> TaskStatusResponse:
    raw = await fetch_json(
        "api",
        "GET",
        f"/api/v1/models/training-runs/{ref}",
        principal=principal,
        model=_TrainingRun,
    )
    result = raw.result or {}
    # 上游的失败原因在 result.error；status 也可能被上游在归一化失败时改写。
    error = result.get("error")
    return TaskStatusResponse(
        kind=KIND_TRAINING,
        ref=ref,
        status=normalize_status(raw.status),
        upstream_status=raw.status or None,
        progress_pct=_clamp_pct(raw.progress),
        error=str(error) if error else None,
        result_available=bool(raw.is_completed) and not error,
    )


async def _poll_backtest(
    principal: ExternalPrincipal, ref: str
) -> TaskStatusResponse:
    raw = await fetch_json(
        "engine",
        "GET",
        f"/api/v1/qlib/backtest/{ref}/status",
        principal=principal,
        model=_BacktestStatus,
    )
    normalized = normalize_status(raw.status)
    return TaskStatusResponse(
        kind=KIND_BACKTEST,
        ref=ref,
        status=normalized,
        upstream_status=raw.status or None,
        # 上游的 progress 是 0.0-1.0 且只会是这两个端点值。
        progress_pct=(
            _clamp_pct(round(raw.progress * 100)) if raw.progress is not None else None
        ),
        error=raw.error_message or None,
        result_available=normalized == STATUS_SUCCEEDED,
    )


async def _poll_alpha_evolve(
    principal: ExternalPrincipal, ref: str
) -> TaskStatusResponse:
    raw = await fetch_json(
        "engine",
        "GET",
        f"/api/v1/alpha-agent/tasks/{ref}",
        principal=principal,
        model=_UpstreamEnvelope,
    )
    data = _AlphaTask.model_validate(raw.data or {})
    normalized = normalize_status(data.status)
    return TaskStatusResponse(
        kind=KIND_ALPHA_EVOLVE,
        ref=ref,
        status=normalized,
        upstream_status=data.status or None,
        progress_pct=_clamp_pct(data.progress_pct),
        stage=data.phase or None,
        error=data.error_message or None,
        result_available=normalized == STATUS_SUCCEEDED and data.result is not None,
    )


async def _poll_trading_agents(
    principal: ExternalPrincipal, ref: str
) -> TaskStatusResponse:
    raw = await fetch_json(
        "engine",
        "GET",
        f"/api/v1/trading-agents/progress/{ref}",
        principal=principal,
        model=_UpstreamEnvelope,
    )
    data = _TradingAgentsProgress.model_validate(raw.data or {})
    error = (data.error or "").strip()
    if error:
        normalized = STATUS_FAILED
    elif data.is_complete:
        normalized = STATUS_SUCCEEDED
    elif data.is_running:
        normalized = STATUS_RUNNING
    else:
        # 既没在跑也还没完成：上游的 tracker 刚建好，线程尚未起步。
        normalized = STATUS_QUEUED
    return TaskStatusResponse(
        kind=KIND_TRADING_AGENTS,
        ref=ref,
        status=normalized,
        upstream_status=None,  # 上游没有状态字段，只有这两个布尔
        # 上游**没有**百分比。不拿 completed_stages/12 去凑一个：那个 12 会过期。
        progress_pct=None,
        stage=data.current_stage or None,
        stages_completed=len(data.completed_stages),
        error=error or None,
        result_available=normalized == STATUS_SUCCEEDED,
    )


_POLLERS = {
    KIND_TRAINING: _poll_training,
    KIND_BACKTEST: _poll_backtest,
    KIND_ALPHA_EVOLVE: _poll_alpha_evolve,
    KIND_TRADING_AGENTS: _poll_trading_agents,
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _clamp_pct(value: Any) -> int | None:
    """上游的进度转成 0-100 的 int。

    `None` 与「取不出数」都返回 None（**不返回 0**）：0 的语义是「刚开始」，
    与「上游没这个量」是两件事，混起来会让调用方以为任务在动。
    """
    if value is None:
        return None
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return None


def _unwrap_alpha(raw: Any) -> dict[str, Any]:
    """取出 `{code, data}` 信封里的 `data`。

    上游 `code` 非 200 但 HTTP 200 的情况存在（有些分支这么写）。这种时候
    把 `data` 当成功体去取 id 会得到一个 KeyError 式的 502，掩盖真正的错误；
    所以先看 code。
    """
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
        data = raw["data"]
        code = raw.get("code")
        if code is not None and int(code) != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="upstream_error"
            )
        return data
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY, detail="upstream_contract_changed"
    )


def _normalize_market(raw: str) -> str:
    """对外市场词表内的值 → 归一市场键。

    对外词表本身就是规范化之后的值，所以这里几乎总是恒等；留着是因为
    上游要的是归一键，而**归一实现只该有一份**（`shared/market_sessions`）。
    """
    from backend.shared.market_sessions import normalize_market_key

    return normalize_market_key(raw)


def _to_sync_market(market: str) -> str:
    """对外市场词表 → 上游的**同步码**（`CN`→`A`、`CRYPTO`→`BC`）。

    ⚠️ 不是恒等映射，也不是大小写问题：同步键按这套码写，用 `CN` 去拼会
    永远查不到（这坑上游注释里记着，是体检 C08 假报「无同步记录」的成因）。
    映射实现引上游那一份（`sync_market_token`），不在这里手抄一遍。

    惰性 import：那是 engine 服务的模块，api 进程只在真的触发同步时才碰它。
    """
    from backend.services.engine.tasks.market_sync_scheduler import sync_market_token

    return sync_market_token(market)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.get("/kinds", response_model=KindsResponse)
async def list_kinds(
    principal: ExternalPrincipal = Depends(require_external_principal),
) -> KindsResponse:
    """本部署支持哪些任务种类、各自的入参 schema。

    **先问再做**：上游五个端点的入参形状互不相同，让调用方靠文档猜，猜错的
    代价是「提交成功但跑的不是我想的配置」。schema 由服务端校验用的模型直接
    生成，所以不会与实现漂开。
    """
    return KindsResponse(
        kinds=[
            KindInfo(
                kind=KIND_TRAINING,
                description="模型训练（提交后由训练节点执行）",
                pollable=True,
                request=TrainingSubmit.model_json_schema(),
                notes=[
                    "请求体会原样转发给上游，其完整校验由上游负责；"
                    "本 schema 只列了常用字段，其余字段同样会被转发。",
                    "受理响应可能在 note 里报告被丢弃的特征名——"
                    "那些特征不在特征目录中，会被静默跳过。",
                ],
            ),
            KindInfo(
                kind=KIND_BACKTEST,
                description="Qlib 回测（异步）",
                pollable=True,
                request=BacktestSubmit.model_json_schema(),
                notes=[
                    "只开了一个字段子集；写了子集之外的字段会 422（有意，避免静默忽略）。",
                    "进度只会上报 0 或 100（上游如此）。",
                ],
            ),
            KindInfo(
                kind=KIND_ALPHA_EVOLVE,
                description="RD-Agent 因子演化",
                pollable=True,
                request=AlphaEvolveSubmit.model_json_schema(),
                notes=[
                    "上游同时限流：每用户 >2 个并发或全局 >4 个会 429。",
                    "未配置 LLM 密钥时上游返回 412。",
                ],
            ),
            KindInfo(
                kind=KIND_TRADING_AGENTS,
                description="多 Agent 投研分析（7 分析师 + 辩论 + 风控）",
                pollable=True,
                request=TradingAgentsSubmit.model_json_schema(),
                notes=[
                    "上游不提供百分比进度，progress_pct 恒为 null；"
                    "用 stage + stages_completed 判断进展。",
                ],
            ),
            KindInfo(
                kind=KIND_DATA_SYNC,
                description="触发一次市场数据同步",
                pollable=False,
                request=DataSyncSubmit.model_json_schema(),
                notes=[
                    "**不可轮询**：上游不返回作业句柄（丢弃了 Celery 的 AsyncResult），"
                    "受理响应里 ref 为 null。",
                    "替代做法：轮询数据面的数据集清单，看 as_of 是否推进。",
                    "该市场必须已在平台内启用定时同步，否则上游 400——"
                    "这是有意的产品约束，本面不代写配置。",
                ],
            ),
        ],
        market_vocabulary={
            "CN": "同步码 A / 适配器 a_share",
            "HK": "同步码 HK / 适配器 hong_kong",
            "US": "同步码 US / 适配器 us_stock",
            "CRYPTO": "同步码 BC / 适配器 crypto",
            "FUTURES": "同步码 FUTURES / 适配器 futures",
            "CUSTOM": "仅数据同步可用：同步码 CUSTOM（重建训练用因子集）",
        },
        status_vocabulary=[
            STATUS_QUEUED,
            STATUS_RUNNING,
            STATUS_SUCCEEDED,
            STATUS_FAILED,
            STATUS_CANCELLED,
            STATUS_UNKNOWN,
        ],
    )


@router.post(
    "/{kind}",
    response_model=TaskSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_task(
    kind: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    principal: ExternalPrincipal = Depends(require_external_principal),
) -> TaskSubmitResponse:
    """提交一个任务。受理即返回（202），不代表已开始——轮询看状态。

    `kind` 不在支持列表时 400（不是 404）：路由是存在的，是这个种类不存在。
    404 会让调用方以为接口路径写错了。
    """
    entry = _SUBMITTERS.get(kind)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown_task_kind: {kind}",
        )
    submitter, model_cls = entry

    # 校验用模型只做「入参形状」的检查；上游仍会做它自己的完整校验。
    # 这里失败给 422（与 FastAPI 自带校验错误同码），detail 用 pydantic 的
    # 错误列表——但**只保留字段路径与消息**，避免把输入值原样回显
    # （训练 body 里可能有用户自填的敏感串）。
    try:
        model_cls.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=_HTTP_422_UNPROCESSABLE,
            detail=[
                {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
                for e in exc.errors()[:20]
            ],
        ) from exc

    return await submitter(principal, payload)


@router.get("/{kind}/{ref}", response_model=TaskStatusResponse)
async def get_task_status(
    kind: str,
    ref: str,
    principal: ExternalPrincipal = Depends(require_external_principal),
) -> TaskStatusResponse:
    """轮询任务状态。`ref` 是提交响应里的 `ref`。"""
    poller = _POLLERS.get(kind)
    if poller is None:
        # 区分「没这个种类」与「这个种类不能轮询」——后者是调用方最容易踩的
        # 一个坑（data_sync 就是），错误信息里要直接说清替代做法。
        if kind in _SUBMITTERS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"kind_not_pollable: {kind}",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown_task_kind: {kind}",
        )
    return await poller(principal, ref)


__all__ = [
    "KIND_ALPHA_EVOLVE",
    "KIND_BACKTEST",
    "KIND_DATA_SYNC",
    "KIND_TRADING_AGENTS",
    "KIND_TRAINING",
    "STATUS_CANCELLED",
    "STATUS_FAILED",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "STATUS_SUCCEEDED",
    "STATUS_UNKNOWN",
    "normalize_status",
    "router",
]
