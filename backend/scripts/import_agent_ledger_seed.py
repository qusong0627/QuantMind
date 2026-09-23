#!/usr/bin/env python3
"""分账账本**期初结转**（P3 数据迁移）：隔壁 ``logs/live_ledger.json`` → 本仓 PG 账本。

回答的问题：切换日之后，三个模型（agent）**名下的仓**从哪来。

本仓账本（``qm_agent_ledger_*``）切换后从零开始，而模型名下的持仓**已经在真实账户里**
——不搬这一段，``mine_of`` 全空 ⇒ 提示词把共享账户的持仓裁光 ⇒ 模型看不见自己的仓，
该止盈止损的永远不卖，而账面全绿。与 2026-09-08 那次「pro 卖了 flash 的生益电子」是
同族形态，只是方向反过来（那次是**看得太多**，这次是**什么都看不见**）。

用法（容器内）::

    # 1) 先看计划（默认 dry-run）：解析台账 + 读桥 + 对账，一行不写
    python backend/scripts/import_agent_ledger_seed.py --path /tmp/live_ledger.json \\
        --record /tmp/seed_record.json
    # 2) 核对 record 里的对账记录（幻影/孤仓/锚定/人工判定逐条在案）后写库
    python backend/scripts/import_agent_ledger_seed.py --path /tmp/live_ledger.json \\
        --record /tmp/seed_record.json --apply

退出码：``0`` 完成且对账**全平** / ``1`` **要人看一眼**（解析或对账拒绝、账本非空、
有跳过行、有对账差异或人工判定）/ ``2`` 环境或参数错误（文件读不了、JSON 坏了、
桥读不到、``--apply`` 缺 ``--record``）。

五条纪律（各自防一种静默损坏）
------------------------------
1. **只搬 ``agents`` 段**：``applied_fills`` 是隔壁用**委托号**做的当日幂等标记，本仓的
   幂等是 ``(租户, 用户, 成交日, fill_key)`` 唯一索引、键是券商成交号——委托号搬过来
   既不是键也认不出归属，只记条数。
2. **仓位以桥为准，不照搬台账**（P0.4）：台账是派生数据（卖出没归因回去时只增不减），
   桥（PG ``real_account_snapshots``，经 ``real_positions.load_real_positions`` 这唯一口径）
   才是仓位事实源。幻影仓剔除、单认领人按桥锚定量、孤仓显式记录——**都对账留痕**，
   不静默丢弃。多认领人合计不齐 ⇒ 阻断（任何摊派都是编造 `--resolve` 给出人工判定）。
3. **只许在空账本上结转**：这三个 agent 在账户/持仓/流水表里只要有一行，整批拒绝、一行
   不写（理由见 ``import_legacy_seed``）。本命令**不删不改**任何既有行。
4. **状态要能从流水推回来**：每条结转的持仓配一行买入流水（``fill_key`` 带
   ``legacy-seed:`` 前缀、``applied_volume = volume``）。体检 C14 按同一前缀单独计数，
   不当「无对应成交」报；被锚定/人工判定过的标的，判定写进该行 ``note``。
5. **既存仓不入分账**：账户里 2026-08-31 之前那批（约 ¥92 万）属**总账户**，隔壁自己
   也不把它们记进任何 agent——文件里没有它们，本命令同样不会凭空造出归属；桥侧若有
   **无人认领**的仓，按「孤仓」记录在案，同样不入户。

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


def _print_bridge(rows: list[dict], meta: dict | None, label: str) -> None:
    total = sum(float(r.get("volume") or 0) for r in rows)
    print(f"[对账] 桥侧快照（{label}）：{len(rows)} 只 / {total:g} 股")
    if meta:
        for src, info in sorted((meta.get("sources") or {}).items()):
            print(
                f"  · {src}: {info.get('positions')} 只 @ {info.get('snapshot_at')}"
                + ("（**已停更，未并入**）" if info.get("stale") else "")
            )
    for r in rows:
        print(
            f"    {r.get('code')} {float(r.get('volume') or 0):g} 股"
            f"（{r.get('source') or '?'}）"
        )


def _print_reconciliation(rec) -> None:
    mark = {
        "match": "=",
        "anchored": "~",
        "phantom": "✗",
        "orphan": "?",
        "override": "!",
    }
    print(
        f"[对账] 台账合计 → 迁入 {rec.carried_total:g} 股 / 桥合计 {rec.bridge_total:g} 股"
        f" / 孤仓 {rec.orphan_total:g} 股"
    )
    for r in rec.records:
        print(
            f"  {mark.get(r.kind, ' ')} [{r.kind}] {r.code}: 台账 {r.ledger_volume:g} → "
            f"迁入 {r.carried_volume:g}（桥 {r.bridge_volume:g}，差 {r.delta:+g}）"
        )
        if r.kind != "match":
            print(f"      {r.verdict}")
    reason = rec.assert_balances()
    print(f"[对账] 双向断言：{'通过' if not reason else '**未通过** ' + reason}")
    for line in rec.problems:
        print(f"  ✗ 阻断：{line}")
    for line in rec.notes:
        print(f"  · {line}")


def _read_json(path: str) -> tuple[object | None, str]:
    """读 JSON；返回 ``(数据, 错误说明)``（错误非空 = 读不了，调用点按退出码 2 处理）。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), ""
    except OSError as exc:
        return None, f"{path}: {exc}"
    except json.JSONDecodeError as exc:
        return None, f"{path}: 不是合法 JSON：{exc}"


def _bridge_rows_from_file(data: object) -> tuple[list[dict] | None, str]:
    """``--bridge-json`` 的内容 → 捕获行（认两种形态：``{"positions": [...]}`` 或裸列表）。"""
    if isinstance(data, dict):
        rows = data.get("positions")
    elif isinstance(data, list):
        rows = data
    else:
        return (
            None,
            f"桥快照形态不认识（{type(data).__name__}）：要 {{'positions': [...]}} 或裸列表",
        )
    if not isinstance(rows, list):
        return None, "桥快照的 positions 不是列表"
    return rows, ""


def _load_resolutions(
    data: object,
) -> tuple[dict[str, dict[str, float]], dict[str, str], list[str]]:
    """``--resolve`` 的内容 → ``(overrides, reasons, 形态错误)``（语义校验在对账层）。

    形态：``{"decisions": [{"code": "002074.SZ", "claims": {"<agent>": 200},
    "reason": "…"}]}``。理由**必填**（不加理由的判定不可审计）——这里只查形状，
    「agent 不在台账里 / 数量非正 / 缺理由」由 ``reconcile_seed_with_bridge`` 判。
    """
    decisions = (data or {}).get("decisions") if isinstance(data, dict) else None
    if not isinstance(decisions, list):
        return {}, {}, ["resolve 文件要 {'decisions': [...]}"]
    overrides: dict[str, dict[str, float]] = {}
    reasons: dict[str, str] = {}
    errors: list[str] = []
    for i, dec in enumerate(decisions):
        if not isinstance(dec, dict):
            errors.append(f"decisions[{i}] 不是对象（{type(dec).__name__}）")
            continue
        code = str(dec.get("code") or "").strip()
        claims = dec.get("claims")
        if not code or not isinstance(claims, dict):
            errors.append(f"decisions[{i}] 缺 code 或 claims 不是对象")
            continue
        reasons[code] = str(dec.get("reason") or "")
        overrides[code] = {str(k): v for k, v in claims.items()}
    return overrides, reasons, errors


def _record_doc(
    *,
    args: argparse.Namespace,
    seed,
    rec,
    bridge_rows: list[dict],
    bridge_meta: dict | None,
    bridge_label: str,
    resolutions: dict[str, dict[str, float]],
    result: dict | None,
) -> dict:
    """迁移产物里的**对账记录**（P0.4 规则 2：幻影/孤仓不是静默丢弃，要留痕）。"""
    return {
        "tool": "import_agent_ledger_seed",
        "ran_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "apply" if args.apply else "dry-run",
        "account": {"tenant": args.tenant, "user": args.user},
        "as_of": args.as_of or _today_cst(),
        "seed_file": str(Path(args.path).resolve()),
        "seed_ledger_totals": {
            "agents": [
                {
                    "agent": a.agent,
                    "virtual_cash": a.virtual_cash,
                    "positions": len(a.positions),
                    "used": a.used,
                }
                for a in seed.agents
            ],
            "applied_fills": seed.applied_fills,
        },
        "bridge": {
            "source": bridge_label,
            "meta": bridge_meta,
            "positions": bridge_rows,
            "total_volume": sum(float(r.get("volume") or 0) for r in bridge_rows),
        },
        "resolutions": [
            {"code": c, "claims": cl} for c, cl in sorted((resolutions or {}).items())
        ],
        "records": [
            {
                "code": r.code,
                "kind": r.kind,
                "ledger_volume": r.ledger_volume,
                "bridge_volume": r.bridge_volume,
                "carried_volume": r.carried_volume,
                "delta": r.delta,
                "bridge_present": r.bridge_present,
                "claims": [{"agent": a, "volume": v} for a, v in r.claims],
                "verdict": r.verdict,
                "reason": r.reason,
            }
            for r in rec.records
        ],
        "assert": {
            "ok": not rec.assert_balances(),
            "detail": rec.assert_balances(),
            "bridge_total": rec.bridge_total,
            "carried_total": rec.carried_total,
            "orphan_total": rec.orphan_total,
        },
        "plan": [
            {
                "agent": a.agent,
                "virtual_cash": a.virtual_cash,
                "used": a.used,
                "positions": [
                    {"code": p.code, "volume": p.volume, "cost_price": p.cost_price}
                    for p in a.positions
                ],
            }
            for a in rec.seed.agents
        ],
        "problems": list(rec.problems),
        "notes": list(rec.notes),
        "result": result,
    }


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


def _report_json(rep, args: argparse.Namespace) -> dict:
    return {
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
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="分账账本期初结转（P3 数据迁移；桥锚定对账，默认 dry-run）"
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
    parser.add_argument(
        "--bridge-json",
        dest="bridge_json",
        default=None,
        help="用捕获的桥快照文件（默认实时读 PG real_account_snapshots）",
    )
    parser.add_argument(
        "--save-bridge",
        dest="save_bridge",
        default=None,
        help="把本次读到的桥快照写盘（留档/复放：apply 时可再用 --bridge-json 读回同一份）",
    )
    parser.add_argument(
        "--resolve",
        default=None,
        help="人工判定文件（仅多认领人对账不齐时需要；每条必须带 reason）",
    )
    parser.add_argument(
        "--record",
        default=None,
        help="对账记录落盘路径（**--apply 必须给**：迁移产物里要留痕）",
    )
    parser.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run）")
    parser.add_argument("--json", action="store_true", help="附一段机读 JSON")
    args = parser.parse_args()

    if args.apply and not args.record:
        print(
            "[结转] --apply 必须带 --record <path>：幻影/孤仓/锚定必须留一条对账记录"
            "（P0.4 规则 2），否则日后没人能解释「为什么台账里曾经有那两只」"
        )
        return EXIT_USAGE

    from backend.shared.decision.agent_ledger import (
        bridge_positions,
        bridge_rows_from_view,
        parse_legacy_ledger,
        reconcile_seed_with_bridge,
    )
    from backend.shared.simulation_account_keys import resolve_db_account_user

    if args.user is None:
        # 与决策轮同源（决定账本落在哪个身份下），不许手打
        from backend.services.trade.services.decision_round_core import ENV_ACCOUNT_USER

        args.user = resolve_db_account_user(ENV_ACCOUNT_USER)

    raw, err = _read_json(args.path)
    if err:
        print(f"[结转] 读不到台账文件：{err}")
        return EXIT_USAGE

    seed = parse_legacy_ledger(raw)
    _print_plan(seed, args)
    if not seed.ok:
        print("[结转] 解析未通过（阻断项见上）：**一行未写**，先修文件再跑")
        return EXIT_ATTENTION

    resolutions: dict[str, dict[str, float]] = {}
    res_reasons: dict[str, str] = {}
    if args.resolve:
        data, err = _read_json(args.resolve)
        if err:
            print(f"[结转] 读不到人工判定文件：{err}")
            return EXIT_USAGE
        resolutions, res_reasons, res_errors = _load_resolutions(data)
        for line in res_errors:
            print(f"  ✗ 人工判定：{line}")
        if res_errors:
            return EXIT_ATTENTION
        print(f"[结转] 人工判定 {len(resolutions)} 条：{sorted(resolutions)}")

    async def _run() -> int:
        from backend.shared.agent_ledger_store import import_legacy_seed
        from backend.shared.database_manager_v2 import close_database, get_session

        try:
            # ── 桥：唯一仓位事实源（实时 PG 读取，或复放捕获文件）────────────
            bridge_meta: dict | None = None
            if args.bridge_json:
                data, err = _read_json(args.bridge_json)
                if err:
                    print(f"[结转] 读不到桥快照文件：{err}")
                    return EXIT_USAGE
                rows, err = _bridge_rows_from_file(data)
                if err:
                    print(f"[结转] {err}")
                    return EXIT_USAGE
                bridge_label = f"file:{Path(args.bridge_json).resolve()}"
                if isinstance(data, dict) and isinstance(data.get("meta"), dict):
                    bridge_meta = data["meta"]
            else:
                from backend.shared.real_positions import load_real_positions

                try:
                    view, bridge_meta = await load_real_positions(
                        args.tenant, args.user
                    )
                except Exception as exc:  # noqa: BLE001 - 读不到桥就**不结转**
                    print(f"[结转] 读不到桥侧持仓：{type(exc).__name__}: {exc}")
                    return EXIT_USAGE
                rows = bridge_rows_from_view(view)
                bridge_label = "live:real_account_snapshots"

            bmap, b_problems = bridge_positions(rows)
            bridge_rows = [
                {
                    "code": bp.code,
                    "volume": bp.volume,
                    "cost_price": bp.cost_price,
                    "source": bp.source,
                }
                for bp in sorted(bmap.values(), key=lambda b: b.code)
            ]
            _print_bridge(bridge_rows, bridge_meta, bridge_label)
            if b_problems:
                print("[结转] 桥快照有问题（见下）：**一行未写**")
                for line in b_problems:
                    print(f"  ✗ {line}")
                return EXIT_ATTENTION
            if args.save_bridge:
                Path(args.save_bridge).parent.mkdir(parents=True, exist_ok=True)
                Path(args.save_bridge).write_text(
                    json.dumps(
                        {
                            "captured_at": datetime.now(timezone.utc)
                            .isoformat()
                            .replace("+00:00", "Z"),
                            "source": bridge_label,
                            "meta": bridge_meta,
                            "positions": bridge_rows,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                print(f"[对账] 桥快照已留档：{args.save_bridge}")

            # ── 对账（纯函数）：幻影剔除 / 单认领人按桥锚定 / 孤仓记录 / 断言 ──
            rec = reconcile_seed_with_bridge(
                seed, bmap, overrides=resolutions, reasons=res_reasons
            )
            _print_reconciliation(rec)

            dry_run = not args.apply
            report = None
            result_doc: dict | None = None
            if not rec.ok or not rec.seed.agents:
                # 对账没过 / 无可结转：**一行不写**（记录照落，便于转交人工）。
                print("[结转] 对账未通过或计划为空：**一行未写**")
                result_doc = {"skipped": "对账未通过" if not rec.ok else "计划为空"}
            else:
                try:
                    async with get_session(read_only=dry_run) as session:
                        report = await import_legacy_seed(
                            session,
                            tenant_id=args.tenant,
                            user_id=args.user,
                            plan=rec,
                            as_of=args.as_of or _today_cst(),
                            dry_run=dry_run,
                        )
                        if report.applied and not dry_run:
                            await session.commit()
                except Exception as exc:  # noqa: BLE001 环境/连接问题：如实报错
                    print(f"[结转] 写库失败：{type(exc).__name__}: {exc}")
                    return EXIT_USAGE
                _print_report(report, args)
                result_doc = _report_json(report, args)

            if args.record:
                doc = _record_doc(
                    args=args,
                    seed=seed,
                    rec=rec,
                    bridge_rows=bridge_rows,
                    bridge_meta=bridge_meta,
                    bridge_label=bridge_label,
                    resolutions=resolutions,
                    result=result_doc,
                )
                Path(args.record).parent.mkdir(parents=True, exist_ok=True)
                Path(args.record).write_text(
                    json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(f"[结转] 对账记录已落盘：{args.record}")

            if args.json:
                print(json.dumps(result_doc, ensure_ascii=False))
            if rec.changed:
                print(
                    f"[结转] ⚠ 对账差异 {len(rec.changed)} 项"
                    f"（幻影 {len(rec.phantom_codes)} / 孤仓 {len(rec.orphan_codes)}"
                    " / 其余锚定或人工判定）——逐条在上方与 --record 里，请核对"
                )

            if not rec.ok:
                return EXIT_ATTENTION
            if report is None:
                return EXIT_OK
            if report.refused or report.fills_skipped or rec.changed:
                return EXIT_ATTENTION
            return EXIT_OK
        finally:
            await close_database()

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover - 人工中断
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
