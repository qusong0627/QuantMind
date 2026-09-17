"""执行核 SPI（T-P6-17）：日频核（daily，现行为）↔ 快照级核（snapshot，F2）切换。

- 模式解析：Redis ``qm:sim:exec_core`` 的 ``mode`` 字段（前端/运维热改）> env ``SIM_EXEC_CORE``
  > 默认 ``daily``（F1 平价：默认路径行为零变化，可随时回退）；
- ``snapshot`` 模式只在**盘口新鲜且完整**时用快照核（盘口深度为准），否则**自动回退**日频核
  并把回退计数暴露在 ``snapshot_core_stats()``（P6 纪律：降级可见，不静默）；
- 快照核的成交价来源标注 ``price_source=snapshot``、``execution_model=snapshot_core``
  （交易台/影子对照据此区分保真度级别）。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

MODE_DAILY = "daily"
MODE_SNAPSHOT = "snapshot"

CONFIG_KEY = "qm:sim:exec_core"
STATUS_KEY = "qm:sim:exec_core:stats"

_stats_lock = threading.Lock()
_stats: dict[str, int] = {"snapshot_fills": 0, "fallbacks": 0, "last_ts": 0}


def _bump(key: str, delta: int = 1) -> None:
    with _stats_lock:
        _stats[key] = int(_stats.get(key, 0)) + int(delta)


def snapshot_core_stats() -> dict[str, Any]:
    with _stats_lock:
        return dict(_stats)


def resolve_exec_core() -> str:
    """当前执行核模式（Redis 热配置 > env > daily）；读取失败回落 daily（安全）。"""
    try:
        import redis as _redis

        client = _redis.Redis(
            host=os.getenv("REDIS_HOST") or "redis",
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            db=int(os.getenv("REDIS_DB_GENERAL", "0")),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=3,
        )
        try:
            raw = client.hget(CONFIG_KEY, "mode")
        finally:
            client.close()
        mode = str(raw or "").strip().lower()
        if mode in {MODE_DAILY, MODE_SNAPSHOT}:
            return mode
    except Exception:  # noqa: BLE001
        pass
    mode = str(os.getenv("SIM_EXEC_CORE", "") or "").strip().lower()
    return mode if mode in {MODE_DAILY, MODE_SNAPSHOT} else MODE_DAILY


def try_snapshot_fill(
    *,
    symbol: str,
    side: str,
    quantity: float,
    order_type: str,
    limit_price: float | None,
    lot_size: int,
) -> tuple[Any, Any]:
    """快照核尝试：返回 (BookFill | None, Book | None)；None = 回退日频核（计数可见）。"""
    from backend.services.simulation.services.snapshot_book import fetch_book, walk_book

    book = fetch_book(symbol)
    if book is None:
        _bump("fallbacks")
        return None, None
    fill = walk_book(
        side, quantity, book, order_type=order_type, limit_price=limit_price, lot_size=lot_size
    )
    if fill is None:
        # 封板排队/无对手盘：交给日频核处置（涨停可成交语义由其既有闸门裁定）
        _bump("fallbacks")
        return None, book
    _bump("snapshot_fills")
    return fill, book
