#!/usr/bin/env python3
"""分账账本**期初结转**（P3 数据迁移）：隔壁 ``logs/live_ledger.json`` → 本仓 PG 账本。

回答的问题：切换日之后，三个模型（agent）**名下的仓**从哪来。

本仓账本（``qm_agent_ledger_*``）切换后从零开始，而模型名下的持仓**已经在真实账户里**
——不搬这一段，``mine_of`` 全空 ⇒ 提示词把共享账户的持仓裁光 ⇒ 模型看不见自己的仓，
该止盈止损的永远不卖，而账面全绿。与 2026-09-08 那次「pro 卖了 flash 的生益电子」是
同族形态，只是方向反过来（那次是**看得太多**，这次是**什么都看不见**）。

用法（容器内）:
    # 默认 dry-run：只解析 + 查空账本，一行不写
    python backend/scripts/import_agent_ledger_seed.py --path /tmp/live_ledger.json
    # 确认无误后写库（切换窗口内执行，收盘后）
    python backend/scripts/import_agent_ledger_seed.py --path /tmp/live_ledger.json --apply

退出码：``0`` 完成（含 dry-run 展示与空计划）/ ``1`` **要人看一眼**（解析拒绝、账本非空
被拒、有跳过行）/ ``2`` 环境或参数错误（文件读不了、JSON 坏了、库连不上）。

四条纪律（各自防一种静默损坏）
------------------------------
1. **只搬 ``agents`` 段**：``applied_fills`` 是隔壁用**委托号**做的当日幂等标记，本仓的
   幂等是 ``(租户, 用户, 成交日, fill_key)`` 唯一索引、键是券商成交号——委托号搬过来
   既不是键也认不出归属，只记条数。
2. **只许在空账本上结转**：这三个 agent 在账户/持仓/流水表里只要有一行，整批拒绝、一行
   不写（理由见 ``import_legacy_seed``）。本命令**不删不改**任何既有行。
3. **状态要能从流水推回来**：每条结转的持仓配一行买入流水（``fill_key`` 带
   ``legacy-seed:`` 前缀、``applied_volume = volume``）。体检 C14 按同一前缀单独计数，
   不当「无对应成交」报。
4. **既存仓不入分账**：账户里 2026-08-31 之前那 5 只（约 ¥92 万）属**总账户**，隔壁自己
   也不把它们记进任何 agent——文件里没有它们，本命令同样不会凭空造出归属。

账户身份（``--tenant`` / ``--user``）默认取**与决策轮同一处解析**
（``resolve_db_account_user(QM_DECISION_ACCOUNT_USER_ID)``，规范名 10000001），
不许手打：写错一个身份，结转出来的账本在决策轮眼里就是**另一本空账**。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

#: 与其它脚本同款（``ghost`` / ``l05_store`` 等都是各自定义一份）。
CST = timezone(timedelta(hours=8))

EXIT_OK = 0
EXIT_ATTENTION = 1
EXIT_USAGE = 2


def _today_cst() -> str:
    return datetime.now(CST).date().isoformat()


def _print_plan(seed, args: argparse.Namespace) -> None:
    print(
        f"[结转] 解析 {args.path}：{len(seed.agents)} 个 agent、"
        f"applied_fills {seed.applied_fills} 条（不搬，只计数）"
    )
    for a in seed.agents:
        print(
            f"  {a.agent}: 虚拟现金 ¥{a.virtual_cash:,.2f} / 持仓 {len(a.positions)} 只 "
            f"/ 现持仓成本 ¥{a.used:,.2f}"
        )
        for p in a.positions:
            ts = p.buy_ts.isoformat() if p.buy_ts is not None else "无 buy_ts"
            print(f"    {p.code} {p.volume:g} 股 @ {p.cost_price:g}（{ts}）")
    for line in seed.problems:
        print(f"  ✗ 阻断：{line}")
    for line in seed.notes:
        print(f"  · {line}")


def _print_report(rep, args: argparse.Namespace) -> None:
    if rep.refused:
        print("[结转] 拒绝：**一行未写**")
        for line in rep.refused:
            print(f"  ✗ {line}")
        return
    verb = "将写入" if rep.dry_run else "已写入"
    print(
        f"[结转] {verb}：账户 {rep.accounts_written} 行 / 持仓 {rep.positions_written} 行 / "
        f"流水 {rep.fills_written} 行（现持仓成本合计 ¥{rep.cost_total:,.2f}）"
    )
    if rep.fills_skipped:
        print(
            f"  ⚠ 有 {rep.fills_skipped} 行流水因**当日同键已存在**被跳过：请核对账本"
        )
    if rep.dry_run:
        print("[结转] DRY-RUN（未写库）；确认无误后加 --apply")
    else:
        print(
            f"[结转] 落库身份：tenant={args.tenant} user={args.user}；"
            "下一步：`python backend/scripts/diagnose/health.py --only C14` 核账"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="分账账本期初结转（P3 数据迁移；默认 dry-run）"
    )
    parser.add_argument("--path", required=True, help="隔壁 live_ledger.json 路径")
    parser.add_argument("--tenant", default="default", help="租户（默认 default）")
    parser.add_argument(
        "--user",
        default=None,
        help="账户 user_id（默认与决策轮同源：resolve_db_account_user(QM_DECISION_ACCOUNT_USER_ID)）",
    )
    parser.add_argument(
        "--as-of", dest="as_of", default=None, help="结转日 YYYY-MM-DD（默认今天 CST）"
    )
    parser.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run）")
    parser.add_argument("--json", action="store_true", help="附一段机读 JSON")
    args = parser.parse_args()

    from backend.shared.decision.agent_ledger import parse_legacy_ledger
    from backend.shared.simulation_account_keys import resolve_db_account_user

    if args.user is None:
        # 与决策轮同源（决定账本落在哪个身份下），不许手打
        from backend.services.trade.services.decision_round_core import ENV_ACCOUNT_USER

        args.user = resolve_db_account_user(ENV_ACCOUNT_USER)

    try:
        raw = json.loads(Path(args.path).read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"[结转] 读不到文件：{exc}")
        return EXIT_USAGE
    except json.JSONDecodeError as exc:
        print(f"[结转] 不是合法 JSON：{exc}")
        return EXIT_USAGE

    seed = parse_legacy_ledger(raw)
    _print_plan(seed, args)
    if not seed.ok:
        print("[结转] 解析未通过（阻断项见上）：**一行未写**，先修文件再跑")
        return EXIT_ATTENTION
    if not seed.agents:
        print("[结转] 计划为空（没有可结转的 agent）：一行未写")
        return EXIT_OK

    async def _run() -> int:
        from backend.shared.agent_ledger_store import import_legacy_seed
        from backend.shared.database_manager_v2 import close_database, get_session

        dry_run = not args.apply
        try:
            async with get_session(read_only=dry_run) as session:
                rep = await import_legacy_seed(
                    session,
                    tenant_id=args.tenant,
                    user_id=args.user,
                    seed=seed,
                    as_of=args.as_of or _today_cst(),
                    dry_run=dry_run,
                )
                if rep.applied and not dry_run:
                    await session.commit()
        except Exception as exc:  # noqa: BLE001 环境/连接问题：如实报错退出，不吞
            print(f"[结转] 写库失败：{type(exc).__name__}: {exc}")
            return EXIT_USAGE
        finally:
            await close_database()

        _print_report(rep, args)
        if args.json:
            print(
                json.dumps(
                    {
                        "applied": rep.applied,
                        "dry_run": rep.dry_run,
                        "refused": list(rep.refused),
                        "accounts_written": rep.accounts_written,
                        "positions_written": rep.positions_written,
                        "fills_written": rep.fills_written,
                        "fills_skipped": rep.fills_skipped,
                        "cost_total": rep.cost_total,
                        "agents": [
                            {
                                "agent": w.agent,
                                "virtual_cash": w.virtual_cash,
                                "positions": w.positions,
                                "cost": w.cost,
                            }
                            for w in rep.agents
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        if rep.refused or rep.fills_skipped:
            return EXIT_ATTENTION
        return EXIT_OK

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
