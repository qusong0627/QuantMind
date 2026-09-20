#!/usr/bin/env python3
"""市场真实性门禁 —— 把「离线好看、实盘翻车」的缺陷变成非零退出码。

和同目录其他 `audit_*.py` 的区别：那些是**一次性分析师**（打印完 `return 0`），
这个是**门禁** —— 有 CRITICAL 就退出 1，可以挂进 CI / 发布前检查。

三种模式：

    # 1. 数据集体检：年代指示器 + 缺失率。读 parquet footer 的 null count，
    #    不解码任何数据页 —— 整个 features_daily（2604 个分区）只要 0.8 秒，
    #    所以**永远全量扫描，不采样**（采样会把「零散缺失」误判成年代分隔）。
    python3 backend/scripts/audit/check_market_fidelity.py dataset \
        --root data/quantdb/6_ml_datasets/features_daily --from 20220101

    # 2. 源码扫描：绕过唯一权威实现、自写的涨跌停阈值
    python3 backend/scripts/audit/check_market_fidelity.py source --fail-on high

    # 3. 回测窗口：短窗报年化
    python3 backend/scripts/audit/check_market_fidelity.py window --days 12 --context "L2 Top20"

判据实现全部在 `backend/shared/market_fidelity.py`，此处只负责取数与呈现。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许以脚本方式直接运行（`python3 backend/scripts/audit/xxx.py`）
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.shared.market_fidelity import (  # noqa: E402
    FidelityError,
    FidelityReport,
    Finding,
    availability_island,
    check_backtest_window,
    era_switch,
    scan_hardcoded_limit_thresholds,
)

#: 「全空」/「全有」的判定门槛。era_switch 保证每日缺失率非 0 即 1。
_NULL = 0.5

#: 尾部全空不超过这么多天、且断点紧贴区间起点时，判为**指标预热期**而非年代指示器。
#: 例：5 日收益在序列头 5 天必然全空，这是数学必然，不是缺陷。
#: 阈值是启发式的（真实所需窗口未知），所以预热期只报 MEDIUM 并把天数写进 detail，
#: 由人决定窗口是否真的从断点之后开始。
DEFAULT_WARMUP_MAX = 30


def _read_partition_meta(parquet_file: Path) -> tuple[int, dict[str, int]]:
    """从 parquet footer 读 (总行数, {列名: null 数})。

    **不解码任何数据页**。null count 是列名绑定的，所以完全不受
    parquet 物理列序漂移影响（见 parquet-column-order-drift）。
    """
    import pyarrow.parquet as pq

    md = pq.ParquetFile(parquet_file).metadata
    nulls: dict[str, int] = {}
    for rg in range(md.num_row_groups):
        row_group = md.row_group(rg)
        for c in range(row_group.num_columns):
            col = row_group.column(c)
            name = col.path_in_schema
            nulls[name] = nulls.get(name, 0) + (col.statistics.null_count or 0)
    return md.num_rows, nulls


def _collect_fractions(
    root: Path, date_from: str, date_to: str
) -> tuple[list[str], dict[str, dict[str, float]]]:
    """全量收集「列 → {日期: 缺失率}」。缺列记 1.0。

    某日在文件里**根本没有这一列**，与「整列全空」对模型是同一种伤害，
    判据上也该同一条规则 —— 所以缺席按 1.0 计，不特殊处理。
    """
    # 日期与目录**成对**流转。曾经用两个平行列表 + zip 做过滤，两次过滤时
    # 第二次 zip 会把已过滤的目录配上未过滤的日期，--from 与 --to 同时给出
    # 就静默丢掉合法分区。
    partitions: list[tuple[str, Path]] = sorted(
        (p.name.split("=", 1)[1], p) for p in root.glob("dt=*") if p.is_dir()
    )
    if not partitions:
        raise SystemExit(f"数据集目录下没有 dt=* 分区：{root}")

    if date_from:
        partitions = [(d, p) for d, p in partitions if d >= date_from]
    if date_to:
        partitions = [(d, p) for d, p in partitions if d <= date_to]
    if not partitions:
        raise SystemExit(f"区间 [{date_from or '*'}, {date_to or '*'}] 内没有任何分区")

    fractions: dict[str, dict[str, float]] = {}
    for d, d_dir in partitions:
        files = sorted(d_dir.glob("*.parquet"))
        if not files:
            continue
        n_rows, nulls = _read_partition_meta(files[0])
        if n_rows == 0:
            continue
        for name, cnt in nulls.items():
            fractions.setdefault(name, {})[d] = cnt / n_rows

    # 逐列回填缺席日期。**必须在取数层做**：某日文件里根本没有这一列，与
    # 「整列全空」对模型是同一种伤害，判据上该是同一条规则。收集循环做不到
    # 这件事（缺席的日期压根不会进入该列的字典），若留给消费者各自
    # `.get(d, 1.0)`，漏一处就是一处静默免检。
    dates_seq = [d for d, _ in partitions]
    for per_date in fractions.values():
        for d in dates_seq:
            per_date.setdefault(d, 1.0)

    return dates_seq, fractions


def _scan_dataset(
    root: Path,
    date_from: str,
    date_to: str,
    warmup_max: int,
    max_rate: float,
) -> FidelityReport:
    print(f"数据集 {root}")
    dates_seq, fractions = _collect_fractions(root, date_from, date_to)
    if not fractions:
        raise SystemExit(f"未从 {root} 读到任何列元数据")
    print(
        f"  全量扫描 {len(dates_seq)} 个分区（{dates_seq[0]} ~ {dates_seq[-1]}），{len(fractions)} 列"
    )

    findings: list[Finding] = []
    for col in sorted(fractions):
        # 直接索引不兜底：缺席已由 `_collect_fractions` 统一回填为 1.0，
        # 这里再写一次 `.get(d, 1.0)` 就是同一条规则的第二份实现。
        fracs = [fractions[col][d] for d in dates_seq]
        n_dates = len(dates_seq)
        already_reported = False

        hit = era_switch(dates_seq, fracs)
        if hit is not None:
            already_reported = True
            switch_date, side = hit
            run = sum(1 for f in fracs if f > _NULL)  # era_switch 保证只有一段连续全空

            if side == "before" and run <= warmup_max:
                findings.append(
                    Finding(
                        rule="series_warmup",
                        severity="MEDIUM",
                        subject=col,
                        detail=(
                            f"区间头 {run} 天全空（断点 {switch_date}），与指标自身的回看窗口吻合，"
                            f"疑似预热期；若训练/评估窗口起点晚于 {switch_date} 则无害"
                        ),
                    )
                )
            else:
                absent = "此前不存在" if side == "before" else "此后被删除"
                findings.append(
                    Finding(
                        rule="era_indicator",
                        severity="CRITICAL",
                        subject=col,
                        detail=(
                            f"该列{absent}，断点 {switch_date}，全空 {run}/{n_dates} 天；"
                            f"列本身编码了年份，树模型会优先学它 —— 须从特征轴剔除，"
                            f"或把窗口收在断点同一侧"
                        ),
                    )
                )
        else:
            # era_switch 只认「恰好一次切换」，中段孤岛归这条管。
            island = availability_island(dates_seq, fracs)
            if island is not None:
                already_reported = True
                start, span, total = island
                findings.append(
                    Finding(
                        rule="data_island",
                        severity="CRITICAL",
                        subject=col,
                        detail=(
                            f"该列只在 {start} 起的 {span} 天内有值（全区间 {total} 天）；"
                            f"可用性本身是日期的函数，模型会把这段日期当特征学走"
                        ),
                    )
                )

        # 缺失率分母是**全部日期**。只统计「该列可用的日期」会让几乎全空的列
        # 因分母为空而静默免检 —— 实测 l1_factors.fun_peg 每日缺失率恒 0.9996，
        # 就是这样整列躲过检查的。
        rate = sum(fracs) / n_dates
        if rate > max_rate and not already_reported:
            findings.append(
                Finding(
                    rule="missing_rate",
                    severity="HIGH",
                    subject=col,
                    detail=f"全区间平均缺失率 {rate:.1%} > 上限 {max_rate:.1%}",
                )
            )

    return FidelityReport(findings=tuple(findings), n_checked=len(fractions))


def _scan_source(paths: list[str]) -> FidelityReport:
    findings = scan_hardcoded_limit_thresholds(paths)
    print(f"源码扫描 {len(paths)} 个根目录，命中 {len(findings)} 处")
    return FidelityReport(findings=tuple(findings), n_checked=len(paths))


def _check_window(days: int, context: str, minimum: int) -> FidelityReport:
    dates = [f"{i:05d}" for i in range(days)]
    return FidelityReport(
        findings=tuple(
            check_backtest_window(dates, min_trading_days=minimum, context=context)
        ),
        n_checked=1,
    )


def _add_fail_on(parser) -> None:
    """`--fail-on` 在顶层与各子命令上都可用。

    只挂在顶层的话，`source --fail-on high` 会被 argparse 判为未知参数并以
    退出码 2 失败 —— 而本模块的文档示例正是这个写法。
    `default=SUPPRESS` 是关键：子命令不写默认值，才不会把顶层已解析到的
    取值覆盖回 "critical"。
    """
    parser.add_argument(
        "--fail-on",
        choices=("critical", "high"),
        default=argparse.SUPPRESS,
        help="退出非零的严重度门槛（默认 critical）",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--fail-on",
        choices=("critical", "high"),
        default="critical",
        help="退出非零的严重度门槛（默认 critical）",
    )
    sub = ap.add_subparsers(dest="mode", required=True)

    p_ds = sub.add_parser("dataset", help="数据集体检（年代指示器 / 缺失率）")
    _add_fail_on(p_ds)
    p_ds.add_argument("--root", required=True, type=Path)
    p_ds.add_argument(
        "--from",
        dest="date_from",
        default="",
        help="只考察该日期（含）之后，YYYYMMDD；预热期与早期断点会被排除在外",
    )
    p_ds.add_argument(
        "--to", dest="date_to", default="", help="只考察该日期（含）之前，YYYYMMDD"
    )
    p_ds.add_argument(
        "--warmup-max",
        type=int,
        default=DEFAULT_WARMUP_MAX,
        help=f"区间头全空不超过这么多天判为预热期（默认 {DEFAULT_WARMUP_MAX}）",
    )
    p_ds.add_argument(
        "--max-rate", type=float, default=0.5, help="缺失率上限（默认 0.5）"
    )

    p_src = sub.add_parser("source", help="源码扫描（自写涨跌停阈值）")
    _add_fail_on(p_src)
    p_src.add_argument("paths", nargs="*", default=["backend", "scripts", "tools"])

    p_win = sub.add_parser("window", help="回测窗口长度")
    _add_fail_on(p_win)
    p_win.add_argument("--days", type=int, required=True)
    p_win.add_argument("--context", default="")
    p_win.add_argument("--min-days", type=int, default=60)

    args = ap.parse_args(argv)

    if args.mode == "dataset":
        report = _scan_dataset(
            args.root, args.date_from, args.date_to, args.warmup_max, args.max_rate
        )
    elif args.mode == "source":
        report = _scan_source(args.paths)
    else:
        report = _check_window(args.days, args.context, args.min_days)

    print()
    print(report.summary(), flush=True)  # 先冲 stdout，否则 stderr 的结论会插到报告前面

    blocked = report.critical if args.fail_on == "critical" else report.findings
    if blocked:
        print(
            f"\n✗ 门禁未通过：{len(blocked)} 条达到 --fail-on={args.fail_on}",
            file=sys.stderr,
        )
        return 1
    print("\n✓ 门禁通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FidelityError as exc:  # 门禁自检失败也算失败，不能当异常吞掉
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
