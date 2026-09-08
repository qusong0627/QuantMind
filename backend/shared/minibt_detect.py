# -*- coding: utf-8 -*-
"""minibt 策略识别（统一口径）。

minibt 脚本框架只装在 ``quantmind-minibt-runner`` 镜像里，qlib 引擎（主镜像）
没有该库。识别规则供三处共用，避免口径漂移：

- ``backend/services/engine/routers/ai_ide/executor.py``：AI-IDE 运行时切换镜像
- ``backend/services/engine/qlib_app/services/minibt_backtest_service.py``：回测中心派发
- 前端 ``electron/src/utils/minibt.ts``：同一条正则（元数据 + 代码双判）

只认行首的真实 import：注释/字符串里提到 minibt 不算，``minibt_qdb`` 这类
同前缀的后端模块也不算（``\\b`` 词界保证）。
"""
from __future__ import annotations

import re

MINIBT_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+minibt\b", re.MULTILINE)


def detect_minibt(code: str | None) -> bool:
    """策略代码是否 import 了 minibt 框架。"""
    return bool(MINIBT_IMPORT_RE.search(code or ""))
