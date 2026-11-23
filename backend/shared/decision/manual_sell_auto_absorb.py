"""人工/风控卖出成交的运行期自动吸收（决策轮与止损读账本之前对齐）。

背景：``agent_ledger_fill`` 纪律 1——``orders.agent`` 为空的成交（人工在 TDX
客户端点的单、风控单）**不写分账**。人工清仓后账本留下幻影仓：下一次 agent
分析仍会看到「我还持有」并按分账成本算盈亏/额度，止损也会按幻影仓评估。
手动修复入口是 ``backend/scripts/repair_agent_ledger_manual_sells.py``；
本模块把同一套装载/判定/写入搬到运行期，由 trade 服务的周期任务调用，
让「下次手动卖出 → 下一次 agent 分析」之间自动对上账。

判据与脚本完全一致（复用其 loader 与 ``plan_absorption`` 纯函数）：

1. 该标的已不在真实账户（``load_real_positions``，快照过旧则整批不动）；
2. 窗口内存在 ``orders.agent`` 为空的已成交卖出，且数量覆盖账本该标的合计持仓；
3. 账本每一行的买入时刻早于分配到的卖出成交。

另返回两类**对账差异**（只报告，不猜）：
* ``ledger_excess``：账本 > 账户（有卖出成交但未被吸收，如部分覆盖/买入时刻不明）；
* ``account_excess``：账户 > 账本（如 TDX 过期委托实际成交但未落分账，见
  ``close_cleanup_audit_task`` 的跨日收口——柜台只给当日委托，历史成交查不到）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from backend.shared.database_manager_v2 import get_session
from backend.shared.decision.manual_sell_absorption import EPS, plan_manual_flows

logger = logging.getLogger(__name__)

BEIJING = timezone(timedelta(hours=8))

DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_MAX_SNAPSHOT_AGE_S = 1800


@dataclass
class AbsorbResult:
    """一次自动吸收的结果（调用方据此记日志/发通知）。"""

    fills: list[dict[str, Any]] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    ledger_excess: dict[str, dict[str, float]] = field(default_factory=dict)
    account_excess: dict[str, dict[str, float]] = field(default_factory=dict)
    cash_after: dict[str, float] = field(default_factory=dict)
    proceeds_by_agent: dict[str, float] = field(default_factory=dict)
    pnl_by_agent: dict[str, float] = field(default_factory=dict)
    snapshot_note: str = ""
    backup_path: str | None = None
    error: str | None = None

    @property
    def absorbed(self) -> bool:
        return bool(self.fills) and self.error is None

    @property
    def mismatch(self) -> bool:
        return bool(self.ledger_excess or self.account_excess)


def _project_root() -> Path:
    # backend/shared/decision/manual_sell_auto_absorb.py → 项目根
    return Path(__file__).resolve().parents[3]


#: 分账只覆盖 TDX 共享账户；QMT 是另一个互不相交的真实账户，不能并入对账
#: （并集里 QMT 独有持仓会被误报成「账户多于分账」，2026-11-18 首跑已踩过）。
_TDX_SNAPSHOT_SQL = (
    "SELECT snapshot_at, payload_json FROM real_account_snapshots "
    "WHERE tenant_id = :tid AND user_id = :uid AND source = 'tdx_bridge' "
    "ORDER BY snapshot_at DESC LIMIT 1"
)


def _decode_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return raw if isinstance(raw, dict) else {}


async def load_tdx_account_volumes(
    tenant: str, user: str, *, max_age_s: int = DEFAULT_MAX_SNAPSHOT_AGE_S
) -> tuple[dict[str, float] | None, str]:
    """**仅 TDX 账户**的持仓量（suffix 键）+ 结论说明；读不到/太旧返回 ``(None, 原因)``。"""
    from sqlalchemy import text

    from backend.shared.decision.manual_sell_absorption import to_suffix_code

    async with get_session(read_only=True) as db:
        row = (
            await db.execute(
                text(_TDX_SNAPSHOT_SQL), {"tid": tenant, "uid": user}
            )
        ).fetchone()
    if row is None:
        return None, "无 tdx_bridge 快照（桥可能未同步）"

    snap_at, payload = row[0], _decode_payload(row[1])
    if isinstance(snap_at, datetime):
        stamp = snap_at if snap_at.tzinfo else snap_at.replace(tzinfo=timezone.utc)
        age_s = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
        if age_s > max_age_s:
            return None, f"tdx_bridge 快照 {age_s:.0f}s 前（阈值 {max_age_s}s），桥可能掉线"

    out: dict[str, float] = {}
    for item in payload.get("positions") or []:
        if not isinstance(item, dict):
            continue
        code = to_suffix_code(item.get("symbol"))
        volume = float(item.get("volume") or 0)
        if code and volume > 0:
            out[code] = volume
    return out, f"tdx_bridge 快照，{len(out)} 只持仓"


async def load_tdx_account_capital(tenant: str, user: str) -> float:
    """TDX 快照里的持仓市值合计——人工买入的 ``manual`` 账户按真实账户规模开户。

    快照只有 positions（无 cash/asset 字段），用 ``market_value`` 合计近似账户规模：
    目的只是让 ``manual`` 的虚拟现金不因一笔大额人工买入变成负几十万，不参与模型额度。
    """
    from sqlalchemy import text

    async with get_session(read_only=True) as db:
        row = (
            await db.execute(
                text(_TDX_SNAPSHOT_SQL), {"tid": tenant, "uid": user}
            )
        ).fetchone()
    if row is None:
        return 0.0
    payload = _decode_payload(row[1])
    total = 0.0
    for item in payload.get("positions") or []:
        if not isinstance(item, dict):
            continue
        try:
            total += float(item.get("market_value") or 0.0)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def _write_backup(payload: Mapping[str, Any]) -> Path:
    """写前留痕（与手动脚本同口径，便于逐笔对账）。"""
    directory = Path(os.getenv("QM_LOG_DIR", str(_project_root() / "logs")))
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BEIJING).strftime("%Y%m%d_%H%M%S")
    path = directory / f"manual_absorb_auto_{stamp}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def _ledger_totals(ledger_rows: list[Mapping[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for row in ledger_rows:
        code = str(row.get("code") or "")
        volume = float(row.get("volume") or 0)
        if code and volume > 0:
            totals[code] = totals.get(code, 0.0) + volume
    return totals


def compute_mismatch(
    ledger_rows: list[Mapping[str, Any]], real_volumes: Mapping[str, float]
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """账本 vs 账户的持仓差异（纯函数）：返回 ``(ledger_excess, account_excess)``。"""
    ledger = _ledger_totals(ledger_rows)
    ledger_excess: dict[str, dict[str, float]] = {}
    account_excess: dict[str, dict[str, float]] = {}
    for code in sorted(set(ledger) | set(real_volumes)):
        led = float(ledger.get(code, 0.0))
        acc = float(real_volumes.get(code, 0.0))
        if led - acc > EPS:
            ledger_excess[code] = {"ledger": led, "account": acc}
        elif acc - led > EPS:
            account_excess[code] = {"ledger": led, "account": acc}
    return ledger_excess, account_excess


async def absorb_manual_sells(
    tenant: str,
    user: str,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_snapshot_age_s: int = DEFAULT_MAX_SNAPSHOT_AGE_S,
    write: bool = True,
) -> AbsorbResult:
    """执行一次「未归属卖出 → 分账」吸收；幂等（fill_key 固定），可每分钟跑。"""
    # 复用 CLI 脚本的装载/写库实现：判定与手动修复必须是同一份代码
    from backend.scripts.repair_agent_ledger_manual_sells import (
        apply_plan,
        load_accounts,
        load_ledger_rows,
        load_unattributed_buys,
        load_unattributed_sells,
        verify_plan,
    )

    result = AbsorbResult()
    since = datetime.now(BEIJING).replace(tzinfo=None) - timedelta(days=lookback_days)

    async with get_session(read_only=True) as db:
        from sqlalchemy import text as sa_text

        ledger_rows = await load_ledger_rows(db, tenant, user)
        accounts_before = await load_accounts(db, tenant, user)
        sells = await load_unattributed_sells(db, tenant, user, since)
        buys = await load_unattributed_buys(db, tenant, user, since)
        # 幂等：已入过账的 order（manual-absorb / manual-buy 流水存在）不再参与计划，
        # 否则下一轮会把「已含入账的账本」当成期初，重复生成一笔期初调整。
        absorbed_rows = (
            await db.execute(
                sa_text(
                    "SELECT DISTINCT order_id FROM qm_agent_ledger_fill "
                    "WHERE tenant_id = :tid AND user_id = :uid "
                    "  AND trade_date >= :since "
                    "  AND (fill_key LIKE 'manual-absorb:%' "
                    "       OR fill_key LIKE 'manual-buy:%')"
                ),
                {"tid": tenant, "uid": user, "since": since.date()},
            )
        ).fetchall()
        absorbed_ids = {str(r[0]) for r in absorbed_rows if r[0]}

    real_volumes, note = await load_tdx_account_volumes(
        tenant, user, max_age_s=max_snapshot_age_s
    )
    result.snapshot_note = note
    if real_volumes is None:
        # 账户不可读/太旧：整批不动，也不要报告差异（否则每次桥掉线都告警）
        logger.warning("[manual-absorb] 跳过：%s", note)
        return result

    plan = plan_manual_flows(
        ledger_rows,
        buys,
        sells,
        real_volumes,
        absorbed_order_ids=absorbed_ids,
    )
    result.skipped = {s.code: f"{s.reason}（{s.detail}）" for s in plan.skipped}
    result.proceeds_by_agent = dict(plan.proceeds_by_agent)
    result.pnl_by_agent = dict(plan.pnl_by_agent)

    if plan.fills:
        if not write:
            result.fills = [
                {
                    "agent": f.agent,
                    "code": f.code,
                    "side": f.side,
                    "volume": f.volume,
                    "price": f.price,
                    "order_id": f.order_id,
                }
                for f in plan.fills
            ]
        else:
            result.backup_path = str(
                _write_backup(
                    {
                        "tenant": tenant,
                        "user": user,
                        "ledger_rows": ledger_rows,
                        "accounts_before": accounts_before,
                        "real_volumes": real_volumes,
                        "sells": sells,
                        "buys": buys,
                        "fills": [
                            {
                                "agent": f.agent,
                                "code": f.code,
                                "side": f.side,
                                "volume": f.volume,
                                "price": f.price,
                                "order_id": f.order_id,
                                "fill_key": f.fill_key,
                                "trade_date": str(f.trade_date),
                                "cost_price": f.cost_price,
                                "realized_pnl": f.realized_pnl,
                                "closes": f.closes,
                            }
                            for f in plan.fills
                        ],
                    }
                )
            )
            try:
                capital = await load_tdx_account_capital(tenant, user)
                outcomes = await apply_plan(
                    plan, tenant=tenant, user=user, quota=capital
                )
                problems = await verify_plan(
                    plan,
                    outcomes,
                    tenant=tenant,
                    user=user,
                    accounts_before=accounts_before,
                    ledger_before=ledger_rows,
                    quota=capital,
                )
                if problems:
                    result.error = "写后复验失败：" + "；".join(problems)
                result.fills = [
                    {
                        "agent": o.get("agent"),
                        "code": o.get("code"),
                        "volume": o.get("volume"),
                        "price": o.get("price"),
                        "virtual_cash": o.get("virtual_cash"),
                    }
                    for o in outcomes
                ]
            except Exception as exc:  # noqa: BLE001 整批回滚由 apply_plan 保证
                result.error = f"{type(exc).__name__}: {exc}"
                logger.exception("[manual-absorb] 吸收失败（已回滚）")

    # 写后（或无需写时）再算差异：吸收成功的标的应从 ledger 侧消失
    async with get_session(read_only=True) as db:
        ledger_after = await load_ledger_rows(db, tenant, user)
        result.cash_after = await load_accounts(db, tenant, user)
    result.ledger_excess, result.account_excess = compute_mismatch(
        ledger_after, real_volumes
    )
    return result
