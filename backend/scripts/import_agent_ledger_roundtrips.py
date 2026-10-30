#!/usr/bin/env python3
"""历史回合台账迁移：宿主 ``logs/live_roundtrips.jsonl`` → PG 回合台账。

背景：文件账本 ``logs/live_ledger.json`` 自 2026-09-30 迁移夜冻结；09-14..09-29 的
已平/部分平回合只留在 JSONL 里。实况页「已完成」靠在读侧临时合并两源，但绩效统计、
回合归因、研究总控的 PG 查询都看不到这段历史。本脚本把文件回合**原样归档**进
``qm_agent_ledger_roundtrip``：

* **只写回合表**：不碰持仓 / 现金 / 成交流水——历史状态已由期初结转（``legacy-seed``）
  与后续真单反映，回放成交会二次记账；
* **幂等**：按 ``(agent, code, buy_ts, sell_ts, volume, sell_price)`` 去重（表上没有
  唯一键，历史文件也没有成交号），重复跑只跳过；
* **默认 dry-run**；``--apply`` 必须带 ``--record``，把写前/写后计数与插入清单留痕。

用法（容器内；``data/`` 是挂载卷）::

    python backend/scripts/import_agent_ledger_roundtrips.py
    python backend/scripts/import_agent_ledger_roundtrips.py --apply --record /tmp/rt.json

退出码：``0`` 完成/干跑干净；``1`` 有非法行需人看一眼；``2`` 参数或环境错误。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CST = timezone(timedelta(hours=8))

EXIT_OK = 0
EXIT_ATTENTION = 1
EXIT_USAGE = 2

#: 文件里的市场口径 → 账本列口径（文件用 "A"，PG 契约用 "CN"）。
_MARKET_ALIASES = {"A": "CN", "CN": "CN", "HK": "HK", "US": "US"}

#: 默认文件落点（宿主机仓库 data/ 符号链接到真实数据盘）。
DEFAULT_PATH = PROJECT_ROOT / "data" / "logs" / "live_roundtrips.jsonl"

#: 迁移事务的 advisory lock 键：并发跑两份不会各插一遍（表上无唯一键）。
_LOCK_KEY = "agent_ledger_roundtrip_import"


@dataclass(frozen=True, slots=True)
class ImportPlan:
    rows: tuple[dict[str, Any], ...]
    duplicates: int
    invalid: tuple[tuple[int, str], ...]


def _parse_ts(value: Any, field: str) -> datetime:
    """时间串 → aware UTC；无时区按北京时间解释（历史文件全是 +08:00）。"""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{field} 为空")
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} 不是 ISO 时间：{text!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(timezone.utc)


def _finite(value: Any, field: str, *, positive: bool = False) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 不是数字：{value!r}") from exc
    if not math.isfinite(out) or (positive and out <= 0):
        raise ValueError(f"{field} 非有限或非正：{value!r}")
    return out


def parse_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    """文件一行 → ``write_roundtrip`` 的入参形状（脏值当场抛，不猜）。"""
    if not isinstance(raw, Mapping):
        raise ValueError("不是 JSON 对象")
    agent = str(raw.get("agent") or "").strip()
    if not agent:
        raise ValueError("agent 为空")
    code = str(raw.get("code") or "").strip()
    if not code:
        raise ValueError("code 为空")
    buy_ts = _parse_ts(raw.get("buy_ts"), "buy_ts")
    sell_ts = _parse_ts(raw.get("sell_ts"), "sell_ts")
    if sell_ts < buy_ts:
        raise ValueError("sell_ts 早于 buy_ts")
    market = str(raw.get("market") or "CN").strip().upper()
    return {
        "agent": agent,
        "market": _MARKET_ALIASES.get(market, market)[:16],
        "code": code[:32],
        "volume": _finite(raw.get("volume"), "volume", positive=True),
        "cost_price": _finite(raw.get("cost_price"), "cost_price", positive=True),
        "sell_price": _finite(raw.get("sell_price"), "sell_price", positive=True),
        "realized_pnl": _finite(raw.get("realized_pnl"), "realized_pnl"),
        "pnl_pct": _finite(raw.get("pnl_pct"), "pnl_pct")
        if raw.get("pnl_pct") not in (None, "")
        else None,
        "buy_ts": buy_ts,
        "sell_ts": sell_ts,
        "holding_days": _finite(raw.get("holding_days"), "holding_days")
        if raw.get("holding_days") not in (None, "")
        else None,
        "closed": bool(raw.get("closed")),
        "exit_reason": str(raw.get("exit_reason") or "")[:32],
    }


def row_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """去重键：同一 agent / 标的 / 买卖时刻 / 量价 = 同一回合（表上无唯一键）。"""
    buy_ts = row.get("buy_ts")
    sell_ts = row.get("sell_ts")
    return (
        str(row.get("agent") or ""),
        str(row.get("code") or ""),
        buy_ts.isoformat() if isinstance(buy_ts, datetime) else str(buy_ts or ""),
        sell_ts.isoformat() if isinstance(sell_ts, datetime) else str(sell_ts or ""),
        round(float(row.get("volume") or 0), 6),
        round(float(row.get("sell_price") or 0), 6),
    )


def plan_import(
    raw_rows: Sequence[Any], existing_keys: set[tuple[Any, ...]]
) -> ImportPlan:
    """纯规划：解析、去重（库内 + 文件内）、收集非法行。"""
    seen = set(existing_keys)
    rows: list[dict[str, Any]] = []
    invalid: list[tuple[int, str]] = []
    duplicates = 0
    for index, raw in enumerate(raw_rows, start=1):
        try:
            row = parse_row(raw)  # type: ignore[arg-type]
        except ValueError as exc:
            invalid.append((index, str(exc)))
            continue
        key = row_key(row)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        rows.append(row)
    return ImportPlan(rows=tuple(rows), duplicates=duplicates, invalid=tuple(invalid))


def read_rows(path: Path) -> list[Any]:
    """读 JSONL；坏行保留为 ``None`` 交给 ``plan_import`` 记非法（不静默丢）。"""
    out: list[Any] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            text = line.strip()
            if not text:
                continue
            try:
                out.append(json.loads(text))
            except json.JSONDecodeError:
                out.append(None)
    return out


async def load_existing_keys(session: Any, *, tenant_id: str, user_id: str) -> set:
    from sqlalchemy import text

    res = await session.execute(
        text(
            "SELECT agent, code, buy_ts, sell_ts, volume, sell_price "
            "FROM qm_agent_ledger_roundtrip "
            "WHERE tenant_id = :t AND user_id = :u"
        ),
        {"t": tenant_id, "u": user_id},
    )
    return {
        row_key({"agent": r["agent"], "code": r["code"], "buy_ts": r["buy_ts"],
                 "sell_ts": r["sell_ts"], "volume": r["volume"],
                 "sell_price": r["sell_price"]})
        for r in res.mappings()
    }


async def load_existing_keys_for(
    *, tenant_id: str, user_id: str
) -> set[tuple[Any, ...]]:
    """只读打开会话读现有键（dry-run 也要如实报「已在库多少条」）。"""
    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        return await load_existing_keys(session, tenant_id=tenant_id, user_id=user_id)


async def apply_plan(
    plan: ImportPlan, *, tenant_id: str, user_id: str
) -> tuple[int, list[dict[str, Any]]]:
    """写库（一个事务）；返回 ``(写前已有, 实际插入的行)``。

    advisory lock 把并发导入串行化；锁内**重新读一次** existing——外面算计划与这里
    之间可能已有另一份跑完，重读才能保证不双插。
    """
    from sqlalchemy import text

    from backend.shared.agent_ledger_store import write_roundtrip
    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": _LOCK_KEY}
        )
        existing = await load_existing_keys(
            session, tenant_id=tenant_id, user_id=user_id
        )
        to_insert = [row for row in plan.rows if row_key(row) not in existing]
        for row in to_insert:
            await write_roundtrip(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                agent=str(row["agent"]),
                row=row,
            )
        return len(existing), to_insert


def _print_plan(plan: ImportPlan, args: argparse.Namespace) -> None:
    print(
        f"[回合迁移] {args.path}：待写 {len(plan.rows)} 条 / "
        f"已在库 {plan.duplicates} 条 / 非法 {len(plan.invalid)} 条"
    )
    for row in plan.rows[:5]:
        print(
            f"  {row['agent']} {row['code']} {row['volume']:g}股 "
            f"买 {row['buy_ts']:%F} → 卖 {row['sell_ts']:%F} "
            f"¥{row['sell_price']:.4f} pnl={row['realized_pnl']:+.2f}"
        )
    if len(plan.rows) > 5:
        print(f"  … 其余 {len(plan.rows) - 5} 条")
    for index, reason in plan.invalid[:10]:
        print(f"  ❌ 第 {index} 行：{reason}")
    if len(plan.invalid) > 10:
        print(f"  … 另有 {len(plan.invalid) - 10} 条非法行")


def main() -> int:
    parser = argparse.ArgumentParser(description="历史回合台账 → PG")
    parser.add_argument("--path", default=str(DEFAULT_PATH), help="live_roundtrips.jsonl 路径")
    parser.add_argument("--tenant", default="default")
    parser.add_argument(
        "--user",
        default="",
        help="账本 user（缺省=resolve_db_account_user(QM_DECISION_ACCOUNT_USER_ID)）",
    )
    parser.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run）")
    parser.add_argument("--record", default="", help="写库留痕 JSON（--apply 必填）")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.is_file():
        print(f"❌ 文件不存在：{path}", file=sys.stderr)
        return EXIT_USAGE
    if args.apply and not args.record:
        print("❌ --apply 必须带 --record：历史归档要留写前/写后计数", file=sys.stderr)
        return EXIT_USAGE

    from backend.shared.simulation_account_keys import resolve_db_account_user

    user = str(args.user or "").strip() or resolve_db_account_user(
        "QM_DECISION_ACCOUNT_USER_ID"
    )
    raw_rows = read_rows(path)
    existing = asyncio.run(
        load_existing_keys_for(tenant_id=str(args.tenant), user_id=user)
    )
    plan = plan_import(raw_rows, existing)
    _print_plan(plan, args)
    if not args.apply:
        print("[回合迁移] DRY-RUN（未写库）；确认后加 --apply --record <path>")
        return EXIT_ATTENTION if plan.invalid else EXIT_OK

    before, inserted_rows = asyncio.run(
        apply_plan(plan, tenant_id=str(args.tenant), user_id=user)
    )
    record = {
        "ts": datetime.now(CST).isoformat(),
        "source": str(path),
        "tenant": str(args.tenant),
        "user": user,
        "source_rows": len(raw_rows),
        "existing_before": before,
        "inserted": len(inserted_rows),
        "duplicates_skipped": plan.duplicates,
        "invalid": [{"line": i, "reason": r} for i, r in plan.invalid],
        "inserted_keys": [
            {"agent": row["agent"], "code": row["code"],
             "buy_ts": row["buy_ts"].isoformat(), "sell_ts": row["sell_ts"].isoformat(),
             "volume": row["volume"], "sell_price": row["sell_price"]}
            for row in inserted_rows
        ],
    }
    record_path = Path(args.record)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"[回合迁移] 完成：写前 {before} 条 → 插入 {len(inserted_rows)} 条（"
        f"跳过重复 {plan.duplicates} / 非法 {len(plan.invalid)}）；留痕 {record_path}"
    )
    return EXIT_ATTENTION if plan.invalid else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
