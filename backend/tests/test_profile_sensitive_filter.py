"""Profile 敏感字段过滤 + MinerU Token 字段契约。

社区功能允许已登录用户读**他人**基础档案（GET /api/v1/profiles/{user_id}），
凭据类字段必须全部剔除——漏一个就是把 Key 按页面发给所有人。2026-10 补审
时发现 embedding_api_key / llm_extra_headers 从未被剔除（只剔了 api_key），
随用户级 MinerU Token 上线一并修掉。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.api.routers.profiles import (  # noqa: E402
    _filter_sensitive_profile_data,
)
from backend.services.api.user_app.schemas.user import (  # noqa: E402
    UserProfileResponse,
    UserProfileUpdate,
)

CREDENTIAL_FIELDS = (
    "api_key",
    "embedding_api_key",
    "mineru_api_token",
    "llm_extra_headers",
)


def _full_profile() -> dict:
    return {
        "display_name": "n",
        "phone": "13800000000",
        "api_key": "sk-chat",
        "embedding_api_key": "sk-embed",
        "mineru_api_token": "mineru-tok",
        "llm_extra_headers": '{"Authorization": "Bearer x"}',
    }


def test_non_owner_credentials_all_stripped() -> None:
    out = _filter_sensitive_profile_data(
        _full_profile(), {"user_id": "u2", "username": "u2"}, "u1"
    )
    for field in CREDENTIAL_FIELDS:
        assert field not in out, f"{field} 泄露给非本人"
    assert out["display_name"] == "n", "非敏感字段保留"
    assert out["phone"] == "138****0000", "手机号脱敏"


def test_owner_keeps_credentials() -> None:
    data = _full_profile()
    out = _filter_sensitive_profile_data(
        data, {"user_id": "u1", "username": "u1"}, "u1"
    )
    for field in CREDENTIAL_FIELDS:
        assert out[field] == data[field]


def test_internal_call_keeps_credentials() -> None:
    """引擎经内部网关读 Profile（X-Internal-Call + username=internal）取配置。"""
    out = _filter_sensitive_profile_data(
        _full_profile(), {"user_id": "svc", "username": "internal"}, "u1"
    )
    for field in CREDENTIAL_FIELDS:
        assert field in out


def test_short_phone_not_mangled() -> None:
    out = _filter_sensitive_profile_data(
        {"phone": "12345"}, {"user_id": "u2", "username": "u2"}, "u1"
    )
    assert out["phone"] == "12345"


def test_profile_schema_carries_mineru_token() -> None:
    """更新/响应模型都必须带 mineru_api_token（漏了 = 保存 200 但静默丢弃）。"""
    update = UserProfileUpdate(mineru_api_token="tok-1")
    assert update.model_dump(exclude_unset=True) == {"mineru_api_token": "tok-1"}
    assert "mineru_api_token" in UserProfileResponse.model_fields


def test_profile_model_has_mineru_column() -> None:
    from backend.services.api.user_app.models.user import UserProfile

    assert "mineru_api_token" in UserProfile.__table__.columns


if __name__ == "__main__":  # pragma: no cover
    import pytest as _pytest

    sys.exit(_pytest.main([__file__, "-v"]))
