"""对外控制面 —— **只读**。

存在理由：任务面（`task.py`）的报文里要填 `strategy_id` / `model_id`，
而下发任务的节点在第一次跑之前**不知道这些 id**。没有控制面，它只能靠人把 id
抄进配置文件——那正是机器接口要消灭的东西。所以这个面的定位是「**先问再做**」
的后半句：能力查询告诉你有哪些面，控制面告诉你面里有哪些东西。

## 为什么这一批不写

写（建策略、改模型、删）需要先回答「谁有权改」——而 `api_keys.permissions`
现在只发了 `trade.read` / `trade.write` 两个码，且**没有编辑权限的界面**
（见 `permissions.py` 的说明）。给控制面开写口子等于让一枚设计上只给交易用的
凭据能改策略，那是一条比多写几个 GET 严重得多的边界。写口子等权限模型补齐再说。

## 为什么没有 `/control/markets`

曾经想加一个「本部署启用了哪些市场」。不加，因为那是**第二份事实源**：
数据面已经逐数据集给出 `available` / `as_of` / `freshness`
（`GET /api/ext/v1/data/datasets`），那才是「你到底有哪些数据、新不新」的
权威答案。再挂一个「市场开关」端点，两者会在某次配置变更后开始互相矛盾，
而调用方无从判断信谁。要判断某个市场能不能用，去读它的数据集。

## 与上游的关系

这里**不经过** engine/trade —— 策略与模型都在 api 进程能直连的 PG 里，
绕一圈子只是多一次失败点。

* 策略走 `strategy_storage`（**唯一入口**，本仓的硬规矩）。它是**同步阻塞**的
  （内部用同步 session），而 api 服务是单 worker 单事件循环，所以必须
  `run_in_threadpool`。直接在 async 函数里调会卡住整个服务的所有请求。
* 模型直接只读查 `qm_user_models`。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_serializer
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from backend.services.api.routers.external.auth import ExternalPrincipal, require_external_principal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["External API · Control"])

#: 单次列表返回的条数上限。与数据面的分页口径一致（`le=` 硬上限），
#: 免得一个 `limit=999999` 把 97 个模型的 `metadata_json` 全捞进内存。
_MAX_LIMIT = 500


def _dt(value: datetime | str | None) -> str | None:
    """两个来源、两种处理，**刻意不统一**：

    * PG 直读（模型列表）拿到的是 `datetime` → 走 `to_utc_iso`，带 `Z`。
    * `strategy_storage.list()` 已经把它 `.isoformat()` 成字符串了 → **原样透传**。

    第二条不是偷懒：那份字符串是与人工 API（`/api/v1/strategies`）共用的线上格式，
    在这里改写会让同一个字段在两套接口上长得不一样，而调用方很可能同时接了两套。
    要统一得去改 `strategy_storage`——那是它的事，不是对外面顺手改的。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    from backend.shared.utc_datetime import to_utc_iso

    return to_utc_iso(value)


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------


class StrategySummary(BaseModel):
    """策略摘要。**不含源码**：列表接口给的是「有哪些策略」，不是「策略长什么样」。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., description="策略 id（用户策略为数字串，系统模板为 `sys_` 前缀）")
    name: str
    description: str | None = None
    status: str | None = Field(None, description="生命周期状态（ACTIVE/…）")
    market: str = Field(
        "CN",
        description=(
            "市场。取自 `parameters.market`；**历史行没有这个键，一律视为 CN**"
            "（本仓既有约定，不是猜）——所以这个字段永远有值，不会为 null。"
        ),
    )
    tags: list[str] = Field(default_factory=list)
    is_verified: bool = False
    is_system: bool = Field(
        False, description="true = 平台内置模板（`sys_` 前缀），不是该用户的策略"
    )
    created_at: str | None = None
    updated_at: str | None = None

    @field_serializer("created_at", "updated_at", when_used="json")
    def _d(self, value: str | None) -> str | None:
        return value


class StrategiesResponse(BaseModel):
    strategies: list[StrategySummary]
    count: int


@router.get("/strategies", response_model=StrategiesResponse)
async def list_strategies(
    market: str | None = Query(None, description="按市场过滤（CN/HK/US/…）"),
    search: str | None = Query(None, max_length=100, description="按名称/描述模糊匹配"),
    include_templates: bool = Query(
        False, description="是否并入平台内置模板（id 带 `sys_` 前缀）"
    ),
    limit: int = Query(100, ge=1, le=_MAX_LIMIT),
    principal: ExternalPrincipal = Depends(require_external_principal),
) -> StrategiesResponse:
    """当前凭据所属用户的策略列表。

    **没有 `offset`**：策略数量是几十量级（不是行情那种百万行），且
    `strategy_storage.list()` 本身不分页。给一个假的 offset 参数比不给更糟
    ——调用方会以为翻页有效。真到了要翻页的量级再加。
    """
    from backend.shared.strategy_storage import get_strategy_storage_service

    store = get_strategy_storage_service()

    def _load() -> list[dict[str, Any]]:
        # 同步阻塞调用，必须在线程池里跑（见模块 docstring）。
        return store.list(
            principal.user_id,
            search=search,
            market=market,
            include_templates=include_templates,
        )

    try:
        rows = await run_in_threadpool(_load)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        # 失败**不**吞成空列表：`strategies: []` 在调用方眼里是「你没有策略」，
        # 与「查询炸了」是两件事，而后者会让它去建一个重名的。
        logger.error("[ExtControl] 策略列表查询失败：%s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="strategy_source_unavailable"
        ) from exc

    items = [_to_summary(row) for row in rows[:limit]]
    return StrategiesResponse(strategies=items, count=len(items))


def _to_summary(row: dict[str, Any]) -> StrategySummary:
    """把 `strategy_storage.list()` 的行收成一个摘要。

    `parameters.market` 的取值口径与 `strategy_storage.list(market=...)` 的过滤
    逐字对齐：`A` / `A_SHARE` / 空 / 缺键都归 `CN`。两处口径若分叉，会出现
    「按 CN 过滤能查到、返回的 market 却写着 A」这种自相矛盾的结果。
    """
    params = row.get("parameters") or {}
    raw_market = str((params or {}).get("market") or "").upper()
    market = "CN" if raw_market in ("", "A", "A_SHARE", "CN") else raw_market

    return StrategySummary(
        id=str(row.get("id") or ""),
        name=str(row.get("name") or ""),
        description=row.get("description"),
        status=row.get("status"),
        market=market,
        tags=[str(t) for t in (row.get("tags") or [])],
        is_verified=bool(row.get("is_verified")),
        is_system=bool(row.get("is_system")),
        created_at=_dt(row.get("created_at")),
        updated_at=_dt(row.get("updated_at")),
    )


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


class ModelSummary(BaseModel):
    """已训练模型摘要。

    指标按**数据划分**原样给出（`train` / `val` / `test` 三段），**不合并、
    不提炼单一 headline**。本仓在这件事上有实测结论：合并窗口的 headline 会
    显著高于样本外（同一份产物曾算出 1.47× 的虚高），把那个数当作「这个模型的
    水平」是会误导决策的。所以这里只给分段，让调用方自己按样本外那段判断。
    """

    model_config = ConfigDict(extra="ignore")

    model_id: str
    display_name: str | None = None
    status: str = Field(..., description="ready = 可推理；candidate/archived = 不可用")
    is_default: bool = False
    model_type: str | None = Field(
        None,
        description=(
            "模型族（NativeTFT / LightGBM / …）。⚠️ **跨模型族不可直接比 IC/ICIR**"
            "（波动是族常数），要比就比同一族内的。"
        ),
    )
    market: str | None = None
    data_source: str | None = Field(
        None,
        description="训练取数来源（quantdb_factors / model_features_parquet / …）",
    )
    framework: str | None = None
    feature_count: int | None = None
    metrics: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description="按划分段（train/val/test）的指标，如 {'val': {'auc': 0.56}}",
    )
    created_at: str | None = None
    updated_at: str | None = None

    @field_serializer("created_at", "updated_at", when_used="json")
    def _d(self, value: str | None) -> str | None:
        return value


class ModelsResponse(BaseModel):
    models: list[ModelSummary]
    count: int


@router.get("/models", response_model=ModelsResponse)
async def list_models(
    status_filter: str | None = Query(
        None, alias="status", description="ready/candidate/archived；缺省返回全部"
    ),
    limit: int = Query(200, ge=1, le=_MAX_LIMIT),
    principal: ExternalPrincipal = Depends(require_external_principal),
) -> ModelsResponse:
    """当前凭据所属用户的模型列表。

    `qm_user_models.user_id` 与 `api_keys.user_id` 是**同一个空间**（业务 id，
    实测都是 `'10000001'`），所以这里 `user_id = :uid` 直接可用，不需要像
    `strategies` 那样过一道主键映射。

    ⚠️ 别把这一条推广到 `strategies.user_id`：那张表存的是 `users.id` 主键，
    裸等号会把结果查空且不报错（本仓 2026-09-16 的真实事故，见
    `strategy_userid-users-pk-space`）。两张表两个空间，不能互相参照。
    """
    from backend.shared.database_manager_v2 import get_session

    sql = (
        "SELECT model_id, status, is_default, metadata_json, metrics_json, "
        "created_at, updated_at FROM qm_user_models "
        "WHERE tenant_id = :tid AND user_id = :uid "
    )
    params: dict[str, Any] = {"tid": principal.tenant_id, "uid": principal.user_id}
    if status_filter:
        sql += "AND status = :st "
        params["st"] = status_filter
    sql += "ORDER BY updated_at DESC LIMIT :lim"
    params["lim"] = limit

    try:
        async with get_session(read_only=True) as session:
            rows = (await session.execute(text(sql), params)).mappings().all()
    except Exception as exc:  # noqa: BLE001
        logger.error("[ExtControl] 模型列表查询失败：%s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="model_source_unavailable"
        ) from exc

    models = [_to_model_summary(dict(row)) for row in rows]
    return ModelsResponse(models=models, count=len(models))


def _to_model_summary(row: dict[str, Any]) -> ModelSummary:
    """抽取摘要。

    单个模型的 `metadata_json`/`metrics_json` 里可能是 JSON 字符串、可能是
    `None`、可能混着非数值指标——**任何一条坏数据都不该让整个列表 502**。
    所以这里逐字段容错：坏的那个字段降级成 null，其余照常返回。
    （这与账户端点相反：那边整个响应就是那个对象，形状错必须硬失败。）
    """
    meta = _as_dict(row.get("metadata_json"))
    metrics_raw = _as_dict(row.get("metrics_json"))

    metrics: dict[str, dict[str, float]] = {}
    for split, values in metrics_raw.items():
        if not isinstance(values, dict):
            continue
        clean = {
            str(k): float(v)
            for k, v in values.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        if clean:
            metrics[str(split)] = clean

    feature_count = meta.get("feature_count")
    return ModelSummary(
        model_id=str(row.get("model_id") or ""),
        display_name=_opt_str(meta.get("display_name")),
        status=str(row.get("status") or "unknown"),
        is_default=bool(row.get("is_default")),
        # model_class_name 只覆盖部分历史模型（实测 97 个里 36 个），
        # 所以它是**兜底**而不是主口径。
        model_type=_opt_str(meta.get("model_type") or meta.get("model_class_name")),
        market=_opt_str(meta.get("market")),
        data_source=_opt_str(meta.get("data_source")),
        framework=_opt_str(meta.get("framework")),
        feature_count=int(feature_count) if isinstance(feature_count, (int, float)) else None,
        metrics=metrics,
        created_at=_dt(row.get("created_at")),
        updated_at=_dt(row.get("updated_at")),
    )


def _as_dict(value: Any) -> dict[str, Any]:
    """jsonb 列可能是 dict（asyncpg 已解），也可能是 str（旧库/文本列）。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _opt_str(value: Any) -> str | None:
    """空串归 None：本仓契约里 null = 缺失，空串会让人以为「有这个字段但是空的」。"""
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


__all__ = [
    "router",
    "ModelSummary",
    "ModelsResponse",
    "StrategiesResponse",
    "StrategySummary",
]
