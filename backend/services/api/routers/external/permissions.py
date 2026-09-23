"""对外命名空间的**凭据范围**（`api_keys.permissions`）判定。

这个模块存在的理由，一句话：**一枚泄露的只读凭据不能自己升格成可下单的凭据。**

`api_keys.permissions` 是凭据自带的字符串数组，**在本次之前没有任何地方读它**
（全仓只有签发侧写入与前端展示）。于是它的实际语义是「装饰」：一枚
`["trade.read"]` 的凭据与一枚 `["trade.write"]` 的凭据在服务端完全等价。

⚠️ 它**不是** RBAC（`user_app/services/rbac_service`）。那是按 `user_id` 查角色，
管的是「这个人能做什么」；这里是「这枚凭据被授权做什么」。两者刻意分开：
拿用户 JWT 的人本来就拥有该用户的全部权限，而**只有 secret_key 的人不应该拥有**——
这正是把 key 交给外部节点时想要的性质（只给读，那就只剩读）。

## 语义（三条，都进测试）

1. **空列表 = 没有任何被关的权限**（不是「全部」）。
   `permissions` 可空是列的事实；把 NULL/[] 解释成「全权」会让「漏填」与
   「故意收窄」变成同一件事，而它们的失败后果正好相反。
2. **判定只认精确相等**，不做 `trade.*` 这类通配。本仓在闸门放行表上已经吃过
   「前缀匹配把子树顺带放行」的亏（见 `live_trading_gate` 两处注释）。
3. **不带权限码的端点不受本模块影响**。当前只有交易面挂了权限码，
   理由与现状见 `GATED_ENDPOINTS` 上方那段。

## 为什么现在只关交易面

权限码是**给已经存在的凭据追加约束**，不是新建就生效的机制：现网
`api_keys` 里已有的凭据（默认签发 `["trade.read","trade.write"]`）必须先
带上新码才不会被自己的凭据锁在门外，而**当前没有任何界面可以编辑权限列表**
（`/api-keys/init` 是唯一的写入点）。所以：

* 交易面用**已存在且已签发**的 `trade.read` / `trade.write` —— 零迁移成本，
  现网两枚凭据原样可用（实测 `SELECT access_key, permissions FROM api_keys`）。
* 数据面/控制面/任务面暂不挂码。给它们新造 `data.read` 之类的码，后果是
  现网凭据**立刻**失去数据面访问，而用户没有任何地方能把它加回来。

这条边界由 `test_external_api_permissions.py` 钉住：**挂了码的端点集合是枚举出来的**，
新加一个要么出现在名单里、要么测试红。将来要收窄数据面时，得先有权限编辑界面
或一条给存量凭据补码的迁移，那是另一批的事——不是把码一挂就完了。
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, status

from backend.services.api.routers.external.auth import (
    ExternalPrincipal,
    require_external_principal,
)

logger = logging.getLogger(__name__)

#: 交易面读：账户、持仓、委托、成交、风控状态。
PERMISSION_TRADE_READ = "trade.read"

#: 交易面写：下单、撤单。**这是全仓唯一一个能让钱动起来的权限码。**
PERMISSION_TRADE_WRITE = "trade.write"

#: 当前被权限码关住的端点（方法 + 对外路径形状）。**唯一出处**——
#: 测试从这里取名单去逐条验证「没有该码就 403」，文档也从这里引。
#: 加端点时改这里；改这里时测试会逼你确认这是有意的。
#:
#: ⚠️ 名单里的每一条都必须**真的存在**于 `external/trading.py` 的路由表里。
#: 曾经这里有一条 `GET /api/ext/v1/trading/risk`——那是规划时写下的，而风控
#: 四级状态机**至今没有 HTTP 端点**（它挂在撮合链路上，不是可查询的资源）。
#: 一条指向不存在端点的权限条目比没有更糟：测试会「验证」一个 404 的 403 行为，
#: 全绿，而外部节点按文档去调只会拿到 404。`test_external_api_permissions.py`
#: 现在会把本表与实际路由逐条对表。
GATED_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("GET", "/api/ext/v1/trading/sim/account"),
    ("GET", "/api/ext/v1/trading/sim/orders"),
    ("GET", "/api/ext/v1/trading/sim/orders/{order_id}"),
    ("GET", "/api/ext/v1/trading/sim/trades"),
    ("POST", "/api/ext/v1/trading/sim/orders"),
    ("POST", "/api/ext/v1/trading/sim/orders/{order_id}/cancel"),
)

#: 每个受关端点的权限码要求。`{order_id}` 这类参数位在判定时是具体值，
#: 所以这里用**形状**做键，而不是用它去匹配运行时路径。
GATED_REQUIREMENTS: dict[tuple[str, str], str] = {
    ("GET", "/api/ext/v1/trading/sim/account"): PERMISSION_TRADE_READ,
    ("GET", "/api/ext/v1/trading/sim/orders"): PERMISSION_TRADE_READ,
    ("GET", "/api/ext/v1/trading/sim/orders/{order_id}"): PERMISSION_TRADE_READ,
    ("GET", "/api/ext/v1/trading/sim/trades"): PERMISSION_TRADE_READ,
    ("POST", "/api/ext/v1/trading/sim/orders"): PERMISSION_TRADE_WRITE,
    ("POST", "/api/ext/v1/trading/sim/orders/{order_id}/cancel"): PERMISSION_TRADE_WRITE,
}


def has_permission(principal: ExternalPrincipal, code: str) -> bool:
    """纯判定，便于单测与审计。空列表 = 没有该权限（见模块 docstring 第 1 条）。"""
    return code in principal.permissions


def require_permission(code: str):
    """返回一个 FastAPI 依赖：凭据没有 `code` 就 403。

    用法（与鉴权链成依赖，不要在函数体里手工判）：

        async def handler(p: ExternalPrincipal = Depends(require_permission(PERMISSION_TRADE_WRITE))): ...

    403 而不是 401：凭据是**有效**的，只是没有被授权做这件事。报 401 会让
    调用方去重新握手换令牌，而换一百次令牌权限也不会变——方向反了。
    """

    async def _dependency(
        principal: ExternalPrincipal = Depends(require_external_principal),
    ) -> ExternalPrincipal:
        if not has_permission(principal, code):
            # 日志里带指纹与需要的码，不带凭据明文（与 auth 同一约定）。
            logger.warning(
                "[ExtAuth] 权限不足 ak_fp=%s 需要=%s 持有=%s",
                _fingerprint(principal.access_key),
                code,
                ",".join(principal.permissions) or "-",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="permission_denied",
            )
        return principal

    return _dependency


def _fingerprint(access_key: str) -> str:
    """与 `auth.access_key_fp` 同一实现——**不复制算法**，只做转发。

    `access_key` 是请求体里的自由字符串（可含换行/ANSI），明文进日志等于给
    匿名者一个伪造日志行的通道（见 README「给改这个目录的人」）。
    """
    from backend.services.api.routers.external.auth import access_key_fp

    return access_key_fp(access_key)


__all__ = [
    "GATED_ENDPOINTS",
    "GATED_REQUIREMENTS",
    "PERMISSION_TRADE_READ",
    "PERMISSION_TRADE_WRITE",
    "has_permission",
    "require_permission",
]
