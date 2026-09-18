"""哨兵告警 T+1 回填（T-P6-15）：告警后标的表现 → 命中判定 → 误报率分母（不假填）。

口径（细案 §六.4，模块 docstring 与 sentinel_alert_contract 同源）：
- 入场 = 告警日（非交易日顺延到下一交易日）前复权收盘；出场 = 其后**下一交易日**收盘
  （qdb_daily_forward，QuantDBDataHub.fetch_series）；
- realized = 出场/入场 − 1；benchmark = 000300.SH 同窗；excess = realized − benchmark；
- 命中 = 方向（up/down）达标（compute_hit）；方向 none 的告警 outcome_status='not_scorable'
  不进误报率分母（如实标注）；
- 次日数据未齐 → 保持 pending（后续每日重试）；入口日缺失且超过宽限窗 → 'no_data'
  （如长期停牌/退市），**绝不编造收益**。

调度：trade 服务常驻 worker，交易日 16:10 后每日一次（Redis done 键防重跑）；
也可 CLI：``python -m backend.services.trade.services.sentinel_backfill``。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_SH_TZ = ZoneInfo("Asia/Shanghai")

BACKFILL_DONE_PREFIX = "qm:sentinel:backfill:"
GRACE_DAYS = 7  # 告警日之后超过宽限仍无行情 → no_data（停牌/退市）
BENCHMARK = "000300.SH"
# 指数标的集合（走 qdb_index_daily 且**不做** to_suffix 归一——指数码 000300 会被误判 SZ）
INDEX_SYMBOLS = {"000300.SH", "000001.SH", "000905.SH", "000016.SH", "000688.SH",
                 "399001.SZ", "399006.SZ", "399300.SZ"}


def _hub():
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    return QuantDBDataHub.get_instance()


def _dt_int(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


def load_relative_closes(
    symbol: str,
    base_date: date,
    horizons: tuple[int, ...] = (1,),
    *,
    view: str | None = None,
    normalize: bool = True,
) -> tuple[float, dict[int, float]] | None:
    """(基准收盘, {h: 第 h 个后续交易日收盘})：基准=≥base_date 首个交易日。

    指数（INDEX_SYMBOLS）走 ``qdb_index_daily`` 且不做后缀归一（000300→SZ 误判）；
    其余走 ``qdb_daily_forward`` + ``StockCodeUtil.to_suffix``。
    horizon 缺行不进 dict（渐进兑现用，如 T+5 数据未到）；基准行缺失 → None。
    **唯一实现**：哨兵 T+1 回填（h=1）与建议卡 T+1/T+3/T+5 兑现共用，禁第三份。
    """
    text = str(symbol or "").strip().upper()
    if view is None:
        view = "qdb_index_daily" if text in INDEX_SYMBOLS else "qdb_daily_forward"
        normalize = text not in INDEX_SYMBOLS
    if normalize:
        from backend.shared.stock_utils import StockCodeUtil

        text = StockCodeUtil.to_suffix(text) or text
    max_h = max(horizons) if horizons else 1
    start = base_date - timedelta(days=5)
    end = base_date + timedelta(days=max(15, 6 * max_h))
    try:
        df = _hub().fetch_series(
            view, text, _dt_int(start), _dt_int(end), columns=["close"]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[forward-prices] %s 行情读取失败: %s", symbol, exc)
        return None
    if df is None or df.empty:
        return None
    rows = df.dropna(subset=["close"]).sort_values("dt")
    entry_i = None
    base_int = _dt_int(base_date)
    for i, (_, row) in enumerate(rows.iterrows()):
        if int(row["dt"]) >= base_int:
            entry_i = i
            break
    if entry_i is None:
        return None
    entry = float(rows.iloc[entry_i]["close"])
    if entry <= 0:
        return None
    path: dict[int, float] = {}
    for h in horizons:
        idx = entry_i + h
        if idx < len(rows):
            close = float(rows.iloc[idx]["close"])
            if close > 0:
                path[int(h)] = close
    return entry, path


def load_forward_pair(
    symbol: str, alert_date: date, *, view: str | None = None, normalize: bool = True
) -> tuple[float, float] | None:
    """(入场收盘, 出场收盘)：入场=≥告警日首个交易日，出场=其下一交易日；不足两行 → None。"""
    res = load_relative_closes(
        symbol, alert_date, (1,), view=view, normalize=normalize
    )
    if res is None:
        return None
    entry, path = res
    if 1 not in path:
        return None
    return entry, path[1]


def score_row(symbol: str, alert_date: date) -> dict[str, Any] | None:
    """单标的兑现结果；None=数据未齐（保持 pending；超宽限由调用方判 no_data）。"""
    pair = load_forward_pair(symbol, alert_date)
    if pair is None:
        return None
    entry, exit_ = pair
    realized = exit_ / entry - 1.0
    benchmark = None
    bench_pair = load_forward_pair(BENCHMARK, alert_date, view="qdb_index_daily", normalize=False)
    if bench_pair is not None:
        benchmark = bench_pair[1] / bench_pair[0] - 1.0
    excess = realized - benchmark if benchmark is not None else None
    return {"realized": realized, "benchmark": benchmark, "excess": excess}


def backfill_pending(limit: int = 200, *, today: date | None = None) -> dict[str, int]:
    """回填 pending 行（trade_date < today；标的为通配 '*' 的跳过 → not_scorable）。"""
    from sqlalchemy import text

    from backend.shared.sentinel_alert_contract import (
        compute_hit,
        ensure_sentinel_alerts_table,
    )
    from backend.shared.sync_db import sync_session

    if not ensure_sentinel_alerts_table():
        return {"filled": 0, "no_data": 0, "failed": 1}
    today = today or datetime.now(_SH_TZ).date()
    stats = {"filled": 0, "no_data": 0, "skipped": 0, "failed": 0}
    with sync_session() as session:
        rows = session.execute(
            text(
                "SELECT id, symbol, trade_date, direction FROM sentinel_alerts "
                "WHERE outcome_status = 'pending' AND trade_date < :today "
                "ORDER BY trade_date ASC LIMIT :lim"
            ),
            {"today": today, "lim": int(limit)},
        ).fetchall()
        for row_id, symbol, trade_date, direction in rows:
            if not symbol or symbol == "*":
                session.execute(
                    text("UPDATE sentinel_alerts SET outcome_status='not_scorable', "
                         "outcome_checked_at=NOW() WHERE id=:i"),
                    {"i": row_id},
                )
                stats["skipped"] += 1
                continue
            scored = None
            try:
                scored = score_row(str(symbol), trade_date)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[sentinel-backfill] 评分失败 #%s %s: %s", row_id, symbol, exc)
            if scored is None:
                if (today - trade_date) > timedelta(days=GRACE_DAYS):
                    session.execute(
                        text("UPDATE sentinel_alerts SET outcome_status='no_data', "
                             "outcome_checked_at=NOW() WHERE id=:i"),
                        {"i": row_id},
                    )
                    stats["no_data"] += 1
                else:
                    stats["failed"] += 1
                continue
            hit = compute_hit(str(direction or "none"), scored["realized"])
            session.execute(
                text(
                    "UPDATE sentinel_alerts SET outcome_status=:st, outcome_checked_at=NOW(), "
                    "realized_return=:r, benchmark_return=:b, excess_return=:e, hit=:h "
                    "WHERE id=:i"
                ),
                {"st": "filled" if hit is not None else "not_scorable",
                 "r": scored["realized"], "b": scored["benchmark"], "e": scored["excess"],
                 "h": hit, "i": row_id},
            )
            stats["filled"] += 1
        session.commit()
    return stats


async def run_sentinel_backfill_worker() -> None:
    """常驻：每日 01:35 后回填一次（Redis done 键防重跑；失败不置键下轮重试）。

    时点口径（2026-09-18 调整）：T+1 退出柱为次日数据、次日 00:55 落盘——01:35 是
    最早可兑现时点；原 16:10 场次对同批数据零增益（晚 ~15h），故前移。
    """
    from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

    logger.info("[sentinel-backfill] 回填循环启动")
    while True:
        try:
            _sched_heartbeat("sentinel_backfill")
        except Exception:  # noqa: BLE001
            pass
        now = datetime.now(_SH_TZ)
        if now.weekday() < 5 and (now.hour, now.minute) >= (1, 35):
            done_key = f"{BACKFILL_DONE_PREFIX}{now.date().isoformat()}"
            try:
                from backend.shared.sync_db import sync_session  # noqa: F401
                import os

                import redis as _redis

                client = _redis.Redis(
                    host=os.getenv("REDIS_HOST") or "redis",
                    port=int(os.getenv("REDIS_PORT", "6379")),
                    password=os.getenv("REDIS_PASSWORD") or None,
                    db=int(os.getenv("REDIS_DB_GENERAL", "0")),
                    decode_responses=True,
                    socket_connect_timeout=3,
                    socket_timeout=5,
                )
                try:
                    if not client.set(done_key, "1", nx=True, ex=172800):
                        await asyncio.sleep(300)
                        continue
                finally:
                    client.close()
                stats = await asyncio.to_thread(backfill_pending)
                logger.info("[sentinel-backfill] 完成 %s", stats)
            except Exception as exc:  # noqa: BLE001 - 失败不置键，下轮重试
                logger.warning("[sentinel-backfill] 失败（下轮重试）: %s", exc)
        await asyncio.sleep(300)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    stats = backfill_pending(limit=1000)
    print(f"[sentinel-backfill] {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
