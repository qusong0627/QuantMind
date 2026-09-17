"""异动类型契约（T-P6-14）：``qm_market_anomalies.anomaly_type`` 枚举扩展的自愈。

表由 market_analysis（API 服务）的模型定义，原 CHECK 仅含日频六类；识别引擎 v1 新增
盘中四类（价格大幅波动/账户撤单率/账户集中度/数据跳变缺口零成交/模型 IC 骤降）。
本模块以**幂等自愈**方式把约束扩到并集（存在即零 DDL 快路径；失败仅告警不抛出），
识别引擎启动时调用——与 eval_contract / risk_trigger_service 同一套三纪律。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

CONSTRAINT_NAME = "ck_qm_anomaly_type"
TABLE = "qm_market_anomalies"
RECENT_SYMBOLS_KEY = "qm:anomaly:recent_symbols"


def read_recent_anomaly_symbols(limit: int = 100) -> list[str]:
    """识别引擎近 1h 异动标的（热集构建器"异动源"唯一读取口；本机通用库 db0）。"""
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
        return sorted(client.smembers(RECENT_SYMBOLS_KEY) or [])[: max(1, int(limit))]
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

# 并集：原有六类（日频，勿删）+ 识别引擎 v1 新增
ALLOWED_ANOMALY_TYPES: tuple[str, ...] = (
    # 既有（日频 market_analysis）
    "volume_surge",
    "price_limit_up",
    "price_limit_down",
    "sector_rotation",
    "flow_reversal",
    "breadth_divergence",
    # T-P6-14 识别引擎 v1
    "price_surge",
    "account_cancel_ratio",
    "account_concentration",
    "data_jump",
    "data_gap",
    "data_zero_volume",
    "model_ic_drop",
)


def ensure_anomaly_types() -> bool:
    """幂等扩展 anomaly_type CHECK 约束（同步引擎；失败仅告警）。返回是否已满足。"""
    from sqlalchemy import text

    from backend.shared.sync_db import sync_session

    try:
        with sync_session() as session:
            row = session.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = :name"
                ),
                {"name": CONSTRAINT_NAME},
            ).fetchone()
        if row is not None:
            definition = str(row[0] or "")
            if all(f"'{value}'" in definition for value in ALLOWED_ANOMALY_TYPES):
                return True
        values = ", ".join(f"'{v}'" for v in ALLOWED_ANOMALY_TYPES)
        with sync_session() as session:
            session.execute(text("SET LOCAL lock_timeout = '3s'"))
            session.execute(text(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {CONSTRAINT_NAME}"))
            session.execute(
                text(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT_NAME} "
                     f"CHECK (anomaly_type IN ({values}))")
            )
            session.commit()
        logger.info("[AnomalyContract] anomaly_type 约束已扩展（%d 类）", len(ALLOWED_ANOMALY_TYPES))
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断主链路
        logger.warning("[AnomalyContract] 约束自愈失败（不阻断）: %s", exc)
        return False
