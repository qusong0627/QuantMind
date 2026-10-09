"""文档解析链凭据解析 —— 用户自带 MinerU Token 优先、env 兜底（唯一口径）。

与 LLM Key 的用户级模式一致：Token 在个人中心保存进 Profile（明文存储，
仅配置页做掩码回显）。这里是解析链**唯一**的有效 token 判定：
上传端点（未配就 503 拦下）、解析服务（按行记录的来源重建客户端）、
配额/统计端点（token_configured 提示用户去哪配）共用，避免出现第二个
「悄悄不一样」的判断。

``strict`` 透传给 profile_gateway.fetch_profile_raw：
- False（展示面/上传预检）：Profile 读不到 → 回退 env 兜底；
- True（后台轮询重建客户端）：Profile 读不到 → 抛 ``ProfileGatewayError``
  （可重试），与「用户确实清掉了 Token」（200 但没有）区分开。
"""

from __future__ import annotations

import os

from backend.services.engine.alpha_agent.llm_client import _is_placeholder
from backend.services.engine.alpha_agent.profile_gateway import fetch_profile_raw

ENV_MINERU_TOKEN = "MINERU_API_TOKEN"

TOKEN_SRC_USER = "user"
TOKEN_SRC_ENV = "env"
TOKEN_SRC_NONE = "none"


async def resolve_effective_mineru_token(
    user_id: str | None,
    tenant_id: str | None,
    *,
    strict: bool = False,
) -> tuple[str | None, str]:
    """→ ``(token, src)``，src ∈ ``{"user", "env", "none"}``。

    没有 user_id/tenant_id（老行/内部调用）就不读 Profile，直接走 env——
    读不成 Profile 是因为缺少必要的请求头参数，再试多少次都一样。
    """
    uid = str(user_id or "")
    tid = str(tenant_id or "")
    if uid and tid:
        profile = await fetch_profile_raw(uid, tid, strict=strict)
        token = str((profile or {}).get("mineru_api_token") or "").strip()
        if token and not _is_placeholder(token):
            return token, TOKEN_SRC_USER
    env_token = (os.getenv(ENV_MINERU_TOKEN) or "").strip()
    if env_token and not _is_placeholder(env_token):
        return env_token, TOKEN_SRC_ENV
    return None, TOKEN_SRC_NONE
