#!/usr/bin/env python3
"""把次年交易日播进 DB override —— ``qm_market_calendar_day`` 的运维入口。

为什么需要它
------------
``exchange_calendars`` 只印发到某个固定日期（容器内实测 XSHG → 2026-12-31、
XHKG/XNYS → 2027-09-24）。越过覆盖截止后 ``trading_day_verdict`` 退化成
``SRC_WEEKDAY_FALLBACK``（只按周末判），决策轮**拒绝**降级依据（fail-closed）⇒
不是乱下单，是**一轮决策都不出**（静默停摆，体检 C13 会在剩 90/30 天时告警）。

修法只有两条：升级库（2027 年 A 股节假日安排国务院年底才印发，升不了）；
或把次年交易日写进 DB override（优先级最高，``SRC_DB_OVERRIDE``）。后者的读路径
一直在，缺的是生产者 —— 本脚本就是它。

用法
----
容器内执行（``--apply`` 才写库，默认只预演）：

    # 0) 看现状：真日历覆盖到哪天 + 库里已有哪些 override
    python backend/scripts/seed_trading_calendar.py --status

    # 1) 拿到国务院次年放假安排后，抄成一份清单（每行一个 YYYY-MM-DD，可带 # 注释）
    #    2027-01-01     # 元旦
    #    2027-02-05     # 春节
    #    ...
    python backend/scripts/seed_trading_calendar.py --year 2027 \
        --holidays-file /data/holidays-2027.txt              # 预演（不写库）
    python backend/scripts/seed_trading_calendar.py --year 2027 \
        --holidays-file /data/holidays-2027.txt --apply      # 落库

    # 2) 回读校验：走**真实判定路径**（trading_day_verdict），确认已生效
    python backend/scripts/seed_trading_calendar.py --year 2027 \
        --holidays-file /data/holidays-2027.txt --verify

    # 3) 临时单日覆盖（临时休市/临时交易），替代手写 SQL
    python backend/scripts/seed_trading_calendar.py --set 2027-07-01=0 \
        --reason "临时休市" --apply

默认只播**真日历覆盖不到的缺口**（``--from`` 缺省＝覆盖末日次日）；要整年覆盖
就显式给 ``--from/--to``。默认作用域 ``('default','*')`` 是**全局兜底**——
它在任何 (tenant,user) 的作用域列表里排最后，故对所有租户生效，不必逐个租户播种。

一个 ``--market`` 会展开成**整组查询键**（``CN`` → ``CN``/``SSE``/``SZSE``）：
判定层不做别名归一，决策轮查 ``CN``、celery 自动推理查 ``SSE``、代码推断查
``SZSE``，只写一个键等于只补了三分之一，另两条链路在节假日照样降级。``--status``
会点名「哪一组只播了一部分」。

退出码：0 正常；1 需人工处理（sanity 拒绝 / 回读不一致）；2 用法错误。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared import market_calendar_seed as seed  # noqa: E402
from backend.shared.trading_calendar import (  # noqa: E402
    SRC_DB_OVERRIDE,
    TradingCalendarService,
    xcal_coverage,
)

EXIT_OK = 0
EXIT_ATTENTION = 1
EXIT_USAGE = 2


def _gap_start(market: str) -> date | None:
    """真日历覆盖末日的**次日**（缺口的起点）；覆盖取不到时返回 None。"""
    name = seed.market_calendar_name(market)
    last, _reason = xcal_coverage().get(name, (None, "未收录"))
    return None if last is None else last + timedelta(days=1)


def _resolve_range(
    *, market: str, year: int, date_from: str | None, date_to: str | None
) -> tuple[date, date]:
    start = date.fromisoformat(date_from) if date_from else (_gap_start(market) or date(year, 1, 1))
    end = date.fromisoformat(date_to) if date_to else date(year, 12, 31)
    return start, end


def _parse_set_spec(spec: str) -> tuple[date, bool]:
    """``YYYY-MM-DD=0|1`` → ``(日期, 是否交易日)``；非法即抛 ``ValueError``。"""
    raw_day, _, raw_value = str(spec).partition("=")
    if raw_value not in {"0", "1"}:
        raise ValueError(f"--set 需要 DATE=0|1 形式（收到 {spec!r}）")
    return date.fromisoformat(raw_day.strip()), raw_value == "1"


def _keys_label(market: str) -> str:
    """查询键组的可读形式；只有一个键时就不啰嗦。"""
    keys = seed.market_keys(market)
    return keys[0] if len(keys) == 1 else f"{keys[0]}（同时写 {'、'.join(keys[1:])}）"


def _preview_lines(rows: list[seed.DayRow], start: date, end: date) -> list[str]:
    stats = seed.summarise(rows)
    trading = [r.trade_date for r in rows if r.is_trading_day]
    lines = [
        f"范围 {start.isoformat()} → {end.isoformat()}：共 {stats['days']} 天，"
        f"交易日 {stats['trading_days']} 天，休市 {stats['closed_days']} 天",
    ]
    if trading:
        lines.append(f"首个交易日 {trading[0].isoformat()}，末个交易日 {trading[-1].isoformat()}")
    return lines


async def _apply(
    *,
    market: str,
    rows: list[seed.DayRow],
    tenant: str,
    user: str,
    source: str,
    version: str | None,
    metadata: dict,
) -> int:
    from backend.shared.database_manager_v2 import close_database

    try:
        return await seed.apply_rows(
            market=market,
            rows=rows,
            tenant_id=tenant,
            user_id=user,
            source=source,
            version=version,
            metadata=metadata,
        )
    finally:
        # 跑完就断开：CLI 进程里不留悬挂连接（也避免测试进程里把连接留在死 loop 上）
        await close_database()


async def _verify(
    *, market: str, rows: list[seed.DayRow], tenant: str, user: str
) -> list[str]:
    """走真实读路径回读：**逐个查询键 × 逐日**比对判定值与依据。

    逐键是必须的：同一物理市场在三条链路上用的键不同（``CN`` 决策轮 / ``SSE``
    celery 自动推理 / ``SZSE`` 代码推断）。只验一个键，就是把「另两条链路照样
    降级」放过去了 —— 那正是本脚本要修的缺口本身。
    """
    from backend.shared.database_manager_v2 import close_database

    await seed.ensure_pool_usable()
    service = TradingCalendarService()
    mismatches: list[str] = []
    try:
        for key in seed.market_keys(market):
            for row in rows:
                verdict, source = await service.trading_day_verdict(
                    market=key,
                    trade_date=row.trade_date,
                    tenant_id=tenant,
                    user_id=user,
                )
                if verdict != row.is_trading_day or source != SRC_DB_OVERRIDE:
                    mismatches.append(
                        f"[{key}] {row.trade_date.isoformat()} 期望 "
                        f"{'交易日' if row.is_trading_day else '休市'}/db_override，"
                        f"实际 {'交易日' if verdict else '休市'}/{source}"
                    )
    finally:
        await close_database()
    return mismatches


async def _status() -> dict:
    """现状：各市场真日历覆盖末日 + 库里 override 存量（按 市场/年/来源/作用域）。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import close_database, get_session

    await seed.ensure_pool_usable()
    coverage = {
        name: {"last_session": last.isoformat() if last else None, "reason": reason}
        for name, (last, reason) in xcal_coverage().items()
    }
    inventory: list[dict] = []
    try:
        async with get_session(read_only=True) as session:
            result = await session.execute(
                text(
                    """
                    SELECT market,
                           LEFT(CAST(trade_date AS TEXT), 4) AS year,
                           source, tenant_id, user_id,
                           COUNT(*) AS days,
                           MIN(trade_date) AS first_day,
                           MAX(trade_date) AS last_day
                    FROM qm_market_calendar_day
                    GROUP BY 1, 2, 3, 4, 5
                    ORDER BY 1, 2, 3, 4, 5
                    """
                )
            )
            inventory = [
                {
                    "market": r.market,
                    "year": r.year,
                    "source": r.source,
                    "tenant_id": r.tenant_id,
                    "user_id": r.user_id,
                    "days": int(r.days),
                    "first_day": r.first_day.isoformat(),
                    "last_day": r.last_day.isoformat(),
                }
                for r in result
            ]
    finally:
        await close_database()
    return {"coverage": coverage, "inventory": inventory}


def _incomplete_groups(inventory: list[dict]) -> list[str]:
    """库里只播了**一部分查询键**的市场组（另一条链路仍会降级）。

    这是「我以为补完了、其实只补了三分之一」的唯一可见面：判定层不做别名归一，
    决策轮查 ``CN``、celery 自动推理查 ``SSE``、代码推断查 ``SZSE``，三处各查各的。
    """
    seen: dict[str, set[str]] = {}
    for item in inventory:
        for group, keys in seed.MARKET_KEY_GROUPS.items():
            if item["market"] in keys:
                seen.setdefault(group, set()).add(item["market"])
    notices: list[str] = []
    for group, keys in seed.MARKET_KEY_GROUPS.items():
        got = seen.get(group, set())
        if not got or got == set(keys):
            continue
        missing = sorted(set(keys) - got)
        notices.append(
            f"{group} 组只播了 {'、'.join(sorted(got))}，缺 {'、'.join(missing)}"
            "——缺的那些查询键仍会退化成真日历/周末兜底"
        )
    return notices


def _unknown_market_keys(inventory: list[dict]) -> list[str]:
    """库里存在、但**不在任何已知键组里**的 market（遗留数据的痕迹）。

    真实的例子：库里曾有过 9862 行 ``market='A'`` / ``source='baostock'``（2000–2026
    全年、**含周末全部判交易日**），2026-09-24 已删除（备份 CSV、回滚脚本与哈希
    见 ``/media/zbox/data/quantmind/backups_calendar_20260924/``）。判定层不做别名
    归一，所以决策轮（查 ``CN``）看不到它们；但任何显式传 ``'A'`` 的调用方会查到，
    并且因为那批行把周末也写成交易日，结果是 **fail-open**（该休市的日子判成可
    交易）。本脚本不负责删这类行——只把它**说出来**，否则运维看到满屏
    「A 2026 365 天」会误以为次年日历已经补好。
    """
    known = {key for keys in seed.MARKET_KEY_GROUPS.values() for key in keys}
    totals: dict[str, int] = {}
    for item in inventory:
        if item["market"] not in known:
            totals[item["market"]] = totals.get(item["market"], 0) + item["days"]
    return [
        f"market={market!r} 共 {days} 行不在任何已知查询键组里"
        "（判定层不会查到它，除非调用方显式传这个键——那是遗留数据，不是本次播种的成果）"
        for market, days in sorted(totals.items())
    ]


def _run_status(args: argparse.Namespace) -> int:
    today = date.today()
    report = asyncio.run(_status())
    print("真日历覆盖：")
    for name, info in sorted(report["coverage"].items()):
        if not info["last_session"]:
            print(f"  {name}: 取不到（{info['reason'] or '未知'}）")
            continue
        last = date.fromisoformat(info["last_session"])
        print(f"  {name}: 覆盖到 {info['last_session']}（剩 {(last - today).days} 天）")
    print("库内 override 存量（qm_market_calendar_day）：")
    if not report["inventory"]:
        print("  （空——没有任何 override；越过真日历覆盖截止后判定会退化成只按周末）")
    for item in report["inventory"]:
        scope = f"{item['tenant_id']}/{item['user_id']}"
        print(
            f"  {item['market']} {item['year']} 来源={item['source']} 作用域={scope} "
            f"{item['days']} 天（{item['first_day']} → {item['last_day']}）"
        )
    for notice in _incomplete_groups(report["inventory"]):
        print(f"⚠️ {notice}")
    for note in _unknown_market_keys(report["inventory"]):
        print(f"ℹ️ {note}")
    return EXIT_OK


def _run_set(args: argparse.Namespace) -> int:
    try:
        day, is_trading = _parse_set_spec(args.set_spec)
    except ValueError as exc:
        print(f"用法错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    row = seed.DayRow(trade_date=day, is_trading_day=is_trading)
    print(
        f"单日覆盖：{day.isoformat()} → {'交易日' if is_trading else '休市'}"
        f"（市场 {_keys_label(args.market)}，作用域 {args.tenant}/{args.user}，"
        f"原因：{args.reason}）"
    )
    if not args.apply:
        print("预演结束（未写库）。加 --apply 落库。")
        return EXIT_OK

    written = asyncio.run(
        _apply(
            market=args.market,
            rows=[row],
            tenant=args.tenant,
            user=args.user,
            source=seed.MANUAL_SOURCE,
            version=None,
            metadata={"reason": args.reason, "written_by": "seed_trading_calendar.py"},
        )
    )
    print(f"已写入 {written} 行，来源={seed.MANUAL_SOURCE}。")
    return EXIT_OK


def _run_seed(args: argparse.Namespace) -> int:
    path = Path(args.holidays_file)
    if not path.is_file():
        print(f"用法错误：休市清单不存在：{path}", file=sys.stderr)
        return EXIT_USAGE
    raw_text = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    parsed = seed.parse_holidays(raw_text)
    for note in parsed.notes:
        print(f"提示：{note}")
    if parsed.problems:
        print("休市清单有问题，**未写库**：", file=sys.stderr)
        for problem in parsed.problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_ATTENTION

    all_rows = seed.build_day_rows(args.year, parsed.dates)
    problems, notes = seed.sanity(year=args.year, holidays=parsed.dates, rows=all_rows)
    for note in notes:
        print(f"提示：{note}")
    if problems:
        print("sanity 闸门拒绝，**未写库**：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_ATTENTION

    try:
        start, end = _resolve_range(
            market=args.market, year=args.year, date_from=args.date_from, date_to=args.date_to
        )
    except ValueError as exc:
        print(f"用法错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    window = [row for row in all_rows if start <= row.trade_date <= end]
    print(f"休市清单：{len(parsed.dates)} 天（来自 {path.name}，sha256 {digest[:12]}）")
    print(f"全年校验通过：{args.year} 年 {seed.summarise(all_rows)['trading_days']} 个交易日")
    if not window:
        print(f"范围内无日期可播（{start.isoformat()} → {end.isoformat()}），无事可做。")
        return EXIT_OK
    for line in _preview_lines(window, start, end):
        print(line)
    print(f"市场：{_keys_label(args.market)}（同一物理市场的多个查询键要一起写）")
    print(f"作用域：{args.tenant}/{args.user}（'default/*' 是全局兜底，对所有租户生效）")

    if args.verify:
        mismatches = asyncio.run(
            _verify(market=args.market, rows=window, tenant=args.tenant, user=args.user)
        )
        if mismatches:
            print(f"回读不一致 {len(mismatches)} 天（**未生效**）：", file=sys.stderr)
            for item in mismatches[:10]:
                print(f"  - {item}", file=sys.stderr)
            if len(mismatches) > 10:
                print(f"  ……另有 {len(mismatches) - 10} 天", file=sys.stderr)
            return EXIT_ATTENTION
        print(f"回读通过：{len(window)} 天全部是 {SRC_DB_OVERRIDE}（真日历降级不再兜底）")
        return EXIT_OK

    if not args.apply:
        print("预演结束（未写库）。加 --apply 落库。")
        return EXIT_OK

    written = asyncio.run(
        _apply(
            market=args.market,
            rows=window,
            tenant=args.tenant,
            user=args.user,
            source=seed.SEED_SOURCE,
            version=digest[:12],
            metadata={
                "file": path.name,
                "sha256": digest,
                "written_by": "seed_trading_calendar.py",
                "written_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
    )
    print(f"已写入 {written} 行，来源={seed.SEED_SOURCE}，版本={digest[:12]}。")
    print("下一步：跑 --verify 回读确认（走真实判定路径）。")
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把次年交易日播进 DB override（qm_market_calendar_day）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--market", default=seed.DEFAULT_MARKET, help="市场代码（默认 CN）")
    parser.add_argument(
        "--tenant", default=seed.DEFAULT_TENANT, help="作用域租户（默认 default＝全局兜底）"
    )
    parser.add_argument(
        "--user", default=seed.DEFAULT_USER, help="作用域用户（默认 *＝全局兜底）"
    )
    parser.add_argument("--year", type=int, help="播种年份（如 2027）")
    parser.add_argument("--holidays-file", help="休市清单文件（每行一个 YYYY-MM-DD）")
    parser.add_argument("--from", dest="date_from", help="起始日（缺省＝真日历覆盖末日次日）")
    parser.add_argument("--to", dest="date_to", help="截止日（缺省＝当年 12-31）")
    parser.add_argument("--apply", action="store_true", help="落库（默认只预演）")
    parser.add_argument("--verify", action="store_true", help="回读校验（走 trading_day_verdict）")
    parser.add_argument("--status", action="store_true", help="只看现状（真日历覆盖 + 库内存量）")
    parser.add_argument("--set", dest="set_spec", help="单日覆盖 DATE=0|1（需配 --reason）")
    parser.add_argument("--reason", default="", help="单日覆盖的原因（写进 metadata）")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        seed.market_calendar_name(args.market)
    except ValueError as exc:
        print(f"用法错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    modes = [bool(args.status), bool(args.set_spec), bool(args.year or args.holidays_file)]
    if sum(modes) != 1:
        print(
            "用法错误：三选一 —— --status / --set DATE=0|1 / (--year + --holidays-file)",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if args.status:
        return _run_status(args)
    if args.set_spec:
        if not str(args.reason).strip():
            print("用法错误：--set 必须带 --reason（覆盖要留原因）", file=sys.stderr)
            return EXIT_USAGE
        return _run_set(args)
    if not args.year or not args.holidays_file:
        print("用法错误：--year 与 --holidays-file 必须同时给", file=sys.stderr)
        return EXIT_USAGE
    return _run_seed(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
