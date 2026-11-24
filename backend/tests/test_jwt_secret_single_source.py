"""JWT 密钥单一来源回归：签发与校验必须都走 ``get_jwt_secret()``。

2026-11-24 实盘事故：``AuthService`` 用启动期快照 ``settings.SECRET_KEY`` 签发，
而 trade/stream 的 ``decode_jwt_token`` 走 runtime.env 权威的 ``get_jwt_secret()``。
runtime.env 里的 SECRET_KEY 一换，刷新接口仍 200 但所有下游 401
（"Invalid authentication credentials"），策略启动不了。这里用源码断言锁死。
"""

from __future__ import annotations

import inspect

from backend.services.api.user_app.services import auth_service as auth_service_module
from backend.shared import auth as shared_auth


def test_auth_service_does_not_use_settings_secret_key():
    source = inspect.getsource(auth_service_module)
    assert "settings.SECRET_KEY" not in source, (
        "AuthService 仍在用 settings.SECRET_KEY（启动期快照）；"
        "必须改用 get_jwt_secret()，否则 runtime.env 轮换后与 trade/stream 校验不一致"
    )
    assert source.count("get_jwt_secret()") >= 6


def test_shared_auth_manager_and_decoder_share_single_source():
    manager_source = inspect.getsource(shared_auth.AuthManager.secret_key.fget)
    decoder_source = inspect.getsource(shared_auth.decode_jwt_token)
    assert "get_jwt_secret()" in manager_source
    assert "get_jwt_secret()" in decoder_source
