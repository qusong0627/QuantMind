# -*- coding: utf-8 -*-
"""今日行情缓存（V1：最新快照 + 当天 1m + Parquet part）。"""

from .config import TodayCacheConfig
from .service import TodayCacheService

__all__ = ["TodayCacheConfig", "TodayCacheService", "build_service"]


def build_service(
    config_get,
    base_dir: str,
    fetcher,
    universe_provider,
    session_provider=None,
    logger=None,
):
    """按桥配置构建并启动服务；失败返回 None（桥继续透传，不影响交易）。"""
    try:
        config = TodayCacheConfig.from_config(config_get, base_dir)
        if not config.enabled:
            if logger is not None:
                logger("today_cache disabled")
            return None
        service = TodayCacheService(
            config,
            fetcher=fetcher,
            universe_provider=universe_provider,
            session_provider=session_provider,
            logger=logger,
        )
        service.start()
        return service
    except Exception as exc:  # noqa: BLE001 - 缓存启动失败不能拖垮桥
        if logger is not None:
            logger("today_cache build failed: %r" % (exc,))
        return None
