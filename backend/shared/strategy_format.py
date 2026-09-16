"""策略代码格式（T-P3-03）：五形态识别 + 声明式骨架——**纯函数唯一实现**。

设计口径（`docs/统一交易栈_设计方案.md` §4.2）：策略库只保留两种可执行格式——
- ``STRATEGY_CONFIG`` 声明式（``params`` 格式，Redis*Strategy 类 + kwargs）；
- ``minibt`` DSL（``import minibt`` + 钩子类）。

其余三形态的处置（2026-09-16 全库实测清点）：
- ``handle_data`` 聚宽风格（initialize/handle_data）——**无执行器**：存量 0 行，
  但两处 AI 兜底生成路径会产出（strategy_service / json_utils）→ 已下架生成
  （改产声明式骨架）；
- 空/桩代码（``''`` / ``# New Strategy`` 等无入口的残壳）——按名称回填模板或列清单人工；
- 沙箱 ``on_tick`` 钩子与 AI-IDE 脚本（``__main__``/main/run）——**运行时形态**，
  不进策略库，无需转换（记录边界）。

消费方：`scripts/strategy_format_audit.py`（审计/下架清单）、
`scripts/repair_strategy_code_formats.py`（回填）、AI 兜底生成路径（骨架）。
"""

from __future__ import annotations

import re
from typing import Any

FORMAT_STRATEGY_CONFIG = "strategy_config"  # 声明式（保留）
FORMAT_MINIBT = "minibt"  # DSL（保留）
FORMAT_HANDLE_DATA = "handle_data"  # 聚宽风（下架）
FORMAT_SCRIPT = "script"  # 沙箱/AI-IDE 运行时形态（不进库）
FORMAT_EMPTY = "empty"  # 空/桩残壳

EXECUTABLE_FORMATS = frozenset({FORMAT_STRATEGY_CONFIG, FORMAT_MINIBT})

_SCRIPT_ENTRY_RE = re.compile(r"^def (main|run)\s*\(", re.MULTILINE)
_STUB_MAX_LEN = 60  # 桩壳长度阈值（如 "# New Strategy"）


def classify_strategy_code(code: Any) -> str:
    """策略代码 → 五形态之一（顺序敏感：先精确形态，后残壳兜底）。"""
    text = str(code or "")
    if not text.strip():
        return FORMAT_EMPTY
    if "STRATEGY_CONFIG" in text:
        return FORMAT_STRATEGY_CONFIG
    if "import minibt" in text or "from minibt" in text:
        return FORMAT_MINIBT
    if "handle_data" in text:
        return FORMAT_HANDLE_DATA
    if "__main__" in text or _SCRIPT_ENTRY_RE.search(text):
        return FORMAT_SCRIPT
    if "on_tick" in text:
        return FORMAT_SCRIPT  # 沙箱钩子：运行时形态
    if len(text.strip()) <= _STUB_MAX_LEN and "#" in text:
        return FORMAT_EMPTY  # "# New Strategy" 类桩壳
    return FORMAT_EMPTY


def is_executable_format(fmt: Any) -> bool:
    """是否策略库可执行格式（声明式 / minibt）。"""
    return str(fmt) in EXECUTABLE_FORMATS


def build_scaffold_strategy_code(description: str) -> str:
    """AI 兜底声明式骨架（替换原 handle_data 伪代码——那形态无执行器）。

    产出合法 STRATEGY_CONFIG（TopK 默认参数），docstring 明确标注为占位骨架，
    用户/AI 应补全参数或重新生成；保证"落库即可回测"的格式契约。
    """
    desc = str(description or "").strip()[:120] or "未命名"
    desc = desc.replace('"', "").replace("'", "").replace("\n", " ")
    return (
        "# -*- coding: utf-8 -*-\n"
        f'"""AI 生成占位骨架：{desc}\n\n'
        "[Native · A股] 声明式骨架（模型未返回可用代码时的兜底）——默认 TopK 选股，\n"
        "未含专属因子定制；请补全参数或重新生成后再回测/上实盘。\n"
        '"""\n'
        "STRATEGY_CONFIG = {\n"
        '    "class": "RedisRecordingStrategy",\n'
        '    "module_path": "backend.services.engine.qlib_app.utils.recording_strategy",\n'
        '    "kwargs": {\n'
        '        "signal": "<PRED>",\n'
        '        "topk": 30,\n'
        '        "n_drop": 6,\n'
        '        "rebalance_days": 5,\n'
        "    },\n"
        "}\n"
    )
