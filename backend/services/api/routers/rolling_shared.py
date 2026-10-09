"""重训调度配置的共享校验/保存实现（内部端点与用户态端点同口径）。

背景：调度配置原先只有内部端点（``X-Internal-Call-Secret``）可写；模型管理页
「滚动训练」需要给登录用户开放同一配置面，但**校验纪律必须逐字一致**——两份
实现漂移会制造「内部拒了、用户态放行」的静默陷阱。故 PUT 语义收敛到本模块，
``internal_rolling`` 与 ``model_rolling`` 两个路由只是不同鉴权门 + 同一函数。

注意：``retrain_scheduler`` / ``recipe_registry`` 必须**调用时 import**——
测试与运维脚本 monkeypatch 的是模块属性，模块导入期绑定符号会绕过补丁。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field


class ScheduleUpdateRequest(BaseModel):
    """单市场重训调度配置（键面与 retrain_scheduler.DEFAULT_SCHEDULE 对齐）。

    ``day_rule`` 不在入口做白名单——未知规则由调度器的 ``judge_due`` 拒绝并
    每日告警：坏配置要**可见**，不能被保存口静默改写成「每月首交易日」。

    ``window_policy`` / ``purge_days`` 只接受 ``None``（缺省=沿用配方策略；
    只为 GET→PUT 往返保真而留在键面）。窗口策略归**配方**所有——派发链路
    （rolling_dispatch / rolling_train.py）一律读 ``recipe.window_policy``，
    调度配置里的覆写没有任何消费者；非 None 一律 400 拒绝，宁可让配置人
    当场看到错误，也不存一份「看起来生效实则被忽略」的假配置。

    ``executor="remote"`` 尚未接线：派发 payload 不携带 ``node_id``，admin 侧
    编排器永远落回 ``"local"``，唯一实际效果是跳过调度器的内存守卫——PUT 一律
    400 拒绝；接线（节点路由）前只认 ``local``。

    字段全部带界：配置整份落进共享 Redis（noeviction）并被 GET/告警原文回显，
    无界字符串=任一登录用户可向平台级键写入任意大值（多写几次打爆 Redis）。
    ``time`` 用 24h ``HH:MM`` 严校验——坏时间此前会被调度器静默归一成默认值，
    保存成功但改了个寂寞，不如 422 当场可见。
    """

    enabled: bool = False
    day_rule: str = Field(default="first_trading_day", max_length=64)
    time: str = Field(default="15:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    recipe_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]{1,128}$"
    )
    observation_days: int = Field(default=20, ge=1, le=250)
    max_time_minutes: int = Field(default=240, ge=10, le=1440)
    executor: Literal["local", "remote"] = "local"
    window_policy: dict[str, Any] | None = None
    purge_days: int | None = None


def apply_schedule_update(market: str, req: ScheduleUpdateRequest) -> dict[str, Any]:
    """校验并保存单市场调度配置；返回 ``{"market", "schedule"}``。

    校验顺序（GIGO 拒绝先于持久化）：
    1. 窗口策略覆写 → 400（归配方所有，见 ScheduleUpdateRequest docstring）
    2. remote 执行器 → 400（未接线）
    3. 市场无有效配方 → 404（存了只会每天刷 recipe_invalid 告警）
    4. 配方不存在 → 404；配方市场错配 → 400
    """
    from backend.services.engine.tasks import retrain_scheduler as rts
    from backend.shared.training import recipe_registry as rr

    if req.window_policy is not None or req.purge_days is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "窗口策略（window_policy/purge_days）归配方所有，调度配置不支持覆写；"
                "请修改配方后重新保存"
            ),
        )
    if req.executor == "remote":
        raise HTTPException(
            status_code=400,
            detail="executor=remote 尚未接线（训练节点路由未打通），暂仅支持 local",
        )

    market_code = str(market or "").strip().upper()
    if market_code not in rts.recipe_markets():
        raise HTTPException(
            status_code=404, detail=f"市场无有效重训配方: {market_code or market}"
        )
    try:
        recipe = rr.load_recipe(req.recipe_id)
    except rr.RecipeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if str(recipe.market or "").upper() != market_code:
        raise HTTPException(
            status_code=400,
            detail=f"配方 {req.recipe_id} 属 {recipe.market}，不能配置给 {market_code}",
        )
    saved = rts.save_schedule(market_code, req.model_dump())
    return {"market": market_code, "schedule": saved}
