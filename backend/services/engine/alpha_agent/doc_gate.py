"""文档挖掘闸门（`ENABLE_DOC_MINING`，T-FM-13）—— 默认关闭。

为什么默认关
------------
文档解析链会把用户上传的原件**送出本机**（MinerU 云端 API），是敏感出网
行为；且 MinerU 页数配额是账号级共享的。功能未验收前，闸门必须默认关。

拦法：挂在 docs router 的 **router 级 dependencies** 上——新加的路由默认
落在拒绝侧（与实盘中间件同一个失败方向）；`/evolve` 带 `doc_id` 的分支
另调 :func:`require_doc_mining`（它在另一个 router 上，够不到本闸门）。

403 而非 404：403 说得清「这东西存在但本部署没开」；404 会让运维去查
路由注册，方向反了（与 `live_trading_gate` 同款判据）。

detail 用机器可读字面量 ``doc_mining_disabled``：前端与探针据此断言，改字面量
必须同步改测试。读法走 `shared/env_flags`（调用时读 env，不 import 冻结）。
"""

from __future__ import annotations

from fastapi import HTTPException

from backend.shared.env_flags import env_flag

ENV_KEY = "ENABLE_DOC_MINING"

#: 闸门拒绝时返回的机器可读原因（前端/探针据此断言，不要改字面量）
DISABLED_DETAIL = "doc_mining_disabled"


def is_doc_mining_enabled() -> bool:
    """文档挖掘是否启用。**调用时读 env**——测试要能 monkeypatch，
    运维改 env 重启即生效，没有中间态。"""
    return env_flag(ENV_KEY)


def require_doc_mining() -> None:
    """闸门关闭时抛 403（router dependency 或 handler 内显式调用）。"""
    if not is_doc_mining_enabled():
        raise HTTPException(status_code=403, detail=DISABLED_DETAIL)
