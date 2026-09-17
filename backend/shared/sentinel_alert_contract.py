"""哨兵告警留痕契约（T-P6-15）：表 DDL + 自愈 + 命中口径（口径单源，纯函数可测）。

**误报率口径（细案 §六.4，首版）**：告警后 T+1 标的表现未朝告警方向走 = 误报；
人工标注可修正（true_positive/false_positive 覆盖自动判定）。
- 方向：由告警类型映射（新闻风险/利空 → down；利好/涨停/大涨 → up；量能类 → none 不可评分）；
- T+1 收益：告警日收盘 → 次日收盘（qdb_daily_forward 前复权）；基准 000300.SH 同窗（存参）；
- 命中：down → realized < 0；up → realized > 0；|realized| 打平（=0）不计命中；
- 无数据（次日未收盘/停牌无价）→ outcome_status='no_data'，**不假填**、不进误报率分母。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

TABLE = "sentinel_alerts"
BENCHMARK_SYMBOL = "000300.SH"

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id                BIGSERIAL PRIMARY KEY,
    alert_id          UUID NOT NULL DEFAULT gen_random_uuid(),
    dedupe_key        VARCHAR(64) NOT NULL UNIQUE,
    tenant_id         VARCHAR(64) NOT NULL DEFAULT 'default',
    ts                TIMESTAMPTZ NOT NULL,
    trade_date        DATE NOT NULL,
    market            VARCHAR(16) NOT NULL DEFAULT 'CN',
    symbol            VARCHAR(32) NOT NULL DEFAULT '*',
    targets           JSONB NOT NULL DEFAULT '[]',
    alert_type        VARCHAR(64) NOT NULL,
    severity          VARCHAR(16) NOT NULL,
    source            VARCHAR(64) NOT NULL,
    title             VARCHAR(256) NOT NULL,
    detail            JSONB NOT NULL DEFAULT '{{}}',
    direction         VARCHAR(8) NOT NULL DEFAULT 'none',
    pushed            BOOLEAN NOT NULL DEFAULT FALSE,
    push_reason       VARCHAR(32),
    outcome_status    VARCHAR(16) NOT NULL DEFAULT 'pending',
    outcome_checked_at TIMESTAMPTZ,
    realized_return   DOUBLE PRECISION,
    benchmark_return  DOUBLE PRECISION,
    excess_return     DOUBLE PRECISION,
    hit               BOOLEAN,
    annotation        VARCHAR(16),
    annotated_by      INTEGER,
    annotated_at      TIMESTAMPTZ,
    annotation_note   TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sentinel_alerts_date ON {TABLE} (trade_date, severity);
CREATE INDEX IF NOT EXISTS idx_sentinel_alerts_symbol ON {TABLE} (symbol, trade_date);
CREATE INDEX IF NOT EXISTS idx_sentinel_alerts_outcome ON {TABLE} (outcome_status);
"""

_CANCEL_LIKE = {"cancel", "canceled", "cancelled", "撤单"}


def ensure_sentinel_alerts_table() -> bool:
    """幂等建表（存在即零 DDL 快路径；失败仅告警不抛出）。"""
    from sqlalchemy import text

    from backend.shared.sync_db import sync_session

    try:
        with sync_session() as session:
            exists = session.execute(
                text("SELECT to_regclass(:t)"), {"t": f"public.{TABLE}"}
            ).scalar()
        if exists is not None:
            return True
        with sync_session() as session:
            session.execute(text("SET LOCAL lock_timeout = '3s'"))
            for statement in _CREATE_SQL.strip().split(";\n"):
                if statement.strip():
                    session.execute(text(statement))
            session.commit()
        logger.info("[SentinelContract] %s 表已创建", TABLE)
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断业务
        logger.warning("[SentinelContract] 自愈建表失败（不阻断）: %s", exc)
        return False


def alert_direction(alert_type: str, payload: Mapping[str, Any] | None = None) -> str:
    """告警方向（up/down/none）：T+1 命中判定用；none = 不可评分（只留痕）。"""
    payload = payload or {}
    kind = str(payload.get("kind") or "").strip().lower()
    atype = str(alert_type or "").strip().lower()
    if atype.startswith("news"):
        if kind in {"risk_event", "negative"}:
            return "down"
        if kind == "positive":
            return "up"
        return "none"
    if atype.startswith("anomaly"):
        if kind in {"price_limit_down", "data_jump", "data_gap", "model_ic_drop",
                    "account_cancel_ratio", "account_concentration"}:
            return "down"
        if kind in {"price_limit_up", "price_surge"}:
            return "up"
        return "none"
    if atype.startswith("regime"):
        state = str(payload.get("state") or payload.get("regime") or "").strip().lower()
        if state in {"bear", "down"}:
            return "down"
        if state in {"bull", "up"}:
            return "up"
        return "none"
    return "none"


def compute_hit(direction: str, realized_return: float | None) -> bool | None:
    """命中判定（口径见模块 docstring）；不可评分/无数据 → None。"""
    if direction not in {"up", "down"} or realized_return is None:
        return None
    if direction == "down":
        return bool(realized_return < 0)
    return bool(realized_return > 0)


def make_dedupe_key(
    *, source: str, alert_type: str, symbol: str, trade_date: str, title_hash: str = ""
) -> str:
    """幂等键（同来源/类型/标的/日 + 标题哈希去重）。"""
    raw = f"{source}|{alert_type}|{symbol}|{trade_date}|{title_hash}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
