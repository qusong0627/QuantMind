"""QMT 执行端凭据脱敏单测（不进真网络）。

底层库抛错时经常把 ``redis://user:密码@host`` 原样带出来，这段文本会进日志，
也会经 ``/broker-config/qmt_exec/test`` 回显给管理员 → 出网前必须抹掉。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.services.live_trading.services.qmt_exec_client import (
    QmtExecClient,
    QmtExecError,
    mask_account_id,
    redact_secrets,
)


class TestRedactSecrets:
    def test_known_secret_replaced(self) -> None:
        assert "***" in redact_secrets("auth failed pw=s3cret", "s3cret")
        assert "s3cret" not in redact_secrets("auth failed pw=s3cret", "s3cret")

    def test_url_password_masked(self) -> None:
        text = redact_secrets("Error connecting to redis://:s3cret@10.0.0.9:6380/0")
        assert "s3cret" not in text
        assert "redis://:***@10.0.0.9:6380/0" in text

    def test_url_with_username_and_encoded_password(self) -> None:
        text = redact_secrets("redis://qm:p%40ss@host:6380/1 boom")
        assert "p%40ss" not in text
        assert "redis://qm:***@host:6380/1 boom" == text

    def test_empty_and_none_inputs(self) -> None:
        assert redact_secrets("") == ""
        assert redact_secrets("plain") == "plain"
        assert redact_secrets("plain", "") == "plain"

    def test_non_string_input(self) -> None:
        assert redact_secrets(ValueError("boom")) == "boom"


class TestMaskAccountId:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", ""),
            ("123", "***"),
            ("1234", "***"),
            ("12345678", "12***78"),
        ],
    )
    def test_mask(self, raw: str, expected: str) -> None:
        assert mask_account_id(raw) == expected


class TestCallRedaction:
    def test_rpc_error_text_redacted(self) -> None:
        client = QmtExecClient(
            enabled=True,
            account_id="8888",
            bridge_redis={"redis_password": "s3cret"},
            timeout=1.0,
        )

        def _boom() -> None:
            raise ConnectionError("connect redis://:s3cret@10.0.0.9:6380/0 failed")

        with pytest.raises(QmtExecError) as exc:
            asyncio.run(client._call(_boom))
        assert "s3cret" not in str(exc.value)
        assert "***" in str(exc.value)
