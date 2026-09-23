"""T-P0-06 tripwire：trade/main.py 必须注册模拟盘关键 worker。

背景：run_simulation_eod_worker 与 run_simulation_pending_order_worker
曾长期"只有定义、无调用方"——simulation_account_daily 无写入者、pending
单永久悬空。此源扫描闸门防未来重构误删注册。

**2026-09-21 补**：原闸门只断言 `worker in src`（"出现过"），于是漏掉了反面——
同一个 worker 被注册**两次**。pending order worker 在 trade/main.py 两处各
`create_task` 一次，两个协程抢同一批 pending 单：日志每行打两遍、同一笔单被
两轮并发取价/占流动性额度；且第二处挂在 `app.state.*` 上**从不被取消**。
故此处把「出现过」收紧为「恰好一次」，并钉死任务变量必须进 shutdown 取消清单。
"""

import re
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


# ---------------------------------------------------------------------------
# 注册**次数**与**取消可达性**
# ---------------------------------------------------------------------------

def _call_site_count(src: str, worker: str) -> int:
    """worker 的调用点数。

    带括号匹配：import 行是裸名字（`run_x,`），只有调用点写成 `run_x()`。
    """
    assert worker, "worker 名为空 —— 这条断言将零项通过"
    return src.count(f"{worker}()")


def test_critical_simulation_workers_registered_exactly_once():
    """恰好一次。只查"出现过"会放过重复注册——本仓真实发生过。"""
    src = _TRADE_MAIN.read_text(encoding="utf-8")
    assert _REQUIRED_WORKERS, "必注册清单为空 —— 这条测试将零项通过"
    for worker in _REQUIRED_WORKERS:
        count = _call_site_count(src, worker)
        assert count == 1, (
            f"{worker} 有 {count} 个调用点，必须恰好 1 个："
            f"重复注册会让两个协程抢同一批单（重复取价/占额度），且日志翻倍"
        )


def test_started_worker_tasks_are_cancelled_on_shutdown():
    """起过的任务必须挂到 shutdown 会取消的变量上。

    `app.state.xxx_task = asyncio.create_task(...)` 在本文件里是**死赋值**：
    既无人读取，也不在取消清单里 —— 进程退出时任务泄漏。故要求赋值目标
    出现在 shutdown 的 `for task in (...)` 清单中。
    """
    src = _TRADE_MAIN.read_text(encoding="utf-8")

    cancel_list = re.search(r"for task in \(([^)]*)\)", src)
    assert cancel_list is not None, "找不到 shutdown 的取消清单"
    cancelled = {name.strip() for name in cancel_list.group(1).split(",") if name.strip()}
    assert cancelled, "取消清单为空 —— 这条测试将零项通过"

    # 逐个 worker 找「某个变量 = asyncio.create_task(worker())」的赋值目标
    checked = 0
    for worker in _REQUIRED_WORKERS:
        match = re.search(
            rf"(\w+)\s*=\s*asyncio\.create_task\(\s*{re.escape(worker)}\(\)",
            src,
        )
        if match is None:
            continue  # 该 worker 的注册形态不由本测试管辖（另有测试兜"必须注册"）
        checked += 1
        assert match.group(1) in cancelled, (
            f"{worker} 的任务变量 {match.group(1)} 不在 shutdown 取消清单里，"
            f"进程退出时会泄漏"
        )
    assert checked > 0, "没有匹配到任何 create_task 赋值 —— 这条测试零项通过"

