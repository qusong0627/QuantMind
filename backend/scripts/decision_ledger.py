#!/usr/bin/env python3
"""LLM 决策账（P2.1d）——抽取 / 记账 / 出表 一条命令链。

回答的问题：**这个模型选股行不行，以及在什么模式下不行**（见
`backend/shared/decision_ledger_contract.py` 模块头）。三个子命令都可反复跑：

    extract     决策账（PG）→ 池快照 JSONL（与隔壁 `logs/decision_pool.jsonl` 对账）
    scorecard   决策账 + 本仓 qfq 行情 → 入场价 / 四期收益 / 形态标签（默认 dry-run）
    report      已记账的行 → 记分卡（agent × 类别 × 周期；stdout + 落盘）
    import-pool 隔壁存量池 JSONL → 决策账（一次性迁移，默认 dry-run）

用法（容器内）:
    python backend/scripts/decision_ledger.py extract --days 120
    python backend/scripts/decision_ledger.py scorecard --days 120          # DRY-RUN
    python backend/scripts/decision_ledger.py scorecard --days 120 --apply
    python backend/scripts/decision_ledger.py report --days 120
    python backend/scripts/decision_ledger.py import-pool --from <池路径>   # 一次性迁移

退出码：``0`` 完成且无待办 / ``1`` 完成但有**要人看一眼**的事 / ``2`` 环境或参数错误
（与 `risk_ghost_ledger.py` 同约定，便于 cron 判读）。

**行情口径（本模块存在的理由）**：记分卡的全部价格走本仓 **qfq**
（`1_kline_data/daily_forward`，经 `GhostMarket`）。隔壁 `decision_track.py` 用
`daily_backward`，那是坏的（跨 vintage 拼缝、逐日平滑漂移，实测 20 日窗 54.5% 的
标的差 >1pp）——照搬它的 `entry_px` / `fwd` 等于把一套符号可能翻转的历史战绩喂给
LLM 做决策。故：**决策原样收，价格一律重算**。

四条纪律（各自防一种静默损坏）
------------------------------
* 记账**默认 dry-run**，`--apply` 才写；写走 `upsert_rows` 的定价拨——决策字段与
  执行结果一概不动（写纪律见 `decision_ledger_store`）；
* 记账重跑走 `price_row_monotone`：一轮失败的读盘不得把已算出的数抹成 `not_matured`
  （与 P1.6 影子账同一条防线）；
* 待记账集**包含已定价的行**（`load_pending` 的说明）：t20/t60 晚到，只捞
  `priced_at IS NULL` 会让它们永远补不上，而报表上只表现为「样本一直很少」；
* 导入**要么全进、要么一条不进**（`import-pool`）：少掉的那几行事后与「那天模型没
  说话」完全同形，没有任何下游会把它报出来。故坏行逐条点名后整体拒收。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.decision.tags import HIGH_LOOKBACK, form_tags  # noqa: E402
from backend.shared.decision_ledger_store import (  # noqa: E402
    KIND_BULLISH,
    KIND_POSITION,
    KIND_SELL,
    POOL_IMPORT_PREFIX,
    DecisionRecord,
    iso_date,
    load_pending,
    load_pool,
    records_from_pool,
    upsert_rows,
)
from backend.shared.risk.ghost import CST, GhostRow  # noqa: E402
from backend.shared.risk.ghost_pricing import (  # noqa: E402
    H_NOT_MATURED,
    H_NO_DATA,
    H_OK,
    HORIZONS,
    PriceInput,
    horizon_key,
    price_row_monotone,
)
from backend.shared.utc_datetime import utc_now  # noqa: E402

EXIT_OK = 0
EXIT_ATTENTION = 1
EXIT_USAGE = 2

#: 默认窗口（自然日）。取 120 是因为 t60 要 60 个交易日 ≈ 88 自然日，
#: 再留一个多月的缓冲，晚到的期才有机会被补上。
DEFAULT_DAYS = 120

#: 低于此样本量**只展示不结论**（与隔壁 `decision_track.MIN_SAMPLE` 同值同语义：
#: 借 Kelly 样本折扣的纪律，30 以下的均值不配当结论）。
MIN_SAMPLE = 30

#: 标签分组的最小样本（与隔壁同值）：单个标签 5 条以下不出组
BY_TAG_MIN = 5

#: 取多少根**入场前**收盘喂给 `form_tags`：它内部用 `closes[-60:]` 算「接近60日高」，
#: 多取 1 根是给停牌/缺 bar 留的余量（缺的日子不补，见 `GhostMarket.prior_closes`）。
TAG_LOOKBACK = HIGH_LOOKBACK + 1

#: 面板一次能读的交易日上限（`GhostMarket.MAX_PANEL_DATES`）——超了要给收窄建议
_PANEL_LIMIT = 400

_KIND_LABEL = {
    KIND_BULLISH: "看多意向（选股能力）",
    KIND_POSITION: "持仓管理",
    KIND_SELL: "卖出决策（正数=卖后跌）",
}


# ── 纯函数（可单测）─────────────────────────────────────────────────
def side_of(action: str) -> str:
    """决策动作 → 反事实方向：只有 `sell` 是卖，`buy`/`watch` 都是「假如买」。

    可成交性判定按方向取涨停/跌停（见 `ghost_pricing.entry_unfillable_reason`）：
    方向写反，一字板那批会全判错。
    """
    return "sell" if str(action or "").strip().lower() == "sell" else "buy"


def needs_pricing(rec: DecisionRecord) -> bool:
    """还**能补出东西**的行（与 P1.6 `_needs_pricing` 同一判据）。

    没定过价 → 要；定过价但还有期停在「没到期/缺数」→ 也要（日历在推进）。
    判据是"还有没有可能变好"，不是"有没有价"。
    """
    if not rec.fwd:
        return True
    for h in HORIZONS:
        ent = rec.fwd.get(horizon_key(h))
        if not isinstance(ent, dict):
            return True
        if str(ent.get("state") or "") in (H_NOT_MATURED, H_NO_DATA):
            return True
    return False


def as_ghost(rec: DecisionRecord) -> GhostRow:
    """审计行 → 影子账行（**只为复用定价状态机**，不落库、不参与影子账统计）。

    `ghost_pricing.price_row` 的五个状态与「绝不降级」是 P1.6 已审过的口径，
    在这里再造一份必然分叉——故把行形状翻译过去，而不是把口径复制过来。
    """
    return GhostRow(
        date=rec.trade_date.isoformat(),
        rule_id="llm.decision",
        kind=rec.kind,
        tenant=rec.tenant_id,
        uid=rec.user_id,
        symbol=rec.code,
        side=side_of(rec.action),
        quantity=None,
        source="decision_ledger",
        reason=rec.reason[:200],
        fwd=dict(rec.fwd) if rec.fwd else None,
        entry_date=rec.entry_date.isoformat() if rec.entry_date else None,
        entry_px=rec.entry_px,
        tradable=rec.tradable,
    )


def _date_of(day: str | None) -> date | None:
    """影子账的 `entry_date`（ISO）→ `date`；格式漂移必须**当场炸**，不许猜。

    形态判据在 :func:`iso_date`（库里一份，导入路径与影子账共用同一条边界）；这里只
    多一层「缺席 → `None`」——影子账的行可以是没入场日的，那不是格式漂移。
    """
    if not day:
        return None
    return iso_date(day, what="影子账入场日")


def price_record(
    rec: DecisionRecord,
    inp: PriceInput,
    *,
    tags: tuple[str, ...],
    priced_at: Any,
) -> tuple[DecisionRecord, tuple[int, ...]]:
    """给一条决策记账（返回新记录 + 被拒降级的期号）。

    `fwd` 用影子账的形状（`{"t1": {"state","ret","bench","excess","cost"}}`）——
    它是隔壁池 `{"t1": {"ret","bench","excess"}}` 的**超集**，逐键比 `ret`/`bench`/
    `excess` 即可与存量池对照，而 `state` 让「没到期」与「缺数」保持可分。
    """
    row, rejected = price_row_monotone(
        as_ghost(rec), inp, priced_at=priced_at.isoformat()
    )
    return (
        replace(
            rec,
            entry_date=_date_of(row.entry_date),
            entry_px=row.entry_px,
            tradable=row.tradable,
            fwd=dict(row.fwd or {}),
            tags=tuple(tags),
            priced_at=priced_at,
        ),
        rejected,
    )


def _stat(
    entries: list[DecisionRecord], h: int, *, invert: bool = False
) -> dict[str, Any]:
    """单组单周期统计（隔壁 `decision_track._stat` 逐字同口径）。

    `invert=True` 用于卖出决策：收益取负，"卖出后下跌"才算对。
    只认 `ok` 期的数：`ret` 为 None 的一律不进分母（不可得绝不退化成 0）。
    """
    rets: list[float] = []
    exs: list[float] = []
    key = horizon_key(h)
    for r in entries:
        ent = (r.fwd or {}).get(key)
        if not isinstance(ent, dict) or ent.get("ret") is None:
            continue
        ret = float(ent["ret"])
        rets.append(-ret if invert else ret)
        if ent.get("excess") is not None:
            exc = float(ent["excess"])
            exs.append(-exc if invert else exc)
    if not rets:
        return {"n": 0, "win_rate": None, "avg_ret": None, "avg_excess": None}
    return {
        "n": len(rets),
        "win_rate": round(sum(1 for x in rets if x > 0) / len(rets), 3),
        "avg_ret": round(sum(rets) / len(rets), 4),
        "avg_excess": round(sum(exs) / len(exs), 4) if exs else None,
    }


def build_scorecard(
    pool: list[DecisionRecord], *, today: str | None = None
) -> dict[str, Any]:
    """按 agent × 类别 × 周期汇总（键名与隔壁 `decision_scorecard.json` 一致）。

    复刻隔壁的三条口径，好让两份记分卡是**同口径的两个数**（差异即数据源差异）：

    * `sell` 类别收益取负；
    * `bullish` 剔除 `tradable is False`（一字板买不进，会系统性高估）；
    * 单个标签样本 < `BY_TAG_MIN` 不出组。
    """
    out: dict[str, Any] = {
        "generated_at": today or datetime.now(tz=CST).date().isoformat(),
        "n_entries": len(pool),
        "min_sample": MIN_SAMPLE,
        "bench": "全市场等权（同窗口 open→close 均值）",
        "entry_rule": "T+1 开盘价；一字板剔除",
        "agents": {},
    }
    agents = sorted({r.agent for r in pool if r.agent})
    for agent in agents:
        mine = [r for r in pool if r.agent == agent]
        blk: dict[str, Any] = {"n_total": len(mine)}
        for kind in (KIND_BULLISH, KIND_POSITION, KIND_SELL):
            sub = [r for r in mine if r.kind == kind]
            if kind == KIND_BULLISH:
                sub = [r for r in sub if r.tradable is not False]
            if not sub:
                continue
            blk[kind] = {
                "n": len(sub),
                **{
                    horizon_key(h): _stat(sub, h, invert=(kind == KIND_SELL))
                    for h in HORIZONS
                },
            }
            if kind == KIND_BULLISH:
                by_tag: dict[str, Any] = {}
                for tag in sorted({t for r in sub for t in (r.tags or ())}):
                    sel = [r for r in sub if tag in (r.tags or ())]
                    if len(sel) >= BY_TAG_MIN:
                        by_tag[tag] = {"n": len(sel), **_stat(sel, 5)}
                if by_tag:
                    blk[KIND_BULLISH]["by_tag_t5"] = by_tag
        if len(blk) > 1:  # 只有 n_total = 这个 agent 没有任何可比类别的行
            out["agents"][agent] = blk
    return out


def _fmt_stat(s: dict[str, Any]) -> str:
    """一行的统计串（`—` = 不可得；绝不显示成 0，0 是"平的"）。"""
    if not s.get("n"):
        return "n=0"
    exc = f" · 超额 {s['avg_excess']:+.1%}" if s.get("avg_excess") is not None else ""
    win = f"{s['win_rate']:.0%}" if s.get("win_rate") is not None else "—"
    ret = f"{s['avg_ret']:+.1%}" if s.get("avg_ret") is not None else "—"
    return f"n={s['n']} · 胜率 {win} · 均收益 {ret}{exc}"


def render(sc: dict[str, Any]) -> list[str]:
    """记分卡 → 人读行（骨架同隔壁 `print_report`，三处增设，逐条留痕）。

    已逐字对齐的部分：标题行、`■ agent（总决策 n）`、类别标签文案、`T+{h:<3}` 对齐、
    `[T+5·标签]` 行。三处**有意增设**（不是抄漏）：

    1. 口径行尾部加「（行情 qfq）」——本仓入场价与基准同出自 `1_kline_data/daily_forward`，
       隔壁那句只写基准名，换到本仓读的人无从知道价是什么口径；
    2. 类别行加「（n 条）」与样本不足告警——隔壁 `MIN_SAMPLE=30` 只写进了
       `decision_scorecard.json` 的字段，`print_report` **从不打印它**，于是「30 以下
       不结论」这条纪律在终端输出里是隐形的（读的人只看到一行光秃秃的均值）；
    3. 无任何可比类别时补一行提示——隔壁此时打印一个空标题行，看起来像报表坏了。
    """
    lines = [
        f"决策远期记分卡 · {sc['generated_at']} · 池内 {sc['n_entries']} 条",
        f"口径：{sc['entry_rule']}；基准：{sc['bench']}（行情 qfq）",
    ]
    if not sc.get("agents"):
        lines.append("（没有任何 agent 有可比类别的行 —— 先跑 scorecard --apply 记账）")
        return lines
    for agent, blk in sc["agents"].items():
        lines.append("")
        lines.append(f"■ {agent}（总决策 {blk['n_total']}）")
        for kind in (KIND_BULLISH, KIND_POSITION, KIND_SELL):
            sub = blk.get(kind)
            if not sub:
                continue
            n_note = (
                "" if sub["n"] >= MIN_SAMPLE else f"  ⚠ 样本<{MIN_SAMPLE}，只展示不结论"
            )
            lines.append(f"  {_KIND_LABEL[kind]}（{sub['n']} 条）{n_note}")
            for h in HORIZONS:
                lines.append(f"    T+{h:<3} {_fmt_stat(sub.get(horizon_key(h)) or {})}")
            for tag, st in (sub.get("by_tag_t5") or {}).items():
                lines.append(f"    [T+5·{tag}] {_fmt_stat(st)}")
    return lines


def _pool_row(rec: DecisionRecord) -> dict[str, Any]:
    """一行 → 导出形态（**隔壁池字段名在前**，本表自有字段在后）。

    对账怎么用：按 `(agent, date, code|code_raw, action)` 元组配对——隔壁的 `code`
    是模型原样写法，本表 `code` 是归一后的前缀式，`code_raw` 才是隔壁口径。
    **不要按 `id` 配对**：两边哈希输入不同（见 `decision_ledger_store.pool_key`）。
    `fwd` 是本表的超集，逐键比 `ret`/`bench`/`excess`，别整 dict 比。
    """
    return {
        # —— 与隔壁池同名的字段 ——
        "agent": rec.agent,
        "date": rec.trade_date.isoformat(),
        "ts": rec.decided_at.isoformat(),
        "action": rec.action,
        "kind": rec.kind,
        "code_raw": rec.code_raw,
        "code": rec.code,
        "confidence": rec.confidence,
        "stop_loss": rec.stop_loss,
        "take_profit": rec.take_profit,
        "reason": rec.reason,
        "entry_dt": int(rec.entry_date.strftime("%Y%m%d")) if rec.entry_date else None,
        "entry_px": rec.entry_px,
        "tradable": rec.tradable,
        "tags": list(rec.tags or ()),
        "fwd": dict(rec.fwd) if rec.fwd else {},
        # —— 本表自有（隔壁没有的列）——
        "id": rec.id,
        "pool_key": rec.pool_key,
        "round_id": rec.round_id,
        "tenant_id": rec.tenant_id,
        "user_id": rec.user_id,
        "market": rec.market,
        "pct": rec.pct,
        "pct_state": rec.pct_state,
        "pct_raw": rec.pct_raw,
        "move_stop": rec.move_stop,
        "invalidation": rec.invalidation,
        "risk_amount": rec.risk_amount,
        "armed": rec.armed,
        "reject_reason": rec.reject_reason,
        "notes": list(rec.notes or ()),
        "order_id": rec.order_id,
        "pool_ctx": dict(rec.pool_ctx) if rec.pool_ctx is not None else None,
        "context_meta": dict(rec.context_meta or {}),
        "priced_at": rec.priced_at.isoformat() if rec.priced_at else None,
    }


def _window(*, days: int, end: str | None) -> tuple[str, str]:
    """`(--days, --end)` → `(since, until)`（含两端，CST）。

    窗口两端只是**捞候选**的粗筛（真正的入场日/到期日由交易日历推），多捞几天无害；
    少捞会让晚到的期补不上，故窗口宁可宽。`--end` 缺省 = CST 今天。
    """
    hi = (
        datetime.strptime(end, "%Y-%m-%d").date()
        if end
        else datetime.now(tz=CST).date()
    )
    lo = hi - timedelta(days=max(int(days), 1))
    return lo.isoformat(), hi.isoformat()


def _reports_dir() -> Path:
    return (
        Path(os.getenv("QM_REPORTS_DIR", str(PROJECT_ROOT / "data" / "reports")))
        / "decision_ledger"
    )


# ── extract ─────────────────────────────────────────────────────────
def cmd_extract(args: argparse.Namespace) -> int:
    """把**记分卡池**导成 JSONL（只读；与隔壁存量池对账用）。"""
    import asyncio

    since, until = _window(days=args.days, end=args.end)

    async def _run() -> list[DecisionRecord]:
        from backend.shared.database_manager_v2 import close_database, get_session

        try:
            async with get_session(read_only=True) as session:
                return await load_pool(
                    session,
                    start=since,
                    end=until,
                    tenant_id=args.tenant or "",
                    agent=args.agent or "",
                    limit=args.limit,
                    dedup=not args.no_dedup,
                    # 导出要连未记账的一起给（隔壁池里也有 fwd 为空的行）——
                    # 只导已记账的，对账时会少掉一半行却看不出哪里不对
                    include_unpriced=True,
                )
        finally:
            await close_database()

    pool = asyncio.run(_run())
    lines = [json.dumps(_pool_row(r), ensure_ascii=False, default=str) for r in pool]
    out = Path(args.out) if args.out else _reports_dir() / f"{until}_pool.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    kinds = {
        k: sum(1 for r in pool if r.kind == k) for k in sorted({r.kind for r in pool})
    }
    priced = sum(1 for r in pool if r.fwd)
    print(f"[决策账] 窗口 {since} ~ {until}：池内 {len(pool)} 行 → {out}")
    print(f"    类别 {kinds}；已记账 {priced} 行，未记账 {len(pool) - priced} 行")
    if args.no_dedup:
        print("    （--no-dedup：审计视角全量，同一 pool_key 的多次决策都在）")
    if not pool:
        print("  ⚠ 池是空的 —— 决策账还没有写入方（P2.2）或窗口太窄")
        return EXIT_ATTENTION
    codeless = sum(1 for r in pool if not r.code)
    if codeless:
        print(f"  ⚠ 有 {codeless} 行没有标的代码（kind 非 none 的行不该如此）")
        return EXIT_ATTENTION
    return EXIT_OK


# ── scorecard ───────────────────────────────────────────────────────
def cmd_scorecard(args: argparse.Namespace) -> int:
    """给待记账的行补入场价 / 四期收益 / 形态标签（**默认 dry-run**）。"""
    import asyncio

    since, until = _window(days=args.days, end=args.end)
    now = utc_now()

    def _price(
        rows: list[DecisionRecord],
    ) -> tuple[list[DecisionRecord], int, int, int]:
        from backend.scripts.risk_ghost_market import GhostMarket, PriceQuery

        market = GhostMarket()
        queries = [
            PriceQuery(
                key=r.id,
                day=r.trade_date.isoformat(),
                symbol=r.code,
                side=side_of(r.action),
            )
            for r in rows
        ]
        batch = market.price_batch(queries, lookback_days=TAG_LOOKBACK)
        priced: list[DecisionRecord] = []
        n_rejected = n_ok = n_tagged = 0
        for r in rows:
            inp = batch.inputs.get(r.id)
            if inp is None:  # 防御：price_batch 对每条问句都给结果
                continue
            tags = form_tags(
                market.prior_closes(batch.panel, r.code, inp.entry_day, TAG_LOOKBACK)
            )
            out, rejected = price_record(r, inp, tags=tags, priced_at=now)
            if rejected:
                n_rejected += 1
                print(
                    f"  [降级被拒] {r.trade_date} {r.code} {r.action} "
                    f"期 {','.join(str(h) for h in rejected)}："
                    "本轮读盘与已存结果冲突，保留已存的"
                )
            if any(
                isinstance(ent := (out.fwd or {}).get(horizon_key(h)), dict)
                and ent.get("state") == H_OK
                for h in HORIZONS
            ):
                n_ok += 1
            if tags:
                n_tagged += 1
            priced.append(out)
        return priced, n_rejected, n_ok, n_tagged

    async def _run() -> int:
        from backend.shared.database_manager_v2 import close_database, get_session

        try:
            async with get_session(read_only=True) as session:
                rows = await load_pending(
                    session, since=since, until=until, limit=args.limit
                )
        finally:
            await close_database()

        todo = [r for r in rows if needs_pricing(r)]
        print(
            f"[决策账] 窗口 {since} ~ {until}：候选 {len(rows)} 行，"
            f"其中待记账 {len(todo)} 行（含已定价但期未满的）"
        )
        if not todo:
            print("[决策账] 没有需要记账的行")
            return EXIT_OK
        try:
            priced, n_rejected, n_ok, n_tagged = _price(todo)
        except ValueError as exc:
            # `GhostMarket.panel` 的面板上限：一次读太多交易日时给出可操作的提示
            print(f"[决策账] 取数窗口过大：{exc}")
            print(
                f"    一次最多读 {_PANEL_LIMIT} 个交易日；当前 --days {args.days}，"
                f"另需 {TAG_LOOKBACK} 个回看日 + 60 个远期日。"
                "把 --days 收窄到 250 以内再跑"
            )
            return EXIT_USAGE

        if not args.apply:
            print(
                f"[决策账] DRY-RUN（未写库）：{len(priced)} 行待回填，"
                f"可计价 {n_ok} 行，带标签 {n_tagged} 行"
            )
            print("  确认无误后加 --apply")
            return EXIT_ATTENTION if n_rejected else EXIT_OK
        if not priced:
            print("[决策账] 没有可写的行")
            return EXIT_ATTENTION if n_rejected else EXIT_OK

        async def _write() -> int:
            from backend.shared.database_manager_v2 import close_database, get_session

            try:
                async with get_session() as session:
                    n = await upsert_rows(session, priced)
                    await session.commit()
                return n
            finally:
                await close_database()

        # 这里必须 `await`：`_write` 嵌在**已经在跑的** `_run` 里（`_run` 由本函数的
        # 末行 `asyncio.run(_run())` 驱动），再套一层 `asyncio.run` 会当场抛
        # 「asyncio.run() cannot be called from a running event loop」——
        # 而这条分支只在 `--apply` **且真捞到可计价行**时才走到，干跑永远看不见。
        n = await _write()
        print(f"[决策账] 已回填 {n} 行（可计价 {n_ok} 行，带标签 {n_tagged} 行）")
        if n_rejected:
            print(
                f"  ⚠ 有 {n_rejected} 行的本期结果低于已存结果（降级被拒）——"
                "这通常意味着**今天这次读盘有问题**（分区没落/日历短了），请查"
            )
            return EXIT_ATTENTION
        return EXIT_OK

    return asyncio.run(_run())


# ── report ──────────────────────────────────────────────────────────
def cmd_report(args: argparse.Namespace) -> int:
    import asyncio

    since, until = _window(days=args.days, end=args.end)

    async def _run() -> list[DecisionRecord]:
        from backend.shared.database_manager_v2 import close_database, get_session

        try:
            async with get_session(read_only=True) as session:
                return await load_pool(
                    session,
                    start=since,
                    end=until,
                    tenant_id=args.tenant or "",
                    agent=args.agent or "",
                    limit=args.limit,
                    dedup=True,
                )
        finally:
            await close_database()

    pool = asyncio.run(_run())
    sc = build_scorecard(pool)
    lines = render(sc)
    print("\n".join(lines))

    priced = sum(1 for r in pool if r.fwd)
    pending = len(pool) - priced
    if pending:
        print(
            f"\n[决策账] 池内 {pending} 行未记账（列表里已排除）——"
            "跑 `scorecard --apply` 后重出表"
        )

    if not args.no_save:
        out_dir = Path(args.out) if args.out else _reports_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        as_of = sc["generated_at"]
        (out_dir / f"{as_of}_scorecard.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        (out_dir / f"{as_of}_scorecard.json").write_text(
            json.dumps(sc, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[决策账] 记分卡已落盘: {out_dir}")

    small = [
        (a, k)
        for a, blk in sc["agents"].items()
        for k in (KIND_BULLISH, KIND_POSITION, KIND_SELL)
        if blk.get(k) and blk[k]["n"] < MIN_SAMPLE
    ]
    if small:
        print(
            f"  ⚠ 有 {len(small)} 组样本 <{MIN_SAMPLE}（只展示不结论）："
            + "、".join(f"{a}/{k}" for a, k in small)
        )
    return EXIT_ATTENTION if small else EXIT_OK


# ── import-pool ─────────────────────────────────────────────────────
def _read_pool_rows(path: str) -> list[dict[str, Any]]:
    """读隔壁 `decision_pool.jsonl`（一行一 dict；坏行**当场报行号**，不跳过）。

    不静默跳过坏行：跳掉的那几行看起来与「那天没这条决策」一模一样，而这个文件
    正要随隔壁系统一起下线——**它是我方唯一的证据**，读坏一个字节都该让人知道。
    """
    rows: list[dict[str, Any]] = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{n} 不是合法 JSON：{exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"{path}:{n} 不是对象（{type(obj).__name__}）")
        rows.append(obj)
    return rows


def _dupes(items: list[str]) -> list[str]:
    """重复项（保序、去重）——导入前的撞键体检用。

    不用 `Counter` 是为了保住「第一次出现的位置序」：报给人看的前 5 条要按文件里的
    顺序出，否则同一份坏文件每次报的样例都不同，没法对照。
    """
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x in seen:
            if x not in out:
                out.append(x)
        else:
            seen.add(x)
    return out


def cmd_import_pool(args: argparse.Namespace) -> int:
    """存量池 → 决策账（默认 dry-run；**行情段留空，由 scorecard --apply 重算**）。"""
    import asyncio

    rows = _read_pool_rows(args.src)
    records, problems = records_from_pool(
        rows, round_prefix=args.prefix, tenant_id=args.tenant or "default"
    )
    if problems:
        print(f"[决策账] {len(problems)} 行不合格，**一条都不导**（先修数据再跑）：")
        for p in problems[:20]:
            print(f"    {p}")
        if len(problems) > 20:
            print(f"    ……另有 {len(problems) - 20} 条")
        return EXIT_USAGE

    keys = [r.pool_key for r in records]
    print(
        f"[决策账] 读入 {len(rows)} 行 → {len(records)} 条审计行"
        f"（pool_key 去重后 {len(set(keys))} 个）"
    )
    kinds = {
        k: sum(1 for r in records if r.kind == k)
        for k in sorted({r.kind for r in records})
    }
    print(f"    类别 {kinds}")

    # 撞 id 是**会丢决策**的那一类：`id = sha1(round_id|0|code|action)`，两条行同 id
    # 必然同 (agent/日/标的/动作)，进库时被 `_dedup` 并成一条——而并掉的那条事后与
    # 「模型那天没说话」长得一模一样。故这里是硬停，不是告警。
    by_id = _dupes([r.round_id for r in records])
    if by_id:
        print(
            f"  ⚠ 存量池里有 {len(by_id)} 个重复 id（round_id 撞键 ⇒ 导进去会被并成一条）："
        )
        for x in by_id[:5]:
            print(f"    {x}")
        print("    会丢决策，故**一条都不导**——先回隔壁查它那份 pool 的 id 出处")
        return EXIT_USAGE

    # 池内同 (agent/日/标的/动作) 出现两次则**不丢数据**（两条决策都记，只是记分卡
    # 视图按 `DISTINCT ON (pool_key)` 只留第一条，正是隔壁的池语义）——故只是告警。
    if _dupes([r.pool_key for r in records]):
        print("  ⚠ 池内出现重复 pool_key（同一 agent/日/标的/动作）——先查清再导")
        return EXIT_ATTENTION
    if not args.apply:
        print("    （dry-run：加 --apply 才写库；写后仍需 scorecard --apply 补行情）")
        return EXIT_OK

    async def _run() -> tuple[int, int, int]:
        from backend.shared.database_manager_v2 import close_database, get_session

        from backend.shared.decision_ledger_contract import (
            ensure_decision_ledger_table_async,
        )

        try:
            # 先收掉可能已存在的引擎：本命令用**自己的** loop 跑（`asyncio.run`），
            # 而全局引擎与创建它的 loop 绑定——进程里若有人先碰过库（测试、探针、
            # 集成脚本），这里就会拿到绑在别人 loop 上的引擎，表现为
            # `got Future attached to a different loop` 被 `main()` 收成「导入失败」。
            # 独立进程跑时这行是空操作。
            await close_database()
            if not await ensure_decision_ledger_table_async():
                raise RuntimeError("决策账建表/补列失败（见上一条告警）")
            async with get_session(read_only=True) as session:
                before = await load_pool(
                    session, tenant_id=args.tenant or "default", dedup=False
                )
            async with get_session(read_only=False) as session:
                # 不带 `priced=`：拨次由 `upsert_rows` 按行内有无 `fwd` **自己分**
                # （导入行的 `fwd` 一律为空 → 自然落「决策拨」，决策字段先写为准）。
                written = await upsert_rows(session, records)
            return written, len(before), len({r.id for r in before})
        finally:
            await close_database()

    written, before_n, before_ids = asyncio.run(_run())
    # 「写入/刷新」不写成「写入」：`upsert_rows` 返回的是**发出去的行数**（命中已有
    # 行走 ON CONFLICT 更新也算），重跑一次行数不变但打印的仍是同一个数——写成
    # 「写入」会让人以为重跑又造了 207 条。
    print(
        f"    写入/刷新 {written} 条（该租户原有 {before_n} 行 / {before_ids} 个 id）；"
        f"round_id 前缀 {args.prefix!r} 标记出处"
    )
    print(
        "    下一步：decision_ledger.py scorecard --apply（用本仓 qfq 补入场价/收益/标签）"
    )
    return EXIT_OK


# ── 入口 ────────────────────────────────────────────────────────────
def _add_window(p: argparse.ArgumentParser) -> None:
    p.add_argument("--days", type=int, default=DEFAULT_DAYS, help="回溯自然日")
    p.add_argument("--end", help="窗口结束日 YYYY-MM-DD（默认今天 CST）")


def _add_filters(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tenant", help="只看某个租户")
    p.add_argument("--agent", help="只看某个模型/agent")
    p.add_argument("--limit", type=int, default=5000)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM 决策账（P2.1d）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ex = sub.add_parser("extract", help="决策账 → 池快照 JSONL（只读）")
    _add_window(p_ex)
    _add_filters(p_ex)
    p_ex.add_argument("--no-dedup", action="store_true", help="不去重（审计视角全量）")
    p_ex.add_argument("--out", help="输出文件（默认 data/reports/decision_ledger/）")

    p_sc = sub.add_parser("scorecard", help="补入场价/四期收益/标签（默认 dry-run）")
    _add_window(p_sc)
    _add_filters(p_sc)
    p_sc.add_argument("--apply", action="store_true", help="实际写库")

    p_re = sub.add_parser("report", help="记分卡（agent × 类别 × 周期）")
    _add_window(p_re)
    _add_filters(p_re)
    p_re.add_argument("--out", help="输出目录（默认 data/reports/decision_ledger）")
    p_re.add_argument("--no-save", action="store_true", help="只打印，不落盘")

    p_im = sub.add_parser(
        "import-pool", help="隔壁存量池 JSONL → 决策账（默认 dry-run，行情段留空）"
    )
    p_im.add_argument("--from", dest="src", required=True, help="存量池 JSONL 路径")
    p_im.add_argument(
        "--prefix", default=POOL_IMPORT_PREFIX, help="round_id 前缀（出处标记）"
    )
    p_im.add_argument("--tenant", help="写入租户（默认 default）")
    p_im.add_argument("--apply", action="store_true", help="实际写库")

    args = parser.parse_args(argv)
    handlers = {
        "extract": cmd_extract,
        "scorecard": cmd_scorecard,
        "report": cmd_report,
        "import-pool": cmd_import_pool,
    }
    try:
        return handlers[args.cmd](args)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"[决策账] 环境/参数错误：{exc}")
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
