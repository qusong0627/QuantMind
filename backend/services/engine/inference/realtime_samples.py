"""盘中分数时序采样（T-RT-05）：实时推理每 N 秒落一次逐标的分，供事后评估。

**为什么需要它**：`engine_signal_scores` 每标的每日只保留最后写入的一行（实时 upsert
覆盖），Redis 台账只存 ``scores_digest`` 与均值——两者都无法回答「盘中分是否比批量分
更有用」。本表把时间维度补上：同一交易日、同一标的、同一变体（生产/影子）按采样时刻
各存一行，直接可算 IC / 分组收益 / 盘中分漂移。

**写入纪律**：
- 只在发布成功后采样（被闸门拦下的周期没有分数可采）；
- 单次写入 = 一条多行 INSERT（``executemany``），597 行一次往返；
- 唯一键 ``(sample_ts, symbol, model_version, variant)`` + ``DO NOTHING``——重放幂等；
- 采样失败只计数（``sample_errors``），绝不影响推理主循环；
- 保留期 ``QM_REALTIME_SAMPLE_KEEP_DAYS``（默认 180 天），每进程每天最多清理一次。

表结构自愈（``CREATE TABLE IF NOT EXISTS``），不依赖升级脚本——与 Signal 契约列
（``ensure_signal_contract_columns_async``）同一做法。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)

SAMPLE_TABLE = "engine_realtime_score_samples"
DEFAULT_KEEP_DAYS = 180

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {SAMPLE_TABLE} (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT NOT NULL,
    trade_date      DATE NOT NULL,
    sample_ts       TIMESTAMPTZ NOT NULL,
    symbol          TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    feature_version TEXT NOT NULL DEFAULT '',
    variant         TEXT NOT NULL DEFAULT 'production',
    score           DOUBLE PRECISION NOT NULL,
    score_rank      INTEGER,
    rank_pct        DOUBLE PRECISION,
    live_coverage   REAL,
    degraded_level  INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_CREATE_INDEX_SQL = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS uq_erss_sample_symbol_model_variant "
    f"ON {SAMPLE_TABLE} (sample_ts, symbol, model_version, variant)",
    f"CREATE INDEX IF NOT EXISTS idx_erss_date_variant "
    f"ON {SAMPLE_TABLE} (trade_date, variant)",
    f"CREATE INDEX IF NOT EXISTS idx_erss_symbol_ts "
    f"ON {SAMPLE_TABLE} (symbol, sample_ts DESC)",
)

_INSERT_SQL = text(
    f"""
    INSERT INTO {SAMPLE_TABLE} (
        run_id, trade_date, sample_ts, symbol, model_version, feature_version,
        variant, score, score_rank, rank_pct, live_coverage, degraded_level
    ) VALUES (
        :run_id, :trade_date, :sample_ts, :symbol, :model_version, :feature_version,
        :variant, :score, :score_rank, :rank_pct, :live_coverage, :degraded_level
    )
    ON CONFLICT (sample_ts, symbol, model_version, variant) DO NOTHING
    """
)

_ensured = False
_pruned_day: date | None = None


def keep_days() -> int:
    """保留天数（env 唯一读取点；非法值回落到默认）。"""
    try:
        return max(1, int(os.getenv("QM_REALTIME_SAMPLE_KEEP_DAYS", DEFAULT_KEEP_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_KEEP_DAYS


def build_sample_rows(
    *,
    run_id: str,
    trade_date: date,
    sample_ts: datetime,
    model_version: str,
    feature_version: str,
    variant: str,
    items: Iterable[dict[str, Any]],
    live_coverage: float | None,
    degraded_level: int | None,
) -> list[dict[str, Any]]:
    """载荷条目 → 采样行（纯函数，便于单测）。

    ``items`` 每项至少含 ``symbol`` 与 ``fusion_score``；``score_rank`` 可缺省。
    ``rank_pct`` 按 Signal 契约同一公式现算（与写入 ``engine_signal_scores`` 的值一致）。
    """
    from backend.shared.signal_contract import compute_rank_pct

    rows = list(items)
    scores = [float(item["fusion_score"]) for item in rows]
    pcts = compute_rank_pct(scores)
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(rows):
        out.append(
            {
                "run_id": run_id,
                "trade_date": trade_date,
                "sample_ts": sample_ts,
                "symbol": str(item["symbol"]),
                "model_version": model_version,
                "feature_version": feature_version or "",
                "variant": variant,
                "score": scores[idx],
                "score_rank": item.get("score_rank"),
                "rank_pct": pcts[idx],
                "live_coverage": live_coverage,
                "degraded_level": degraded_level,
            }
        )
    return out


async def ensure_sample_table(db: Any) -> None:
    """建表 + 唯一索引（进程内一次；失败向上抛，由调用方计数）。"""
    global _ensured
    if _ensured:
        return
    await db.execute(text(_CREATE_TABLE_SQL))
    for sql in _CREATE_INDEX_SQL:
        await db.execute(text(sql))
    _ensured = True


async def _prune_old(db: Any) -> None:
    """按保留期清理（每天一次）。失败只告警——采样是观测面，不是主链路。"""
    global _pruned_day
    today = datetime.now(tz=timezone.utc).date()
    if _pruned_day == today:
        return
    try:
        await db.execute(
            text(
                f"DELETE FROM {SAMPLE_TABLE} "
                "WHERE sample_ts < NOW() - make_interval(days => :days)"
            ),
            {"days": keep_days()},
        )
        _pruned_day = today
    except Exception as exc:  # noqa: BLE001 - 清理失败不影响写入
        logger.warning("[realtime-samples] 保留期清理失败: %s", exc)


async def write_score_samples(
    *,
    run_id: str,
    trade_date: date,
    sample_ts: datetime,
    model_version: str,
    feature_version: str,
    variant: str,
    items: Iterable[dict[str, Any]],
    live_coverage: float | None = None,
    degraded_level: int | None = None,
) -> int:
    """写入一批采样行；返回尝试写入的行数（冲突跳过也算已尝试）。"""
    rows = build_sample_rows(
        run_id=run_id,
        trade_date=trade_date,
        sample_ts=sample_ts,
        model_version=model_version,
        feature_version=feature_version,
        variant=variant,
        items=items,
        live_coverage=live_coverage,
        degraded_level=degraded_level,
    )
    if not rows:
        return 0
    async with get_session(read_only=False) as db:
        await ensure_sample_table(db)
        await db.execute(_INSERT_SQL, rows)
        await _prune_old(db)
    return len(rows)
