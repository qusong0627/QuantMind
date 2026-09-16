"""热集合成（T-P6-06 纯函数）：持仓 ∪ 异动 ∪ 候选 ∪ 常驻指数 → 去重/优先级/截断。

规则：
- **优先级**：持仓 > 异动 > 候选（截断只吃低优先级尾部）；组内保序（调用方给定序）；
- **常驻指数（T-P6-13）**：``indexes`` 组不占 cap 配额、永不截断（regime 服务依赖其行情），
  与股票池去重后追加尾部；指数采用指数代码口径（如 ``000300.SH``）；
- **归一**：一律规范为 Qlib 后缀式（``600036.SH``）；非沪深北（如港股/美股/非法码）跳过并计数
  （当前订阅通道为 A 股口径，跨市场属后续）；
- 输出含显式统计（total/kept/truncated/skipped/indexes），超限如实记录不静默。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_SUFFIX_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
DEFAULT_CAP = 2000
REGIME_INDEXES: tuple[str, ...] = ("000300.SH", "000001.SH")  # 沪深300 + 上证指数（regime 常驻源）


def normalize_hot_symbol(symbol: object) -> str | None:
    """任意形态 → 后缀式（``600036.SH``）；非沪深北/非法 → None。"""
    try:
        from backend.shared.stock_utils import StockCodeUtil

        suffix = str(StockCodeUtil.to_suffix(str(symbol or "").strip()) or "").strip().upper()
    except Exception:  # noqa: BLE001
        return None
    return suffix if _SUFFIX_RE.match(suffix) else None


def compose_hot_set(
    *,
    positions: Iterable[object],
    anomalies: Iterable[object],
    candidates: Iterable[object],
    indexes: Iterable[object] = (),
    cap: int = DEFAULT_CAP,
) -> dict[str, Any]:
    """合成热集：返回 ``{"symbols": [...], "stats": {...}}``。

    ``indexes``（T-P6-13）：常驻指数源——**不占 cap、永不截断**，去重后追加在股票池尾部。
    """
    cap = max(0, int(cap))
    seen: set[str] = set()
    ordered: list[str] = []
    total = skipped = 0
    for group in (positions, anomalies, candidates):
        for item in group:
            total += 1
            norm = normalize_hot_symbol(item)
            if norm is None:
                skipped += 1
                continue
            if norm in seen:
                continue
            seen.add(norm)
            ordered.append(norm)
    truncated = max(0, len(ordered) - cap)
    kept = ordered[:cap]
    index_kept: list[str] = []
    for item in indexes:
        total += 1
        norm = normalize_hot_symbol(item)
        if norm is None:
            skipped += 1
            continue
        if norm in seen:
            continue
        seen.add(norm)
        index_kept.append(norm)
    return {
        "symbols": kept + index_kept,
        "stats": {
            "total": total,
            "unique": len(ordered) + len(index_kept),
            "kept": len(kept) + len(index_kept),
            "truncated": truncated,
            "skipped": skipped,
            "indexes": len(index_kept),
        },
    }
