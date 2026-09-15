#!/usr/bin/env python3
"""模拟盘/信号/调度一键体检（T-P0-07）。

设计（见 docs/可维护性与静默失效防治_设计方案.md §四）：
- 每项检查 = ``CheckResult``（id/name/level/detail/suggestion/metrics）；
- 判定逻辑抽纯函数（如 ``classify_signal_distribution``）供单测，
  IO 只做取数——检查函数只依赖 ``HealthContext`` 的三个注入点
  （query / redis_get / redis_scan），测试可传假实现；
- 有 ``fail`` 项时退出码 = 1，可直接进 cron / CI。

十项断言（原 12 项中「模型契约一致」「风控状态」待 P1/P4 前置设施就绪后补）：
  C01 信号分布（全 HOLD / 分布坍缩）      C06 对账差异
  C02 信号就绪标记与残 run                C07 调度心跳（收盘核对）
  C03 账户键一致性（1 vs 00000001 类）    C08 数据同步新鲜度
  C04 快照 ↔ Redis 同源                   C09 远端行情配置状态
  C05 台账写入（成交必落账）              C10 账户种子存在性

用法（容器内）:
    python backend/scripts/diagnose/health.py                # 全量
    python backend/scripts/diagnose/health.py --only C01,C03
    python backend/scripts/diagnose/health.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from collections.abc import Callable

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, PROJECT_ROOT)

REDIS_DB_GENERAL = 0
REDIS_DB_TRADE = 2

LEVEL_ORDER = {"ok": 0, "warn": 1, "fail": 2}


@dataclass
class CheckResult:
    id: str
    name: str
    level: str  # ok | warn | fail
    detail: str
    suggestion: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthContext:
    """注入式上下文：query(sql, **params)->list[dict]；redis_* 带 db 号。"""

    query: Callable[..., list[dict]]
    redis_get: Callable[[str, int], str | None]
    redis_scan: Callable[[str, int], list[str]]
    today: date


# ---------------------------------------------------------------------------
# 纯判定函数（可单测）
# ---------------------------------------------------------------------------


def classify_signal_distribution(counts: dict[str, int]) -> CheckResult:
    """C01 判定：全 HOLD / 分布坍缩 / 正常。"""
    total = sum(counts.values())
    if total == 0:
        return CheckResult("C01", "信号分布", "fail", "无任何信号数据", "检查推理任务是否执行")
    buy = counts.get("BUY", 0)
    sell = counts.get("SELL", 0)
    hold = counts.get("HOLD", 0)
    metrics = {"total": total, "BUY": buy, "SELL": sell, "HOLD": hold}
    if buy + sell == 0:
        # 2026-08-11「全 HOLD 一个月」事故形态
        return CheckResult(
            "C01",
            "信号分布",
            "fail",
            f"全 HOLD（{total} 行无 BUY/SELL）",
            "检查信号闸门（confidence/共识/归一化强度），见 [RULE:SIGNAL-GATE] 日志",
            metrics,
        )
    side_ratio = max(buy, sell) / total
    if side_ratio > 0.95:
        return CheckResult(
            "C01", "信号分布", "warn", f"分布坍缩：单侧占比 {side_ratio:.1%}", "核对闸门阈值与当日截面", metrics
        )
    return CheckResult("C01", "信号分布", "ok", f"BUY {buy} / SELL {sell} / HOLD {hold}", "", metrics)


def classify_account_key_forms(forms: set[str], numeric_user: str) -> CheckResult:
    """C03 判定：同一数字用户存在多种键形（如 1 与 00000001）。"""
    if len(forms) <= 1:
        return CheckResult("C03", f"账户键一致性(u={numeric_user})", "ok", f"键形唯一：{sorted(forms) or ['-']}")
    return CheckResult(
        "C03",
        f"账户键一致性(u={numeric_user})",
        "fail",
        f"同用户多种键形共存：{sorted(forms)}",
        "统一走 require_sim_user_id 归一（曲线/账户/下单同一口径），确认哪份是活跃账本",
        {"forms": sorted(forms)},
    )


def classify_snapshot_consistency(
    redis_total: float, snapshot_total: float, tolerance: float = 1.0
) -> CheckResult:
    """C04 判定：用户级快照总资产 vs Redis 各市场账户之和。"""
    diff = abs(redis_total - snapshot_total)
    if diff <= tolerance:
        return CheckResult("C04", "快照同源", "ok", f"总资产一致（差 {diff:.2f}）")
    return CheckResult(
        "C04",
        "快照同源",
        "fail",
        f"快照 {snapshot_total:.2f} vs Redis {redis_total:.2f}（差 {diff:.2f}）",
        "检查 capture_all 聚合口径与权益结算 worker 是否在跑",
        {"redis_total": redis_total, "snapshot_total": snapshot_total},
    )


def classify_ledger_writes(ledger_rows: int, accounts_with_positions: int) -> CheckResult:
    """C05 判定：PG 台账写入 vs Redis 有持仓账户。"""
    if ledger_rows > 0:
        return CheckResult("C05", "台账写入", "ok", f"PG 台账 {ledger_rows} 行")
    if accounts_with_positions > 0:
        return CheckResult(
            "C05",
            "台账写入",
            "warn",
            f"PG 台账为空，但 {accounts_with_positions} 个 Redis 账户有持仓",
            "Ledger 写入链未填充（T-P1-04）；成交必落账原则暂未闭环",
            {"ledger_rows": 0, "accounts_with_positions": accounts_with_positions},
        )
    return CheckResult("C05", "台账写入", "ok", "台账与账户均为空（无交易）")


# ---------------------------------------------------------------------------
# 检查实现（IO 经 ctx 注入）
# ---------------------------------------------------------------------------

_USER_KEY_RE = re.compile(r"^simulation:account:([^:]+):([^:]+)(?::([A-Z]+))?$")


async def check_c01_signal_distribution(ctx: HealthContext) -> CheckResult:
    rows = ctx.query(
        "SELECT signal_side, count(*) AS n FROM engine_signal_scores "
        "WHERE trade_date = (SELECT max(trade_date) FROM engine_signal_scores) "
        "GROUP BY signal_side"
    )
    counts = {str(r["signal_side"]): int(r["n"]) for r in rows}
    return classify_signal_distribution(counts)


async def check_c02_signal_readiness(ctx: HealthContext) -> CheckResult:
    rows = ctx.query(
        "SELECT max(trade_date) AS d FROM engine_signal_scores"
    )
    latest = rows[0]["d"] if rows else None
    if latest is None:
        return CheckResult("C02", "信号就绪", "fail", "信号表为空", "检查推理任务")
    latest_str = str(latest)
    # T-P1-02：首选就绪标记（全量校验通过才置位）；完成标记为兼容回退
    from backend.shared.inference_lock import ready_key

    ready_raw = ctx.redis_get(ready_key("CN", latest_str), REDIS_DB_GENERAL)
    marker = ctx.redis_get(f"qm:inference:completed:{latest_str}", REDIS_DB_GENERAL)
    # 残 run 迹象：近 7 日单日多 run（>2 说明重跑/竞态频发）
    run_rows = ctx.query(
        "SELECT count(DISTINCT run_id) AS n FROM engine_signal_scores "
        "WHERE trade_date = :d",
        d=latest,
    )
    runs = int(run_rows[0]["n"]) if run_rows else 0
    if not ready_raw and not marker:
        return CheckResult(
            "C02",
            "信号就绪",
            "warn",
            f"{latest_str} 无就绪/完成标记（runs={runs}）",
            "确认推理调度执行；标记键 qm:signal:ready:{market}:{date}",
            {"trade_date": latest_str, "runs": runs},
        )
    ready_desc = (
        f"就绪标记={str(ready_raw)[:48]}…" if ready_raw else "无就绪标记（回退读完成标记）"
    )
    detail = f"{latest_str} {ready_desc} runs={runs}"
    if runs > 2:
        return CheckResult("C02", "信号就绪", "warn", detail + "（同日多 run，检查竞态/回填）")
    return CheckResult("C02", "信号就绪", "ok", detail)


async def check_c03_account_key_consistency(ctx: HealthContext) -> CheckResult:
    keys = ctx.redis_scan("simulation:account:*", REDIS_DB_TRADE)
    grouped: dict[tuple[str, str], set[str]] = {}
    for key in keys:
        m = _USER_KEY_RE.match(key)
        if not m:
            continue
        tenant, user, _market = m.group(1), m.group(2), m.group(3)
        if not user.isdigit():
            continue
        grouped.setdefault((tenant, str(int(user))), set()).add(user)
    bad = [classify_account_key_forms(forms, num) for (_, num), forms in grouped.items() if len(forms) > 1]
    if bad:
        return bad[0] if len(bad) == 1 else CheckResult(
            "C03",
            "账户键一致性",
            "fail",
            f"{len(bad)} 个用户存在多键形：" + "; ".join(r.detail for r in bad),
            "统一 require_sim_user_id 归一口径",
        )
    return CheckResult("C03", "账户键一致性", "ok", f"扫描 {len(keys)} 键，无多键形")


async def check_c04_snapshot_consistency(ctx: HealthContext) -> CheckResult:
    keys = ctx.redis_scan("simulation:account:*", REDIS_DB_TRADE)
    per_user: dict[tuple[str, str], float] = {}
    for key in keys:
        m = _USER_KEY_RE.match(key)
        if not m:
            continue
        tenant, user = m.group(1), m.group(2)
        raw = ctx.redis_get(key, REDIS_DB_TRADE)
        if not raw:
            continue
        try:
            total = float(json.loads(raw).get("total_asset") or 0)
        except (TypeError, ValueError):
            continue
        per_user[(tenant, user)] = per_user.get((tenant, user), 0.0) + total

    rows = ctx.query(
        "SELECT tenant_id, user_id, total_asset FROM simulation_fund_snapshots "
        "WHERE snapshot_date = (SELECT max(snapshot_date) FROM simulation_fund_snapshots)"
    )
    # 快照的用户键是账户键解析原文；Redis 侧同口径比对
    snapshot_total = {(str(r["tenant_id"]), str(r["user_id"])): float(r["total_asset"] or 0) for r in rows}
    checked = 0
    for (tenant, user), redis_total in per_user.items():
        if (tenant, user) not in snapshot_total:
            continue
        checked += 1
        result = classify_snapshot_consistency(redis_total, snapshot_total[(tenant, user)])
        if result.level != "ok":
            result.id = "C04"
            result.detail = f"u={tenant}:{user} " + result.detail
            return result
    if checked == 0:
        return CheckResult("C04", "快照同源", "warn", "无可比对行（快照或账户为空）")
    return CheckResult("C04", "快照同源", "ok", f"{checked} 个用户快照与 Redis 一致")


async def check_c05_ledger_writes(ctx: HealthContext) -> CheckResult:
    rows = ctx.query("SELECT (SELECT count(*) FROM sim_trades) AS trades")
    trades = int(rows[0]["trades"]) if rows else 0
    keys = ctx.redis_scan("simulation:account:*", REDIS_DB_TRADE)
    with_positions = 0
    for key in keys:
        raw = ctx.redis_get(key, REDIS_DB_TRADE)
        if not raw:
            continue
        try:
            positions = json.loads(raw).get("positions") or {}
        except (TypeError, ValueError):
            continue
        if any(float((p or {}).get("volume") or 0) > 0 for p in positions.values()):
            with_positions += 1
    return classify_ledger_writes(trades, with_positions)


async def check_c06_reconcile_diff(ctx: HealthContext) -> CheckResult:
    try:
        rows = ctx.query(
            "SELECT * FROM simulation_reconcile_reports ORDER BY checked_at DESC LIMIT 1"
        )
    except Exception as exc:  # 表缺失等
        return CheckResult("C06", "对账差异", "warn", f"查询失败: {exc}")
    if not rows:
        return CheckResult("C06", "对账差异", "warn", "无对账报告")
    row = rows[0]
    diff = None
    for k, v in row.items():
        if "diff" in str(k).lower() and isinstance(v, (int, float)):
            diff = int(v)
            break
    if diff is None:
        return CheckResult("C06", "对账差异", "warn", "报告无差异字段，无法判定")
    if diff > 0:
        return CheckResult("C06", "对账差异", "fail", f"最近一次对账 {diff} 处差异", "查 reconcile_reports 明细")
    return CheckResult("C06", "对账差异", "ok", "最近一次对账零差异")


async def check_c07_scheduler_heartbeat(ctx: HealthContext) -> CheckResult:
    rows = ctx.query("SELECT max(trade_date) AS d FROM engine_signal_scores")
    latest = rows[0]["d"] if rows else None
    if latest is None:
        return CheckResult("C07", "调度心跳", "warn", "无交易日基准")
    ymd = str(latest).replace("-", "")
    audit = ctx.redis_get(f"trade:close-audit:done:{ymd}", REDIS_DB_TRADE)
    if not audit:
        return CheckResult(
            "C07", "调度心跳", "warn", f"收盘核对标记缺失 trade:close-audit:done:{ymd}", "确认 close_cleanup_audit 任务运行"
        )
    return CheckResult("C07", "调度心跳", "ok", f"收盘核对已执行（{ymd}）")


async def check_c08_data_sync_freshness(ctx: HealthContext) -> CheckResult:
    rows = ctx.query("SELECT max(trade_date) AS d FROM engine_signal_scores")
    latest = rows[0]["d"] if rows else None
    if latest is None:
        return CheckResult("C08", "数据同步", "warn", "无交易日基准")
    d = str(latest)
    marker = ctx.redis_get(f"quantmind:sync_schedule_last_run:A:{d}", REDIS_DB_GENERAL)
    if not marker:
        return CheckResult(
            "C08", "数据同步", "warn", f"A 股最近交易日 {d} 无同步记录", "检查同步调度配置与执行"
        )
    return CheckResult("C08", "数据同步", "ok", f"A 股 {d} 已同步")


async def check_c09_remote_quote_config(ctx: HealthContext) -> CheckResult:
    from backend.shared.remote_quote_config import resolve_remote_quote_redis

    resolved = resolve_remote_quote_redis()
    if resolved is None:
        return CheckResult(
            "C09", "远端行情配置", "warn", "远端行情已禁用/未配置（撮合走本地日线兜底）", "如需盘中实时取价，配置 REMOTE_QUOTE_REDIS_*"
        )
    host, port, _password, db = resolved
    return CheckResult("C09", "远端行情配置", "ok", f"{host}:{port}/db{db}")


async def check_c10_initial_seed_presence(ctx: HealthContext) -> CheckResult:
    keys = ctx.redis_scan("simulation:account:*", REDIS_DB_TRADE)
    unknown: list[str] = []
    for key in keys:
        raw = ctx.redis_get(key, REDIS_DB_TRADE)
        if not raw:
            continue
        try:
            acc = json.loads(raw)
        except (TypeError, ValueError):
            continue
        has_seed = acc.get("initial_cash") is not None
        positions = acc.get("positions") or {}
        traded = any(float((p or {}).get("volume") or 0) > 0 for p in positions.values())
        if traded and not has_seed:
            # CN 可回退 settings；非 CN 无种子即"未知"（快照求和时被排除）
            market = str(acc.get("market") or "").upper()
            if market not in ("", "CN"):
                unknown.append(key)
    if unknown:
        return CheckResult(
            "C10",
            "账户种子",
            "warn",
            f"{len(unknown)} 个非 CN 已交易账户缺 initial_cash：{unknown[:3]}",
            "该账户在用户级快照中种子未知（不计入 initial_capital）；新账户已自动落种子",
        )
    return CheckResult("C10", "账户种子", "ok", f"扫描 {len(keys)} 键，无缺种子账户")


CHECKS: list[tuple[str, str, Callable]] = [
    ("C01", "信号分布", check_c01_signal_distribution),
    ("C02", "信号就绪与残 run", check_c02_signal_readiness),
    ("C03", "账户键一致性", check_c03_account_key_consistency),
    ("C04", "快照同源", check_c04_snapshot_consistency),
    ("C05", "台账写入", check_c05_ledger_writes),
    ("C06", "对账差异", check_c06_reconcile_diff),
    ("C07", "调度心跳", check_c07_scheduler_heartbeat),
    ("C08", "数据同步", check_c08_data_sync_freshness),
    ("C09", "远端行情配置", check_c09_remote_quote_config),
    ("C10", "账户种子", check_c10_initial_seed_presence),
]


def summarize(results: list[CheckResult]) -> dict[str, int]:
    """汇总计数（纯函数）。"""
    counts = {"ok": 0, "warn": 0, "fail": 0}
    for r in results:
        counts[r.level] = counts.get(r.level, 0) + 1
    return counts


def exit_code(results: list[CheckResult]) -> int:
    return 1 if any(r.level == "fail" for r in results) else 0


# ---------------------------------------------------------------------------
# 真实环境上下文与入口
# ---------------------------------------------------------------------------


def _build_context() -> HealthContext:
    import redis as redis_lib
    from sqlalchemy import create_engine, text

    db_url = os.getenv(
        "DATABASE_URL",
        f"postgresql://{os.getenv('DB_USER', 'quantmind')}:{os.getenv('DB_PASSWORD', '')}"
        f"@{os.getenv('DB_HOST', 'db')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'quantmind')}",
    )
    # DATABASE_URL 一般是 asyncpg 方言，体检脚本用同步驱动
    sync_url = db_url.replace("+asyncpg", "")
    engine = create_engine(sync_url, pool_pre_ping=True)

    redis_host = os.getenv("REDIS_HOST", "redis")
    redis_port = int(os.getenv("REDIS_PORT", "6379"))
    clients = {
        REDIS_DB_GENERAL: redis_lib.Redis(host=redis_host, port=redis_port, db=REDIS_DB_GENERAL),
        REDIS_DB_TRADE: redis_lib.Redis(host=redis_host, port=redis_port, db=REDIS_DB_TRADE),
    }

    def query(sql: str, **params) -> list[dict]:
        with engine.connect() as conn:
            result = conn.execute(text(sql), params)
            return [dict(row._mapping) for row in result]

    def redis_get(key: str, db: int) -> str | None:
        raw = clients[db].get(key)
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    def redis_scan(pattern: str, db: int) -> list[str]:
        return [k.decode() if isinstance(k, bytes) else str(k) for k in clients[db].scan_iter(pattern)]

    return HealthContext(query=query, redis_get=redis_get, redis_scan=redis_scan, today=date.today())


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟盘/信号/调度一键体检")
    parser.add_argument("--only", default="", help="只跑指定项，如 C01,C03")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    selected = {s.strip().upper() for s in args.only.split(",") if s.strip()}
    ctx = _build_context()
    results: list[CheckResult] = []
    for cid, _name, fn in CHECKS:
        if selected and cid not in selected:
            continue
        try:
            import asyncio

            results.append(asyncio.run(fn(ctx)))
        except Exception as exc:  # 单项异常不拖垮整体
            results.append(CheckResult(cid, _name, "fail", f"检查执行异常: {exc}"))
        if args.json:
            continue
        r = results[-1]
        icon = {"ok": "✓", "warn": "!", "fail": "✗"}[r.level]
        print(f"{icon} [{r.id}] {r.name}: {r.detail}")
        if r.suggestion:
            print(f"    → {r.suggestion}")

    counts = summarize(results)
    if args.json:
        print(json.dumps({"summary": counts, "results": [r.__dict__ for r in results]}, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"\n合计: ok={counts['ok']} warn={counts['warn']} fail={counts['fail']}")
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
