"""QMT Agent 鉴权的 API Key 到期检查（2026-09-23 修）。

问题形状
--------
`validate_api_key_secret` 里那行到期判断写成 `key.expires_at < datetime.now()`：
左边来自 `ApiKey.expires_at = Column(DateTime(timezone=True))`，asyncpg 回的是
**aware UTC**；右边 `datetime.now()` 是 **naive 本地墙钟**。两者相比不是
「偏 8 小时」这种温和走样，而是**直接抛异常**：

    TypeError: can't compare offset-naive and offset-aware datetimes

即整条 QMT agent 鉴权链路 500。

为什么此前没人发现
------------------
因为那一行**从来没被执行过**：现网 `api_keys` 的 `expires_at` 全是 NULL
（复核时实测 0/2），`if key.expires_at and ...` 直接短路。所以这不是「一个偶尔
出错的分支」，而是**一条从未生效的到期检查**——签发带有效期的 Key 那天才会炸。

本文件要钉住的
--------------
1. 到期判定真的按 aware UTC 比较（过去=过期、未来=不过期）；
2. **判据是承重的**：`expires_at` 必须非 NULL 才走得到那一行——用 None 写的
   回归测试在坏代码上也会绿，等于没测（本仓已经栽过两次的空转断言形状）；
3. 把「naive 比较会抛」这件事显式写下来，免得有人把 `utcnow()` 改回去。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from backend.services.trade.services.qmt_agent_auth import validate_api_key_secret

UTC = timezone.utc


@dataclass(frozen=True)
class _FakeApiKey:
    """`validate_api_key_secret` 只碰这三个属性，不必起真 ORM 对象。"""

    is_active: bool = True
    expires_at: Any = None
    secret_hash: str = "$2b$12$ignored"  # 空 secret 会在 verify 前短路，不会真比对


def test_expired_key_is_rejected() -> None:
    """**核心断言**：带过期时间且已过期的 Key 必须被拒。

    坏代码（`datetime.now()`）在这条上抛 TypeError → 红。
    """
    key = _FakeApiKey(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert validate_api_key_secret(key, "") == "access_key_expired"


def test_future_expiry_is_not_expired() -> None:
    """未到期的 Key 不能误判为过期——收紧判定别收紧过头（C1「拦多了」那一面）。

    空 secret 会在 bcrypt 之前短路返回 `secret_key_invalid`，正好当「没走到过期
    分支」的探针，且不付 bcrypt 的 ~0.3s。
    """
    key = _FakeApiKey(expires_at=datetime.now(UTC) + timedelta(days=1))
    assert validate_api_key_secret(key, "") == "secret_key_invalid"


def test_null_expiry_still_means_never_expires() -> None:
    """NULL 语义是「永不过期」，不是「立刻过期」——别把 NULL 收成 utc_now()。"""
    assert (
        validate_api_key_secret(_FakeApiKey(expires_at=None), "")
        == "secret_key_invalid"
    )


def test_naive_expiry_follows_the_documented_utc_convention() -> None:
    """naive 的 `expires_at` 按本仓约定**当 UTC 解释**，不是「当上海墙钟」。

    库列是 `DateTime(timezone=True)`，驱动只会回 aware；naive 只会来自测试替身或
    将来的非 PG 方言。此时按 `shared/utc_datetime` 的成文约定收口，而不是抛异常
    ——否则同一份判定在不同调用点会有不同行为，那正是这次要消灭的东西。
    """
    # naive 的过去 → 按 UTC 解释仍是过去 → 过期
    assert validate_api_key_secret(
        _FakeApiKey(expires_at=datetime(2020, 1, 1)), ""
    ) == ("access_key_expired")
    # naive 的未来 → 未过期（走到 bcrypt，被空 secret 短路）
    far_future = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)
    assert (
        validate_api_key_secret(_FakeApiKey(expires_at=far_future), "")
        == "secret_key_invalid"
    )


def test_the_naive_comparison_really_does_raise() -> None:
    """把修这个 bug 的**理由**钉下来：aware 与 naive 比较确实不可比。

    没有这条，将来有人把 `utcnow()` 换回 `datetime.now()` 时，可能顺手把上面
    那条 `pytest.raises(TypeError)` 删掉「让测试过」——这条让删除的代价是显式的。
    """
    aware = datetime.now(UTC)
    with pytest.raises(TypeError, match="offset-naive and offset-aware"):
        _ = aware < datetime.now()


def test_boundary_is_inclusive() -> None:
    """边界：``expires_at <= now`` 即过期——到期的**那一刻**就失效，不留缝。

    统一到收紧侧（原先 qmt 那份是 `<`、其余三份是 `<=`，四份拷贝连边界都不一致）。
    """
    now = datetime.now(UTC)
    assert (
        validate_api_key_secret(_FakeApiKey(expires_at=now), "") == "access_key_expired"
    )


@pytest.mark.parametrize("garbage", [0, "", "2026-01-01", [], {}])
def test_garbage_expiry_fails_closed_instead_of_raising(garbage: Any) -> None:
    """非 datetime 的 `expires_at` 一律判**过期**，不是抛异常。

    `expires_at` 正常是 datetime 或 None，但这里是认证路径：「意外输入 → 异常」
    等于把一次 401 变成 500。而 `as_utc` 对无法解释的值回落 `utc_now()`，
    与 `<= now` 一比就是「已过期」——**失败方向落在拒绝侧**，正是想要的。
    """
    assert (
        validate_api_key_secret(_FakeApiKey(expires_at=garbage), "")
        == "access_key_expired"
    )
