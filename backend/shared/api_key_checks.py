"""API Key 可用性判定的**唯一实现**。

为什么要有这个文件
------------------
2026-09-23 复核发现，「这枚 access_key 还能用吗」这件事在仓库里被手写了 **4 遍**：

===================  ==========================================  ========
位置                  判定                                         状态
===================  ==========================================  ========
``api_key_service``  ``validate_key``                            ✅
``external/auth``    ``load_active_key``（对外 API 每请求一次）     ✅
``external/router``  ``create_session`` 内联                    ✅
``trade/qmt_agent``  ``validate_api_key_secret``               ❌ 曾错
===================  ==========================================  ========

错的那一份写的是 ``key.expires_at < datetime.now()``：左边是 TIMESTAMPTZ
（asyncpg 回 **aware**），右边是 **naive** 本地墙钟，比较直接抛
``TypeError``。而且因为现网 ``api_keys.expires_at`` 全是 NULL，短路让它
**一次都没被执行过**——直到有人签发一枚带有效期的凭据，鉴权整个 500。

四份拷贝、一份漂了、修的时候得手工改回口径，这就是本文件存在的理由：
判定逻辑只写一遍，其余位置连数据库、各自处理错误，但**判定本身**不再各写各的。

口径
----
* 时区：一律经 ``shared/utc_datetime``。naive 输入按 UTC 解释（本仓约定：
  「无时区输入一律视为 UTC，禁止当成 Asia/Shanghai 再减 8 小时」）。
* 边界：``expires_at <= now`` 即过期（到期的**那一刻**就失效，不留 1μs 缝）。
* 缺省：``expires_at is None`` = 永不过期；``is_active`` 缺失按 **不可用**
  （``getattr(..., False)``）——认证路径上「属性没了」必须落向拒绝侧。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from backend.shared.utc_datetime import as_utc, utc_now

#: 拒绝原因（机器可读）。**不要**把这些字符串回给外部调用方——
#: 区分「不存在」与「密钥错」等于给匿名者一个枚举 oracle，
#: 对外一律同一个 detail（见 `external/auth.AUTH_FAILED_DETAIL`）。
REASON_NOT_FOUND = "access_key_not_found"
REASON_INACTIVE = "access_key_inactive"
REASON_EXPIRED = "access_key_expired"


def api_key_rejection_reason(key: Any, *, now: datetime | None = None) -> str | None:
    """``None`` = 这枚 Key 可用；否则返回拒绝原因。

    纯函数、不碰 I/O——查库、bcrypt、计时拉平都由调用方按各自需要做。
    ``now`` 只为测试注入，生产路径不要传。

    ⚠️ 本函数**不能**替代 bcrypt 比对。「可用」只表示这枚 Key 本身有效，
    不表示调用方知道它的 secret。
    """
    if key is None:
        return REASON_NOT_FOUND
    # 缺属性按不可用：DB 里 is_active 是 NOT NULL，真读不到说明模型/查询出问题了，
    # 此时放行比拒绝危险得多。
    if not getattr(key, "is_active", False):
        return REASON_INACTIVE

    expires_at = getattr(key, "expires_at", None)
    if expires_at is not None:
        # 非 datetime 的到期值一律判过期。**不能**直接交给 `as_utc`：它对无法
        # 解释的值回落 `utc_now()`，而这里紧接着要跟一个**更早**取的 `current`
        # 比，于是「垃圾输入算不算过期」取决于两次 `utc_now()` 的微秒先后——
        # 同一个值今天拒绝、明天放行。认证路径上不接受这种不确定性，
        # 显式钉到拒绝侧。正常路径（PG 的 TIMESTAMPTZ）永远是 datetime。
        if not isinstance(expires_at, datetime):
            return REASON_EXPIRED
        current = utc_now() if now is None else now
        if as_utc(expires_at) <= current:
            return REASON_EXPIRED
    return None


__all__ = [
    "REASON_EXPIRED",
    "REASON_INACTIVE",
    "REASON_NOT_FOUND",
    "api_key_rejection_reason",
]
