"""模型结构适配检查（不依赖 torch，训练器与测试共用）。

当前只覆盖 TCN：Qlib 的 `TemporalConvNet` 使用 `dilations = 2 ** i`
（i=0..L-1），感受野为

    RF = 1 + (kernel_size - 1) * (2 ** num_layers - 1)

若 RF < `step_len`，模型看不到完整窗口，序列尾部之外的信息永远进不来。
默认 kernel=5 / num_layers=2 → RF=13，小于默认 step_len=20，属于结构失配。
这里只计算并给出建议，不静默改写用户显式参数。
"""

from __future__ import annotations

from typing import Any


def tcn_receptive_field(kernel_size: int, num_layers: int) -> int:
    """Qlib TCN 感受野（dilations=2^i）。"""
    k = max(1, int(kernel_size))
    layers = max(1, int(num_layers))
    return 1 + (k - 1) * (2**layers - 1)


def min_layers_for_window(kernel_size: int, step_len: int, max_layers: int = 8) -> int | None:
    """给定 kernel，覆盖 step_len 所需的最小层数；max_layers 内无解返回 None。"""
    for layers in range(1, max(1, int(max_layers)) + 1):
        if tcn_receptive_field(kernel_size, layers) >= int(step_len):
            return layers
    return None


def min_kernel_for_window(num_layers: int, step_len: int, max_kernel: int = 33) -> int | None:
    """给定层数，覆盖 step_len 所需的最小奇数 kernel；max_kernel 内无解返回 None。"""
    layers = max(1, int(num_layers))
    span = 2**layers - 1
    need = int(step_len) - 1
    kernel = 1 + (need + span - 1) // span  # ceil(need/span)+1
    if kernel % 2 == 0:
        kernel += 1
    return kernel if kernel <= int(max_kernel) else None


def tcn_structure_guard(kernel_size: int, num_layers: int, step_len: int) -> dict[str, Any]:
    """返回结构适配报告（写入 dl_metadata 供审计）。"""
    rf = tcn_receptive_field(kernel_size, num_layers)
    window = max(1, int(step_len))
    return {
        "kernel_size": int(kernel_size),
        "num_layers": int(num_layers),
        "receptive_field": rf,
        "step_len": window,
        "covers_window": rf >= window,
        "suggested_num_layers": None if rf >= window else min_layers_for_window(kernel_size, window),
        "suggested_kernel_size": None if rf >= window else min_kernel_for_window(num_layers, window),
    }
