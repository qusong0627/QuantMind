"""用户 Profile 读取（经内部网关）—— 引擎侧唯一入口。

拆出本模块的原因：文档解析链的后台轮询（doc_parse_service，非路由代码）
要读用户级 MinerU Token，而 routers/alpha_agent.py 里的 ``_fetch_profile_raw``
是路由层私有名。两边共用这一份实现，避免出现第二个「悄悄不一样」的读取口径。

``strict`` 语义（「读不到」与「确实没配」必须区分开）：

- ``False``（对话/embedding 等展示与预检面）：网关不可达 → 记日志返回 None。
  配置读取失败不该打断主链——调用方回退 env 兜底；
- ``True``（文档解析后台轮询重建客户端）：网关 5xx/网络错误 → 抛
  ``ProfileGatewayError``，调用方把「读不到」当可重试；只有 200 且确实没有
  Token 才算「用户清掉了」（不可重试的定格失败）。
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

PROFILE_FETCH_TIMEOUT_S = 5.0


class ProfileGatewayError(RuntimeError):
    """Profile 网关不可达/异常响应（strict 模式）。调用方按可重试处理。"""


def profile_gateway_url() -> str:
    return os.getenv("INTERNAL_API_GATEWAY_URL") or "http://127.0.0.1:8000"


async def fetch_profile_raw(
    user_id: str, tenant_id: str, *, strict: bool = False
) -> dict | None:
    """直接取用户 Profile 原始字段（不做「有没有 chat key」的判断）。

    embedding/MinerU 状态必须走这条：``_fetch_profile_llm_config`` 在 chat key
    缺失时返回 None，而各通道是独立的——没配 chat 的账号照样要能看见自己的
    其他配置。
    """
    from backend.shared.auth import get_internal_call_secret

    try:
        async with httpx.AsyncClient(timeout=PROFILE_FETCH_TIMEOUT_S) as client:
            resp = await client.get(
                f"{profile_gateway_url()}/api/v1/profiles/{user_id}",
                headers={
                    "X-Internal-Call": get_internal_call_secret(),
                    "X-User-Id": user_id,
                    "X-Tenant-Id": tenant_id,
                },
            )
        if resp.status_code != 200:
            logger.warning(
                "[alpha-agent] fetch profile %s: http %s", user_id, resp.status_code
            )
            # 404/403 = 这个账号确实没有/读不到这条 Profile（非瞬态）；
            # 5xx = 网关自己病了（瞬态，strict 下交给调用方重试）。
            if strict and resp.status_code >= 500:
                raise ProfileGatewayError(f"profile gateway http {resp.status_code}")
            return None
        return resp.json().get("data", {}) or {}
    except ProfileGatewayError:
        raise
    except Exception as exc:
        logger.exception("[alpha-agent] fetch profile %s failed", user_id)
        if strict:
            raise ProfileGatewayError(
                f"profile gateway 读取失败（{type(exc).__name__}）"
            ) from exc
        return None
