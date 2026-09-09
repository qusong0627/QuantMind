"""``broker_config`` 安全口径单测（假 Redis，不触网、不连券商）。

覆盖三件事：
  1. 管理员门（``require_admin``）——非管理员一律 403；
  2. 换连接地址必须重填凭据——否则库内/env 的密钥会被发往新地址（凭据外泄）；
  3. 地址/端口字段格式校验。

异步用例统一 ``asyncio.run``（容器内无 pytest-asyncio）。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from backend.services.trade.routers import broker_config as mod
from backend.services.trade_shared.deps import AuthContext, require_admin
from backend.tests.test_qmt_exec_mirror import FakeRedisClient


def _redis(**stored: str) -> Any:
    key = mod._CONFIG_KEY.format(broker="qmt_exec")
    return SimpleNamespace(client=FakeRedisClient(strings={key: json.dumps(stored)}))


def _redis_for(broker: str, **stored: str) -> Any:
    key = mod._CONFIG_KEY.format(broker=broker)
    return SimpleNamespace(client=FakeRedisClient(strings={key: json.dumps(stored)}))


def _auth() -> Any:
    return SimpleNamespace(tenant_id="default", user_id="1", roles=["admin"], is_admin=True)


def _stored(broker: str, redis: Any) -> dict[str, str]:
    return json.loads(redis.client.strings[mod._CONFIG_KEY.format(broker=broker)])


class TestAdminGate:
    def test_non_admin_rejected(self) -> None:
        auth = AuthContext(
            user_id="7", tenant_id="default", raw_sub="7", roles=["user"]
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(require_admin(auth))
        assert exc.value.status_code == 403

    def test_admin_role_allows(self) -> None:
        auth = AuthContext(
            user_id="7", tenant_id="default", raw_sub="7", roles=["admin"]
        )
        assert asyncio.run(require_admin(auth)) is auth

    def test_is_admin_flag_allows(self) -> None:
        auth = AuthContext(
            user_id="7",
            tenant_id="default",
            raw_sub="7",
            roles=["user"],
            is_admin=True,
        )
        assert asyncio.run(require_admin(auth)).is_admin is True


class TestCredentialGuard:
    def test_host_change_without_password_rejected(self) -> None:
        redis = _redis(
            redis_host="192.168.1.9", redis_password="s3cret", account_id="8888"
        )
        payload = mod.BrokerConfigUpdate(values={"redis_host": "evil.example.com"})
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                mod.update_broker_config("qmt_exec", payload, auth=_auth(), redis=redis)
            )
        assert exc.value.status_code == 422
        # 配置保持原样，凭据没被搬走
        assert _stored("qmt_exec", redis)["redis_host"] == "192.168.1.9"

    def test_host_change_with_password_allowed(self) -> None:
        redis = _redis(redis_host="192.168.1.9", redis_password="s3cret")
        payload = mod.BrokerConfigUpdate(
            values={"redis_host": "192.168.1.10", "redis_password": "newpass"}
        )
        asyncio.run(
            mod.update_broker_config("qmt_exec", payload, auth=_auth(), redis=redis)
        )
        stored = _stored("qmt_exec", redis)
        assert stored["redis_host"] == "192.168.1.10"
        assert stored["redis_password"] == "newpass"

    def test_unrelated_change_keeps_password(self) -> None:
        redis = _redis(
            redis_host="192.168.1.9", redis_password="s3cret", account_id="8888"
        )
        payload = mod.BrokerConfigUpdate(values={"account_id": "9999"})
        asyncio.run(
            mod.update_broker_config("qmt_exec", payload, auth=_auth(), redis=redis)
        )
        stored = _stored("qmt_exec", redis)
        assert stored["account_id"] == "9999"
        assert stored["redis_password"] == "s3cret"

    def test_clearing_host_needs_no_password(self) -> None:
        redis = _redis(redis_host="192.168.1.9", redis_password="s3cret")
        payload = mod.BrokerConfigUpdate(values={"redis_host": ""})
        asyncio.run(
            mod.update_broker_config("qmt_exec", payload, auth=_auth(), redis=redis)
        )
        assert "redis_host" not in _stored("qmt_exec", redis)

    def test_env_secret_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """页面没存密码、但 env 里有 → 运行时照样会用它，换地址同样要重填。"""
        monkeypatch.setenv("QMT_EXEC_REDIS_PASSWORD", "from-env")
        redis = _redis(redis_host="192.168.1.9")
        payload = mod.BrokerConfigUpdate(values={"redis_host": "evil.example.com"})
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                mod.update_broker_config("qmt_exec", payload, auth=_auth(), redis=redis)
            )
        assert exc.value.status_code == 422

    def test_no_secret_anywhere_allows_host_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("QMT_EXEC_REDIS_PASSWORD", raising=False)
        monkeypatch.delenv("BIGQMT_REDIS_PASSWORD", raising=False)
        redis = _redis()
        payload = mod.BrokerConfigUpdate(values={"redis_host": "192.168.1.9"})
        asyncio.run(
            mod.update_broker_config("qmt_exec", payload, auth=_auth(), redis=redis)
        )
        assert _stored("qmt_exec", redis)["redis_host"] == "192.168.1.9"

    def test_tdx_bridge_url_change_requires_token(self) -> None:
        redis = _redis_for("tdx", bridge_url="http://192.168.1.9:8550", bridge_token="tok")
        payload = mod.BrokerConfigUpdate(values={"bridge_url": "http://evil:8550"})
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                mod.update_broker_config("tdx", payload, auth=_auth(), redis=redis)
            )
        assert exc.value.status_code == 422

    def test_test_endpoint_auto_save_also_guarded(self) -> None:
        """测试连接会先自动保存表单值，这条路径同样不能搬凭据。"""
        redis = _redis(redis_host="192.168.1.9", redis_password="s3cret")
        payload = mod.BrokerTestRequest(values={"redis_host": "evil.example.com"})
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                mod.test_broker_connection(
                    "qmt_exec", payload, auth=_auth(), redis=redis
                )
            )
        assert exc.value.status_code == 422
        assert _stored("qmt_exec", redis)["redis_host"] == "192.168.1.9"


class TestFieldValidation:
    @pytest.mark.parametrize(
        "broker,field,bad",
        [
            ("qmt_exec", "redis_host", "evil host"),
            ("qmt_exec", "redis_host", "evil\nhost"),
            ("qmt_exec", "redis_port", "6379;rm -rf /"),
            ("qmt_exec", "redis_db", "abc"),
            ("tdx", "bridge_url", "ftp://192.168.1.9"),
            ("tdx", "bridge_url", "http://192.168.1.9:8550/?x=1"),
        ],
    )
    def test_bad_values_rejected(self, broker: str, field: str, bad: str) -> None:
        redis = _redis_for(broker)
        payload = mod.BrokerConfigUpdate(values={field: bad})
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                mod.update_broker_config(broker, payload, auth=_auth(), redis=redis)
            )
        assert exc.value.status_code == 422

    def test_good_values_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 容器 env 里可能有 TDX_BRIDGE_TOKEN → 换地址会要求重填，这里显式清掉
        monkeypatch.delenv("TDX_BRIDGE_TOKEN", raising=False)
        redis = _redis_for("tdx")
        payload = mod.BrokerConfigUpdate(
            values={"bridge_url": "http://192.168.31.13:8550", "account": "8888"}
        )
        asyncio.run(mod.update_broker_config("tdx", payload, auth=_auth(), redis=redis))
        assert _stored("tdx", redis)["bridge_url"] == "http://192.168.31.13:8550"
