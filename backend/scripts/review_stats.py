"""每日复盘统计 —— **转发到** ``backend.shared.market_breadth``（唯一事实源）。

本模块历史上是 ``market_breadth`` 的整份副本：24 个同名定义（常量、分类函数、
聚合函数）逐一对齐。2026-09-20 用 AST 逐段比对，两份除 docstring 措辞、
``dict.fromkeys`` vs 字典推导、以及一句 ``0.004 if not is_bse(x) else 0.004``
（两个分支同值）之外**零行为差异** —— 即副本从未真正分叉，只是白养了一份。

副本的危害不在于此刻不同，而在于**下一处口径改动只会改到一份**。本模块此前就带着
``TOL_SHSZ = 0.50`` 的独立字面量，而 ``market_breadth`` 的同一常量已收口到
``local_market_data.LIMIT_TOLERANCE``；再晚收口一次就是两个口径。

同时删掉原来的 ``except ImportError`` 退化路径：它把 ``limit_pct`` / ``compute_limits``
用 Python 内建 ``round()``（银行家舍入）重实现了一遍，与交易所的「四舍五入到分」
在 .005 边界上不一致 —— 拿不到权威实现时应当**响亮地失败**，而不是静默换一套
舍入规则继续出报告。

保留 ``_find_repo_root`` / sys.path 引导：本模块会被当普通模块 import
（``daily_review.py`` 的 ``import review_stats as rs``），需自行保证 ``backend`` 可导入。
"""
from __future__ import annotations

import sys
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "backend" / "main_oss.py").is_file():
            return p
    raise FileNotFoundError("未找到仓库根（含 backend/main_oss.py）")


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.services.simulation.services.local_market_data import (  # noqa: E402
    compute_limits,
    limit_pct,
)
from backend.shared.market_breadth import (  # noqa: E402
    CAT_BROKE_UP,
    CAT_CORP_ACTION,
    CAT_DOWN,
    CAT_FLAT,
    CAT_LIMIT_DOWN,
    CAT_LIMIT_UP,
    CAT_NORMAL,
    CAT_UP,
    TOL_BJ,
    TOL_SHSZ,
    breadth_distribution,
    classify_by_pct,
    classify_price,
    fmt_yi,
    is_bse_symbol,
    is_corp_action_pct,
    is_ex_div,
    limit_up_down_counts,
    market_breadth,
    price_tolerance,
    sector_aggregate,
    streak_from_tail,
    volume_ratio_5,
    wan_to_yi,
)

__all__ = [
    "CAT_BROKE_UP",
    "CAT_CORP_ACTION",
    "CAT_DOWN",
    "CAT_FLAT",
    "CAT_LIMIT_DOWN",
    "CAT_LIMIT_UP",
    "CAT_NORMAL",
    "CAT_UP",
    "TOL_BJ",
    "TOL_SHSZ",
    "breadth_distribution",
    "classify_by_pct",
    "classify_price",
    "compute_limits",
    "fmt_yi",
    "is_bse_symbol",
    "is_corp_action_pct",
    "is_ex_div",
    "limit_pct",
    "limit_up_down_counts",
    "market_breadth",
    "price_tolerance",
    "sector_aggregate",
    "streak_from_tail",
    "volume_ratio_5",
    "wan_to_yi",
]
