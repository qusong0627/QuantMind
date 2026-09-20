"""每日选股回填（设计 §1.4）：把前向窗口已闭合的历史快照重跑一遍。

**为什么必须自动回填**：`daily_selection` 的事后验证维是 T+1..T+H 的真实超额，
评分当天数据还没发生 → 只能落一条 `pending †`。没有回填，这些天会**永久停在
pending**（评估中心的每日选股榜挂着一排待回填）。本模块把最近 N 天的快照扫一遍，
挑出「待回填 ∧ 窗口已闭合」的重跑；`save_eval_score` 按
``(object_type, object_id, snapshot_date, tenant_id, user_id)`` 幂等 upsert，天然覆盖。

**三条纪律**：

1. **窗口没闭合不跑**：`daily_selection._forward_return` 在 bar 不够时会拿最后一根
   bar 顶替 —— 早跑一次就把 T+5 悄悄读成 T+2 的收益（且会落库覆盖）。等不到就等；
2. **键形按原行走**：唯一键含 `user_id`，原行是 ``''`` 时换一个 user 重跑只会**新增
   一行**而不是覆盖（榜单同一天两条）。所以回填必须沿用原行的 `tenant_id/user_id`；
3. **空转要说清是哪一种空**：没有可回填日期时如实区分「都已回填」与「窗口都没闭合」，
   不许笼统报「完成」。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from collections.abc import Awaitable, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.benchmark import BENCHMARK_SYMBOL  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_HORIZON = 5
# 回看窗口（自然日）：需覆盖 horizon(5) + 节假日 + 基准数据的落库延迟
DEFAULT_LOOKBACK_DAYS = 21
# 基准前复权 bar 的数据集视图（与 daily_selection 取前向收益**同一个源**——
# 口径不一致会出现「闸门说闭合了、取数却取不到」）
INDEX_VIEW = "qdb_index_daily"


# ── 纯函数（可单测） ─────────────────────────────────────────────────


def is_pending(dimensions: Any) -> bool:
    """该快照的 realized 维是否仍待前向数据（`pending †`）。

    只有**维度在、分数为 None** 才算待回填：整维缺失是另一回事（重跑也长不出来）。
    """
    if not isinstance(dimensions, dict):
        return False
    dim = dimensions.get("realized")
    if not isinstance(dim, dict):
        return False
    return dim.get("score") is None


def normalize_date(value: Any) -> str | None:
    """日期归一为 ISO ``YYYY-MM-DD``；认不出的形态返回 None。

    这条链两边来自不同源：快照侧是 ``date.isoformat()``（``2026-09-18``），
    基准侧是 QuantDB ``dt`` 列 ``astype(str)``（``20260918``）。**不归一直接比
    字符串**会静默判错——``"20260831" > "2026-09-18"`` 为真（第 5 位 ``0`` > ``-``），
    于是每一根 bar 都被算成「在候选日之后」，闭窗闸门对所有日期放行，回填就在
    数据缺口上跑出一堆短窗口假收益（实测踩到：15 根 bar 被算成「09-18 之后 15 根」）。
    """
    s = str(value or "").strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    digits = s[:8]
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return None


def count_bars_after(bar_dates: list[str], candidates: list[str]) -> dict[str, int]:
    """候选日 → 该日**之后**（不含当日）的 bar 根数。

    前向是 T+1..T+H：候选日当天的 bar 不算进前向窗口。

    两边的日期形态在这里归一（比较点即归一点，调用方不必先洗手）——认不出的
    日期按 **0 根**算，与「基准序列取不到」同待遇：把「不知道」当「够了」正是
    这个闸门唯一要防的事。
    """
    ordered = sorted(d for d in (normalize_date(b) for b in bar_dates or []) if d)
    out: dict[str, int] = {}
    for raw in candidates or []:
        key = str(raw)
        norm = normalize_date(key)
        if norm is None:
            out[key] = 0
            continue
        # 首个 > norm 的位置之后全部计入
        lo, hi = 0, len(ordered)
        while lo < hi:
            mid = (lo + hi) // 2
            if ordered[mid] > norm:
                hi = mid
            else:
                lo = mid + 1
        out[key] = len(ordered) - lo
    return out


def plan_backfill(
    scored: dict[str, Any], forward_bars: dict[str, int], *, horizon: int
) -> list[str]:
    """待回填日期（升序）：本身 pending ∧ 前向 bar 已够（≥ horizon+1）。

    `forward_bars` 里没有的日期**不算闭合**（基准序列取不到就等下次）——
    把「不知道」当「够了」会让回填在数据缺口上跑出一堆短窗口假收益。
    """
    need = int(horizon) + 1
    out = [
        day
        for day, value in (scored or {}).items()
        if is_pending(value) and int((forward_bars or {}).get(day, 0)) >= need
    ]
    return sorted(out)


# ── DB 取数 ──────────────────────────────────────────────────────────


async def load_pending_snapshots(
    tenant_id: str, *, since: date, until: date
) -> list[dict[str, Any]]:
    """窗口内 `daily_selection` 快照（含 user_id 键形，回填必须沿用原行）。"""
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    _text(
                        "SELECT object_id, user_id, snapshot_date, dimensions "
                        "FROM eval_scores "
                        "WHERE object_type = 'daily_selection' AND tenant_id = :t "
                        "AND snapshot_date >= :since AND snapshot_date <= :until "
                        "ORDER BY snapshot_date DESC"
                    ),
                    {"t": tenant_id, "since": since, "until": until},
                )
            )
            .mappings()
            .all()
        )
    return [
        {
            "object_id": str(r["object_id"]),
            "user_id": str(r["user_id"] or ""),
            "snapshot_date": r["snapshot_date"].isoformat()
            if r["snapshot_date"]
            else str(r["object_id"]),
            "dimensions": r["dimensions"],
        }
        for r in rows
    ]


def _index_bar_dates(start_dt: int, end_dt: int) -> list[str]:
    """基准指数前复权 bar 日期（`index_daily`，与取收益同一个 view）。

    形态是 QuantDB 的原样（`astype(str)` → ``20260918``）；**归一只在
    :func:`count_bars_after` 里做**（比较点即归一点），这里不再转一次。
    """
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    hub = QuantDBDataHub.get_instance()
    df = hub.fetch_series(
        INDEX_VIEW, BENCHMARK_SYMBOL, start_dt, end_dt, columns=["close"]
    )
    if df is None or len(df) == 0:
        return []
    return [
        str(v)[:10]
        for v in (
            df.sort_values("dt")["dt"].astype(str).tolist()
            if "dt" in df.columns
            else []
        )
    ]


# ── 编排 ─────────────────────────────────────────────────────────────


async def backfill_recent(
    *,
    days: int = DEFAULT_LOOKBACK_DAYS,
    horizon: int = DEFAULT_HORIZON,
    tenant_id: str = "default",
    save: bool = True,
    dry_run: bool = False,
    runner: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """重跑近期「待回填 ∧ 窗口已闭合」的每日选股快照。"""
    today = date.today()
    since = today - timedelta(days=max(1, int(days)))
    summary: dict[str, Any] = {
        "lookback_days": int(days),
        "horizon": int(horizon),
        "since": since.isoformat(),
        "planned": 0,
        "rerun": 0,
        "still_pending": 0,
        "errors": [],
        "note": "",
    }

    snapshots = await load_pending_snapshots(tenant_id, since=since, until=today)
    if not snapshots:
        summary["note"] = (
            f"{since.isoformat()} 以来无 daily_selection 快照——先跑当日评分再回填"
        )
        return summary

    # 基准 bar 覆盖到 today + 缓冲（前向窗口按交易日算，多取几天不影响计数）
    bar_dates = _index_bar_dates(
        int(since.strftime("%Y%m%d")),
        int((today + timedelta(days=7)).strftime("%Y%m%d")),
    )
    day_keys = [s["snapshot_date"] for s in snapshots]
    bars_after = count_bars_after(bar_dates, day_keys)
    scored = {s["snapshot_date"]: s["dimensions"] for s in snapshots}
    planned = plan_backfill(scored, bars_after, horizon=horizon)
    summary["planned"] = len(planned)
    summary["bar_dates"] = len(bar_dates)
    pending_total = sum(1 for s in snapshots if is_pending(s["dimensions"]))
    summary["pending_total"] = pending_total

    if not planned:
        if pending_total:
            summary["note"] = (
                f"无待回填日期：窗口内 {len(snapshots)} 条快照中 {pending_total} 条 pending，"
                f"但前向窗口均未闭合（基准 bar {len(bar_dates)} 根）——明晚再试"
            )
        else:
            summary["note"] = (
                f"无待回填日期：窗口内 {len(snapshots)} 条快照的事后验证维均已算出"
                "（无需回填）"
            )
        return summary

    if dry_run:
        summary["note"] = f"[dry-run] 计划回填 {len(planned)} 天，未执行"
        summary["planned_dates"] = planned
        return summary

    run_one = runner or _runner_for(horizon=horizon, save=save)
    for day in planned:
        # 同一天可能有多个 user_id 键形的行（各账户视图），逐个沿用原键重跑
        for snap in (s for s in snapshots if s["snapshot_date"] == day):
            try:
                result = await run_one(day, snap["user_id"])
            except Exception as exc:  # noqa: BLE001 - 单日隔离，不拖垮整轮
                summary["errors"].append(f"backfill:{day}:{snap['user_id']}: {exc}")
                continue
            if is_pending((result or {}).get("dimensions")):
                summary["still_pending"] += 1
                summary["errors"].append(
                    f"backfill:{day}:{snap['user_id']}: 重跑后事后验证维仍缺省（"
                    f"{_realized_note(result)}）"
                )
            else:
                summary["rerun"] += 1
    summary["note"] = (
        f"回填 {summary['rerun']} 条"
        + (
            f"；仍缺省 {summary['still_pending']} 条"
            if summary["still_pending"]
            else ""
        )
        + (f"；失败 {len(summary['errors'])} 条" if summary["errors"] else "")
    )
    logger.info("[EvalBackfill] %s", summary["note"])
    return summary


def _realized_note(result: dict[str, Any] | None) -> str:
    """重跑后仍 pending 时，把该维自带的 note 原样带出（缺省原因不许丢）。"""
    dim = ((result or {}).get("dimensions") or {}).get("realized") or {}
    return str((dim.get("detail") or {}).get("note") or "无 note")


def _runner_for(*, horizon: int, save: bool):
    """真实重跑：走 `score_daily_selection` 落表（幂等 upsert 覆盖原行）。

    `user_id` 原样回传（`''` → `None`，与落表时的 `str(user_id or "")` 同键），
    否则唯一键对不上，回填会新增行而不是覆盖。
    """

    async def _run(trade_date: str, user_id: str) -> dict[str, Any]:
        from backend.scripts.eval.daily_selection import score_daily_selection

        return await score_daily_selection(
            trade_date, horizon=horizon, user_id=user_id or None, save=save
        )

    return _run


def _env_days() -> int:
    raw = str(os.getenv("EVAL_BACKFILL_DAYS", "")).strip()
    if not raw:
        return DEFAULT_LOOKBACK_DAYS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "EVAL_BACKFILL_DAYS=%s 非法，回落 %s", raw, DEFAULT_LOOKBACK_DAYS
        )
        return DEFAULT_LOOKBACK_DAYS


def main() -> int:
    parser = argparse.ArgumentParser(description="每日选股事后验证回填")
    parser.add_argument("--days", type=int, default=_env_days(), help="回看自然日")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--dry-run", action="store_true", help="只报计划，不重跑")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    summary = asyncio.run(
        backfill_recent(
            days=args.days,
            horizon=args.horizon,
            tenant_id=args.tenant,
            save=not args.dry_run,
            runner=(None if args.dry_run else None),
        )
    )
    if args.dry_run:
        summary["note"] = f"[dry-run] {summary['note']}"
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
