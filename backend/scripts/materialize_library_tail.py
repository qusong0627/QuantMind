#!/usr/bin/env python3
"""自有因子库尾窗物化编排（生产入口，**主机侧**运行）。

职责：把「每天新增交易日」的因子值串行补齐到各正式库，并在合并前后各做一次
证伪——这些库的公式不在数据平台的同步链路里，漏一天，第二天推理就少一批特征。

库与生产者的分工：

============  =====================================================
库            生产者
============  =====================================================
l1_batch1     ``backend/scripts/l1_factors_dev/build_production.py``
rd_mined      ``rd_mined_materialize --refresh-since``（容器内，scipy 依赖）
factor_defs   ``factor_defs_factors.py``（1336 列，两遍物化）
cand_factors  由 factor_defs 派生（本包 ``cand_factors.py``）
factor1300    ``data/factor1300/scripts/f1300_compute.py``（两遍物化）
gap_mined     ``factor_materialize.gap_mined build``
============  =====================================================

两遍物化（2026-10-20 起）：清单里的 ``window`` 只是单层窗口，嵌套 rolling 的
需求是**相加**（``REF(504)+TSRANK(504)=1008``）。第一遍按 900/400 日跑全库
（长窗列自然为空，``skip_empty_columns`` 不覆盖存量）；第二遍用独立 staging
``--only`` 重算这批因子（factor_defs 1 条 1008 日、factor1300 41 条 ≤1220 日），
探针通过后列级合并。超过 ``MAX_DAILY_WARMUP_DAYS`` 的非必需因子延后到每周
长窗档（见 ``docs/自有因子库每日物化_实施细案_20261019.md`` §六）。

执行纪律：

- **串行**：生产者之间共享 ``_partial_pilot`` 与内存预算，绝不并行；
- **staging 隔离**：生产者写 ``_staging_<库>``，正式库只经列级合并改动；
- **重叠日探针**：生产者多写前一个交易日（不并入），用「短预热复算值」与
  「正式库全历史值」比对；必需列由有值变全空、覆盖跌到一半以下、秩相关跌破
  0.9 即判失败（预热不足/取错列/单位切换）；qfq 每晚重算带来的数值级相对
  误差只报信息（实测秩相关 0.985~0.9999，相对误差最高 0.66）；
- **回补**：近 ``--repair-window`` 个交易日里必需列「前一日有值、当日全空」的
  日期自动重算（典型场景：两融分区当晚未同步，MG2 当日只能写 NaN）；
- **不吞错**：任一步骤失败照常写状态与日志，进程退出码非 0，由外层告警。

用法::

    python3 backend/scripts/materialize_library_tail.py --dry-run
    python3 backend/scripts/materialize_library_tail.py                 # 补最新缺口
    python3 backend/scripts/materialize_library_tail.py --since 20261009  # 显式回填
    python3 backend/scripts/materialize_library_tail.py --libraries factor_defs,cand_factors

日志：``logs/keepers/factor_materialize_<run_id>.log``；
状态：``<quantdb>/6_ml_datasets/_materialize_status.json``。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import resource
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from backend.scripts.factor_materialize import cand_factors  # noqa: E402
from backend.scripts.factor_materialize import contract as model_contract  # noqa: E402
from backend.scripts.factor_materialize import library_ops as ops  # noqa: E402
from backend.scripts.factor_materialize import probe  # noqa: E402
from backend.scripts.factor_materialize import warmup  # noqa: E402
from backend.scripts.factor_materialize.merge import (  # noqa: E402
    backup_partition,
    merge_day_columns,
    partition_path,
)

LOG = logging.getLogger("factor_materialize.orchestrator")

STATUS_NAME = "_materialize_status.json"
STAGING_PREFIX = "_staging_"
PARTIAL_PILOT = "_partial_pilot"
#: 修复/回填前的正式分区快照（首次快照保留，供人工回滚）
REPAIR_BACKUP = "_repair_backup"
DEFAULT_F1300_SCRIPT = REPO / "data" / "factor1300" / "scripts" / "f1300_compute.py"
#: 各库第一遍（全库）预热交易日数：覆盖绝大多数公式的单层窗口。
#: 嵌套窗口相加后仍超出它的因子，由第二遍 ``--only`` 长窗复算补齐
#: （见 ``factor_materialize.warmup``）。
WARMUP_DAYS = {
    "factor_defs": 900,
    "factor1300": 400,
}
#: 第二遍预热余量：rolling(H) 在窗口首日已需 H 日（含当日），留几天吸收停牌/
#: 非交易日错位；``_max_days`` 另有日期跨度补足。
WARMUP_MARGIN_DAYS = 8
#: 每晚第二遍可承受的最长预热（交易日）。超过它的非必需因子不进入日更链，
#: 由每周长窗档补齐（细案 §六）。2026-10-20 全标的 1228 日实测峰值 10.19GB
#: （172 帧；固定开销约 2.8GB，其余随标的数增长），不再是早期按快档合并峰值
#: 外推的 26GB。模型契约列不受此限。
MAX_DAILY_WARMUP_DAYS = 1300
#: 第二遍开跑前的最小可用内存（GB）。长窗遍实测峰值 10.19GB，写入/合并另需
#: 余量；不足时**只跳过第二遍**并记 partial（第一遍照常合并，缺失列由下一轮
#: 近窗回补自愈），绝不整轮失败或静默留空。
LONG_PASS_MIN_MEM_GB = float(os.environ.get("QM_MATERIALIZE_LONG_MIN_MEM_GB", "11"))
#: 长窗遍求值驻留：每批 8 条（1234 日 × 5579 标的每条约 55MB）。默认 30 条会
#: 多占 1.2GB，把实测 10.19GB 峰值推过 11GB 闸门。
LONG_PASS_BANK_SIZE = 8
#: alpha 刷新/补池的 flock 文件（与 scripts/factor_materialize_daily.sh 同源）。
#: 补池重试可能在日更链起跑**之后**才拿锁；长窗遍开跑前再探一次，避免 24GB
#: 峰值与本链 10GB 峰值叠加。
ALPHA_LOCKS = (
    Path(os.environ.get("QM_ALPHA_LOCK", "/tmp/.qm_alpha_refresh.lock")),
    Path(os.environ.get("QM_ALPHA_RETRY_LOCK", "/tmp/.qm_alpha_retry.lock")),
)
#: 单次回填允许的最大交易日数（防止误把 ``--since`` 写成很久以前）
MAX_BACKFILL_DAYS = 60
#: 新建当日分区时，全市场标的数相对上一正式分区的最低比例。低于它说明
#: daily_backward 当日分区还没同步完，落盘会造出「永远补不上标的」的残分区。
UNIVERSE_MIN_RATIO = 0.9


@dataclass
class Context:
    repo: Path
    quantdb: Path
    custom: Path
    target: str
    since: str | None
    dry_run: bool
    keep_staging: bool
    python: str
    docker: str
    container: str
    timeout_s: int
    repair_window: int
    l1_workers: int
    bank_size: int
    f1300_script: Path
    calendar: list[str] = field(default_factory=list)
    contract: model_contract.Contract | None = None
    libraries: tuple[str, ...] = ()
    dates: dict[str, list[str]] = field(default_factory=dict)
    plan_errors: dict[str, str] = field(default_factory=dict)
    log_path: Path = Path("logs/keepers/factor_materialize.log")

    def staging(self, library: str) -> Path:
        return self.quantdb / "6_ml_datasets" / f"{STAGING_PREFIX}{library}"


@dataclass
class StepResult:
    library: str
    status: str = "skipped"  # ok | partial | failed | skipped
    dates: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    detail: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "library": self.library,
            "status": self.status,
            "dates": self.dates,
            "elapsed_s": round(self.elapsed_s, 1),
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# 日期选择
# ---------------------------------------------------------------------------


def _normalize_target(calendar: list[str], target: str | None) -> str:
    if not calendar:
        raise SystemExit("daily_forward 无分区，无法推导交易日历")
    if target is None:
        return calendar[-1]
    if target not in calendar:
        earlier = [dt for dt in calendar if dt <= target]
        if not earlier:
            raise SystemExit(f"目标日 {target} 早于交易日历起点 {calendar[0]}")
        chosen = earlier[-1]
        LOG.warning("目标日 %s 不是交易日（或无行情分区），回退到 %s", target, chosen)
        return chosen
    return target


def plan_dates(
    ctx: Context,
    library: str,
    root: Path,
    *,
    required: list[str],
) -> list[str]:
    """本库本次要写的日期：缺口 + 近窗回补（``--since`` 时取闭区间）。

    库为空且未给 ``--since`` 时拒绝猜测——全量回填不是日更任务的默认行为。
    """
    calendar = ctx.calendar
    if ctx.since:
        dates = [dt for dt in calendar if ctx.since <= dt <= ctx.target]
    else:
        latest = ops.latest_partition(root)
        if latest is None:
            raise SystemExit(
                f"{library} 无任何分区：请用 --since 明确回填起点（禁止隐式全量）"
            )
        dates = [dt for dt in calendar if latest < dt <= ctx.target]
    repairs = probe.repair_dates(
        root,
        calendar,
        target=ctx.target,
        required=required,
        window=ctx.repair_window,
    )
    selected = sorted(set(dates) | set(repairs))
    if len(selected) > MAX_BACKFILL_DAYS:
        raise SystemExit(
            f"{library} 本次需写 {len(selected)} 个交易日，超过上限 "
            f"{MAX_BACKFILL_DAYS}：请分段回填"
        )
    if repairs:
        LOG.info("%s 回补日（必需列前值有值、当日全空）：%s", library, repairs)
    return selected


def _producer_start(calendar: list[str], dates: list[str]) -> str:
    """生产者多写的前置日（重叠探针用）；没有更早交易日时返回首日。"""
    first = dates[0]
    index = calendar.index(first)
    return calendar[index - 1] if index > 0 else first


def _max_days(calendar: list[str], start: str, target: str, warmup: int) -> int:
    span = sum(1 for dt in calendar if start <= dt <= target)
    return warmup + max(span, 1)


def _start_year(calendar: list[str], first: str, warmup: int) -> int:
    index = calendar.index(first)
    anchor = calendar[max(0, index - warmup)]
    return int(anchor[:4]) - 1


def split_long_factors(
    windows: dict[str, int],
    fast_warmup: int,
    required: Iterable[str],
    *,
    cap: int = MAX_DAILY_WARMUP_DAYS,
) -> tuple[list[str], list[str], int]:
    """把「嵌套窗口超出第一遍预热」的因子拆成日更第二遍与延后两档。

    返回 ``(daily_long, deferred, long_warmup)``：

    - ``daily_long``：所需历史 ``> fast_warmup`` 且 ``<= cap`` 的因子；
      **模型契约列无条件进入**（推理要读，不能延后）。
    - ``deferred``：超过 ``cap`` 的其余因子（每周长窗档补，见细案 §六）。
    - ``long_warmup``：第二遍应使用的预热（最长需求 + 余量）。
    """
    needed = warmup.select_long(windows, fast_warmup)
    required_set = set(required)
    daily = [name for name in needed if windows[name] <= cap or name in required_set]
    daily_set = set(daily)
    deferred = [name for name in needed if name not in daily_set]
    long_warmup = (
        warmup.longest_requirement(windows, daily) + WARMUP_MARGIN_DAYS
        if daily
        else fast_warmup
    )
    return daily, deferred, long_warmup


# ---------------------------------------------------------------------------
# 子进程与合并
# ---------------------------------------------------------------------------


def producer_env(ctx: Context) -> dict[str, str]:
    """子进程环境：仓库根必须在 ``PYTHONPATH`` 上。

    ``build_production.py`` 直接以脚本路径执行时 ``sys.path[0]`` 是脚本目录，
    ``import backend`` 必崩（2026-10-20 真实回填复现：0.4s 失败）。其余生产者
    虽自行插了 sys.path，统一注入也避免下一个生产者重踩同一坑。
    """
    env = dict(os.environ)
    env.setdefault("QM_QUANTDB_DATA_DIR", str(ctx.quantdb))
    env.setdefault("QM_QUANTCUSTOM_DATA_DIR", str(ctx.custom))
    repo = str(ctx.repo)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = repo if not existing else f"{repo}{os.pathsep}{existing}"
    return env


def run_command(
    ctx: Context,
    cmd: list[str],
    *,
    log_path: Path,
    description: str,
    cwd: Path | None = None,
    fail_markers: tuple[str, ...] = (),
) -> dict[str, object]:
    """执行生产者；``fail_markers`` 命中也判失败（如 rd_mined 的锁冲突退出 0）。"""
    LOG.info("$ %s", " ".join(cmd))
    if ctx.dry_run:
        return {"dry_run": True, "cmd": cmd}
    env = producer_env(ctx)
    started = time.time()
    start_offset = log_path.stat().st_size if log_path.is_file() else 0
    with log_path.open("a", encoding="utf-8") as sink:
        sink.write(
            f"\n===== {datetime.now().isoformat()} {description}\n$ {' '.join(cmd)}\n"
        )
        sink.flush()
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(cwd or ctx.repo),
                env=env,
                stdout=sink,
                stderr=subprocess.STDOUT,
                timeout=ctx.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "returncode": -9,
                "elapsed_s": round(time.time() - started, 1),
                "error": f"超时（>{ctx.timeout_s}s）",
            }
    # ru_maxrss 是「已回收子进程的最大 RSS」（Linux 单位 KB，单调不减），
    # 用于给 cron 内存闸门定标；docker exec 只统计到 docker CLI 本身。
    peak_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // 1024
    result: dict[str, object] = {
        "returncode": completed.returncode,
        "elapsed_s": round(time.time() - started, 1),
        "child_peak_rss_mb": int(peak_mb),
    }
    if fail_markers:
        with log_path.open("rb") as handle:
            handle.seek(start_offset)
            text = handle.read().decode("utf-8", "ignore")
        hits = [marker for marker in fail_markers if marker in text]
        if hits:
            result["returncode"] = -8
            result["error"] = f"日志命中失败标记：{hits}"
    return result


def prepare_staging(ctx: Context, staging: Path, dates: list[str]) -> None:
    """清掉本次要重算的 staging 日期与生产者 partial 残留（dry-run 不动盘）。"""
    if ctx.dry_run:
        return
    ops.clear_staging(staging, dates)
    clear_partial_pilot(ctx.quantdb)


def clear_partial_pilot(quantdb: Path) -> None:
    """清掉生产者共享的 partial 目录（上轮失败残骸会被 merge_partials 误并）。"""
    root = quantdb / "6_ml_datasets" / PARTIAL_PILOT
    if root.is_dir():
        LOG.warning("清理生产者 partial 残留：%s", root)
        # 清理失败必须抛出：残留 partial 会被生产者 merge_partials 当本批结果并入
        shutil.rmtree(root)


def merge_staged(
    ctx: Context,
    library: str,
    dates: list[str],
    *,
    skip_empty_columns: bool,
    pass_symbols: bool = True,
    staging: Path | None = None,
) -> dict[str, object]:
    """staging → 正式库列级合并；只合并生产者真的写出来的日期。"""
    staging = staging or ctx.staging(library)
    root = ops.library_root(ctx.quantdb, library)
    merged: list[str] = []
    missing: list[str] = []
    universe_short: list[dict[str, object]] = []
    backups: list[str] = []
    stats: list[dict[str, object]] = []
    for dt in dates:
        if not partition_path(staging, dt).is_file():
            missing.append(dt)
            continue
        if ctx.dry_run:
            merged.append(dt)
            continue
        symbols = ops.symbols_for_date(ctx.quantdb, dt) if pass_symbols else None
        target_exists = partition_path(root, dt).is_file()
        if pass_symbols and not target_exists:
            # 新日的行集一旦落盘就补不了标的：行情分区未同步完时宁可跳过
            previous = ops.previous_partition(root, dt)
            previous_count = (
                len(ops.symbols_for_date(ctx.quantdb, previous)) if previous else 0
            )
            if not symbols:
                universe_short.append(
                    {
                        "dt": dt,
                        "symbols": 0,
                        "reason": "daily_backward 无当日分区",
                    }
                )
                LOG.error("%s %s 无当日全市场标的，跳过合并", library, dt)
                continue
            if previous_count and len(symbols) < UNIVERSE_MIN_RATIO * previous_count:
                universe_short.append(
                    {
                        "dt": dt,
                        "symbols": len(symbols),
                        "previous_dt": previous,
                        "previous_symbols": previous_count,
                    }
                )
                LOG.error(
                    "%s %s 标的数 %d 低于 %s 的 %d（<%.0f%%），跳过合并",
                    library,
                    dt,
                    len(symbols),
                    previous,
                    previous_count,
                    UNIVERSE_MIN_RATIO * 100,
                )
                continue
        if target_exists:
            backup = backup_partition(
                root, dt, ctx.quantdb / "6_ml_datasets" / REPAIR_BACKUP / library
            )
            if backup is not None:
                backups.append(str(backup))
        stat = merge_day_columns(
            staging,
            root,
            dt,
            template_root=root,
            symbols=symbols if pass_symbols else None,
            skip_empty_columns=skip_empty_columns,
        )
        merged.append(dt)
        stats.append(stat)
    return {
        "merged": merged,
        "missing": missing,
        "universe_short": universe_short,
        "backups": backups,
        "merge_stats": stats,
    }


def cleanup_staging(ctx: Context, staging: Path, dates: list[str]) -> None:
    if ctx.keep_staging or ctx.dry_run:
        return
    for dt in dates:
        shutil.rmtree(staging / f"dt={dt}", ignore_errors=True)


# ---------------------------------------------------------------------------
# 各库步骤
# ---------------------------------------------------------------------------


def _finish_step(
    ctx: Context,
    result: StepResult,
    library: str,
    root: Path,
    dates: list[str],
    start: str,
    run: dict[str, object],
    required: list[str],
    *,
    pass_symbols: bool = True,
    staging: Path | None = None,
) -> None:
    """生产者跑完后的判活与合并：重叠探针先行，失败/探针未过一律不合并。

    失败时**保留 staging**（下一轮 prepare_staging 会清）：半批产物或探针
    样本是事后排查的唯一证据。
    """
    staging = staging or ctx.staging(library)
    probe_report = _overlap_probe(ctx, library, root, start, required, staging=staging)
    result.detail = {"run": run, "producer_start": start, "probe": probe_report}
    blocked = not ctx.dry_run and (
        run.get("returncode") != 0 or probe_report.get("ok") is False
    )
    manifest: dict[str, object] = {}
    if blocked:
        result.detail["merge_skipped"] = "生产者失败或重叠探针未通过"
        merged: dict[str, object] = {"missing": []}
    else:
        merged = merge_staged(
            ctx,
            library,
            dates,
            skip_empty_columns=True,
            pass_symbols=pass_symbols,
            staging=staging,
        )
        result.detail.update(merged)
        manifest = {} if ctx.dry_run else _manifest_summary(ctx, library, staging)
        if manifest:
            result.detail["manifest"] = manifest
        cleanup_staging(ctx, staging, sorted({start, *dates}))
    result.dates = dates
    result.status = _step_status(run, merged, probe_report, manifest)


def step_l1(ctx: Context) -> StepResult:
    library = "l1_batch1_factors"
    root = ops.library_root(ctx.quantdb, library)
    required = list(model_contract.gate_columns(library, ctx.contract))
    result = StepResult(library)
    started = time.time()
    if not ctx.dates.get(library):
        return result
    dates = ctx.dates[library]
    start = _producer_start(ctx.calendar, dates)
    staging = ctx.staging(library)
    prepare_staging(ctx, staging, sorted({start, *dates}))
    cmd = [
        ctx.python,
        str(ctx.repo / "backend/scripts/l1_factors_dev/build_production.py"),
        "--start",
        start,
        "--end",
        ctx.target,
        "--quantdb-root",
        str(ctx.quantdb),
        "--out",
        str(staging),
        "--workers",
        str(ctx.l1_workers),
    ]
    run = run_command(ctx, cmd, log_path=ctx.log_path, description=library)
    _finish_step(
        ctx,
        result,
        library,
        root,
        dates,
        start,
        run,
        required,
        pass_symbols=False,  # l1 行集 = 生产者自己的股票池，不是全市场
    )
    result.elapsed_s = time.time() - started
    return result


def rd_mined_target_ready(custom: Path, target: str) -> bool:
    """refresh 失败时判断目标日是否已落盘（写出一半也必须镜像，不能整批丢弃）。"""
    path = custom / "6_ml_datasets" / "rd_mined" / f"dt={target}" / "data.parquet"
    return path.is_file() and path.stat().st_size > 0


def step_rd_mined(ctx: Context) -> StepResult:
    library = "rd_mined"
    result = StepResult(library)
    started = time.time()
    dates = ctx.dates.get(library)
    if not dates:
        return result
    cmd = [
        ctx.docker,
        "exec",
        ctx.container,
        "python",
        "-m",
        "backend.scripts.rd_mined_materialize",
        "--refresh-since",
        dates[0],
        "--refresh-end",
        ctx.target,
    ]
    run = run_command(
        ctx,
        cmd,
        log_path=ctx.log_path,
        description=library,
        # 容器内锁被占时物化器打日志后退出 0（设计如此）；对编排器必须算失败，
        # 否则会照常 promote 一份没刷新的旧库。
        fail_markers=("另一个物化进程正在运行",),
    )
    # 单个因子超时会让 refresh 整体 rc≠0，但其余因子与目标日分区已经写好
    # （2026-10-20 实测 120/122）。目标日落盘就 promote，状态按 partial 告警；
    # 只有「什么都没写出来」才是 failed。
    target_ready = ctx.dry_run or rd_mined_target_ready(ctx.custom, ctx.target)
    needs_promote = not ctx.dry_run and (run.get("returncode") == 0 or target_ready)
    promote: dict[str, object] = {"returncode": 0, "skipped": True}
    if needs_promote:
        promote = run_command(
            ctx,
            [
                ctx.docker,
                "exec",
                ctx.container,
                "python",
                "backend/scripts/promote_rd_mined.py",
                "--register",
            ],
            log_path=ctx.log_path,
            description="rd_mined promote → quantdb",
        )
    result.dates = dates
    result.detail = {
        "run": run,
        "promote": promote,
        "target_partition_ready": target_ready,
    }
    result.elapsed_s = time.time() - started
    if ctx.dry_run:
        result.status = "skipped"
    elif promote.get("returncode") != 0:
        result.status = "failed"
    elif run.get("returncode") == 0:
        result.status = "ok"
    elif target_ready:
        result.status = "partial"
    else:
        result.status = "failed"
    return result


def _load_script_module(path: Path, name: str):
    """按路径加载仓外脚本（只取清单函数，不执行 main）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_factor_defs_records() -> list[dict]:
    # 用生产者自己的清单函数：可行性/不可用变量的过滤口径只此一份
    from backend.scripts import factor_defs_factors as producer

    return producer.load_factors()


def _load_factor1300_records(ctx: Context) -> list[dict]:
    module = _load_script_module(ctx.f1300_script, "_f1300_compute_warmup")
    return module.load_factors()


def step_factor_defs(ctx: Context) -> StepResult:
    return _run_two_pass(
        ctx,
        "factor_defs",
        WARMUP_DAYS["factor_defs"],
        _load_factor_defs_records,
        _factor_defs_cmd,
    )


def step_factor1300(ctx: Context) -> StepResult:
    return _run_two_pass(
        ctx,
        "factor1300",
        WARMUP_DAYS["factor1300"],
        lambda: _load_factor1300_records(ctx),
        _factor1300_cmd,
    )


def step_cand_factors(ctx: Context) -> StepResult:
    library = "cand_factors"
    result = StepResult(library)
    started = time.time()
    dates = ctx.dates.get(library)
    if not dates:
        return result
    stats = cand_factors.build_dates(
        ctx.quantdb,
        dates,
        dry_run=ctx.dry_run,
        keep_staging=ctx.keep_staging,
    )
    result.dates = dates
    result.detail = {"stats": stats}
    result.elapsed_s = time.time() - started
    if ctx.dry_run:
        result.status = "skipped"
        return result
    missing = stats.get("missing_source") or {}
    # factor_defs 当日 schema 缺的 cand 列：必需模型列缺席才降级，
    # 其余列（历史批次新增）只报告不告警。
    absent_required = sorted(
        set(stats.get("absent_in_defs") or [])
        & set(model_contract.gate_columns(library, ctx.contract))
    )
    if absent_required:
        result.detail["absent_required"] = absent_required
    result.status = "partial" if (missing or absent_required) else "ok"
    return result


def step_gap_mined(ctx: Context) -> StepResult:
    library = "gap_mined"
    root = ops.library_root(ctx.quantdb, library)
    required = list(model_contract.gate_columns(library, ctx.contract))
    result = StepResult(library)
    started = time.time()
    dates = ctx.dates.get(library)
    if not dates:
        return result
    start = _producer_start(ctx.calendar, dates)
    staging = ctx.staging(library)
    prepare_staging(ctx, staging, sorted({start, *dates}))
    cmd = [
        ctx.python,
        "-m",
        "backend.scripts.factor_materialize.gap_mined",
        "--quantdb-root",
        str(ctx.quantdb),
        "--out",
        str(staging),
        "build",
        "--start",
        start,
        "--end",
        ctx.target,
    ]
    run = run_command(ctx, cmd, log_path=ctx.log_path, description=library)
    _finish_step(ctx, result, library, root, dates, start, run, required)
    result.elapsed_s = time.time() - started
    return result


def _factor_defs_cmd(
    ctx: Context,
    staging: Path,
    start: str,
    warmup_days: int,
    only: list[str] | None,
) -> list[str]:
    cmd = [
        ctx.python,
        str(ctx.repo / "backend/scripts/factor_defs_factors.py"),
        "--start-dt",
        start,
        "--start-year",
        str(_start_year(ctx.calendar, start, warmup_days)),
        "--max-days",
        str(_max_days(ctx.calendar, start, ctx.target, warmup_days)),
        "--bank-size",
        str(ctx.bank_size),
        "--out-root",
        str(staging),
    ]
    if only:
        cmd += ["--only", ",".join(only)]
    return cmd


def _factor1300_cmd(
    ctx: Context,
    staging: Path,
    start: str,
    warmup_days: int,
    only: list[str] | None,
) -> list[str]:
    cmd = [
        ctx.python,
        str(ctx.f1300_script),
        "--start-dt",
        start,
        "--start-year",
        str(_start_year(ctx.calendar, start, warmup_days)),
        "--max-days",
        str(_max_days(ctx.calendar, start, ctx.target, warmup_days)),
        "--bank-size",
        str(LONG_PASS_BANK_SIZE if only else ctx.bank_size),
        "--out-root",
        str(staging),
    ]
    if only:
        cmd += ["--only", ",".join(only)]
    return cmd


#: 步骤终态排序（跨多遍取最坏）
_STATUS_RANK = {"skipped": 0, "ok": 1, "partial": 2, "failed": 3}


def _worst_status(statuses: list[str]) -> str:
    return max(statuses, key=_STATUS_RANK.__getitem__) if statuses else "skipped"


def _parse_mem_available(text: str) -> float | None:
    """``/proc/meminfo`` 文本 → MemAvailable（GB）；解析失败返回 None。"""
    for line in text.splitlines():
        if not line.startswith("MemAvailable:"):
            continue
        try:
            return int(line.split()[1]) / 1024 / 1024
        except (IndexError, ValueError):
            return None
    return None


def available_mem_gb() -> float | None:
    """宿主机可用内存（GB）。读不到时返回 None（调用方按「不拦」处理）。"""
    try:
        text = Path("/proc/meminfo").read_text(encoding="ascii")
    except OSError:
        return None
    return _parse_mem_available(text)


def alpha_busy() -> bool:
    """alpha 刷新/补池是否持锁（非阻塞探测，不创建锁文件）。"""
    import fcntl

    for path in ALPHA_LOCKS:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            return True
        finally:
            os.close(fd)
    return False


def long_pass_blocker(min_mem_gb: float = LONG_PASS_MIN_MEM_GB) -> dict | None:
    """长窗遍开跑闸：alpha 补池占用或可用内存不足 → 返回原因字典。

    两个闸都只影响**第二遍**：第一遍结果照常合并；缺的必需列由下一轮
    ``repair_dates`` 回补（状态记 partial，不静默）。
    """
    if alpha_busy():
        return {"reason": "alpha_busy"}
    avail = available_mem_gb()
    if avail is not None and avail < min_mem_gb:
        return {
            "reason": "low_memory",
            "available_mem_gb": round(avail, 2),
            "min_mem_gb": min_mem_gb,
        }
    return None


def _run_two_pass(
    ctx: Context,
    library: str,
    fast_warmup: int,
    load_records: Callable[[], list[dict]],
    build_cmd: Callable[[Context, Path, str, int, list[str] | None], list[str]],
    *,
    pass_symbols: bool = True,
) -> StepResult:
    """两遍物化：全库短预热 + 长窗因子 ``--only`` 长预热。

    第一遍保持原成本（全库），只把「嵌套窗口 > 短预热」的列留空；第二遍用独立
    staging 只算这批长窗因子，再列级合并进正式库。两遍各自做重叠日探针，
    第一遍的必需列集合剔除长窗列，避免把「本轮故意留空」误判成预热不足。
    """
    root = ops.library_root(ctx.quantdb, library)
    required = list(model_contract.gate_columns(library, ctx.contract))
    result = StepResult(library)
    started = time.time()
    dates = ctx.dates.get(library)
    if not dates:
        return result
    start = _producer_start(ctx.calendar, dates)
    windows = warmup.windows_for(load_records())
    daily_long, deferred, long_warmup = split_long_factors(
        windows, fast_warmup, required
    )
    long_set = set(daily_long)
    all_long = set(warmup.select_long(windows, fast_warmup))
    passes: list[tuple[str, Path, int, list[str] | None, list[str]]] = [
        (
            "fast",
            ctx.staging(library),
            fast_warmup,
            None,
            [column for column in required if column not in all_long],
        )
    ]
    if daily_long:
        passes.append(
            (
                "long",
                ctx.staging(f"{library}_long"),
                long_warmup,
                daily_long,
                [column for column in required if column in long_set],
            )
        )
    statuses: list[str] = []
    pass_details: dict[str, object] = {}
    dirty_dates = sorted({start, *dates})
    for name, staging, warmup_days, only, pass_required in passes:
        if name == "long" and not ctx.dry_run:
            blocker = long_pass_blocker()
            if blocker:
                LOG.warning(
                    "%s 长窗遍跳过（%s）：可用内存 %.1fGB，alpha 占用=%s；"
                    "第一遍结果照常合并，缺失列下一轮回补",
                    library,
                    blocker["reason"],
                    blocker.get("available_mem_gb") or -1.0,
                    blocker["reason"] == "alpha_busy",
                )
                pass_details["long"] = {
                    "skipped": blocker["reason"],
                    "warmup": warmup_days,
                    "only": only,
                    **blocker,
                }
                statuses.append("partial")
                continue
        prepare_staging(ctx, staging, dirty_dates)
        cmd = build_cmd(ctx, staging, start, warmup_days, only)
        run = run_command(
            ctx, cmd, log_path=ctx.log_path, description=f"{library}[{name}]"
        )
        probe_report = _overlap_probe(
            ctx, library, root, start, pass_required, staging=staging
        )
        blocked = not ctx.dry_run and (
            run.get("returncode") != 0 or probe_report.get("ok") is False
        )
        detail: dict[str, object] = {
            "run": run,
            "producer_start": start,
            "warmup": warmup_days,
            "probe": probe_report,
        }
        if only is not None:
            detail["only"] = only
        merged: dict[str, object] = {"missing": []}
        manifest: dict[str, object] = {}
        if blocked:
            detail["merge_skipped"] = "生产者失败或重叠探针未通过"
        else:
            merged = merge_staged(
                ctx,
                library,
                dates,
                skip_empty_columns=True,
                pass_symbols=pass_symbols,
                staging=staging,
            )
            detail.update(merged)
            manifest = {} if ctx.dry_run else _manifest_summary(ctx, library, staging)
            if manifest:
                detail["manifest"] = manifest
            cleanup_staging(ctx, staging, dirty_dates)
        statuses.append(_step_status(run, merged, probe_report, manifest))
        pass_details[name] = detail
    if deferred:
        LOG.warning(
            "%s 长窗因子 %d 个超出日更预热上限 %d 日，本轮延后（每周长窗档补）：%s",
            library,
            len(deferred),
            MAX_DAILY_WARMUP_DAYS,
            deferred,
        )
    result.dates = dates
    result.detail = {
        "passes": pass_details,
        "long_factors": daily_long,
        "long_warmup": long_warmup if daily_long else None,
        "deferred_long": deferred,
    }
    result.status = _worst_status(statuses)
    result.elapsed_s = time.time() - started
    return result


# ---------------------------------------------------------------------------
# 探针与状态
# ---------------------------------------------------------------------------


def _overlap_probe(
    ctx: Context,
    library: str,
    root: Path,
    start: str,
    required: list[str],
    *,
    staging: Path | None = None,
) -> dict[str, object]:
    """重叠日（生产者多写的前置日）复算值与正式库存量值比对。"""
    if ctx.dry_run:
        return {"dry_run": True, "date": start}
    official = partition_path(root, start)
    staged = partition_path(staging or ctx.staging(library), start)
    if not official.is_file():
        # 正式库还没这一日（新库首日）→ 无基线可比，不阻断
        return {"skipped": "正式库无前置日分区", "date": start}
    if not staged.is_file():
        # 生产者没写前置日（库首日/无行情）→ 无重叠可验，不阻断
        return {"skipped": "staging 无前置日分区", "date": start}
    report = probe.compare_partitions(official, staged, required=required)
    if not report.get("ok"):
        LOG.error("%s 重叠探针未通过（%s）：%s", library, start, report.get("issues"))
    return report


def _step_status(
    run: dict[str, object],
    merged: dict[str, object],
    probe_report: dict[str, object] | None = None,
    manifest: dict[str, object] | None = None,
) -> str:
    """步骤终态：生产者失败/探针不通过 → failed；缺日/标的不足/必需列求值失败 → partial。"""
    if run.get("dry_run"):
        return "skipped"
    if run.get("returncode") != 0:
        return "failed"
    if probe_report and probe_report.get("ok") is False:
        return "failed"
    if (manifest or {}).get("contract_failures"):
        return "partial"
    if merged.get("missing") or merged.get("universe_short"):
        return "partial"
    return "ok"


def _manifest_summary(
    ctx: Context, library: str, staging: Path | None = None
) -> dict[str, object]:
    """读生产者 MANIFEST.json：求值失败的因子中，命中模型必需列的降级为 partial。"""
    path = (staging or ctx.staging(library)) / "MANIFEST.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOG.warning("%s MANIFEST 不可读：%s", library, exc)
        return {"error": f"{type(exc).__name__}: {exc}"}
    failures = [str(name) for name in (data.get("eval_failures") or [])]
    required = set(model_contract.gate_columns(library, ctx.contract))
    contract_failures = sorted(set(failures) & required)
    if failures:
        LOG.warning(
            "%s 求值失败 %d 个（命中必需列 %d 个）",
            library,
            len(failures),
            len(contract_failures),
        )
    return {
        "n_factors": data.get("n_factors"),
        "partitions": data.get("partitions"),
        "eval_failures": failures,
        "contract_failures": contract_failures,
    }


def _coverage(ctx: Context, library: str) -> dict[str, object]:
    if ctx.dry_run:
        return {"dry_run": True}
    root = ops.library_root(ctx.quantdb, library)
    strict, deferred = model_contract.coverage_columns(library, ctx.contract)
    # 本轮没写不等于不用验：库里已是目标日时也要查最新日覆盖，
    # 否则「当天分区在、必需列全空」的退化只能等次日回补窗口发现。
    dates = ctx.dates.get(library) or [ctx.target]
    if not strict and not deferred:
        return {"skipped": True, "dates": dates}
    report = probe.coverage(root, dates, list(strict))
    if deferred:
        lag = probe.coverage(root, dates, list(deferred))
        report["deferred"] = lag.get("gaps") or {}
    return report


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _normalize_dt_text(value: str | None) -> str | None:
    """``YYYY-MM-DD`` / ``YYYY/MM/DD`` / ``YYYY.MM.DD`` → ``YYYYMMDD``。

    必须先归一再用：交易日历按 8 位串字典序截断，带分隔符的输入会截出
    空日历，最终误报成「daily_forward 无分区」。
    """
    if value is None:
        return None
    digits = "".join(ch for ch in value.strip() if ch.isdigit())
    if len(digits) != 8:
        raise SystemExit(f"日期须为 YYYYMMDD（收到 {value!r}）")
    return digits


def _build_context(args: argparse.Namespace) -> Context:
    quantdb = Path(
        args.quantdb_root
        or os.environ.get("QM_QUANTDB_DATA_DIR")
        or REPO / "data" / "quantdb"
    )
    custom = Path(
        args.custom_root
        or os.environ.get("QM_QUANTCUSTOM_DATA_DIR")
        or REPO / "data" / "quantcustom"
    )
    target_arg = _normalize_dt_text(args.target)
    since_arg = _normalize_dt_text(args.since)
    calendar = ops.calendar(quantdb, target=target_arg)
    target = _normalize_target(calendar, target_arg)
    if since_arg and since_arg > target:
        raise SystemExit(f"--since {since_arg} 晚于目标日 {target}")
    log_dir = REPO / "logs" / "keepers"
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    return Context(
        repo=REPO,
        quantdb=quantdb,
        custom=custom,
        target=target,
        since=since_arg,
        dry_run=args.dry_run,
        keep_staging=args.keep_staging,
        python=args.python,
        docker=args.docker,
        container=args.container,
        timeout_s=args.timeout,
        repair_window=args.repair_window,
        l1_workers=args.l1_workers,
        bank_size=args.bank_size,
        f1300_script=Path(args.f1300_script),
        calendar=calendar,
        log_path=log_dir / f"factor_materialize_{run_id}.log",
    )


def _select_dates(ctx: Context) -> dict[str, list[str]]:
    plans: dict[str, list[str]] = {}
    for library in ctx.libraries:
        root = ops.library_root(ctx.quantdb, library)
        required = list(model_contract.gate_columns(library, ctx.contract))
        try:
            plans[library] = plan_dates(ctx, library, root, required=required)
        except SystemExit as exc:
            # 单库无分区/超上限不能掀翻整轮：其余库照跑，状态文件如实落 failed
            ctx.plan_errors[library] = str(exc) or "日期规划失败"
            plans[library] = []
            LOG.error("%s 日期规划失败：%s", library, ctx.plan_errors[library])
    return plans


def _write_status(ctx: Context, results: list[StepResult], started: float) -> Path:
    path = ctx.quantdb / "6_ml_datasets" / STATUS_NAME
    payload = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "target": ctx.target,
        "since": ctx.since,
        "calendar_latest": ctx.calendar[-1] if ctx.calendar else None,
        "elapsed_s": round(time.time() - started, 1),
        "ok": all(step.status in ("ok", "skipped") for step in results),
        "steps": [step.as_dict() for step in results],
    }
    if not ctx.dry_run:
        previous: dict[str, object] = {}
        if path.is_file():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                previous = {}
        runs = list(previous.get("runs") or [])[-29:]
        runs.append(
            {
                "run_at": payload["run_at"],
                "target": payload["target"],
                "ok": payload["ok"],
                "statuses": {step.library: step.status for step in results},
            }
        )
        payload["runs"] = runs
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="自有因子库尾窗物化编排（主机侧）")
    parser.add_argument("--target", help="目标交易日（默认 daily_forward 最新一日）")
    parser.add_argument(
        "--since", help="显式回填起点（含）；缺省只补各库缺口 + 近窗回补"
    )
    parser.add_argument(
        "--libraries",
        default=",".join(
            (
                "l1_batch1_factors",
                "rd_mined",
                "factor_defs",
                "cand_factors",
                "factor1300",
                "gap_mined",
            )
        ),
        help="逗号分隔的库名（默认全部，按顺序串行）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不执行")
    parser.add_argument(
        "--keep-staging", action="store_true", help="保留 staging 供排查"
    )
    parser.add_argument("--python", default=sys.executable, help="主机 Python 解释器")
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--container", default="quantmind")
    parser.add_argument("--timeout", type=int, default=10800, help="单步超时秒数")
    parser.add_argument(
        "--repair-window", type=int, default=10, help="近窗回补检查的交易日数"
    )
    parser.add_argument("--l1-workers", type=int, default=4)
    parser.add_argument(
        "--bank-size", type=int, default=30, help="求值驻留因子数（内存闸，默认 30）"
    )
    parser.add_argument(
        "--f1300-script",
        default=str(DEFAULT_F1300_SCRIPT),
        help="factor1300 生产者路径",
    )
    parser.add_argument("--quantdb-root", default=None)
    parser.add_argument("--custom-root", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    started = time.time()
    ctx = _build_context(args)
    ctx.libraries = tuple(
        name.strip() for name in args.libraries.split(",") if name.strip()
    )
    unknown = [
        name
        for name in ctx.libraries
        if name not in model_contract.MATERIALIZED_LIBRARIES
    ]
    if unknown:
        parser.error(f"未知库名：{unknown}")
    ctx.contract = model_contract.load_contract(ctx.repo)
    LOG.info(
        "目标 %s（日历最新 %s），库=%s，since=%s",
        ctx.target,
        ctx.calendar[-1],
        ctx.libraries,
        ctx.since,
    )
    ctx.dates = _select_dates(ctx)
    for library, dates in ctx.dates.items():
        LOG.info("%s：%s", library, dates or "无缺口")

    steps = {
        "l1_batch1_factors": step_l1,
        "rd_mined": step_rd_mined,
        "factor_defs": step_factor_defs,
        "cand_factors": step_cand_factors,
        "factor1300": step_factor1300,
        "gap_mined": step_gap_mined,
    }
    results: list[StepResult] = []
    for library in ctx.libraries:
        if library in ctx.plan_errors:
            results.append(
                StepResult(
                    library,
                    status="failed",
                    detail={"error": ctx.plan_errors[library]},
                )
            )
            LOG.error("%s → failed（%s）", library, ctx.plan_errors[library])
            continue
        try:
            result = steps[library](ctx)
        except Exception as exc:  # 单库异常不能掀翻整轮：落状态、继续后续库
            LOG.exception("%s 步骤异常", library)
            result = StepResult(
                library,
                status="failed",
                dates=ctx.dates.get(library) or [],
                detail={"error": f"{type(exc).__name__}: {exc}"},
            )
        coverage = _coverage(ctx, library)
        result.detail["coverage"] = coverage
        if result.status == "ok" and not coverage.get("ok", True):
            result.status = "partial"  # 写了但覆盖不达标 → 同样要告警
        results.append(result)
        runs = [result.detail.get("run") or {}]
        if isinstance(result.detail.get("passes"), dict):
            runs = [
                (pass_detail or {}).get("run") or {}
                for pass_detail in result.detail["passes"].values()
            ]
        peak_rss = max(
            (int(run.get("child_peak_rss_mb") or 0) for run in runs), default=0
        )
        coverage_text = (
            "跳过"
            if coverage.get("dry_run") or coverage.get("skipped")
            else ("ok" if coverage.get("ok") else "有缺口")
        )
        LOG.info(
            "%s → %s（%s）%.1fs 子进程峰值RSS=%sMB 覆盖=%s",
            library,
            result.status,
            result.dates,
            result.elapsed_s,
            peak_rss or "?",
            coverage_text,
        )

    status_path = _write_status(ctx, results, started)
    LOG.info("状态文件：%s", status_path)

    # D2：只对 status=ok 的库记录代次并切 active；失败/部分完成不动指针。
    if not ctx.dry_run:
        try:
            from backend.shared.factor_generation import (
                read_active,
                record_generation,
            )

            for result in results:
                if result.status != "ok":
                    continue
                lib_root = ops.library_root(ctx.quantdb, result.library)
                partitions = ops.partition_dates(lib_root)
                latest = partitions[-1] if partitions else ""
                fingerprint = f"{latest}|{len(partitions)}"
                active = read_active(lib_root)
                if active and active.get("source_fingerprint") == fingerprint:
                    continue
                record_generation(
                    lib_root,
                    source_fingerprint=fingerprint,
                    partitions=len(partitions),
                )
                LOG.info(
                    "代次已切换：%s active=%s partitions=%d",
                    result.library,
                    (read_active(lib_root) or {}).get("generation_id"),
                    len(partitions),
                )
        except Exception as exc:  # noqa: BLE001 - 代次记录失败不影响物化
            LOG.warning("generation 记录失败（不影响物化）: %s", exc)
    failed = [step.library for step in results if step.status == "failed"]
    partial = [step.library for step in results if step.status == "partial"]
    if failed:
        LOG.error("失败库：%s", failed)
    if partial:
        LOG.error("部分完成库：%s", partial)
    if ctx.dry_run:
        return 0
    return 0 if not failed and not partial else 1


if __name__ == "__main__":
    sys.exit(main())
