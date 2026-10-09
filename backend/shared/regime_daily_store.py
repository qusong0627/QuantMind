"""市场状态日表（P3 · 设计 §6.2）：``qm_regime_daily`` 唯一存储层。

**生效日口径**：行 ``(market, trade_date)`` 表示该交易日**开盘前即可确定**的状态——
由截至前一交易日的行情按 ``market_regime.build_state_rows``（i→i+1 标注）算出。
日更任务每日重算最近 K 个生效日，把迟到数据补进「尚未定型」的尾部；
历史行一经写入**不得事后重算覆盖**：``INSERT … ON CONFLICT DO NOTHING``（冻结），
重跑同一批生效日 = 0 新行，天然幂等；改口径 = 换 ``thresholds_hash`` 向前生效，
存量行以 ``computed_at`` 留痕做审计。

- 老库自愈：api 启动期 ``ensure_tables()``（本文件 ``_DDL_STATEMENTS`` 为唯一权威）；
- 全新安装：``backend/shared/db_init.sql`` 逐字镜像 + ``data/upgrade_v1.1.5.sql``；
  两份镜像由 ``backend/tests/test_regime_daily_persist.py`` 的漂移测试守着。
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.market_regime import STATES

_DDL_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS qm_regime_daily (
        market          VARCHAR(16) NOT NULL,
        trade_date      DATE        NOT NULL,
        state           VARCHAR(16) NOT NULL,
        ret_window      DOUBLE PRECISION,
        vol_window      DOUBLE PRECISION,
        volume_ratio    DOUBLE PRECISION,
        thresholds_hash VARCHAR(64),
        computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (market, trade_date),
        CONSTRAINT ck_qm_regime_daily_state CHECK (state IN ('bull', 'neutral', 'bear'))
    )
    """,
)

_INSERT_SQL = """
INSERT INTO qm_regime_daily
    (market, trade_date, state, ret_window, vol_window, volume_ratio, thresholds_hash)
VALUES
    (:market, :trade_date, :state, :ret_window, :vol_window, :volume_ratio, :thresholds_hash)
ON CONFLICT (market, trade_date) DO NOTHING
"""

_SELECT_SQL = """
SELECT trade_date, state FROM qm_regime_daily
WHERE market = :market
"""

_SELECT_STATE_SQL = """
SELECT state FROM qm_regime_daily
WHERE market = :market AND trade_date = :trade_date
"""


async def ensure_tables() -> None:
    """启动期自愈（老库补表）；与 db_init.sql 的镜像由漂移测试守着。"""
    async with get_session() as session:
        for statement in _DDL_STATEMENTS:
            await session.execute(text(statement))


def _as_date(value: Any) -> date:
    """SQL 参数边界：asyncpg 的 DATE 编解码器只收 ``date``（str 会 DataError）。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _validate_row(market: str, row: dict[str, Any]) -> dict[str, Any]:
    """边界校验：坏行进不了库（宁缺勿假）。"""
    effective_date = row.get("effective_date")
    if not effective_date or not isinstance(effective_date, str):
        raise ValueError(f"regime 行缺有效日期（effective_date）：{row!r}")
    state = row.get("state")
    if state not in STATES:
        raise ValueError(f"regime 状态非法（{state!r} 不在 {STATES}）：{row!r}")

    def _num(key: str) -> float | None:
        value = row.get(key)
        if value is None:
            return None
        try:
            out = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"regime 行 {key} 非数值：{row!r}") from exc
        return None if math.isnan(out) or math.isinf(out) else out

    return {
        "market": market,
        "trade_date": _as_date(effective_date),
        "state": state,
        "ret_window": _num("ret_window"),
        "vol_window": _num("vol_window"),
        "volume_ratio": _num("volume_ratio"),
    }


async def insert_frozen_rows(
    market: str,
    rows: list[dict[str, Any]],
    *,
    thresholds_hash: str | None = None,
) -> dict[str, int]:
    """冻结写入：冲突行**不覆盖**。返回 {inserted, existed, total}。

    rows 取自 ``market_regime.build_state_rows``（含 effective_date/state/三输入统计）。
    单事务：任一行校验失败整体不落库（边界校验在 ``_validate_row``）。
    """
    if not market:
        raise ValueError("market 不能为空")
    payload = [_validate_row(market, row) for row in rows]
    inserted = existed = 0
    async with get_session() as session:
        for params in payload:
            params["thresholds_hash"] = thresholds_hash
            result = await session.execute(text(_INSERT_SQL), params)
            if int(result.rowcount or 0) > 0:
                inserted += 1
            else:
                existed += 1
    return {"inserted": inserted, "existed": existed, "total": len(payload)}


def _date_str(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _rows_to_map(rows: list[Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in rows:
        mapping = row._mapping if hasattr(row, "_mapping") else row
        out[_date_str(mapping["trade_date"])] = str(mapping["state"])
    return out


async def load_states(
    market: str,
    *,
    since: str | date | None = None,
    until: str | date | None = None,
) -> dict[str, str]:
    """读侧（异步）：{生效日 'YYYY-MM-DD' → 状态}，升序；缺行为准（不补假值）。"""
    sql = _SELECT_SQL
    params: dict[str, Any] = {"market": market}
    if since is not None:
        sql += " AND trade_date >= :since"
        params["since"] = _as_date(since)
    if until is not None:
        sql += " AND trade_date <= :until"
        params["until"] = _as_date(until)
    sql += " ORDER BY trade_date"
    async with get_session(read_only=True) as session:
        result = await session.execute(text(sql), params)
        return _rows_to_map(list(result.fetchall()))


def load_states_sync(
    market: str,
    *,
    since: str | date | None = None,
    until: str | date | None = None,
) -> dict[str, str]:
    """读侧（同步，psycopg2 池）：异常引擎等同步链路用；语义与 ``load_states`` 一致。"""
    from backend.shared.database_pool import get_db

    sql = _SELECT_SQL
    params: dict[str, Any] = {"market": market}
    if since is not None:
        sql += " AND trade_date >= :since"
        params["since"] = _as_date(since)
    if until is not None:
        sql += " AND trade_date <= :until"
        params["until"] = _as_date(until)
    sql += " ORDER BY trade_date"
    with get_db() as session:
        result = session.execute(text(sql), params)
        return _rows_to_map(list(result.fetchall()))


def load_day_state(session: Any, market: str, trade_date: str | date) -> str | None:
    """单日状态（同步；复用调用方会话——推理写库事务内，不为一行读另开连接）。

    §6.4：``engine_signal_scores.regime`` 填当日生效值用。缺行 → None（不补假值）。
    """
    result = session.execute(
        text(_SELECT_STATE_SQL),
        {"market": market, "trade_date": _as_date(trade_date)},
    )
    row = result.fetchone()
    if not row or row[0] is None:
        return None
    return str(row[0])


async def load_day_state_async(
    session: Any, market: str, trade_date: str | date
) -> str | None:
    """单日状态（异步；复用调用方会话——realtime signal 写库事务内）。

    语义与 :func:`load_day_state` 一致：缺行 → None（不补假值）。
    """
    result = await session.execute(
        text(_SELECT_STATE_SQL),
        {"market": market, "trade_date": _as_date(trade_date)},
    )
    row = result.fetchone()
    if not row or row[0] is None:
        return None
    return str(row[0])
