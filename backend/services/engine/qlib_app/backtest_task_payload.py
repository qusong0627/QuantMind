"""Trusted request metadata transport for queued backtest tasks."""
# OSS port: 与企业版逐字一致。依赖的 3 个 PrivateAttr 见 schemas/backtest.py。

from __future__ import annotations

from typing import Any

from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest


def deserialize_backtest_task_request(
    request_dict: dict[str, Any],
) -> QlibBacktestRequest:
    """Rebuild an API-admitted request and restore trusted private metadata."""

    task_payload = dict(request_dict)
    trusted_replay_signal_data = task_payload.pop(
        "_trusted_replay_signal_data", None
    )
    trusted_pandas_pool_snapshot = task_payload.pop(
        "_trusted_pandas_pool_snapshot", None
    )
    resolved_execution_mode = task_payload.pop("_resolved_execution_mode", None)
    request = QlibBacktestRequest(**task_payload)
    request._trusted_replay_signal_data = trusted_replay_signal_data
    request._trusted_pandas_pool_snapshot = trusted_pandas_pool_snapshot
    request._resolved_execution_mode = resolved_execution_mode
    return request
