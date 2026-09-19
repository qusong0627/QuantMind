"""建议卡兑现回填（T-P6-16 闭环补全）：决策 → 结果 → 成功率回填。

口径（机构，与哨兵 T+1 回填同源共用取价唯一实现 ``load_relative_closes``）：
- 兑现基准 = **决策日**（decided_at 上海日）前复权收盘——决策发生在当晚/盘后，该收盘
  已公开，无未来函数；
- 每个动作按方向测"建议质量"：buy → 实际收益；sell → 规避收益（价格下跌为正）；
- 超额 = 方向调整后收益 − 沪深300（000300.SH）同期；hit = 超额 > 0；
- 渐进兑现：T+1 先落，T+3/T+5 数据到齐再补；全部 horizon 齐 → done，部分 → partial；
  超宽限窗（``GRACE_DAYS``）仍无基准行 → no_data（长期停牌/退市），**绝不编造收益**；
- 纯建议卡（无动作，如纪律卡）→ not_scorable（过程价值不做 P&L 兑现，如实标注）；
- **拒绝的卡同样兑现**（反事实：拒绝对了还是错失了），executed/rejected 平行进统计。

调度：trade 服务常驻 worker，交易日 16:15 后每日一次（Redis done 键防重跑）；
也可 CLI：``python -m backend.services.trade.services.advice_backfill``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from backend.shared.benchmark import BENCHMARK_SYMBOL

logger = logging.getLogger(__name__)
_SH_TZ = ZoneInfo("Asia/Shanghai")

HORIZONS: tuple[int, ...] = (1, 3, 5)
BENCHMARK = BENCHMARK_SYMBOL
GRACE_DAYS = 10  # 决策日之后超过宽限仍无基准行 → no_data
BACKFILL_DONE_PREFIX = "trade:advice-backfill:done:"

_DECIDED_STATUSES = ("executed", "partial", "failed", "rejected")


def score_actions(
    actions: list[dict[str, Any]], base_date: date
) -> dict[str, Any] | None:
    """逐动作兑现 → outcome 文档；任何 horizon 都算不出 → None（保持 pending 重试）。

    纯函数（价格经 load_relative_closes 注入式读取），单测直接喂 monkeypatch。
    """
    from backend.services.trade.services.sentinel_backfill import load_relative_closes

    bench_entry: float | None = None
    bench_path: dict[int, float] = {}
    bench_res = load_relative_closes(
        BENCHMARK, base_date, HORIZONS, view="qdb_index_daily", normalize=False
    )
    if bench_res is not None:
        bench_entry, bench_path = bench_res

    scored: list[dict[str, Any]] = []
    any_progress = False
    for action in actions:
        symbol = str(action.get("symbol") or "").strip()
        side = str(action.get("side") or "").strip().lower()
        res = load_relative_closes(symbol, base_date, HORIZONS)
        if res is None:
            scored.append({"symbol": symbol, "side": side, "error": "no_base_close"})
            continue
        entry, path = res
        horizons: dict[str, dict[str, Any]] = {}
        for h, close in sorted(path.items()):
            ret = close / entry - 1.0
            if side == "sell":
                ret = -ret  # 卖出测"规避收益"：价格跌为正
            bench = None
            if bench_entry and h in bench_path:
                bench = bench_path[h] / bench_entry - 1.0
            excess = (ret - bench) if bench is not None else None
            horizons[str(h)] = {
                "close": round(close, 4),
                "ret": round(ret, 6),
                "bench": round(bench, 6) if bench is not None else None,
                "excess": round(excess, 6) if excess is not None else None,
                "hit": (excess > 0) if excess is not None else None,
            }
        if horizons:
            any_progress = True
        scored.append(
            {
                "symbol": symbol,
                "side": side,
                "entry": round(entry, 4),
                "horizons": horizons,
            }
        )
    if not any_progress:
        return None

    summary: dict[str, dict[str, Any]] = {}
    for h in HORIZONS:
        entries = [
            a["horizons"][str(h)]
            for a in scored
            if str(h) in (a.get("horizons") or {})
        ]
        excesses = [e["excess"] for e in entries if e.get("excess") is not None]
        hits = [e for e in entries if e.get("hit")]
        summary[str(h)] = {
            "n": len(entries),
            "hits": len(hits),
            "avg_excess": (
                round(sum(excesses) / len(excesses), 6) if excesses else None
            ),
        }
    return {
        "base_date": base_date.isoformat(),
        "benchmark": BENCHMARK,
        "actions": scored,
        "summary": summary,
        "note": "决策日收盘→第 h 交易日收盘；卖出取规避收益；超额=个股−沪深300",
    }


def _all_horizons_present(doc: dict[str, Any]) -> bool:
    acted = [a for a in doc.get("actions") or [] if a.get("horizons")]
    if not acted:
        return False
    return all(
        all(str(h) in a["horizons"] for h in HORIZONS) for a in acted
    )


def backfill_once(limit: int = 200, *, today: date | None = None) -> dict[str, int]:
    """回填已决策建议卡（executed/partial/failed/rejected & outcome pending/partial）。"""
    from sqlalchemy import text

    from backend.shared.copilot_contract import ensure_copilot_advice_table
    from backend.shared.sync_db import sync_session

    if not ensure_copilot_advice_table():
        return {"filled": 0, "updated": 0, "failed": 1}
    today = today or datetime.now(_SH_TZ).date()
    stats = {
        "filled": 0,       # 全 horizon 齐（done）
        "updated": 0,      # 部分 horizon（partial）
        "not_scorable": 0,  # 纯建议卡
        "no_data": 0,      # 超宽限无行情
        "waiting": 0,      # 数据未齐，下轮重试
        "failed": 0,
    }
    with sync_session() as session:
        rows = session.execute(
            text(
                "SELECT advice_id::text, actions, decided_at "
                "FROM copilot_advice "
                "WHERE status = ANY(:sts) AND decided_at IS NOT NULL "
                "AND outcome_status IN ('pending','partial') "
                "ORDER BY decided_at ASC LIMIT :lim"
            ),
            {"sts": list(_DECIDED_STATUSES), "lim": int(limit)},
        ).fetchall()
        for advice_id, actions_raw, decided_at in rows:
            actions = (
                actions_raw
                if isinstance(actions_raw, list)
                else json.loads(actions_raw or "[]")
            )
            if not actions:
                session.execute(
                    text(
                        "UPDATE copilot_advice SET outcome_status='not_scorable', "
                        "outcome_checked_at=NOW() WHERE advice_id = CAST(:a AS UUID)"
                    ),
                    {"a": advice_id},
                )
                stats["not_scorable"] += 1
                continue
            base_date = (
                decided_at.astimezone(_SH_TZ).date()
                if decided_at.tzinfo is not None
                else decided_at.date()
            )
            try:
                doc = score_actions(actions, base_date)
            except Exception as exc:  # noqa: BLE001 - 单卡失败不拖垮批
                logger.warning("[advice-backfill] %s 评分失败: %s", advice_id, exc)
                doc = None
            if doc is None:
                if (today - base_date) > timedelta(days=GRACE_DAYS):
                    session.execute(
                        text(
                            "UPDATE copilot_advice SET outcome_status='no_data', "
                            "outcome_checked_at=NOW() WHERE advice_id = CAST(:a AS UUID)"
                        ),
                        {"a": advice_id},
                    )
                    stats["no_data"] += 1
                else:
                    stats["waiting"] += 1
                continue
            done = _all_horizons_present(doc)
            session.execute(
                text(
                    "UPDATE copilot_advice SET outcome = CAST(:o AS JSONB), "
                    "outcome_status = :st, outcome_checked_at = NOW() "
                    "WHERE advice_id = CAST(:a AS UUID)"
                ),
                {
                    "o": json.dumps(doc, ensure_ascii=False, default=str),
                    "st": "done" if done else "partial",
                    "a": advice_id,
                },
            )
            stats["filled" if done else "updated"] += 1
        session.commit()
    return stats


async def run_advice_backfill_worker() -> None:
    """常驻：每日 01:40 后回填一次（Redis done 键防重跑；失败不置键下轮重试）。

    时点口径（2026-09-18 调整）：horizon 收盘柱次日 00:55 落盘——01:40 为最早可兑现
    时点；原 16:15 场次对同批数据零增益，故前移。
    """
    from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

    logger.info("[advice-backfill] 回填循环启动")
    while True:
        try:
            _sched_heartbeat("advice_backfill")
        except Exception:  # noqa: BLE001
            pass
        now = datetime.now(_SH_TZ)
        if now.weekday() < 5 and (now.hour, now.minute) >= (1, 40):
            done_key = f"{BACKFILL_DONE_PREFIX}{now.date().isoformat()}"
            try:
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
                stats = await asyncio.to_thread(backfill_once)
                logger.info("[advice-backfill] 完成 %s", stats)
            except Exception as exc:  # noqa: BLE001 - 失败不置键，下轮重试
                logger.warning("[advice-backfill] 失败（下轮重试）: %s", exc)
        await asyncio.sleep(300)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    stats = backfill_once(limit=1000)
    print(f"[advice-backfill] {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
