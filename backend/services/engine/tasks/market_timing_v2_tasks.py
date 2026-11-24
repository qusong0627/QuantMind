"""机构择时 v2 盘后快照（影子盘）。

交易日 17:10 运行：重建市场面板 → 加载 TFT 聚合分 → walk-forward/最新 MLP →
写 ``/data/market_timing_v2/latest.json``，供 ``/api/v1/market-analysis/timing-v2``
与「大盘分析 → 机构择时」页签读取。

只写文件，不改交易/账本；失败不阻断其他盘后任务。
"""

from __future__ import annotations

import logging

from backend.services.engine.qlib_app.celery_config import celery_app
from backend.shared.market_timing_v2.auto_inference import refresh_snapshot

logger = logging.getLogger(__name__)


@celery_app.task(
    name="engine.tasks.market_timing_v2_snapshot",
    bind=True,
    max_retries=1,
)
def market_timing_v2_snapshot(self) -> dict:
    try:
        snap = refresh_snapshot(force=True)
        logger.info(
            "[market-timing-v2] status=%s as_of=%s tft=%s mlp=%s exposure=%s",
            snap.get("status"),
            snap.get("as_of"),
            snap.get("tft_as_of"),
            snap.get("mlp_as_of"),
            (snap.get("signal") or {}).get("exposure"),
        )
        return {
            "status": snap.get("status"),
            "as_of": snap.get("as_of"),
            "tft_as_of": snap.get("tft_as_of"),
            "mlp_as_of": snap.get("mlp_as_of"),
            "exposure": (snap.get("signal") or {}).get("exposure"),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("[market-timing-v2] snapshot failed: %s", exc)
        raise self.retry(exc=exc, countdown=300) from exc
