"""T-P0-06 tripwire：trade/main.py 必须注册模拟盘关键 worker。

背景：run_simulation_eod_worker 与 run_simulation_pending_order_worker
曾长期"只有定义、无调用方"——simulation_account_daily 无写入者、pending
单永久悬空。此源扫描闸门防未来重构误删注册。
"""

from pathlib import Path

_TRADE_MAIN = (
    Path(__file__).resolve().parents[1] / "services" / "trade" / "main.py"
)

_SANDBOX_MANAGER = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "trade"
    / "sandbox"
    / "manager.py"
)

_REQUIRED_WORKERS = (
    "run_simulation_eod_worker",
    "run_simulation_pending_order_worker",
    "run_simulation_t1_unlock_task",
)


def test_trade_main_registers_critical_simulation_workers():
    src = _TRADE_MAIN.read_text(encoding="utf-8")
    for worker in _REQUIRED_WORKERS:
        assert worker in src, f"trade/main.py 缺少关键 worker 注册: {worker}"


def test_sandbox_submit_strategy_is_gated():
    """T-P0-02：沙箱提交入口必须调用统一安全闸门（防未来重构误删卡点）。"""
    src = _SANDBOX_MANAGER.read_text(encoding="utf-8")
    assert "validate_strategy_code" in src, "submit_strategy 未接安全闸门"
