"""
Real account daily ledger persistence and queries.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dt_time, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade_shared.models.real_account_ledger import (
    RealAccountLedgerDailySnapshot,
)
from backend.services.trade_shared.models.real_account_snapshot import (
    RealAccountSnapshot,
)

logger = logging.getLogger(__name__)
_SH_TZ = ZoneInfo("Asia/Shanghai")

# ── 权益一致性归一（现金账户恒等式：总资产 ≥ 现金 + 市值）──────────────
# 触发阈值：重建值（现金+市值）高出上报总资产的差额必须同时超过
#   ① 相对下限 1%——低于此视为报价/舍入噪声，不动；
#   ② 绝对下限 100 元——小账户的 1% 可能是几块钱，避免噪声误触发。
EQUITY_UNDERREPORT_TOL_PCT = 0.01
EQUITY_UNDERREPORT_TOL_MIN = 100.0

# 盈亏为**本方派生**（非券商上报）的 source：这些行的 today/total_pnl_raw 由
# 「总资产 − 锚」算出，权益被归一后允许同口径重算；其余 source 的 *_raw 是上报值，禁动。
DERIVED_PNL_SOURCES = ("tdx_bridge", "daily_settlement")


def normalize_equity(
    total_asset: float | None,
    cash: float | None,
    market_value: float | None,
) -> tuple[float, dict[str, Any] | None]:
    """总资产 < 现金 + 市值 ⇒ 判上报字段被读错，用两分量重建权益；返回 (权益, 留痕)。

    为什么是**单向**规则：现金账户恒等式 ``总资产 = 现金 + 市值 (+ 冻结/在途)``，
    冻结/在途只会把整体**抬高**，绝不会把整体压到分量之和以下——"整体 < 分量和"
    在任何账面构造下都不可达，只能是上报的 total 字段被读错。反方向
    （total ≥ cash+mv）不动：那可能是冻结/在途，属合法状态。

    实况（2026-09-03/04，tdx 桥）：北京 16:34 起 total 字段一步掉 161,058
    （−17.5%）而**同行** cash/mv 一分钟没动，次日恢复；161k 的洞比持仓总市值
    205,646 的 78% 还大，涨跌停口径下单日不可达 ⇒ 非真实亏损。此前该值直接进
    日度台账，污染账户图与风控档位回撤输入（2026-09-23 修复）。

    保守面：只在两分量**都 > 0** 时核验（单分量无法判读，如桥未报市值的旧行）；
    归一必留痕（``payload_json.equity_normalized`` 保留原值），可审计、可回放。
    """
    try:
        total = float(total_asset or 0.0)
        cash_f = float(cash or 0.0)
        mv_f = float(market_value or 0.0)
    except (TypeError, ValueError):
        return float(total_asset or 0.0), None
    if cash_f <= 0 or mv_f <= 0:
        return total, None
    rebuilt = round(cash_f + mv_f, 2)
    gap = rebuilt - total
    if gap <= max(
        EQUITY_UNDERREPORT_TOL_MIN, abs(rebuilt) * EQUITY_UNDERREPORT_TOL_PCT
    ):
        return total, None
    return rebuilt, {
        "rule": "equity_underreport",
        "raw_total_asset": total,
        "rebuilt": rebuilt,
        "gap": round(gap, 2),
    }


def resolve_daily_pnl_pct(
    *, total_asset: float | None, day_open_equity: float | None
) -> float | None:
    """当日盈亏百分比（负=亏）——风控 ``l1.daily_loss_limit`` 的输入口径。

    **基线不可得 → None，不是 0.0**：0.0 的语义是"当日打平"，把它当"算不出来"的
    替身会让风控证据里留下一个假事实（展示面「缺失一律 —」是同一条原则）；风控规则
    靠 None 走"没有依据就不判"，靠 0.0 就会声称"今天没亏"。

    与 ``derive_equity_returns`` 的 ``daily_return_pct`` 是**同一公式**（那边由本函数
    供值）：账户页显示的当日收益率与风控判定必须逐位同源，两边各写一份的话，
    "页面看着亏 4%、风控认为亏 3.9%"这类分歧没有任何一处会报错。
    """
    try:
        total = float(total_asset)  # type: ignore[arg-type]
        base = float(day_open_equity)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if base <= 0:
        return None
    return (total - base) / base * 100.0


def derive_equity_returns(
    *,
    total_asset: float,
    day_open_equity: float,
    month_open_equity: float,
    initial_equity: float,
    today_pnl: float,
    total_pnl: float,
) -> dict[str, float]:
    """台账派生列口径：月度盈亏（派生）/ 日收益率 / 累计收益率。

    写侧与修复脚本（``backend/scripts/repair_ledger_equity.py``）共用同一实现——
    修复后的行必须与"若当初写入时即已归一"完全一致，两边禁各自重算。

    ``daily_return_pct`` 是**非空列**：分母不可得时落 0.0（与
    ``resolve_daily_pnl_pct`` 的 None 语义差异仅在此处——列没有"未知"这个取值）。
    ``today_pnl`` 只是签名里保留的上报原值（写侧另有列承载它），日收益率不用它兜底：
    收益率的分母是日初权益，拿"上报的今日盈亏"顶上会得到一个既非收益率也非盈亏的数。
    """
    derived_monthly_pnl = (
        total_asset - month_open_equity if month_open_equity > 0 else total_pnl
    )
    cumulative_pnl = total_asset - initial_equity if initial_equity > 0 else total_pnl
    daily_pct = resolve_daily_pnl_pct(
        total_asset=total_asset, day_open_equity=day_open_equity
    )
    return {
        "monthly_pnl_raw": float(derived_monthly_pnl),
        "daily_return_pct": float(daily_pct) if daily_pct is not None else 0.0,
        "total_return_pct": (
            float(cumulative_pnl / initial_equity * 100.0)
            if initial_equity > 0
            else 0.0
        ),
    }


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        parsed = float(value)
        return parsed if parsed == parsed else default
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        parsed = int(value)
        return parsed
    except Exception:
        return default


def _ensure_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


async def upsert_real_account_daily_ledger(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    account_id: str,
    snapshot_at: datetime,
    snapshot_date: date,
    total_asset: float,
    cash: float,
    market_value: float,
    initial_equity: float,
    day_open_equity: float,
    month_open_equity: float,
    today_pnl: float,
    total_pnl: float,
    floating_pnl: float,
    position_count: int,
    source: str,
    payload_json: dict[str, Any] | None = None,
) -> None:
    existing_stmt = (
        select(RealAccountLedgerDailySnapshot)
        .where(
            RealAccountLedgerDailySnapshot.tenant_id == tenant_id,
            RealAccountLedgerDailySnapshot.user_id == user_id,
            RealAccountLedgerDailySnapshot.account_id == account_id,
            RealAccountLedgerDailySnapshot.snapshot_date == snapshot_date,
        )
        .limit(1)
    )
    existing_result = await db.execute(existing_stmt)
    existing = existing_result.scalar_one_or_none()
    incoming_snapshot_at = _ensure_utc_naive(snapshot_at)
    if existing is not None and existing.last_snapshot_at is not None:
        existing_snapshot_at = _ensure_utc_naive(existing.last_snapshot_at)
        if incoming_snapshot_at <= existing_snapshot_at:
            logger.info(
                "Skip stale real-account daily ledger upsert tenant=%s user=%s account=%s snapshot_date=%s incoming=%s existing=%s",
                tenant_id,
                user_id,
                account_id,
                snapshot_date.isoformat(),
                incoming_snapshot_at.isoformat(),
                existing_snapshot_at.isoformat(),
            )
            return

    # 权益一致性归一：上报总资产低于"现金+市值"（物理不可达）时用两分量重建，
    # 原值留痕到 payload_json。归一必须在派生列之前——下方便用归一后权益计算收益率。
    total_asset, normalization = normalize_equity(total_asset, cash, market_value)
    if normalization is not None:
        logger.warning(
            "Real-account ledger equity under-report normalized tenant=%s user=%s account=%s date=%s raw=%.2f rebuilt=%.2f gap=%.2f",
            tenant_id,
            user_id,
            account_id,
            snapshot_date.isoformat(),
            normalization["raw_total_asset"],
            normalization["rebuilt"],
            normalization["gap"],
        )

    derived = derive_equity_returns(
        total_asset=float(total_asset or 0.0),
        day_open_equity=float(day_open_equity or 0.0),
        month_open_equity=float(month_open_equity or 0.0),
        initial_equity=float(initial_equity or 0.0),
        today_pnl=float(today_pnl or 0.0),
        total_pnl=float(total_pnl or 0.0),
    )

    payload = dict(payload_json or {})
    if normalization is not None:
        payload["equity_normalized"] = {
            **normalization,
            "normalized_at": _iso_or_none(_ensure_utc_naive(snapshot_at)),
        }

    row = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "account_id": account_id,
        "snapshot_date": snapshot_date,
        "last_snapshot_at": incoming_snapshot_at,
        "initial_equity": float(initial_equity or 0.0),
        "day_open_equity": float(day_open_equity or 0.0),
        "month_open_equity": float(month_open_equity or 0.0),
        "total_asset": float(total_asset or 0.0),
        "cash": float(cash or 0.0),
        "market_value": float(market_value or 0.0),
        # 原始字段保留券商/桥接层上报值，用于历史审计
        "today_pnl_raw": float(today_pnl or 0.0),
        "monthly_pnl_raw": derived["monthly_pnl_raw"],
        "total_pnl_raw": float(total_pnl or 0.0),
        "floating_pnl_raw": float(floating_pnl or 0.0),
        "daily_return_pct": derived["daily_return_pct"],
        "total_return_pct": derived["total_return_pct"],
        "position_count": int(position_count or 0),
        "source": str(source or "qmt_bridge"),
        "payload_json": payload,
    }

    stmt = (
        pg_insert(RealAccountLedgerDailySnapshot)
        .values(**row)
        .on_conflict_do_update(
            index_elements=[
                RealAccountLedgerDailySnapshot.tenant_id,
                RealAccountLedgerDailySnapshot.user_id,
                RealAccountLedgerDailySnapshot.account_id,
                RealAccountLedgerDailySnapshot.snapshot_date,
            ],
            set_={
                "last_snapshot_at": row["last_snapshot_at"],
                "initial_equity": row["initial_equity"],
                "day_open_equity": row["day_open_equity"],
                "month_open_equity": row["month_open_equity"],
                "total_asset": row["total_asset"],
                "cash": row["cash"],
                "market_value": row["market_value"],
                "today_pnl_raw": row["today_pnl_raw"],
                "monthly_pnl_raw": row["monthly_pnl_raw"],
                "total_pnl_raw": row["total_pnl_raw"],
                "floating_pnl_raw": row["floating_pnl_raw"],
                "daily_return_pct": row["daily_return_pct"],
                "total_return_pct": row["total_return_pct"],
                "position_count": row["position_count"],
                "source": row["source"],
                "payload_json": row["payload_json"],
            },
        )
    )
    await db.execute(stmt)


async def list_real_account_daily_ledgers(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    account_id: str | None = None,
    days: int = 30,
) -> list[RealAccountLedgerDailySnapshot]:
    stmt = select(RealAccountLedgerDailySnapshot).where(
        RealAccountLedgerDailySnapshot.tenant_id == tenant_id,
        RealAccountLedgerDailySnapshot.user_id == user_id,
    )
    if account_id:
        stmt = stmt.where(RealAccountLedgerDailySnapshot.account_id == account_id)
    stmt = stmt.order_by(
        desc(RealAccountLedgerDailySnapshot.snapshot_date),
        desc(RealAccountLedgerDailySnapshot.last_snapshot_at),
    ).limit(max(1, min(days, 3650)))
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    return list(reversed(rows))


def account_family(account_id: str | None) -> str:
    """实盘账户**家族**键：``account_id`` 去掉末段。

    末段是用户标识；2026-09-18 用户 id 规范化（``00000001`` → ``10000001``）时账户
    整键改名（``tdx-default-00000001`` → ``tdx-default-10000001``），同一座真实账户
    的台账因此裂成两段键、且行分落两个 user_id 别名。家族口径与风控档位生产者
    ``risk_tier_producer.merge_ledger_rows`` 完全一致（同一账户不许有两套"家族"定义）。
    """
    text = str(account_id or "").strip()
    if not text:
        return ""
    return text.rsplit("-", 1)[0] or text


def _snapshot_moment(row: Any) -> datetime:
    moment = getattr(row, "last_snapshot_at", None)
    return _ensure_utc_naive(moment) if isinstance(moment, datetime) else datetime.min


def merge_family_ledger_rows(
    rows: list[Any],
    *,
    family: str,
    days: int,
) -> list[Any]:
    """家族行 → 按日合并（同日多行取较晚快照）后最近 ``days`` 天的行（按日升序）。

    纯函数（行只需有 ``account_id`` / ``snapshot_date`` / ``last_snapshot_at``）。
    丢弃非本家族的行——家族按 ``account_family`` 判定，不用前缀匹配（
    ``tdx-default`` 与 ``tdx-default-extra`` 是两座账户）。
    """
    limit = max(1, min(days, 3650))
    by_day: dict[Any, Any] = {}
    for row in rows:
        if account_family(getattr(row, "account_id", None)) != family:
            continue
        day = getattr(row, "snapshot_date", None)
        if day is None:
            continue
        current = by_day.get(day)
        if current is None or _snapshot_moment(row) > _snapshot_moment(current):
            by_day[day] = row
    ordered = [by_day[day] for day in sorted(by_day)]
    return ordered[-limit:]


async def list_real_account_daily_ledgers_by_family(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    account_id: str,
    days: int = 30,
) -> list[RealAccountLedgerDailySnapshot]:
    """按账户家族读台账（跨用户 id 别名 + 跨改名前后的账户键）。

    为什么不能按单一 ``account_id`` 读：账户键在 2026-09-18 规范化时改名，历史行留在
    旧键（``tdx-default-00000001``，08-13..09-16）、新行写新键（``tdx-default-10000001``，
    09-18 起）——单键读会让实盘账户页的权益曲线在改名日"断头"。用户 id 同样按
    ``ledger_user_id_candidates`` 展开别名族（与档位生产者同源）。
    """
    from backend.shared.simulation_account_keys import ledger_user_id_candidates

    family = account_family(account_id)
    if not family:
        return []
    limit = max(1, min(days, 3650))
    users = ledger_user_id_candidates(user_id)
    stmt = (
        select(RealAccountLedgerDailySnapshot)
        .where(
            RealAccountLedgerDailySnapshot.tenant_id == tenant_id,
            RealAccountLedgerDailySnapshot.user_id.in_(users),
        )
        .order_by(
            desc(RealAccountLedgerDailySnapshot.snapshot_date),
            desc(RealAccountLedgerDailySnapshot.last_snapshot_at),
        )
        # 每日每键至多半行；别名/改名重叠最多两三行/日，取 days×4 保证覆盖
        .limit(limit * 4)
    )
    result = await db.execute(stmt)
    return merge_family_ledger_rows(
        list(result.scalars().all()), family=family, days=days
    )


async def finalize_real_account_daily_ledgers(
    db: AsyncSession,
    *,
    snapshot_date: date,
    finalized_local_time: dt_time = dt_time(15, 0, 0),
) -> int:
    settlement_local_dt = datetime.combine(
        snapshot_date, finalized_local_time, tzinfo=_SH_TZ
    )
    settlement_utc_naive = settlement_local_dt.astimezone(timezone.utc).replace(
        tzinfo=None
    )

    rows_result = await db.execute(
        select(RealAccountLedgerDailySnapshot).where(
            RealAccountLedgerDailySnapshot.snapshot_date == snapshot_date,
        )
    )
    rows = list(rows_result.scalars().all())
    if not rows:
        return 0

    valid_asset_filter = or_(
        RealAccountSnapshot.total_asset > 1e-8,
        RealAccountSnapshot.cash > 1e-8,
        RealAccountSnapshot.market_value > 1e-8,
    )
    counts_result = await db.execute(
        select(
            RealAccountSnapshot.tenant_id,
            RealAccountSnapshot.user_id,
            RealAccountSnapshot.account_id,
            func.count(RealAccountSnapshot.id),
            func.max(RealAccountSnapshot.snapshot_at),
        )
        .where(
            RealAccountSnapshot.snapshot_date == snapshot_date,
            valid_asset_filter,
        )
        .group_by(
            RealAccountSnapshot.tenant_id,
            RealAccountSnapshot.user_id,
            RealAccountSnapshot.account_id,
        )
    )
    counts_map = {
        (str(row[0]), str(row[1]), str(row[2])): {
            "snapshot_count": int(row[3] or 0),
            "last_valid_snapshot_at": row[4],
        }
        for row in counts_result.all()
    }

    finalized_rows = 0
    for row in rows:
        key = (str(row.tenant_id), str(row.user_id), str(row.account_id))
        stats = counts_map.get(
            key,
            {
                "snapshot_count": 0,
                "last_valid_snapshot_at": row.last_snapshot_at,
            },
        )
        payload_json = dict(row.payload_json or {})
        payload_json["settlement_finalized"] = True
        payload_json["settlement_finalized_at"] = _iso_or_none(settlement_utc_naive)
        payload_json["settlement_snapshot_count"] = int(stats["snapshot_count"] or 0)
        payload_json["settlement_last_valid_snapshot_at"] = _iso_or_none(
            stats.get("last_valid_snapshot_at")
        )

        row.payload_json = payload_json
        row.source = "daily_settlement"
        if (
            row.last_snapshot_at is None
            or _ensure_utc_naive(row.last_snapshot_at) < settlement_utc_naive
        ):
            row.last_snapshot_at = settlement_utc_naive
        db.add(row)
        finalized_rows += 1

    logger.info(
        "Finalized real-account daily ledgers snapshot_date=%s rows=%d finalized_at=%s",
        snapshot_date.isoformat(),
        finalized_rows,
        settlement_utc_naive.isoformat(),
    )
    return finalized_rows


async def backfill_daily_ledgers_from_snapshots(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    account_id: str,
    days: int = 30,
) -> int:
    """Backfill daily ledger rows from persisted real_account_snapshots.

    This is used as a safety net for legacy users whose daily ledger rows were
    missing, so frontend daily-return charts can still read normalized data.
    """
    scoped_rows_stmt = (
        select(RealAccountSnapshot)
        .where(
            RealAccountSnapshot.tenant_id == tenant_id,
            RealAccountSnapshot.user_id == user_id,
            RealAccountSnapshot.account_id == account_id,
        )
        .order_by(desc(RealAccountSnapshot.snapshot_at))
        .limit(max(1, min(days, 3650)) * 64)
    )
    scoped_result = await db.execute(scoped_rows_stmt)
    snapshots = list(scoped_result.scalars().all())
    if not snapshots:
        return 0

    baseline_stmt = (
        select(RealAccountSnapshot)
        .where(
            RealAccountSnapshot.tenant_id == tenant_id,
            RealAccountSnapshot.user_id == user_id,
            RealAccountSnapshot.account_id == account_id,
        )
        .order_by(asc(RealAccountSnapshot.snapshot_at), asc(RealAccountSnapshot.id))
        .limit(1)
    )
    baseline_result = await db.execute(baseline_stmt)
    baseline_row = baseline_result.scalar_one_or_none()
    baseline_initial_equity = _to_float(
        getattr(baseline_row, "total_asset", None),
        _to_float(getattr(snapshots[-1], "total_asset", None), 0.0),
    )

    latest_per_day: dict[date, RealAccountSnapshot] = {}
    for row in snapshots:
        day = getattr(row, "snapshot_date", None)
        if day is None or day in latest_per_day:
            continue
        if _to_float(getattr(row, "total_asset", None), 0.0) <= 0:
            # 跳过零资产快照（桥空响应时段），避免把 0 写进账本
            continue
        latest_per_day[day] = row
        if len(latest_per_day) >= max(1, min(days, 3650)):
            break

    upserted = 0
    for snapshot_date in sorted(latest_per_day.keys()):
        row = latest_per_day[snapshot_date]
        total_asset = _to_float(getattr(row, "total_asset", None), 0.0)
        cash = _to_float(getattr(row, "cash", None), 0.0)
        market_value = _to_float(getattr(row, "market_value", None), 0.0)
        # 先归一再推 day_open——否则被读错的总资产会当锚，把假跌/假涨带进派生列
        total_asset, _ = normalize_equity(total_asset, cash, market_value)
        today_pnl_raw = _to_float(getattr(row, "today_pnl_raw", None), 0.0)
        total_pnl_raw = _to_float(getattr(row, "total_pnl_raw", None), 0.0)
        floating_pnl_raw = _to_float(getattr(row, "floating_pnl_raw", None), 0.0)

        inferred_day_open = total_asset - today_pnl_raw
        day_open_equity = inferred_day_open if inferred_day_open > 0 else total_asset
        inferred_month_open = total_asset - total_pnl_raw
        month_open_equity = (
            inferred_month_open if inferred_month_open > 0 else total_asset
        )

        payload = getattr(row, "payload_json", None)
        payload_json = payload if isinstance(payload, dict) else {}
        positions = (
            payload_json.get("positions") if isinstance(payload_json, dict) else None
        )
        position_count = len(positions) if isinstance(positions, list) else 0

        await upsert_real_account_daily_ledger(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            account_id=account_id,
            snapshot_at=getattr(row, "snapshot_at", datetime.utcnow()),
            snapshot_date=snapshot_date,
            total_asset=total_asset,
            cash=cash,
            market_value=market_value,
            initial_equity=baseline_initial_equity
            if baseline_initial_equity > 0
            else total_asset,
            day_open_equity=day_open_equity,
            month_open_equity=month_open_equity,
            today_pnl=today_pnl_raw,
            total_pnl=total_pnl_raw,
            floating_pnl=floating_pnl_raw,
            position_count=position_count,
            source="qmt_bridge_backfill",
            payload_json=payload_json,
        )
        upserted += 1

    return upserted
