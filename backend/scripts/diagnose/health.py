#!/usr/bin/env python3
"""模拟盘/信号/调度一键体检（T-P0-07）。

设计（见 docs/可维护性与静默失效防治_设计方案.md §四）：
- 每项检查 = ``CheckResult``（id/name/level/detail/suggestion/metrics）；
- 判定逻辑抽纯函数（如 ``classify_signal_distribution``）供单测，
  IO 只做取数——检查函数只依赖 ``HealthContext`` 的三个注入点
  （query / redis_get / redis_scan），测试可传假实现；
- 有 ``fail`` 项时退出码 = 1，可直接进 cron / CI。

十四项断言（「模型契约一致」「风控状态」待 P1/P4 前置设施就绪后补）：
  C01 信号分布（全 HOLD / 分布坍缩）      C06 对账差异
  C02 信号就绪标记与残 run                C07 调度心跳（收盘核对）
  C03 账户键一致性（1 vs 00000001 类）    C08 数据同步新鲜度
  C04 快照 ↔ Redis 同源                   C09 远端行情配置状态
  C05 台账写入（成交必落账）              C10 账户种子存在性
  C11 runner 只读 DB 账号                 C12 本地行情数据可用性
  C13 真日历覆盖年限（决策轮 fail-closed 的硬期限）
  C14 分账账本一致性（LLM 腿成交 ↔ 分账流水 ↔ 成交唯一键）

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
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any
from collections.abc import Callable

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, PROJECT_ROOT)

REDIS_DB_GENERAL = 0
REDIS_DB_TRADE = 2

#: C13 真日历覆盖年限阈值（天）：距「最后一天」不足 WARN 告警、不足 FAIL 报红。
#: 提前一个季度告警——这段窗口足够升级 exchange_calendars，或把次年交易日
#: 落进 qm_market_calendar_day（DB override 优先于真日历）。
CALENDAR_WARN_DAYS = 90
CALENDAR_FAIL_DAYS = 30

#: C14 回看窗口（天）。比 C05 的 7 天长：分账流水是幂等键的载体，一笔错账不会随时间
#: 自愈（该 agent 的虚拟现金与持仓会一直偏），30 天足够覆盖一次真实成交、又不至于翻成年账。
AGENT_LEDGER_WINDOW_DAYS = 30

#: 窗口两侧各放宽一天再取数：成交侧按 ``executed_at``（瞬时）滤，账本侧按 ``trade_date``
#: （日期）滤，两者由同一个成交瞬时推出来但落点不同——窗口卡死会在边界日把一对合法
#: 记录判成「漏记 + 孤儿」。
AGENT_LEDGER_WINDOW_SLACK_DAYS = 1

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
    """注入式上下文：query(sql, **params)->list[dict]；redis_* 带 db 号。

    ``market``：体检的**数据市场**（CN/HK/US/FUTURES/CRYPTO）。默认 CN 保持命令行
    旧行为；交易台按页签市场传入，否则港股/美股页签会拿 A 股信号与同步记录当
    自己的健康结论（C01 信号分布 / C02 信号就绪 / C08 数据同步 三处按市场取数）。
    """

    query: Callable[..., list[dict]]
    redis_get: Callable[[str, int], str | None]
    redis_scan: Callable[[str, int], list[str]]
    today: date
    market: str = "CN"


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


def classify_cid_duplicates(
    dup_rows: list, *, table: str = "sim_orders"
) -> CheckResult:
    """C05c 判定（T-P2-06 / P2.7-⑧）：幂等键 (tenant,user,client_order_id) 重复扫描。

    重复 = 幂等键在 DB 层失守的痕迹（各写路径"先查后插"竞态）。硬约束唯一索引
    需先统一各路径的 IntegrityError→重复语义（T-P2-08，记录在案），本项先做可见性。
    ``table`` 分开跑模拟（``sim_orders``）与实盘（``orders``）两张台账：REAL 侧的重复
    不只是幂等失守，还会让「旧全库唯一约束 → 租户/账户限定唯一」的迁移**停手不迁**
    （见 ``order_contract.ensure_real_order_scope_unique_index_async``）。
    """
    rows = [dict(r) for r in (dup_rows or [])]
    if not rows:
        return CheckResult("C05", "台账写入", "ok", "幂等键无重复", "", {"cid_dup_groups": 0})
    sample = "，".join(
        f"{r.get('tenant_id')}:{r.get('user_id')}:{r.get('client_order_id')}"
        for r in rows[:3]
    )
    return CheckResult(
        "C05",
        "台账写入",
        "fail",
        f"{table} 幂等键重复 {len(rows)} 组（{sample}）",
        "查重复 client_order_id 来源路径；唯一索引落地见 T-P2-08 / P2.7-⑧",
        {"cid_dup_groups": len(rows)},
    )


def classify_ledger_coverage(trades_7d: int, covered_7d: int) -> CheckResult:
    """C05b 判定（T-P1-04）：近 7 日成交是否全部有对应 cash_ledger 流水。

    过渡期口径：历史成交（T-P1-04 前）未落账属已知缺口 → warn 并点名笔数；
    连续 7 日无未覆盖成交后自然转绿。若出现"新成交不落账"即视为链路回归。
    """
    if trades_7d <= 0:
        return CheckResult("C05", "台账写入", "ok", "近 7 日无成交（覆盖率检查跳过）")
    missing = max(0, int(trades_7d) - int(covered_7d))
    if missing == 0:
        return CheckResult(
            "C05", "台账写入", "ok", f"近 7 日 {trades_7d} 笔成交全部落账", "", {"trades_7d": trades_7d}
        )
    return CheckResult(
        "C05",
        "台账写入",
        "warn",
        f"近 7 日 {trades_7d} 笔成交中 {missing} 笔无 cash_ledger 流水",
        "历史成交属 T-P1-04 前已知缺口（7 日内自然过期）；若为新增成交则是落账链路回归，查 [CONTRACT:LEDGER] 日志",
        {"trades_7d": trades_7d, "covered_7d": covered_7d},
    )


# ---------------------------------------------------------------------------
# 分账账本 ↔ 成交 对账（P2.7）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerParity:
    """分账账本 ↔ 成交的对账结果（纯数据；判定见 :func:`classify_agent_ledger_parity`）。

    以**订单**为锚配对：账本流水行带 ``order_id``，成交行经同一列也能归组，
    同一张委托的多次部分成交天然成对。
    """

    orders: int
    trades: int
    ledger_rows: int
    posted: int
    missing: tuple[str, ...]
    mismatch: tuple[str, ...]
    dup_posts: tuple[str, ...]
    orphans: tuple[str, ...]
    unapplied: tuple[str, ...]
    index_enabled: bool


def _group_by_order(rows: Any) -> dict[tuple[str, str, str], list[dict]]:
    """按 ``(租户, 用户, 订单号)`` 分组。"""
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows or ():
        item = dict(row)
        key = (
            str(item.get("tenant_id") or ""),
            str(item.get("user_id") or ""),
            str(item.get("order_id") or ""),
        )
        grouped.setdefault(key, []).append(item)
    return grouped


def _take_exact(index: dict[str, list[int]], used: set[int], fill_key: str) -> int | None:
    """按成交号精确取一行；取走的划掉——同一行不会被两笔成交认领。"""
    for i in index.get(fill_key, ()):
        if i not in used:
            used.add(i)
            return i
    return None


def _take_prefixed(index: dict[str, list[int]], used: set[int], prefix: str) -> int | None:
    """取一行**合成键**流水（``qmt-synth-…``）：合成成交先落账、真实明细到达后就地升级
    改了成交行 ``exchange_trade_id``，而账本流水按「流水即事实」不跟着改键——这是登记在
    ``agent_ledger_fill`` 里的**有界偏差**，对账必须认这个配对，否则每笔升级都会报漏记。
    """
    for fill_key in sorted(index):
        if not fill_key.startswith(prefix):
            continue
        for i in index[fill_key]:
            if i not in used:
                used.add(i)
                return i
    return None


def ledger_parity(
    trades: Any,
    ledger_fills: Any,
    *,
    index_enabled: bool,
    synth_prefix: str,
) -> LedgerParity:
    """逐单配对（纯函数）：成交行 ↔ 账本流水行，分成五类偏差。

    * ``missing`` —— 成交有、流水没有：账本少记一笔（额度/持仓一直偏小）；
    * ``mismatch`` —— 配对成功但 ``agent`` 不一致：钱记到了别人名下；
    * ``dup_posts`` —— 账本多出的**合成键**流水没有成交行认领：合成成交落账后又把真实
      成交单独记了一遍 ⇒ 虚拟现金被多扣一笔（双记）；
    * ``orphans`` —— 账本多出的**真实键**流水（历史导入 / 成交行被删），要人看；
    * ``unapplied`` —— 流水落了但 ``applied_volume = 0``（方向未知/代码识别不出/卖出超
      持仓被裁剪）：账本其实没反映这笔成交，原因在 ``note`` 列。

    ``synth_prefix`` 由调用方给（与写入方同一个常量），不在这里写死字面量。
    """
    trades_by_order = _group_by_order(trades)
    ledger_by_order = _group_by_order(ledger_fills)
    posted = 0
    missing: list[str] = []
    mismatch: list[str] = []
    dup_posts: list[str] = []
    orphans: list[str] = []
    unapplied: list[str] = []

    for key in sorted(set(trades_by_order) | set(ledger_by_order)):
        order_id = key[2]
        pool = ledger_by_order.get(key, [])
        by_fill_key: dict[str, list[int]] = {}
        for i, row in enumerate(pool):
            fill_key = str(row.get("fill_key") or "")
            by_fill_key.setdefault(fill_key, []).append(i)
            if float(row.get("applied_volume") or 0.0) == 0.0:
                unapplied.append(f"{order_id}:{fill_key}")
        used: set[int] = set()
        for trade in trades_by_order.get(key, []):
            fill_key = str(trade.get("exchange_trade_id") or "")
            agent = str(trade.get("agent") or "")
            partner = _take_exact(by_fill_key, used, fill_key)
            if partner is None:
                partner = _take_prefixed(by_fill_key, used, synth_prefix)
            if partner is None:
                missing.append(f"{order_id}:{fill_key}")
                continue
            booked = str(pool[partner].get("agent") or "")
            if booked != agent:
                mismatch.append(f"{order_id}:{fill_key}（订单 {agent} / 账本 {booked}）")
                continue
            posted += 1
        for i, row in enumerate(pool):
            if i in used:
                continue
            fill_key = str(row.get("fill_key") or "")
            if fill_key.startswith(synth_prefix):
                dup_posts.append(f"{order_id}:{fill_key}")
            else:
                orphans.append(f"{order_id}:{fill_key}")

    return LedgerParity(
        orders=len(set(trades_by_order) | set(ledger_by_order)),
        trades=sum(len(v) for v in trades_by_order.values()),
        ledger_rows=sum(len(v) for v in ledger_by_order.values()),
        posted=posted,
        missing=tuple(missing),
        mismatch=tuple(mismatch),
        dup_posts=tuple(dup_posts),
        orphans=tuple(orphans),
        unapplied=tuple(unapplied),
        index_enabled=index_enabled,
    )


#: C14 取数 SQL（两侧都绑 ``since``：一个日期）。抽成常量是为了让真库用例跑**同一份**
#: SQL——体检的假上下文用例只证明「判定对」，取数断裂（列改名/表搬家）只有真库能证。
C14_TRADES_SQL = (
    "SELECT t.tenant_id, t.user_id, t.order_id, t.exchange_trade_id, o.agent "
    "FROM trades t JOIN orders o ON o.order_id = t.order_id "
    "WHERE o.agent IS NOT NULL AND o.agent <> '' "
    "AND t.exchange_trade_id IS NOT NULL AND t.executed_at >= :since"
)
C14_LEDGER_SQL = (
    "SELECT tenant_id, user_id, order_id, fill_key, agent, applied_volume, note "
    "FROM qm_agent_ledger_fill WHERE trade_date >= :since"
)

#: C14 各偏差类别 → (字段, 级别, 人话标签, 建议)。顺序 = 优先级（同级按业务后果排）。
_LEDGER_PARITY_RULES: tuple[tuple[str, str, str, str], ...] = (
    (
        "missing",
        "fail",
        "漏记",
        "账本少记：额度与持仓会一直偏小（该 agent 可以多买）。账本写失败会整笔回滚"
        "（成交行也在同一事务里），所以「成交在、流水不在」正常不会出现——查 [AgentLedger] "
        "告警、账本表写入权限与写入方是否被改成了先提交后落账",
    ),
    (
        "mismatch",
        "fail",
        "归属不符",
        "账本把成交记在了另一个 agent 名下：等于替别人加仓、替自己减仓，两边额度都会错。"
        "查该订单的 agent 与账本段名是否逐字一致（幂等键段/台账段/订单列三处共用一个名字）",
    ),
    (
        "dup_posts",
        "fail",
        "重复记账",
        "同一笔成交在账本里记了两次（合成成交先落账、真实成交又单独记了一笔）⇒ "
        "虚拟现金被多扣一笔、持仓翻倍。该订单的成交表已收敛（合成行被删/被改键），"
        "账本流水按「流水即事实」不会自动回滚——需人工核对后按冲正处理",
    ),
    (
        "unapplied",
        "warn",
        "记了未生效",
        "流水落了但一行都没生效（applied_volume = 0）：方向未知、代码识别不出或卖出超"
        "持仓被裁剪。账本的现金与持仓其实没反映这笔成交，原因写在流水 note 列",
    ),
    (
        "orphans",
        "warn",
        "无对应成交",
        "账本有流水、成交表没有这一笔：历史导入（隔壁台账迁移）或成交行被删过。"
        "两种都要人看一眼，确认不是「成交被删而账本留着」",
    ),
)


def classify_agent_ledger_parity(p: LedgerParity) -> CheckResult:
    """C14 判定：每一笔 LLM 腿成交在分账账本里有且只有一条流水，且归属一致。

    零参与（窗口内既无 LLM 腿成交、账本也无流水）**如实写「未参与」**——这条检查只有
    在分账真的跑起来之后才可能变红，别把它读成「账本没问题」。成交唯一键未启用单独告警：
    它不在时并发/重投双写没有 DB 兜底，账本可能双记。
    """
    metrics = {
        "orders": p.orders,
        "trades": p.trades,
        "ledger_rows": p.ledger_rows,
        "posted": p.posted,
        "missing": len(p.missing),
        "agent_mismatch": len(p.mismatch),
        "dup_posts": len(p.dup_posts),
        "orphans": len(p.orphans),
        "unapplied": len(p.unapplied),
        "trade_unique_index": p.index_enabled,
    }
    index_note = "" if p.index_enabled else "；成交唯一键未启用（并发双写无 DB 兜底）"
    index_fix = (
        "成交唯一键（(租户, 用户, 成交号) 部分唯一）未建：重投/并发双写时没有 DB 兜底，"
        "账本可能双记。正常由 trade 服务启动期自愈；建不起来多半是存量重复成交挡着"
        "（先去重），见 C14 告警里的重复组"
    )
    if p.trades == 0 and p.ledger_rows == 0:
        if p.index_enabled:
            return CheckResult(
                "C14", "分账账本一致性", "ok", "近 30 日无 LLM 腿成交、账本无流水（分账未参与）",
                metrics=metrics,
            )
        return CheckResult(
            "C14",
            "分账账本一致性",
            "warn",
            f"近 30 日无 LLM 腿成交、账本无流水（分账未参与）{index_note}",
            index_fix,
            metrics,
        )

    parts: list[str] = []
    suggestions: list[str] = []
    level = "ok"
    for bucket, lv, label, fix in _LEDGER_PARITY_RULES:
        rows = getattr(p, bucket)
        if not rows:
            continue
        parts.append(f"{len(rows)} 条{label}（如 {'，'.join(rows[:3])}）")
        suggestions.append(fix)
        if LEVEL_ORDER[lv] > LEVEL_ORDER[level]:
            level = lv
    if not p.index_enabled:
        parts.append("成交唯一键未启用")
        suggestions.append(index_fix)
        if LEVEL_ORDER["warn"] > LEVEL_ORDER[level]:
            level = "warn"
    if not parts:
        return CheckResult(
            "C14",
            "分账账本一致性",
            "ok",
            f"近 30 日 {p.trades} 笔 LLM 腿成交全部落账（账本 {p.ledger_rows} 行，归属一致）",
            metrics=metrics,
        )
    return CheckResult(
        "C14",
        "分账账本一致性",
        level,
        f"近 30 日 {p.trades} 笔 LLM 腿成交 / 账本 {p.ledger_rows} 行：" + "；".join(parts),
        " ".join(dict.fromkeys(suggestions)),
        metrics,
    )


# ---------------------------------------------------------------------------
# 检查实现（IO 经 ctx 注入）
# ---------------------------------------------------------------------------

_USER_KEY_RE = re.compile(r"^simulation:account:([^:]+):([^:]+)(?::([A-Z]+))?$")


async def check_c01_signal_distribution(ctx: HealthContext) -> CheckResult:
    rows = ctx.query(
        "SELECT signal_side, count(*) AS n FROM engine_signal_scores "
        "WHERE COALESCE(market, 'CN') = :m "
        "AND trade_date = (SELECT max(trade_date) FROM engine_signal_scores WHERE COALESCE(market, 'CN') = :m) "
        "GROUP BY signal_side",
        m=ctx.market,
    )
    counts = {str(r["signal_side"]): int(r["n"]) for r in rows}
    result = classify_signal_distribution(counts)
    # 空态说明市场：不分市场时「无任何信号数据」看不出是哪个市场没有
    if result.level == "fail" and not counts:
        return CheckResult(
            "C01", "信号分布", "fail", f"{ctx.market} 市场无任何信号数据", "检查该市场推理任务是否执行"
        )
    return replace(result, metrics={**result.metrics, "market": ctx.market})


async def check_c02_signal_readiness(ctx: HealthContext) -> CheckResult:
    rows = ctx.query(
        "SELECT max(trade_date) AS d FROM engine_signal_scores WHERE COALESCE(market, 'CN') = :m",
        m=ctx.market,
    )
    latest = rows[0]["d"] if rows else None
    if latest is None:
        return CheckResult(
            "C02",
            "信号就绪",
            "fail",
            f"{ctx.market} 市场信号表为空",
            "检查该市场推理任务",
            {"market": ctx.market},
        )
    latest_str = str(latest)
    # T-P1-02：首选就绪标记（全量校验通过才置位）；完成标记为兼容回退
    from backend.shared.inference_lock import ready_key

    ready_raw = ctx.redis_get(ready_key(ctx.market, latest_str), REDIS_DB_GENERAL)
    marker = ctx.redis_get(f"qm:inference:completed:{latest_str}", REDIS_DB_GENERAL)
    # 残 run 迹象：近 7 日单日多 run（>2 说明重跑/竞态频发）
    run_rows = ctx.query(
        "SELECT count(DISTINCT run_id) AS n FROM engine_signal_scores "
        "WHERE trade_date = :d AND COALESCE(market, 'CN') = :m",
        d=latest,
        m=ctx.market,
    )
    runs = int(run_rows[0]["n"]) if run_rows else 0
    if not ready_raw and not marker:
        return CheckResult(
            "C02",
            "信号就绪",
            "warn",
            f"{ctx.market} {latest_str} 无就绪/完成标记（runs={runs}）",
            "确认推理调度执行；标记键 qm:signal:ready:{market}:{date}",
            {"trade_date": latest_str, "runs": runs, "market": ctx.market},
        )
    ready_desc = (
        f"就绪标记={str(ready_raw)[:48]}…" if ready_raw else "无就绪标记（回退读完成标记）"
    )
    detail = f"{latest_str} {ready_desc} runs={runs}"
    metrics = {"trade_date": latest_str, "runs": runs, "market": ctx.market}
    if runs > 2:
        return CheckResult(
            "C02", "信号就绪", "warn", detail + "（同日多 run，检查竞态/回填）", metrics=metrics
        )
    return CheckResult("C02", "信号就绪", "ok", detail, metrics=metrics)


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

    # T-P1-07：快照带市场维度后必须显式取合并行（'ALL'）——否则同日各市场行与
    # 合并行混排，(tenant,user) 字典项会被不确定的一行覆盖（误报/漏报）
    has_market_col = bool(
        ctx.query(
            "SELECT 1 AS present FROM information_schema.columns "
            "WHERE table_name = 'simulation_fund_snapshots' "
            "AND column_name = 'market' LIMIT 1"
        )
    )
    market_clause = "AND market = 'ALL' " if has_market_col else ""
    rows = ctx.query(
        "SELECT tenant_id, user_id, total_asset FROM simulation_fund_snapshots "
        "WHERE snapshot_date = (SELECT max(snapshot_date) FROM simulation_fund_snapshots) "
        f"{market_clause}"
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
    presence = classify_ledger_writes(trades, with_positions)
    if presence.level != "ok":
        return presence
    # T-P2-06：幂等键重复扫描（fail 级——DB 层幂等失守的痕迹）
    try:
        dup_rows = ctx.query(
            "SELECT tenant_id, user_id, client_order_id, count(*) AS c FROM sim_orders "
            "WHERE client_order_id IS NOT NULL "
            "GROUP BY tenant_id, user_id, client_order_id HAVING count(*) > 1 LIMIT 3"
        )
    except Exception:  # noqa: BLE001 - 表缺失等不阻断 C05 主判定
        dup_rows = []
    dup_check = classify_cid_duplicates(dup_rows or [])
    if dup_check.level != "ok":
        return dup_check
    # P2.7-⑧：REAL 台账（orders）同口径扫描。这条既是实盘幂等的可见性，也是
    # 「旧全库唯一约束为什么还没被换成租户/账户限定唯一」的答案（遇重复则迁移停手）。
    try:
        real_dup_rows = ctx.query(
            "SELECT tenant_id, user_id, client_order_id, count(*) AS c FROM orders "
            "WHERE client_order_id IS NOT NULL "
            "GROUP BY tenant_id, user_id, client_order_id HAVING count(*) > 1 LIMIT 3"
        )
    except Exception:  # noqa: BLE001 - 表缺失等不阻断 C05 主判定
        real_dup_rows = []
    real_dup_check = classify_cid_duplicates(real_dup_rows or [], table="orders")
    if real_dup_check.level != "ok":
        return real_dup_check
    # T-P1-04：覆盖率——近 7 日成交 vs cash_ledger 流水（成交必落账）
    cov_rows = ctx.query(
        "SELECT count(*) AS trades_7d, "
        "count(*) FILTER (WHERE EXISTS ("
        "  SELECT 1 FROM simulation_cash_ledger l WHERE l.ref_id = sim_trades.trade_id::text"
        ")) AS covered_7d "
        "FROM sim_trades WHERE executed_at >= now() - interval '7 days'"
    )
    trades_7d = int(cov_rows[0]["trades_7d"]) if cov_rows else 0
    covered_7d = int(cov_rows[0]["covered_7d"]) if cov_rows else 0
    return classify_ledger_coverage(trades_7d, covered_7d)


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
    """T-P1-06：按调度注册表逐项判定心跳（ok/stale/off/missing）；收盘核对折入同级。"""
    import time as _time

    from backend.shared.scheduler_registry import (
        JOBS,
        classify_scheduler_status,
        heartbeat_key,
        switch_enabled,
    )

    now_ts = _time.time()
    entries: list[dict] = []
    for spec in JOBS:
        if spec.heartbeat_ttl is None:  # 未接线心跳的任务不进判定（注册表注明）
            continue
        enabled = switch_enabled(spec)
        raw = ctx.redis_get(heartbeat_key(spec.key), REDIS_DB_GENERAL)
        age = None
        if raw is not None:
            try:
                age = int(now_ts - float(raw))
            except (TypeError, ValueError):
                age = None
        if not enabled:
            state = "off"
        elif age is None:
            state = "missing"
        elif age <= spec.heartbeat_ttl:
            state = "ok"
        else:
            state = "stale"
        entries.append(
            {
                "key": spec.key,
                "name": spec.name,
                "enabled": enabled,
                "state": state,
                "age": age,
                "ttl": spec.heartbeat_ttl,
            }
        )
    level, detail, metrics = classify_scheduler_status(entries)

    # 收盘核对（非心跳类任务，保留原有检查）；与心跳判定取更严者。
    # 时点闸门（2026-09-18 修误报）：只在「该标记应当已产生」时判定——信号日在未来
    # （前夜已出次日信号）或当日收盘核对时刻（CLOSE_AUDIT_TIME，默认 15:05）未到，
    # 一律跳过，避免每个交易日从出信号起到收盘核对前恒报「标记缺失」。
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZoneInfo

    rows = ctx.query("SELECT max(trade_date) AS d FROM engine_signal_scores")
    latest = rows[0]["d"] if rows else None
    if latest is not None:
        ymd = str(latest).replace("-", "")
        now_sh = _dt.now(_ZoneInfo("Asia/Shanghai"))
        today_ymd = now_sh.strftime("%Y%m%d")
        try:
            _h, _m = str(os.getenv("CLOSE_AUDIT_TIME", "15:05")).strip().split(":", 1)
            audit_hhmm = (int(_h), int(_m))
        except (TypeError, ValueError):
            audit_hhmm = (15, 5)
        due = ymd < today_ymd or (
            ymd == today_ymd and (now_sh.hour, now_sh.minute) >= audit_hhmm
        )
        if due:
            audit = ctx.redis_get(f"trade:close-audit:done:{ymd}", REDIS_DB_TRADE)
            if not audit:
                if level == "ok":
                    level = "warn"
                detail = f"{detail}；收盘核对标记缺失（{ymd}）"
    return CheckResult("C07", "调度心跳", level, detail, "", metrics)


async def check_c08_data_sync_freshness(ctx: HealthContext) -> CheckResult:
    rows = ctx.query(
        "SELECT max(trade_date) AS d FROM engine_signal_scores WHERE COALESCE(market, 'CN') = :m",
        m=ctx.market,
    )
    latest = rows[0]["d"] if rows else None
    if latest is None:
        return CheckResult("C08", "数据同步", "warn", f"{ctx.market} 市场无交易日基准")
    d = str(latest)
    # 同步键的市场码与业务市场码**不同名**（CN→A、CRYPTO→BC），映射唯一实现在
    # market_sync_scheduler.sync_market_token；手写 "A" 会让港股/美股永远查不到。
    from backend.services.engine.tasks.market_sync_scheduler import sync_market_token

    token = sync_market_token(ctx.market)
    marker = ctx.redis_get(f"quantmind:sync_schedule_last_run:{token}:{d}", REDIS_DB_GENERAL)
    if not marker:
        return CheckResult(
            "C08",
            "数据同步",
            "warn",
            f"{ctx.market} 市场最近交易日 {d} 无同步记录",
            "检查同步调度配置与执行",
            {"market": ctx.market, "trade_date": d, "sync_token": token},
        )
    return CheckResult(
        "C08",
        "数据同步",
        "ok",
        f"{ctx.market} 市场 {d} 已同步",
        metrics={"market": ctx.market, "trade_date": d, "sync_token": token},
    )


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


def classify_runner_db_privileges(
    role_present: bool,
    can_select: bool,
    can_insert: bool,
) -> tuple[str, str]:
    """runner 只读角色的权限面判定（纯函数）。"""
    if not role_present:
        return (
            "fail",
            "只读角色不存在——runner 启动时将回落主库凭据（最小权限失效）",
        )
    if can_insert:
        return (
            "fail",
            "只读角色可写（INSERT 未被拒绝）——权限面过宽，检查 GRANT",
        )
    if not can_select:
        return ("warn", "只读角色缺 SELECT——runner 数据访问会全线报错")
    return ("ok", "SELECT-only")


async def check_c11_runner_db_role(ctx: HealthContext) -> CheckResult:
    """runner 只读 DB 账号（T-P0-03 遗留）：角色存在 + SELECT 有 + 写被拒绝。"""
    from backend.shared.runner_db_account import runner_db_role_name

    role = runner_db_role_name()
    present = bool(
        ctx.query(
            "SELECT 1 AS present FROM pg_roles WHERE rolname = :r LIMIT 1", r=role
        )
    )
    can_select = False
    can_insert = False
    if present:
        row = ctx.query(
            "SELECT has_table_privilege(:r, 'simulation_fund_snapshots', 'SELECT') AS s, "
            "has_table_privilege(:r, 'simulation_fund_snapshots', 'INSERT') AS i",
            r=role,
        )
        if row:
            can_select = bool(row[0]["s"])
            can_insert = bool(row[0]["i"])
    level, detail = classify_runner_db_privileges(present, can_select, can_insert)
    suggestion = (
        ""
        if level == "ok"
        else "引擎服务启动时自动供给（backend/shared/runner_db_account.py）；"
        "检查 DB 账号是否有 CREATE ROLE 权限或显式配置 RUNNER_DB_USER/RUNNER_DB_PASSWORD"
    )
    return CheckResult("C11", "runner 只读 DB", level, f"{role}：{detail}", suggestion)


def classify_local_market_data(
    available: dict[str, str], missing: dict[str, str]
) -> CheckResult:
    """本地行情可用性判定（纯函数）：缺失市场清单 → 体检结论。"""
    if not missing:
        detail = "、".join(f"{m} {d}" for m, d in available.items()) or "无市场可查"
        return CheckResult("C12", "本地行情数据", "ok", f"最近交易日 {detail}")
    joined = "；".join(f"{m}: {r}" for m, r in missing.items())
    if available:
        return CheckResult(
            "C12",
            "本地行情数据",
            "warn",
            f"{joined}（可用: {'、'.join(available)}）",
            "确认数据目录已挂载且完成同步（QuantDB / 各市场 *_daily_sync）",
        )
    return CheckResult(
        "C12",
        "本地行情数据",
        "fail",
        f"所有市场行情不可用 —— {joined}",
        "数据目录未挂载或未同步：模拟盘撮合、实时行情兜底、回测均取不到数",
    )


async def check_c12_local_market_data(ctx: HealthContext) -> CheckResult:
    """本地行情数据可用性：各市场最近交易日能否取到。

    数据目录没挂载/没同步时进程不会退出，只是每 2 秒刷一条"无可用日线数据"
    —— 静默降级里最容易被忽略的一种，体检必须能一眼看出。
    """
    from backend.services.simulation.services.local_market_data import (
        get_local_market_data,
    )
    from backend.services.simulation.services.market_rules import Market

    available: dict[str, str] = {}
    missing: dict[str, str] = {}
    for market in Market:
        if market is Market.CRYPTO and not _crypto_market_enabled():
            continue  # ENABLE_CRYPTO=false：该市场本就不提供数据
        try:
            market_data = get_local_market_data(market)
            latest = market_data.latest_trade_date()
        except Exception as exc:  # noqa: BLE001 - 体检不因单市场探测失败中断
            missing[market.value] = f"探测失败 {exc}"
            continue
        if latest is None:
            missing[market.value] = (
                market_data.data_unavailable_reason() or "无可用日线"
            )
        else:
            available[market.value] = latest.isoformat()
    return classify_local_market_data(available, missing)


def classify_calendar_coverage(
    coverage: dict[str, tuple[date | None, str]], today: date
) -> CheckResult:
    """C13 判定（纯函数）：真日历覆盖年限。

    逐个日历算「距覆盖截止还有几天」，**取最紧的那个**定级：
    剩余 < ``CALENDAR_FAIL_DAYS``（含已过期）→ fail；< ``CALENDAR_WARN_DAYS`` → warn。
    取不到某个日历（``None``）**只算 warn**：读不到不等于没问题，但也不该让体检
    整体报红——真日历全丢时决策轮本来就 fail-closed，那是 C13 之外的表现。
    """
    parts: list[str] = []
    metrics: dict[str, Any] = {}
    worst = "ok"
    for name, (last, reason) in sorted(coverage.items()):
        if last is None:
            parts.append(f"{name} 取不到（{reason or '未知原因'}）")
            metrics[f"{name}_last_session"] = None
            if LEVEL_ORDER["warn"] > LEVEL_ORDER[worst]:
                worst = "warn"
            continue
        remaining = (last - today).days
        metrics[f"{name}_last_session"] = last.isoformat()
        metrics[f"{name}_remaining_days"] = remaining
        if remaining < CALENDAR_FAIL_DAYS:
            parts.append(f"{name} 覆盖到 {last.isoformat()}（剩 {remaining} 天，告急）")
            worst = "fail"
        elif remaining < CALENDAR_WARN_DAYS:
            parts.append(f"{name} 覆盖到 {last.isoformat()}（剩 {remaining} 天）")
            if LEVEL_ORDER["warn"] > LEVEL_ORDER[worst]:
                worst = "warn"
        else:
            parts.append(f"{name} 覆盖到 {last.isoformat()}（剩 {remaining} 天）")
    detail = "；".join(parts) or "无日历可查"
    if worst == "ok":
        return CheckResult("C13", "真日历覆盖年限", "ok", detail, metrics=metrics)
    suggestion = (
        "越过覆盖截止后 is_session 抛 DateOutOfBounds → 判定退化成「只按周末判断」，"
        "决策轮拒绝降级依据（fail-closed）⇒ 不是乱下单，是**一轮决策都不出**。"
        "两条修法：①升级 exchange_calendars（随版本前移）；②把次年交易日写进 "
        "qm_market_calendar_day（DB override 优先于真日历，SRC_DB_OVERRIDE）。"
    )
    return CheckResult("C13", "真日历覆盖年限", worst, detail, suggestion, metrics)


async def check_c13_trading_calendar_coverage(ctx: HealthContext) -> CheckResult:
    """真日历覆盖年限：CN/HK/US 三个市场各自印发到哪一天。

    这条不查 DB 也不查 Redis（只问进程内库），保留 ``ctx`` 形参是为了与
    ``CHECKS`` 里其余检查同签名。
    """
    from backend.shared.trading_calendar import xcal_coverage

    try:
        coverage = xcal_coverage()
    except Exception as exc:  # noqa: BLE001 - 体检不因探测本身失败中断
        coverage = {"XSHG": (None, f"{type(exc).__name__}: {exc}")}
    return classify_calendar_coverage(coverage, ctx.today)


async def check_c14_agent_ledger_parity(ctx: HealthContext) -> CheckResult:
    """分账账本一致性（P2.7）：LLM 腿成交 ↔ 账本流水 ↔ 成交唯一键。

    只看**实盘链**（``trades`` JOIN ``orders``）：分账账本的两个落账点都在实盘路径上
    （``qmt_exec_reconciler`` 的成交插入分支、``execution_stream_consumer`` 的成交事件），
    模拟盘（``sim_trades``）不写账本。窗口 :data:`AGENT_LEDGER_WINDOW_DAYS` 天，
    两侧各放宽 :data:`AGENT_LEDGER_WINDOW_SLACK_DAYS` 天（见该常量）。
    """
    from backend.shared.trade_contract import (
        DUP_FILLS_SQL,
        SYNTH_TRADE_PREFIX,
        TRADE_UNIQUE_INDEX,
    )

    try:
        dups = ctx.query(DUP_FILLS_SQL, limit=3)
    except Exception as exc:  # noqa: BLE001 - 表缺失等不阻断（旧库）
        return CheckResult("C14", "分账账本一致性", "warn", f"成交重复扫描失败: {exc}")
    if dups:
        sample = "，".join(
            f"{d.get('tenant_id')}:{d.get('user_id')}:{d.get('exchange_trade_id')}×{d.get('c')}"
            for d in dups[:3]
        )
        return CheckResult(
            "C14",
            "分账账本一致性",
            "fail",
            f"成交重复 {len(dups)} 组（{sample}）",
            "同租户同用户同成交号落了两行 ⇒ 成交数量与分账账本都会双记。不自动删金融行："
            "先按两份记录的时间与来源人工定夺哪一行是真的，去重后再让成交唯一键启用",
            {"dup_groups": len(dups)},
        )
    since = ctx.today - timedelta(
        days=AGENT_LEDGER_WINDOW_DAYS + AGENT_LEDGER_WINDOW_SLACK_DAYS
    )
    try:
        trades = ctx.query(C14_TRADES_SQL, since=since)
    except Exception as exc:  # noqa: BLE001 - 取数失败不伪造结论
        return CheckResult("C14", "分账账本一致性", "warn", f"成交取数失败: {exc}")
    try:
        ledger = ctx.query(C14_LEDGER_SQL, since=since)
    except Exception as exc:  # noqa: BLE001 - 账本表可能还没建
        if not trades:
            # 没有 LLM 腿成交 = 分账还没参与，表不在属正常（模拟盘-only 部署不建账本表）
            return CheckResult(
                "C14",
                "分账账本一致性",
                "ok",
                f"近 {AGENT_LEDGER_WINDOW_DAYS} 日无 LLM 腿成交，账本表未建（分账未参与）",
            )
        return CheckResult(
            "C14",
            "分账账本一致性",
            "warn",
            f"{len(trades)} 笔 LLM 腿成交但账本取数失败: {exc}",
            "账本表由 trade 服务启动期建（ensure_agent_ledger_tables）；表不在 = 落账在"
            "「成交已提交、账本没写」的失败形态上——查 trade 服务启动日志",
            {"trades": len(trades)},
        )
    try:
        indexed = bool(
            ctx.query(
                "SELECT 1 AS present FROM pg_indexes WHERE indexname = :n LIMIT 1",
                n=TRADE_UNIQUE_INDEX,
            )
        )
    except Exception:  # noqa: BLE001 - 探测失败按未启用（与 trade_contract 同口径）
        indexed = False
    return classify_agent_ledger_parity(
        ledger_parity(
            trades, ledger, index_enabled=indexed, synth_prefix=SYNTH_TRADE_PREFIX
        )
    )


def _crypto_market_enabled() -> bool:
    """加密市场是否启用（委托 quantbc_hub，避免 ENABLE_CRYPTO 解析两处口径分叉）。"""
    from backend.services.engine.data_platform.quantbc_hub import _crypto_enabled

    return _crypto_enabled()


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
    ("C11", "runner 只读 DB", check_c11_runner_db_role),
    ("C12", "本地行情数据", check_c12_local_market_data),
    ("C13", "真日历覆盖年限", check_c13_trading_calendar_coverage),
    ("C14", "分账账本一致性", check_c14_agent_ledger_parity),
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


def _build_context(market: str = "CN") -> HealthContext:
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

    from backend.shared.simulation_account_keys import normalize_market

    return HealthContext(
        query=query,
        redis_get=redis_get,
        redis_scan=redis_scan,
        today=date.today(),
        market=normalize_market(market),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟盘/信号/调度一键体检")
    parser.add_argument("--only", default="", help="只跑指定项，如 C01,C03")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument(
        "--market", default="CN", help="数据市场（CN/HK/US/FUTURES/CRYPTO），默认 CN"
    )
    args = parser.parse_args()

    selected = {s.strip().upper() for s in args.only.split(",") if s.strip()}
    ctx = _build_context(args.market)
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
