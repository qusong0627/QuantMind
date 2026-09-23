"""转发头过滤的唯一登记处（C1 加固续，2026-09-23）。

为什么要有这个模块
------------------
C1 事故（2026-09-17）收尾时的教训原文是：**「密钥校验点清单要按『类』枚举，
不能只查显式 import 了 helper 的地方」**。信任头剥离是同一类问题，而且当时
只处理了 `trade_proxy` 一处口径：

    客户端随便发一枚 ``X-Internal-Call: <密钥>`` + ``X-User-Id: <任意>``
    → 经代理透传到下游 → 下游 `get_current_user` 信任它 → 冒充任意用户。

C1 之后 `engine_proxy` / `trade_proxy` / `ai_ide_proxy` 各自抄了一份剥离清单，
`agent_arena_proxy` / `data_gateway_proxy` / `hub_proxy` / `qwenpaw_proxy` / `news`
**一份都没有**——同一条规则散在 8 个文件里手抄，新增信任头要记得改 8 处，
忘一处就是一个洞。本模块把清单收敛成单一事实源。

本模块管什么、不管什么
----------------------
管：**客户端 → 上游**方向的头过滤（把不可信输入挡在信任边界外）。
不管：**服务 → 服务**方向的头构造。内部调用方要带信任头时，应当用
`shared/auth.get_internal_call_secret()` 主动**重建**，而不是从客户端请求里转发。

用法
----
    from backend.shared.trusted_headers import sanitize_forward_headers

    headers = sanitize_forward_headers(request.headers.items())

需要额外丢的（如 agent-arena 要丢掉本服务的 Bearer，因为上游认 X-API-Token）：

    headers = sanitize_forward_headers(request.headers.items(), extra_drop={"authorization"})
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

#: 内部信任头：**客户端一律不得携带**，代理必须剥离。
#:
#: 这些头在下游服务里是「身份声明」——`get_current_user` 只在
#: `X-Internal-Call` 密钥匹配时才采信 `X-User-Id`。但密钥匹配是下游的事，
#: 网关不能把客户端自带的头原样递过去赌下游会校验。
#:
#: ⚠️ 新增信任头时**只改这里**（`backend/tests/test_trusted_headers.py` 会
#: 断言所有客户端面向的代理都从本集合派生）。
TRUST_HEADERS: frozenset[str] = frozenset(
    {
        "x-internal-call",
        "x-user-id",
        "x-tenant-id",
    }
)

#: 逐跳头（RFC 7230 §6.1）+ 由 httpx/上游重新计算的实体头。
HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)

#: 默认丢弃集合 = 逐跳头 ∪ 信任头
DEFAULT_DROP_HEADERS: frozenset[str] = HOP_HEADERS | TRUST_HEADERS


def sanitize_forward_headers(
    headers: Iterable[tuple[str, str]] | Mapping[str, str],
    *,
    extra_drop: Iterable[str] = (),
) -> dict[str, str]:
    """把客户端请求头过滤成可以安全转发给上游的形状。

    Args:
        headers: 客户端头。接受 `request.headers.items()`（可能含重复键）
            或普通 dict。
        extra_drop: 本代理额外要丢的头（小写）。例如 `{"authorization"}`。

    Returns:
        新的 dict（**不修改入参**，符合本仓「不可变」约定）。键名保持客户端
        原样大小写（上游多为大小写不敏感），值原样；同名重复头后者胜出。

    Note:
        `Mapping` 与 `Iterable[tuple]` 都收——`request.headers.items()` 在
        starlette 下是 **list of tuples**（不是 Mapping），而直接传 dict 的场景
        同样常见。两者都归一成 items 视图再过滤。
    """
    drop = DEFAULT_DROP_HEADERS | {h.lower() for h in extra_drop}
    items = headers.items() if isinstance(headers, Mapping) else headers
    return {k: v for k, v in items if k.lower() not in drop}
