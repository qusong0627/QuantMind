"""决策名册配置面（P2.9）：现状 / 保存 / 清除——require_admin 收口。

- ``GET    /api/v1/decision/roster`` —— 名册现状：逐家模型名/端点/key 配没配/解析结果，
  外加决策轮开关（**只读**）与逐家状态镜像
- ``PUT    /api/v1/decision/roster`` —— 保存名册：校验 → 先写 key 变量 → 再写名册 →
  回读复验，复验失败自动回滚到上一版
- ``DELETE /api/v1/decision/roster`` —— 清空名册，回到单家三件套；顺手清掉本模块生成的
  key 变量（不含全局三件套）

**端点为什么挂在 trade 服务（而不是 api）**：``resolve_roster`` 读的是本进程的
``os.environ``，而 ``set_secret`` 写文件的同时也写本进程环境 ⇒ 只有 trade 进程自己写
才能**免重启**在下一轮 tick 生效。api 进程写只会落盘，trade 得等重启才读到——那就是
「点了保存、界面显示已生效、实际还跑旧的」。前端走网关 8000 的 ``/api/v1/decision/*``
（api 侧 ``trade_proxy`` 已登记转发）。

**本路由不写决策轮开关**：那个开关 worker 只在启动时判一次，代写就是撒谎（详见
``services/decision_roster_config.py`` 模块 docstring）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from backend.services.trade.services import decision_roster_config as roster
from backend.services.trade_shared.deps import AuthContext, require_admin

logger = logging.getLogger(__name__)
router = APIRouter()

_ACCEPTED = {
    "model": "模型名（同时是这个 agent 的身份：进分账账本段/幂等键/审计行）",
    "base_url": "OpenAI 兼容端点（字面值；也可改传 base_url_env 走变量名）",
    "api_key": "只写不回：留空=沿用原来那把；clear_api_key=true 才解除引用",
    "timeout": "单次请求超时秒数（不传=120）",
    "max_tokens": "单次生成上限（不传=4000）",
    "temperature": "采样温度（不传=0.3）",
}


@router.get("/decision/roster")
async def get_roster(
    _auth: AuthContext = Depends(require_admin),
) -> dict[str, Any]:
    """名册现状（**永不 500**：名册坏掉的时候正是最需要看这一页的时候）。"""
    data = roster.describe()
    return {"success": True, "data": data, "accepted": _ACCEPTED}


@router.put("/decision/roster")
async def put_roster(
    body: dict[str, Any] = Body(...),
    _auth: AuthContext = Depends(require_admin),
) -> Any:
    """保存名册。校验不过 → 400 + 逐条人话原因（**一条都不落盘**）。

    成功回 ``data`` = 与 GET **同一个**现状对象（一个资源一种形状），本次落盘细节
    放同级 ``applied``（写了/删了哪些 key 变量）——别把现状塞进 ``data.state``，
    两个消费方各记一种读法迟早读错一边。
    """
    result = roster.apply_roster(body)
    if not result.get("ok"):
        errors = list(result.get("errors") or ["名册未通过校验"])
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": errors[0], "errors": errors},
        )
    logger.info(
        "[decision-roster] 名册已更新：%s 家（新增 key 变量 %d 个，清理 %d 个）",
        len(result.get("agents") or []),
        len(result.get("written_key_vars") or []),
        len(result.get("deleted_key_vars") or []),
    )
    return {"success": True, "data": roster.describe(), "applied": result}


@router.delete("/decision/roster")
async def delete_roster(
    _auth: AuthContext = Depends(require_admin),
) -> Any:
    """清空名册（回单家三件套）。清完连单家都不可用时自动回滚，不把能跑的搞停。

    响应形状与 PUT 一致：``data`` = 现状，``applied`` = 本次删除细节。
    """
    result = roster.clear_roster()
    if not result.get("ok"):
        errors = list(result.get("errors") or ["未能清空名册"])
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": errors[0], "errors": errors},
        )
    logger.info(
        "[decision-roster] 名册已清空（删除 key 变量 %d 个）",
        len(result.get("deleted_key_vars") or []),
    )
    return {"success": True, "data": roster.describe(), "applied": result}
