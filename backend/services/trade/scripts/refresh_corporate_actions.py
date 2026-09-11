"""
一键更新公司行为数据并刷新模拟盘账户 Redis 缓存。

流程:
1. 导入 corp_actions.csv (13列标准格式) 到 simulation_corporate_actions 表
2. 触发 apply_due_actions,将 pending 状态的公司行为应用到持仓/现金流
3. 刷新所有受影响账户的 Redis 账户缓存(simulation:account:* + trade:account:*)
4. 打印受影响账户的权益核对信息

Usage:
    # 本地执行(dry-run 预览)
    source .venv/bin/activate
    python backend/services/trade/scripts/refresh_corporate_actions.py --dry-run

    # 完整执行:导入 + apply + 刷新 Redis
    python backend/services/trade/scripts/refresh_corporate_actions.py \\
        --replace-existing

    # 指定 CSV 文件路径
    python backend/services/trade/scripts/refresh_corporate_actions.py \\
        --file /path/to/corp_actions.csv --replace-existing

    # 仅 apply + 刷新 Redis,不重新导入 CSV
    python backend/services/trade/scripts/refresh_corporate_actions.py \\
        --skip-import

    # 在服务器容器内执行
    docker compose -p quantmind -f docker-compose.server.yml exec quantmind-trade \\
        python backend/services/trade/scripts/refresh_corporate_actions.py \\
        --file /tmp/corp_actions.csv --replace-existing
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import os
from pathlib import Path
import sys

from sqlalchemy import select, text

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
os.environ["PYTHONPATH"] = BASE_DIR

from backend.services.trade_shared.redis_client import redis_client  # noqa: E402
from backend.services.simulation.models.account import SimulationAccount  # noqa: E402
from backend.services.simulation.models.corporate_action import (  # noqa: E402
    SimulationCorporateAction,
)
from backend.services.simulation.models.position_lot import (  # noqa: E402
    SimulationPositionLot,
)
from backend.services.simulation.services.corporate_action_importer import (  # noqa: E402
    load_standard_corp_action_csv,
)
from backend.services.simulation.services.corporate_action_service import (  # noqa: E402
    SimulationCorporateActionService,
)
from backend.services.simulation.services.projection_service import (  # noqa: E402
    SimulationProjectionService,
)
from backend.shared.database_manager_v2 import (  # noqa: E402
    close_database,
    get_db_manager,
    init_database,
)
from backend.shared.stock_utils import StockCodeUtil  # noqa: E402
from backend.shared.simulation_account_keys import account_key  # noqa: E402
from backend.shared.trade_account_cache import (  # noqa: E402
    write_json_cache,
    write_trade_account_cache,
)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "一键更新公司行为数据并刷新模拟盘账户 Redis 缓存。"
            "流程: 导入 CSV → apply pending → 刷新受影响账户 Redis。"
        ),
    )
    parser.add_argument(
        "--file",
        default="data/corp_actions.csv",
        help="CSV 路径,默认 data/corp_actions.csv",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="可选 source 标签,写入 simulation_corporate_actions.source",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="导入前删除同 symbol+action_type+ex_date+source 的历史记录",
    )
    parser.add_argument(
        "--skip-import",
        action="store_true",
        help="跳过 CSV 导入,仅执行 apply + 刷新 Redis",
    )
    parser.add_argument(
        "--skip-apply",
        action="store_true",
        help="跳过 apply_due_actions,仅导入 CSV + 刷新 Redis",
    )
    parser.add_argument(
        "--refresh-all-accounts",
        action="store_true",
        help="刷新所有 active 账户的 Redis 缓存(默认仅刷新本次受影响账户)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式:不写数据库,不 apply,不刷 Redis",
    )
    return parser


async def _import_csv(args: argparse.Namespace) -> tuple[int, int, list[str]]:
    """导入 CSV,返回 (inserted, deleted, affected_symbols)。"""
    decisions = load_standard_corp_action_csv(args.file, source=args.source)
    accepted = [d for d in decisions if d.status == "accepted" and d.mapped]
    skipped = [d for d in decisions if d.status != "accepted"]

    print(f"[1/4] CSV 导入: 共 {len(decisions)} 行, 接受 {len(accepted)} 行, 跳过 {len(skipped)} 行")

    skipped_by_reason: dict[str, int] = {}
    for item in skipped:
        skipped_by_reason[item.reason] = skipped_by_reason.get(item.reason, 0) + 1
    for reason, count in sorted(skipped_by_reason.items()):
        print(f"    跳过 {reason}: {count}")

    by_action: dict[str, int] = {}
    for item in accepted:
        assert item.mapped is not None
        by_action[item.mapped.action_type] = by_action.get(item.mapped.action_type, 0) + 1
    for action_type, count in sorted(by_action.items()):
        print(f"    action_type={action_type}: {count}")

    if args.dry_run:
        print("    [dry-run] 不写入数据库")
        affected_symbols = sorted({item.mapped.symbol for item in accepted if item.mapped})
        return 0, 0, affected_symbols

    db_manager = get_db_manager()
    inserted = 0
    deleted = 0
    async with db_manager.get_master_session() as session:
        for item in accepted:
            mapped = item.mapped
            assert mapped is not None
            stmt = select(SimulationCorporateAction).where(
                SimulationCorporateAction.symbol == mapped.symbol,
                SimulationCorporateAction.action_type == mapped.action_type,
                SimulationCorporateAction.ex_date == mapped.ex_date,
                SimulationCorporateAction.source == mapped.source,
            )
            existing = list((await session.execute(stmt)).scalars().all())
            if existing and not args.replace_existing:
                continue
            for row in existing:
                await session.delete(row)
                deleted += 1
            session.add(
                SimulationCorporateAction(
                    symbol=mapped.symbol,
                    action_type=mapped.action_type,
                    ex_date=mapped.ex_date,
                    effective_date=mapped.effective_date,
                    cash_dividend_per_share=mapped.cash_dividend_per_share,
                    share_ratio=mapped.share_ratio,
                    rights_price=mapped.rights_price,
                    source=mapped.source,
                    note=mapped.note,
                    status="pending",
                )
            )
            inserted += 1
    print(f"    写入: inserted={inserted}, deleted={deleted}")
    affected_symbols = sorted({item.mapped.symbol for item in accepted if item.mapped})
    return inserted, deleted, affected_symbols


async def _apply_pending_actions(args: argparse.Namespace) -> tuple[int, set[str]]:
    """apply pending 公司行为,返回 (applied_count, affected_account_ids)。"""
    if args.skip_apply:
        print("[2/4] 跳过 apply_due_actions (--skip-apply)")
        return 0, set()

    if args.dry_run:
        async with get_db_manager().get_master_session() as session:
            pending_count = (
                await session.execute(
                    select(SimulationCorporateAction).where(
                        SimulationCorporateAction.status == "pending"
                    )
                )
            ).scalars().all()
        print(f"[2/4] [dry-run] apply_due_actions 将处理 {len(pending_count)} 条 pending 记录")
        return 0, set()

    applied = await SimulationCorporateActionService.apply_due_actions()
    print(f"[2/4] apply_due_actions 完成,应用 {applied} 条")

    affected_accounts: set[str] = set()
    if applied > 0:
        db_manager = get_db_manager()
        async with db_manager.get_master_session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT DISTINCT account_id FROM simulation_cash_ledger "
                        "WHERE ref_type = 'corporate_action' "
                        "  AND occurred_at >= :start "
                        "ORDER BY account_id"
                    ),
                    {"start": datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)},
                )
            ).all()
            affected_accounts = {r[0] for r in rows}
        print(f"    受影响账户数: {len(affected_accounts)}")
    return applied, affected_accounts


async def _refresh_redis_for_account(account_id: str) -> bool:
    """刷新单个账户的 Redis 缓存,返回是否成功。"""
    db_manager = get_db_manager()
    async with db_manager.get_master_session() as session:
        account = await session.get(SimulationAccount, account_id)
        if account is None:
            print(f"    [warn] 账户不存在: {account_id}")
            return False
        projection = await SimulationProjectionService(session).load_projection(
            tenant_id=account.tenant_id,
            user_id=account.user_id,
            latest_price_loader=lambda symbol: _load_close_price(session, symbol),
        )
        if projection.account is None:
            print(f"    [warn] projection 为空: {account_id}")
            return False
        payload = SimulationProjectionService.build_cache_payload(
            account=projection.account,
            positions=projection.positions or {},
            source="refresh_corporate_actions_script",
        )
        sim_key = account_key(account.tenant_id, account.user_id)
        write_json_cache(redis_client, sim_key, payload)
        write_trade_account_cache(
            redis_client, account.tenant_id, account.user_id, payload
        )
        return True


async def _load_close_price(session, symbol: str) -> float:
    prefix = StockCodeUtil.to_prefix(symbol)
    suffix = StockCodeUtil.to_suffix(prefix)
    query = text(
        "SELECT close, adj_factor FROM stock_daily_latest "
        "WHERE symbol = :symbol ORDER BY trade_date DESC LIMIT 1"
    )
    for candidate in (prefix, suffix):
        result = await session.execute(query, {"symbol": candidate})
        row = result.fetchone()
        if not row:
            continue
        close_price = float(row[0] or 0.0)
        if close_price <= 0:
            continue
        return close_price
    return 0.0


async def _refresh_redis(args: argparse.Namespace, affected_accounts: set[str]) -> int:
    """刷新受影响账户的 Redis 缓存,返回成功刷新的账户数。"""
    print("[3/4] 刷新 Redis 账户缓存")

    if not redis_client.client:
        from backend.services.trade_shared.redis_client import get_redis

        get_redis()
        if not redis_client.client:
            print("    [error] Redis 不可用,跳过刷新")
            return 0

    if args.dry_run:
        target = "全部 active 账户" if args.refresh_all_accounts else f"{len(affected_accounts)} 个受影响账户"
        print(f"    [dry-run] 将刷新 {target}")
        return 0

    if args.refresh_all_accounts:
        db_manager = get_db_manager()
        async with db_manager.get_master_session() as session:
            accounts = list(
                (
                    await session.execute(
                        select(SimulationAccount).where(
                            SimulationAccount.status == "active"
                        )
                    )
                ).scalars().all()
            )
        target_accounts = [a.account_id for a in accounts]
    else:
        target_accounts = sorted(affected_accounts)

    if not target_accounts:
        print("    无账户需要刷新")
        return 0

    success = 0
    for account_id in target_accounts:
        ok = await _refresh_redis_for_account(account_id)
        if ok:
            success += 1
    print(f"    刷新成功: {success}/{len(target_accounts)}")
    return success


async def _verify_accounts(affected_accounts: set[str]) -> None:
    """打印受影响账户的权益核对信息。"""
    print("[4/4] 账户权益核对")
    if not affected_accounts:
        print("    无受影响账户")
        return

    db_manager = get_db_manager()
    async with db_manager.get_master_session() as session:
        print(
            f"    {'account_id':<32} {'cash':>14} {'total_asset':>14} "
            f"{'today_corp_adj':>14} {'total_corp_adj':>14}"
        )
        for account_id in sorted(affected_accounts):
            account = await session.get(SimulationAccount, account_id)
            if account is None:
                print(f"    {account_id:<32} [账户不存在]")
                continue
            today_start = datetime.combine(datetime.utcnow().date(), datetime.min.time())
            corp_row = (
                await session.execute(
                    text(
                        "SELECT COALESCE(SUM(CASE WHEN occurred_at >= :ts THEN amount ELSE 0 END),0), "
                        "COALESCE(SUM(amount),0) "
                        "FROM simulation_cash_ledger "
                        "WHERE account_id=:aid AND ref_type='corporate_action'"
                    ),
                    {"aid": account_id, "ts": today_start},
                )
            ).first()
            today_corp = float(corp_row[0] or 0) if corp_row else 0.0
            total_corp = float(corp_row[1] or 0) if corp_row else 0.0
            print(
                f"    {account_id:<32} {float(account.cash or 0):>14.2f} "
                f"{float(account.total_asset or 0):>14.2f} "
                f"{today_corp:>14.2f} {total_corp:>14.2f}"
            )


async def _run(args: argparse.Namespace) -> int:
    file_path = Path(args.file)
    if not args.skip_import and not file_path.exists():
        print(f"[error] CSV 文件不存在: {file_path}")
        return 1

    await init_database()
    try:
        # Step 1: 导入 CSV
        if args.skip_import:
            print("[1/4] 跳过 CSV 导入 (--skip-import)")
            affected_symbols: list[str] = []
        else:
            _, _, affected_symbols = await _import_csv(args)

        # Step 2: apply pending 公司行为
        _, affected_accounts = await _apply_pending_actions(args)

        # 如果跳过了 apply 但需要刷新 Redis,降级为刷新所有受影响 symbol 持仓的账户
        if args.skip_apply and not affected_accounts and affected_symbols:
            db_manager = get_db_manager()
            async with db_manager.get_master_session() as session:
                normalized_symbols = {
                    StockCodeUtil.to_prefix(s) for s in affected_symbols
                }
                if normalized_symbols:
                    rows = (
                        await session.execute(
                            select(SimulationPositionLot.account_id).where(
                                SimulationPositionLot.symbol.in_(normalized_symbols)
                            )
                        )
                    ).all()
                    affected_accounts = {r[0] for r in rows}
            print(f"    按 symbol 找到 {len(affected_accounts)} 个持仓账户")

        # Step 3: 刷新 Redis
        await _refresh_redis(args, affected_accounts)

        # Step 4: 权益核对
        await _verify_accounts(affected_accounts)

        print("\n[完成] 公司行为更新流程结束")
        return 0
    finally:
        await close_database()


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
