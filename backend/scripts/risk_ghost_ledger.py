#!/usr/bin/env python3
"""风控影子代价账（P1.6）——抽取 / 定价 / 出表 一条命令链。

回答的问题：**这条闸门该不该留**（见 `backend/shared/risk/ghost.py` 模块头）。
三个子命令各管一段，都可反复跑（幂等）：

    extract             留痕（Redis Stream）→ 影子账行（PG）
    price               影子账行 + 真实行情 → 事后代价（PG 回填）
    report              影子账行 → 人读报表（stdout + `data/reports/risk_ghost/`）
    status              一眼体检（行数/窗口/定价覆盖/翻闸进度）
    purge-test-tenants  清掉集成测试写进来的行（见下）
    rekey-symbols       非规范标的的旧键就地迁移（见下）

用法（容器内）:
    python backend/scripts/risk_ghost_ledger.py extract --days 7            # DRY-RUN
    python backend/scripts/risk_ghost_ledger.py extract --days 7 --apply
    python backend/scripts/risk_ghost_ledger.py price --apply
    python backend/scripts/risk_ghost_ledger.py report --days 30
    python backend/scripts/risk_ghost_ledger.py status
    python backend/scripts/risk_ghost_ledger.py purge-test-tenants --apply
    python backend/scripts/risk_ghost_ledger.py rekey-symbols --apply

退出码：``0`` 完成且无待办 / ``1`` 完成但有**要人看一眼**的事 / ``2`` 环境或参数错误。
`1` 不是失败，是给 cron 一个"这封信需要人拆"的信号；正文里会写明是哪件事。

四条纪律（各自防一种静默损坏，细节见 `risk_ghost_store.py` 与 `ghost_pricing.py`）
--------------------------------------------------------------------------------
* 抽取产出的行走**发现期字段**那一拨，绝不覆盖已算好的价（否则重跑一次 `extract`
  就把全库价格清成 NULL，而报表只显示"未定价"）；
* 重跑定价走 `price_row_monotone`，一次失败的读盘不得抹掉已有观测；
* 重试臂（`retried`）**只在存在翻闸后的行时才查成交**——影子期恒不适用；
* **测试租户的行不进账**（抽取侧拒收 + 读侧排除，存量另有 `purge-test-tenants`）：
  集成测试会真往决策流里写，实测 845 行里 230 行是这么来的，且与真账同表同形。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.risk.ghost import (  # noqa: E402
    CST,
    GhostRow,
    extract_rows,
    is_test_tenant,
    summary_lines,
)
from backend.shared.risk.ghost_pricing import (  # noqa: E402
    H_NOT_MATURED,
    H_NO_DATA,
    H_OK,
    HORIZONS,
    is_retry_superseded,
    price_row_monotone,
    state_of,
)
from backend.shared.risk.ghost_report import (  # noqa: E402
    V_COSTLY,
    build_report,
    render,
)

EXIT_OK = 0
EXIT_ATTENTION = 1
EXIT_USAGE = 2

DEFAULT_EXTRACT_DAYS = 7
DECISIONS_KEY = "qm:risk:decisions:{date}"
CONFIG_KEY = "qm:risk:config"

#: 重试成交的候选来源（两张表同形：tenant_id / user_id / symbol / side / executed_at）
_TRADE_TABLES: tuple[str, ...] = ("trades", "sim_trades")

#: 查重试成交时把窗口放宽 ±1 天：SQL 侧只负责**捞候选**，精确判定一律交给
#: `is_retry_superseded`（300s 窗口的唯一口径）。放宽的理由是两张表的
#: `executed_at` 一个是 naive TIMESTAMP、一个是 TIMESTAMPTZ，naive 参数与
#: TIMESTAMPTZ 列比较会带上会话时区——窗口宁可捞宽，**绝不能捞窄**。
_RETRY_SCAN_SLACK_S = 86400.0


# ── 环境 ────────────────────────────────────────────────────────────
def _redis() -> Any:
    """风控用的 Redis（trade 库；与 `risk_shadow_report.py` 同口径）。"""
    import redis as redis_lib

    return redis_lib.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=int(os.getenv("REDIS_DB_TRADE", "2")),
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


def _decode_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Stream 字段逐个试 json 解码（`decisions`/`evidence` 是 JSON 串）。"""
    out: dict[str, Any] = {}
    for k, v in fields.items():
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except (TypeError, ValueError):
                out[k] = v
        else:
            out[k] = v
    return out


def _day_keys(*, days: int | None, start: str | None, end: str | None) -> list[str]:
    """要读的留痕日键（`YYYYMMDD`，CST）。"""
    if start or end:
        lo = datetime.strptime(start, "%Y-%m-%d").date() if start else None
        hi = datetime.strptime(end, "%Y-%m-%d").date() if end else None
        if lo is None:
            lo = hi - timedelta(days=max(int(days or DEFAULT_EXTRACT_DAYS) - 1, 0))
        if hi is None:
            hi = lo + timedelta(days=max(int(days or DEFAULT_EXTRACT_DAYS) - 1, 0))
        out, d = [], lo
        while d <= hi:
            out.append(d.strftime("%Y%m%d"))
            d += timedelta(days=1)
        return out
    today = datetime.now(tz=CST).date()
    n = max(int(days or DEFAULT_EXTRACT_DAYS), 1)
    return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]


def read_decisions(**kw: Any) -> list[dict[str, Any]]:
    """按日读 `qm:risk:decisions:{YYYYMMDD}` → 归一后的留痕条目。"""
    client = _redis()
    entries: list[dict[str, Any]] = []
    try:
        for day in _day_keys(**kw):
            try:
                rows = client.xrange(DECISIONS_KEY.format(date=day))
            except Exception as exc:  # noqa: BLE001 - 某天读不到不该让整轮失败
                print(f"  [留痕] {day} 读取失败（跳过）: {exc}")
                continue
            entries.extend(_decode_fields(f) for _eid, f in rows)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    return entries


def read_shadow_mode() -> bool | None:
    """风控配置里的 `shadow`（None = 没读到，**不猜**）。"""
    client = _redis()
    try:
        raw = client.hget(CONFIG_KEY, "shadow")
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    if raw is None:
        return None
    return str(raw).strip().lower() == "true"


def _reports_dir() -> Path:
    return Path(os.getenv("QM_REPORTS_DIR", str(PROJECT_ROOT / "data" / "reports"))) / "risk_ghost"


# ── extract ─────────────────────────────────────────────────────────
def cmd_extract(args: argparse.Namespace) -> int:
    entries = read_decisions(days=args.days, start=args.start, end=args.end)
    # 逃生口：给了 --include-test-tenants 就传空清单（**不拒收**，见 ghost.py 模块头②）
    excl: tuple[str, ...] | None = () if args.include_test_tenants else None
    n_test = sum(1 for e in entries if is_test_tenant(e.get("tenant")))
    rows = extract_rows(entries, exclude_tenants=excl)
    note = f"，其中测试租户 {n_test} 条已拒收" if n_test and excl is None else ""
    print(
        f"[影子账] 留痕 {len(entries)} 条{note} → 影子账行 {len(rows)} 条（已按幂等键去重）"
    )
    if n_test and excl is None:
        print("    这些是集成测试真写进来的决策，从未对应过一笔真单；见 ghost.py 模块头②")
    for line in summary_lines(rows):
        print(f"  {line}")
    unregistered = sorted({r.rule_id for r in rows if not r.registered})
    if unregistered:
        print(f"  ⚠ 留痕里出现未登记的规则 id：{', '.join(unregistered)}")
        print("    未登记 ≠ 没问题：它没被审阅过，请补进 gate_registry")
    if not args.apply:
        print("[影子账] DRY-RUN（未写库）；确认无误后加 --apply")
        return EXIT_ATTENTION if unregistered else EXIT_OK
    if not rows:
        print("[影子账] 没有可写的行")
        return EXIT_ATTENTION if unregistered else EXIT_OK

    async def _run() -> int:
        from backend.shared.database_manager_v2 import close_database, get_session

        from backend.scripts.risk_ghost_store import upsert_rows

        try:
            async with get_session() as session:
                n = await upsert_rows(session, rows)
                await session.commit()
            print(f"[影子账] 已写入/刷新 {n} 行")
        finally:
            await close_database()
        return EXIT_ATTENTION if unregistered else EXIT_OK

    return asyncio.run(_run())


# ── price ───────────────────────────────────────────────────────────
def _needs_pricing(row: GhostRow) -> bool:
    """还**能补出东西**的行：没定过价，或还有期停在"没到期/缺数"。

    注意判据是"还有没有可能变好"，不是"有没有价"——已定价的行里 t20/t60 仍会
    随着日历推进到期，那种行必须回到列表里。
    """
    if not row.fwd:
        return True
    return any(state_of(row, h) in (H_NOT_MATURED, H_NO_DATA) for h in HORIZONS)


async def _retry_fill_ts(session: Any, row: GhostRow) -> float | None:
    """该行拦截后 ≤300s 内、同键同向的成交时刻（epoch 秒）；无则 None。

    **只对 `enforced=True` 的行查**（影子期恒为 None，见 `ghost_pricing` 模块头）：
    影子期"当天成交"是实现分支而非污染，查了反而会把有效样本剔掉。
    """
    if not row.enforced or row.ts <= 0:
        return None
    from sqlalchemy import text

    from backend.shared.utc_datetime import as_utc

    lo = datetime.fromtimestamp(row.ts - _RETRY_SCAN_SLACK_S, tz=CST).replace(tzinfo=None)
    hi = datetime.fromtimestamp(row.ts + _RETRY_SCAN_SLACK_S, tz=CST).replace(tzinfo=None)
    best: float | None = None
    for table in _TRADE_TABLES:
        try:
            res = await session.execute(
                text(
                    f"SELECT executed_at FROM {table} "  # noqa: S608 - 表名是模块常量
                    "WHERE tenant_id = :t AND CAST(user_id AS text) = :u "
                    "AND symbol = :s AND CAST(side AS text) = :side "
                    "AND executed_at IS NOT NULL "
                    "AND executed_at >= :lo AND executed_at <= :hi "
                    "ORDER BY executed_at"
                ),
                {
                    "t": row.tenant or "default",
                    "u": str(row.uid or ""),
                    "s": row.symbol,
                    "side": row.side,
                    "lo": lo,
                    "hi": hi,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 单表读不到不该让定价整轮失败
            print(f"  [重试] {table} 查询失败（跳过）: {exc}")
            continue
        for (cand,) in res.all():
            if cand is None:
                continue
            try:
                ep = as_utc(cand).timestamp()
            except (TypeError, ValueError):
                continue
            # 精确判定用唯一口径（300s 窗口只此一处），SQL 只负责捞候选
            if is_retry_superseded(row, ep) and (best is None or ep < best):
                best = ep
    return best


def cmd_price(args: argparse.Namespace) -> int:
    async def _run() -> int:
        from backend.shared.database_manager_v2 import close_database, get_session

        from backend.scripts.risk_ghost_market import GhostMarket
        from backend.scripts.risk_ghost_store import load_rows, upsert_rows

        attention = False
        try:
            async with get_session(read_only=True) as session:
                rows = await load_rows(
                    session,
                    start=args.start,
                    end=args.end,
                    limit=args.limit,
                    include_test_tenants=args.include_test_tenants,
                )
            todo = [r for r in rows if _needs_pricing(r)]
            print(f"[影子账] 窗口内 {len(rows)} 行，其中待补 {len(todo)} 行")
            if not todo:
                print("[影子账] 没有需要定价的行")

            market = GhostMarket()
            inputs = market.price_inputs(todo) if todo else {}
            now = datetime.now(tz=CST).isoformat(timespec="seconds")
            priced: list[GhostRow] = []
            n_rejected = 0
            n_ok = 0
            async with get_session(read_only=True) as session:
                for r in todo:
                    inp = inputs.get(_gid(r))
                    if inp is None:  # 防御：price_inputs 对每行都给结果
                        continue
                    rts = await _retry_fill_ts(session, r)
                    out, rejected = price_row_monotone(
                        r, inp, priced_at=now, retry_fill_ts=rts
                    )
                    if rejected:
                        n_rejected += 1
                        print(
                            f"  [降级被拒] {r.date} {r.symbol} {r.rule_id} "
                            f"期 {','.join(str(h) for h in rejected)}："
                            "本轮读盘与已存结果冲突，保留已存的"
                        )
                    if any(state_of(out, h) == H_OK for h in HORIZONS):
                        n_ok += 1
                    priced.append(out)

            if not args.apply:
                print(f"[影子账] DRY-RUN（未写库）：{len(priced)} 行待回填，可计价 {n_ok} 行")
                print("  确认无误后加 --apply")
                return EXIT_ATTENTION if n_rejected else EXIT_OK
            if priced:
                async with get_session() as session:
                    n = await upsert_rows(session, priced)
                    await session.commit()
                print(f"[影子账] 已回填 {n} 行（可计价 {n_ok} 行）")
            if n_rejected:
                attention = True
                print(
                    f"  ⚠ 有 {n_rejected} 行的本期结果低于已存结果（降级被拒）——"
                    "这通常意味着**今天这次读盘有问题**（分区没落/日历短了），请查"
                )
        finally:
            await close_database()
        return EXIT_ATTENTION if attention else EXIT_OK

    return asyncio.run(_run())


def _gid(row: GhostRow) -> str:
    from backend.shared.risk.ghost import ghost_id

    return ghost_id(row)


# ── report ──────────────────────────────────────────────────────────
def cmd_report(args: argparse.Namespace) -> int:
    as_of = args.as_of or datetime.now(tz=CST).strftime("%Y-%m-%d")
    shadow = read_shadow_mode()

    async def _run() -> list[GhostRow]:
        from backend.shared.database_manager_v2 import close_database, get_session

        from backend.scripts.risk_ghost_store import load_rows

        try:
            async with get_session(read_only=True) as session:
                return await load_rows(
                    session,
                    start=args.start,
                    end=args.end,
                    regex=args.regex,
                    limit=args.limit,
                    include_test_tenants=args.include_test_tenants,
                )
        finally:
            await close_database()

    rows = asyncio.run(_run())
    rep = build_report(rows, as_of=as_of, shadow_mode=shadow)
    lines = render(rep)
    print("\n".join(lines))

    if not args.no_save:
        out_dir = Path(args.out) if args.out else _reports_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{as_of}_ghost_ledger.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (out_dir / f"{as_of}_ghost_ledger.json").write_text(
            json.dumps(_as_json(rep), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[影子账] 报表已落盘: {out_dir}")

    costly = [s for s in rep.verdicts if s.verdict == V_COSTLY]
    if costly or rep.overdue:
        return EXIT_ATTENTION
    return EXIT_OK


def _as_json(rep: Any) -> dict[str, Any]:
    """报表的机器可读形态（字段与 `RuleStats` 一一对应，不另起口径）。"""
    def _stats(s: Any) -> dict[str, Any]:
        return {
            "rule_id": s.rule_id,
            "kind": s.kind,
            "registered": s.registered,
            "horizon": s.horizon,
            "arm": s.arm,
            "n_counted": s.n_counted,
            "n_unpriced": s.n_unpriced,
            "n_total": s.n_total,
            "unpriced": dict(s.unpriced),
            "mean_cost": s.mean_cost,
            "median_cost": s.median_cost,
            "win_rate": s.win_rate,
            "t_stat": s.t_stat,
            "total_cost": s.total_cost,
            "first_date": s.first_date,
            "last_date": s.last_date,
            "verdict": s.verdict,
        }

    return {
        "as_of": rep.as_of,
        "window": list(rep.window) if rep.window else None,
        "shadow_mode": rep.shadow_mode,
        "n_rows": rep.n_rows,
        "n_shadow": rep.n_shadow,
        "n_enforced": rep.n_enforced,
        "versions": list(rep.versions),
        "horizons": list(rep.horizons),
        "stats": [_stats(s) for s in rep.stats],
        "verdicts": [_stats(s) for s in rep.verdicts],
        "silent_rules": [g.rule_id for g in rep.silent_rules],
        "unknown_rules": list(rep.unknown_rules),
        "overdue": [g.rule_id for g in rep.overdue],
    }


# ── status ──────────────────────────────────────────────────────────
def cmd_status(args: argparse.Namespace) -> int:
    shadow = read_shadow_mode()

    async def _run() -> tuple[dict[str, int], dict[str, int]]:
        """(真账, 全表)——两者的差就是测试租户那批脏行。"""
        from backend.shared.database_manager_v2 import close_database, get_session

        from backend.scripts.risk_ghost_store import count_rows
        from backend.shared.risk.ghost import test_tenant_prefixes

        try:
            async with get_session(read_only=True) as session:
                real = await count_rows(session, exclude_tenants=test_tenant_prefixes())
                every = await count_rows(session)
            return real, every
        finally:
            await close_database()

    try:
        got, every = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        print(f"[影子账] 读库失败：{exc}")
        return EXIT_USAGE
    mode = {True: "shadow=true（判定照跑、未拦单）", False: "shadow=false（闸已翻）"}.get(
        shadow, "shadow 未读到"
    )
    print("=== 风控影子代价账 ===")
    print(f"  配置: {mode}")
    print(f"  行数: {got['rows']}    已定价: {got['priced']}    未定价: {got['rows'] - got['priced']}")
    print(f"  规则数: {got['rules']}    翻闸后行数: {got['enforced']}")
    print(f"  窗口: {got['first_date'] or '-'} ~ {got['last_date'] or '-'}")
    dirty = every["rows"] - got["rows"]
    if dirty:
        print(f"  ⚠ 另有 {dirty} 行属**测试租户**（集成测试写入，已从报表/定价中排除）")
        print("    要清理：DELETE FROM qm_risk_ghost_ledger WHERE tenant_id LIKE ...（见 ghost.py 模块头②）")
    if got["enforced"] and not got["priced"]:
        print("  ⚠ 已有翻闸后的行但一行都没定价——先跑 price，否则账上没有任何已实现代价")
    return EXIT_OK


# ── purge-test-tenants ──────────────────────────────────────────────
def cmd_purge_test_tenants(args: argparse.Namespace) -> int:
    """清掉库里由集成测试写进来的行（**默认 dry-run**）。

    为什么需要一条命令：抽取侧拒收只挡得住将来，**存量脏行已经在库里**，和真账
    同表同形。这些行会进 n、进均值、进结论，账面上看不出任何异常，所以清理必须
    是显式的、可审计的（先清单后删），而不是随手一句 DELETE。
    """
    from backend.shared.risk.ghost import test_tenant_prefixes

    prefixes = test_tenant_prefixes()
    if not prefixes:
        print("[影子账] 拒收前缀清单为空——为防误删全表，本命令拒绝执行")
        return EXIT_USAGE

    async def _run() -> tuple[list[tuple[str, int]], int]:
        from backend.shared.database_manager_v2 import close_database, get_session

        from backend.scripts.risk_ghost_store import delete_test_tenants, list_test_tenants

        try:
            async with get_session(read_only=True) as session:
                found = await list_test_tenants(session, prefixes)
            if not args.apply or not found:
                return found, 0
            async with get_session() as session:
                n = await delete_test_tenants(session, prefixes)
                await session.commit()
            return found, n
        finally:
            await close_database()

    found, n = asyncio.run(_run())
    print(f"[影子账] 命中拒收前缀 {len(found)} 个租户，共 {sum(c for _, c in found)} 行")
    for t, c in found:
        print(f"    {t}  {c} 行")
    if not found:
        print("[影子账] 库里没有测试租户的行（干净）")
        return EXIT_OK
    if not args.apply:
        print("[影子账] DRY-RUN（未删除）；确认清单无误后加 --apply")
        return EXIT_ATTENTION
    print(f"[影子账] 已删除 {n} 行")
    if n != sum(c for _, c in found):
        print("  ⚠ 删除条数与清单不符（期间可能有并发写入），请重跑 status 复核")
        return EXIT_ATTENTION
    return EXIT_OK


# ── rekey-symbols ───────────────────────────────────────────────────
def cmd_rekey_symbols(args: argparse.Namespace) -> int:
    """把库里**非规范标的**的行迁到规范键上（**默认 dry-run**）。

    一次性数据迁移（2026-09-23 加标的归一时留下的旧键）：`ghost_id` 含标的，
    换个写法就是另一个键，实测 615 行真账里 83 行与规范行**撞键**——同一笔单
    记了两遍，两遍都在分母里。迁移是**就地改名**（保住已算好的价），
    不重建：留痕会过期，重抽未必还能拿到同样那批决策。
    """
    from backend.shared.risk.ghost import plan_rekey

    async def _run() -> tuple[Any, dict[str, int]]:
        from backend.shared.database_manager_v2 import close_database, get_session

        from backend.scripts.risk_ghost_store import (
            delete_ids,
            load_rows,
            rename_row_ids,
            upsert_rows,
        )

        try:
            async with get_session(read_only=True) as session:
                # 迁移要覆盖全表（含测试租户留下的非规范行），故显式包含
                rows = await load_rows(session, include_test_tenants=True)
            plan = plan_rekey(rows)
            done = {"renamed": 0, "deleted": 0}
            if not args.apply or plan.is_empty:
                return plan, done
            async with get_session() as session:
                if plan.carried:  # 1) 先把价补给保留者（撞键行的价不能丢）
                    await upsert_rows(session, list(plan.carried))
                # 2) 再删撞键的旧行——必须早于改名，否则新键还被占着
                done["deleted"] = await delete_ids(session, list(plan.doomed))
                # 3) 最后改名（保住全部定价列）
                done["renamed"] = await rename_row_ids(session, list(plan.renames))
                await session.commit()
            return plan, done
        finally:
            await close_database()

    plan, done = asyncio.run(_run())
    print(
        f"[影子账] 非规范标的行：需改名 {len(plan.renames)} 行，"
        f"撞键删除 {len(plan.doomed)} 行（其中补给价 {len(plan.carried)} 行）"
    )
    if plan.is_empty:
        print("[影子账] 全部标的已是规范写法（无需迁移）")
        return EXIT_OK
    for old, new, canon in plan.renames[:5]:
        print(f"    改名 {old} → {new}（{canon}）")
    if len(plan.renames) > 5:
        print(f"    … 其余 {len(plan.renames) - 5} 行同理")
    if not args.apply:
        print("[影子账] DRY-RUN（未改动）；确认无误后加 --apply")
        return EXIT_ATTENTION
    print(f"[影子账] 已改名 {done['renamed']} 行、删除 {done['deleted']} 行")
    if done["renamed"] != len(plan.renames) or done["deleted"] != len(plan.doomed):
        print("  ⚠ 实际改动数与计划不符（期间可能有并发写入），请重跑 status 复核")
        return EXIT_ATTENTION
    return EXIT_OK


# ── 入口 ────────────────────────────────────────────────────────────
def _add_window(p: argparse.ArgumentParser) -> None:
    p.add_argument("--start", help="起始日 YYYY-MM-DD")
    p.add_argument("--end", help="结束日 YYYY-MM-DD")


def _add_test_tenants(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--include-test-tenants",
        action="store_true",
        help="把集成测试租户的行也算进来（默认排除；只在查污染时才该加）",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="风控影子代价账（P1.6）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ex = sub.add_parser("extract", help="留痕 → 影子账行（默认 dry-run）")
    _add_window(p_ex)
    p_ex.add_argument("--days", type=int, default=DEFAULT_EXTRACT_DAYS, help="回溯天数")
    p_ex.add_argument("--apply", action="store_true", help="实际写库")
    _add_test_tenants(p_ex)

    p_pr = sub.add_parser("price", help="影子账行 + 行情 → 事后代价（默认 dry-run）")
    _add_window(p_pr)
    p_pr.add_argument("--limit", type=int, default=None)
    p_pr.add_argument("--apply", action="store_true", help="实际写库")
    _add_test_tenants(p_pr)

    p_re = sub.add_parser("report", help="影子账行 → 人读报表")
    _add_window(p_re)
    _add_test_tenants(p_re)
    p_re.add_argument("--regex", help="只出匹配的规则 id（如 '^l1\\.'）")
    p_re.add_argument("--limit", type=int, default=None)
    p_re.add_argument("--as-of", dest="as_of", help="出表日 YYYY-MM-DD（默认今天 CST）")
    p_re.add_argument("--out", help="输出目录（默认 data/reports/risk_ghost）")
    p_re.add_argument("--no-save", action="store_true", help="只打印，不落盘")

    sub.add_parser("status", help="一眼体检")

    p_pg = sub.add_parser(
        "purge-test-tenants", help="删除集成测试写进来的行（默认 dry-run）"
    )
    p_pg.add_argument("--apply", action="store_true", help="实际删除")

    p_rk = sub.add_parser(
        "rekey-symbols", help="非规范标的的旧键就地迁移（默认 dry-run）"
    )
    p_rk.add_argument("--apply", action="store_true", help="实际迁移")

    args = parser.parse_args()
    handlers = {
        "extract": cmd_extract,
        "price": cmd_price,
        "report": cmd_report,
        "status": cmd_status,
        "purge-test-tenants": cmd_purge_test_tenants,
        "rekey-symbols": cmd_rekey_symbols,
    }
    try:
        return handlers[args.cmd](args)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"[影子账] 环境/参数错误：{exc}")
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
