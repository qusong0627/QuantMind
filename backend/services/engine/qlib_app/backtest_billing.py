"""Routing policy for synchronous backtest requests when billing is active."""
# OSS port: 与企业版逐字一致。OSS 无计费模块，调用方以 billing_enabled=False
# 使用（仅 async_mode/pandas 引擎走异步通道），见 api/backtest.py。


def should_use_async_backtest_path(
    *, engine: str, async_mode: bool, billing_enabled: bool
) -> bool:
    """Route billable Qlib sync requests through Celery with a bounded wait.

    Fast runs still return their result to the synchronous caller. Runs that
    exceed the sync wait window remain queryable by task/backtest ID.
    """
    if async_mode or engine == "pandas":
        return True
    return engine in {"auto", "qlib"} and billing_enabled
