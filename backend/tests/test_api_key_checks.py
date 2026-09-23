"""`shared/api_key_checks.api_key_rejection_reason` —— API Key 可用性判定的唯一实现。

为什么单独测这个纯函数
----------------------
这条判定在仓库里曾被手写 **4 遍**（api_key_service / external.auth /
external.router / trade.qmt_agent）。四份拷贝里有一份写成了
``key.expires_at < datetime.now()``：TIMESTAMPTZ 回来是 aware，`datetime.now()`
是 naive，比较直接抛 ``TypeError``——而且因为现网 expires_at 全是 NULL，
短路让它**一次都没被执行过**，直到有人签发带有效期的凭据才 500。

所以这里测的不只是「函数对不对」，而是**那条判定从此只有一处**：
下面每个分支都对应曾经各自实现过的某个调用点。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from backend.shared.api_key_checks import (
    REASON_EXPIRED,
    REASON_INACTIVE,
    REASON_NOT_FOUND,
    api_key_rejection_reason,
)

UTC = timezone.utc


@dataclass(frozen=True)
class _Key:
    is_active: bool = True
    expires_at: object = None


def test_missing_key_is_rejected() -> None:
    assert api_key_rejection_reason(None) == REASON_NOT_FOUND


def test_inactive_key_is_rejected() -> None:
    assert api_key_rejection_reason(_Key(is_active=False)) == REASON_INACTIVE


def test_expired_key_is_rejected() -> None:
    past = datetime.now(UTC) - timedelta(seconds=1)
    assert api_key_rejection_reason(_Key(expires_at=past)) == REASON_EXPIRED


def test_future_expiry_is_accepted() -> None:
    future = datetime.now(UTC) + timedelta(days=1)
    assert api_key_rejection_reason(_Key(expires_at=future)) is None


def test_null_expiry_means_never_expires() -> None:
    """NULL 是「永不过期」，不是「立刻过期」——别把 None 收成 utc_now()。"""
    assert api_key_rejection_reason(_Key(expires_at=None)) is None


def test_inactive_wins_over_valid_expiry() -> None:
    """停用优先于未过期：停用的凭据即便有效期还长也必须拒。

    曾经四份拷贝里的分支顺序并不一致，这里把顺序钉死。
    """
    future = datetime.now(UTC) + timedelta(days=1)
    assert (
        api_key_rejection_reason(_Key(is_active=False, expires_at=future))
        == REASON_INACTIVE
    )


def test_now_can_be_injected_for_determinism() -> None:
    """`now` 注入让边界可确定地测——生产路径不传它。"""
    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    assert api_key_rejection_reason(_Key(expires_at=fixed), now=fixed) == REASON_EXPIRED
    assert (
        api_key_rejection_reason(
            _Key(expires_at=fixed + timedelta(seconds=1)), now=fixed
        )
        is None
    )


def test_missing_is_active_attribute_fails_closed() -> None:
    """对象上没有 `is_active` 属性时按**不可用**处理，不是按可用。

    认证路径上「属性读不到」必须落向拒绝侧；写成 `getattr(key, "is_active", True)`
    就是一个静默放行。
    """

    class _NoIsActive:
        expires_at = None

    assert api_key_rejection_reason(_NoIsActive()) == REASON_INACTIVE


@pytest.mark.parametrize("garbage", [0, "", "2026-01-01", [], {}, 3.14])
def test_garbage_expiry_fails_closed_deterministically(garbage: object) -> None:
    """非 datetime 的到期值**确定地**判过期。

    ⚠️ 这条曾经是不确定的：直接交给 `as_utc` 时，它对无法解释的值回落
    `utc_now()`，而函数里那个用来比较的 `current` 取得更早——于是
    「垃圾值算不算过期」取决于两次 `utc_now()` 的微秒先后，同一个输入
    可能一次拒绝、一次放行。认证路径上不接受这种不确定性。
    """
    assert api_key_rejection_reason(_Key(expires_at=garbage)) == REASON_EXPIRED
    # 连跑几次，结果必须一致（钉住「不是时序碰运气」）
    for _ in range(5):
        assert api_key_rejection_reason(_Key(expires_at=garbage)) == REASON_EXPIRED


def test_naive_datetime_is_interpreted_as_utc() -> None:
    """naive 按本仓约定当 UTC，不是当上海墙钟。

    `shared/utc_datetime` 写死了这条：「无时区输入一律视为 UTC，禁止当成
    Asia/Shanghai 再减 8 小时」。库列是 TIMESTAMPTZ，naive 只会来自测试替身
    或非 PG 方言——此时按成文约定收口，比每个调用点各自解释要好。
    """
    assert (
        api_key_rejection_reason(_Key(expires_at=datetime(2020, 1, 1)))
        == REASON_EXPIRED
    )
    naive_future = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)
    assert api_key_rejection_reason(_Key(expires_at=naive_future)) is None


def test_is_pure() -> None:
    """不改入参、不读全局——同一输入永远同一输出（除时间推进）。"""
    key = _Key(expires_at=datetime.now(UTC) + timedelta(days=1))
    snapshot = (key.is_active, key.expires_at)
    api_key_rejection_reason(key)
    assert (key.is_active, key.expires_at) == snapshot
