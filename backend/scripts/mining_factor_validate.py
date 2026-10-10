#!/usr/bin/env python3
"""T-MV-09 机构级验证流水线：挖掘因子 → 六节验证报告（挖掘收尾自动挂接）。

链路（``scripts/alpha_agent/run_rd_agent.py`` 在落库/物化后自动触发本脚本，
也可手工补跑）：

1. 从 PG ``rd_agent_factors`` 取因子行（``--factor-ids`` / ``--task-id``）；
2. **T-FB 阶段**：``factor_backtest.router.evaluate_and_record``（求值 + 台账
   收口唯一实现）跑全引擎回测 → IC/ICIR/序列/窗口，run_id 链回
   ``rd_agent_factor_backtests``（T-MV-10 排名消费）；
3. **h5 阶段**（a_share + 函数式因子）：用挖掘同源富化 h5 执行因子代码得全
   历史因子值——
   - 衰减：多视界（1/2/5/10 日）IC 曲线 + 半衰期（``factor_report.metrics``
     纯函数单源）；
   - PIT：截断不变性探针——同一抽样（120 只，排序前 N）生成「全量 / 截断到
     探针日」两份 h5，分别执行因子，探针日截面逐点比对（容差 1e-9）；
4. 正交增量节只读透传 ``metadata_json.orthogonality``（T-MV-08 池刷新留痕；
   因子未入池刷新则为空并如实降级）；
5. ``mining_plugins.validation.build_validation_report`` 装配六节 → 落
   ``rd_agent_factor_validations`` 专表（按 (factor_id, market) 保最新结论）。

纪律：

- **复用不复制**：指标体系全部来自 T-FB 引擎与 ``factor_report.metrics`` 纯
  函数层；DSR 修正不属本任务，报告不产出 DSR 结论。
- **诚实降级**：任何一节缺数据 → ``unavailable`` + reason，绝不填 0；报告顶
  层 ``degraded``（缺节）≠ ``failed``（未预期异常，状态列如实区分）；
  衰减节「尝试过但失败」（``h5_stage_error`` + 异常正文）与「从未生成」
  （``no_h5_stats``）分枚举——2026-10-10 实跑取证：真实 bug 曾被后者掩盖。
- **单写者**：全局 flock 串行化——自动挂接可能对同一任务触发多次，后来的
  排队等前单跑完（阻塞锁），不并发写同一张报告表。
- **防假通过**：PIT 抽样 h5 直建（``use_cache=False``），失败即弃——旧的全
  量共享缓存冒充「截断数据」会让探针对比退化成「全量 vs 全量」假通过。

典型用法::

    # 挖掘收尾自动挂接（run_rd_agent 内部调用）
    python backend/scripts/mining_factor_validate.py --task-id <id>

    # 手工补跑单个因子 / 预演
    python backend/scripts/mining_factor_validate.py --factor-ids f-123
    python backend/scripts/mining_factor_validate.py --factor-ids f-123 --dry-run

宿主机无 fastapi/PyTables 时不可运行，真实链路在容器内跑（同物化器）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pandas as pd

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.scripts.rd_mined_materialize import (  # noqa: E402
    _compute_factor_values,
    _now_iso,
    _quantdb_dir,
    _resolve_h5,
)
from backend.services.engine.mining_plugins import validation  # noqa: E402
from backend.services.engine.mining_plugins import validation_store  # noqa: E402

logger = logging.getLogger("mining_factor_validate")

DEFAULT_MARKET = "a_share"

#: 衰减视界（交易日）。半衰期插值语义由 ``metrics.half_life_days`` 单源决定。
DECAY_HORIZONS: tuple[int, ...] = (1, 2, 5, 10)

#: 无 T-FB 窗口时的衰减回看窗口（因子值自身的最后 N 个交易日）。
DECAY_WINDOW_DAYS = 500

#: PIT 抽样标的数（全量/截断两份 h5 必须同一批；120 > PIT_MIN_COMMON=20）。
PIT_SAMPLE_SYMBOLS = 120

#: 因子代码子进程超时（与物化器同款）。
_EXEC_TIMEOUT_S = 900

#: 验证锁文件名（临时目录下；全局串行化的唯一凭据）。
_LOCK_NAME = "_mining_factor_validate.lock"


# ── 运行锁 ────────────────────────────────────────────────────────────


def _lock_path() -> Path:
    return Path(tempfile.gettempdir()) / _LOCK_NAME


def _acquire_run_lock() -> Any:
    """阻塞独占锁：并发触发串行排队（自动挂接可能对同一任务触发多次）。

    与物化器（非阻塞、拿不到即退）不同——验证是挖掘收尾的必达步骤，宁可
    排队等前一单跑完；锁随 fd 关闭 / 进程退出自动释放。
    """
    import fcntl  # POSIX-only：容器/Ubuntu 运行

    handle = open(_lock_path(), "w", encoding="utf-8")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


# ── 因子行查询 ────────────────────────────────────────────────────────


async def _query_factors(
    *, factor_ids: list[str], task_id: str | None, market: str
) -> list[dict[str, Any]]:
    """取因子行（含 T-MV-08 正交留痕）——factor_ids / task_id 至少其一。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    clauses: list[str] = ["COALESCE(market, :default_market) = :market"]
    params: dict[str, Any] = {"market": market, "default_market": DEFAULT_MARKET}
    if factor_ids:
        clauses.append("factor_id = ANY(:ids)")
        params["ids"] = list(factor_ids)
    if task_id:
        clauses.append("metadata_json->>'task_id' = :task_id")
        params["task_id"] = str(task_id)
    sql = (
        "SELECT factor_id, factor_name, factor_code, "
        "COALESCE(market, :default_market) AS market, "
        "COALESCE(universe, '') AS universe, user_id, status, "
        "metadata_json->'orthogonality' AS orthogonality "
        "FROM rd_agent_factors WHERE " + " AND ".join(clauses) + " ORDER BY created_at"
    )
    async with get_session(read_only=True) as session:
        rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(row) for row in rows]


def _as_dict(value: Any) -> dict | None:
    """JSONB 读取归一（asyncpg 可能给 str 或已解码 dict）。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


# ── h5 数据面 ─────────────────────────────────────────────────────────


def _generate_probe_h5(quantdb_dir: Path, out_path: Path) -> bool:
    """探测抽样 h5：直建（``use_cache=False``）——失败不降级共享缓存。

    旧全量缓存冒充「抽样数据」会把 PIT 对比退化成「全量 vs 全量」假通过，
    所以这里既不复用缓存命中也不接受 stale 回退，失败即 False。
    """
    from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper

    wrapper = RDLoopWrapper(market=DEFAULT_MARKET)
    return bool(
        wrapper._generate_h5_from_parquet(  # noqa: SLF001 - 复用既有生成器
            str(quantdb_dir),
            str(out_path),
            symbols_limit=PIT_SAMPLE_SYMBOLS,
            use_cache=False,
        )
    )


def _truncate_h5_rows(src_h5: Path, dst_h5: Path, probe_date: str) -> None:
    """行过滤截断 h5（date ≤ probe），临时文件 + 原子替换。

    与生成式截断（``end_date=probe`` 重跑生成器）**逐行同源**：QuantDB 读取
    本就是 dt 分区行过滤 + 落盘已复权列（``quantdb_hub._read_daily_kline_from_files``），
    无窗口内重定基。行过滤免去对全量 parquet 的二次扫描（分钟级 → 秒级），
    且保证「截断面」行 ≤ 探针日与「全面」同源逐位一致——探针对比只剩因子
    代码这一个变量。
    """
    frame = pd.read_hdf(src_h5, key="data")
    mask = frame.index.get_level_values(0) <= pd.Timestamp(probe_date)
    truncated = frame.loc[mask]
    tmp_path = str(dst_h5) + ".tmp"
    truncated.to_hdf(tmp_path, key="data", mode="w")
    os.replace(tmp_path, dst_h5)


# ── 衰减节 ────────────────────────────────────────────────────────────


def _decay_window(tfb: dict | None, values: pd.Series) -> tuple[str, str] | None:
    """衰减窗口：T-FB 实跑窗口；无则回退因子值最后 ``DECAY_WINDOW_DAYS`` 天。"""
    win = (tfb or {}).get("window") or {}
    start, end = win.get("start"), win.get("end")
    if start and end:
        return str(start), str(end)
    dates = sorted({str(d) for d in values.index.get_level_values(0)})
    if not dates:
        return None
    start = dates[-DECAY_WINDOW_DAYS] if len(dates) > DECAY_WINDOW_DAYS else dates[0]
    return start, dates[-1]


def _to_datetime_index(values: pd.Series) -> pd.Series:
    """物化口径（``_to_canonical`` 的 'trade_date' 字符串）→ 真 Timestamp 层 0。

    引擎对齐链（``_detect_datetime_level``）按**值**判层且拒绝猜字符串日期
    （2026-10-10 集成实跑：不转换则衰减节对所有因子恒降级）。只动标签不动
    值；层 0 已是 datetime64 时原样返回（幂等）。
    """
    idx = values.index
    if not isinstance(idx, pd.MultiIndex) or idx.nlevels != 2:
        return values
    if pd.api.types.is_datetime64_any_dtype(idx.get_level_values(0)):
        return values
    out = values.copy()
    out.index = out.index.set_levels(pd.to_datetime(out.index.levels[0]), level=0)
    return out


def _decay_stats(
    values: pd.Series, close: pd.Series, horizons: tuple[int, ...]
) -> dict[int, dict]:
    """多视界 IC 统计（对齐口径与 T-FB h=1 逐字同源：同规整 + 同分组前移）。"""
    from backend.services.engine.factor_backtest.ic import daily_ic_stats
    from backend.services.engine.routers.alpha_agent import (
        _canonicalize_factor_for_alignment,
        _forward_return,
        _upper_instrument_level,
    )

    f_all = _canonicalize_factor_for_alignment(_to_datetime_index(values))
    stats: dict[int, dict] = {}
    for h in horizons:
        r_h = _upper_instrument_level(_forward_return(close, h))
        common = f_all.index.intersection(r_h.index)
        stats[h] = daily_ic_stats(f_all.loc[common], r_h.loc[common])
    return stats


def _decay_stage(values: pd.Series, tfb: dict | None, *, universe: str) -> dict | None:
    """衰减节数据面：视界 1/2/5/10 的 IC 统计（收益=Qlib ``$close`` 同池）。

    只在 a_share 走到这里（h5 阶段门槛），故市场恒为 ``DEFAULT_MARKET``。
    """
    window = _decay_window(tfb, values)
    if window is None:
        return None
    from backend.services.engine.factor_backtest.engine import (
        _ensure_qlib,
        _load_features,
    )
    from backend.services.engine.factor_backtest.profiles import get_market_profile
    from backend.services.engine.routers.alpha_agent import (
        _resolve_instruments_for_universe,
    )

    profile = get_market_profile(DEFAULT_MARKET)
    _ensure_qlib(profile.qlib_market)
    instruments = _resolve_instruments_for_universe(profile.qlib_market, universe)
    close_df = _load_features(instruments, ["$close"], window[0], window[1])
    if close_df is None or close_df.empty or "$close" not in close_df.columns:
        raise RuntimeError(f"Qlib 收盘价装载为空: {window[0]}~{window[1]}")
    stats = _decay_stats(values, close_df["$close"], DECAY_HORIZONS)
    return {
        "stats_by_horizon": stats,
        "window": {"start": window[0], "end": window[1]},
        "universe": universe,
    }


# ── PIT 探针 ──────────────────────────────────────────────────────────


def _pit_stage(row: Mapping[str, Any], *, timeout: int) -> dict | None:
    """截断不变性探针：抽样全量 h5 → 行过滤截断 → 双执行 → 判定。

    探针日 = 抽样执行值日历倒数第 ``PIT_PROBE_OFFSET_DAYS`` 个交易日（留足
    视界余量）；探针日之后仍有数据留在「全面」里——值若依赖它们即露馅。
    说明：真前视因子若在截断后于探针日整列变 NaN（如 ``shift(-k)`` 型），
    判定为 ``unknown/no_probe_section`` → 报告降级而**非通过**（未知 ≠ 通过）。
    """
    quantdb_dir = _quantdb_dir()
    with tempfile.TemporaryDirectory(prefix="mining_pit_") as tmp:
        full_h5 = Path(tmp) / "daily_pv_full.h5"
        if not _generate_probe_h5(quantdb_dir, full_h5):
            logger.warning("探测抽样 h5 生成失败 → PIT 降级（不降级共享缓存）")
            return None
        values_full = _compute_factor_values(row, full_h5, timeout=timeout)
        dates = sorted({str(d) for d in values_full.index.get_level_values(0)})
        if not dates:
            logger.warning("抽样执行无有效值 → PIT 降级")
            return None
        probe_date = dates[max(0, len(dates) - validation.PIT_PROBE_OFFSET_DAYS)]
        trunc_h5 = Path(tmp) / "daily_pv_trunc.h5"
        _truncate_h5_rows(full_h5, trunc_h5, probe_date)
        values_trunc = _compute_factor_values(row, trunc_h5, timeout=timeout)
    return validation.judge_truncation(values_full, values_trunc, probe_date=probe_date)


# ── T-FB 阶段 ─────────────────────────────────────────────────────────


async def _run_tfb_stage(
    row: Mapping[str, Any], *, market: str, kind: str, universe: str
) -> tuple[dict, dict | None]:
    """T-FB 阶段：start_run → evaluate_and_record（台账映射唯一实现）。

    异常不抛：台账已由 ``evaluate_and_record`` 收口 failed/cancelled——本
    阶段返回不可用态供报告降级（ic 节随后走 h5 兜底或 unavailable）。
    """
    from backend.services.engine.factor_backtest import store as fb_store
    from backend.services.engine.factor_backtest.router import evaluate_and_record
    from backend.services.engine.routers.alpha_agent import _format_backtest_error

    factor_id = str(row.get("factor_id") or "")
    run_id = await fb_store.start_run(
        factor_id,
        kind=kind,
        market=market,
        universe=universe,
        params={"source": "mining_validation"},
        factor_name=row.get("factor_name"),
        user_id=row.get("user_id"),
    )
    try:
        res = await evaluate_and_record(
            factor_id,
            {
                "factor_id": factor_id,
                "factor_code": str(row.get("factor_code") or ""),
                "factor_name": row.get("factor_name"),
            },
            run_id,
            market=market,
            universe=universe,
            start=None,
            end=None,
            cost_bps=None,
        )
    except Exception as exc:  # noqa: BLE001 — 台账已收口，报告降级
        logger.warning("[%s] T-FB 求值异常（降级）：%s", factor_id, exc)
        return {
            "run_id": run_id,
            "ok": False,
            "status": "failed",
            "metrics": None,
            "window": {"start": None, "end": None},
            "universe": universe,
            "reason": _format_backtest_error(exc)[-500:],
        }, None
    win = res.get("window") or {}
    return {
        "run_id": run_id,
        "ok": res.get("status") == "ok",
        "status": res.get("status"),
        "metrics": res.get("metrics"),
        "window": {"start": win.get("start"), "end": win.get("end")},
        "universe": res.get("universe"),
        "reason": res.get("message") or res.get("reason"),
    }, res.get("series")


# ── 单因子全链 ────────────────────────────────────────────────────────


def _h5_error_text(exc: BaseException) -> str:
    """h5 数据面异常 → 报告内错误正文：只取首行 message（截 300 字符）。

    traceback 全文留给进程日志（``exc_info``）——报告列要能一眼念出病因，
    不是塞进一坨调用栈。
    """
    return f"{type(exc).__name__}: {exc}"[:300]


async def validate_one(
    row: Mapping[str, Any], *, h5_path: str | None, timeout: int
) -> dict:
    """单因子全链：T-FB 阶段 → h5 阶段（a_share 函数式）→ 装配 → 落表。"""
    from backend.services.engine.factor_backtest.profiles import get_market_profile
    from backend.services.engine.routers.alpha_agent import _detect_factor_kind

    factor_id = str(row.get("factor_id") or "")
    market = str(row.get("market") or DEFAULT_MARKET)
    profile = get_market_profile(market)
    universe = str(row.get("universe") or "").strip() or profile.default_universe
    try:
        kind = _detect_factor_kind(str(row.get("factor_code") or ""))
    except RuntimeError:
        kind = "unknown"

    await validation_store.start_validation(factor_id, market)

    tfb, series = await _run_tfb_stage(row, market=market, kind=kind, universe=universe)

    h5_ic: dict | None = None
    pit: dict | None = None
    h5_error: str | None = None
    if market == DEFAULT_MARKET and kind == "functional":
        values: pd.Series | None = None
        try:
            h5 = _resolve_h5(h5_path)
            values = _compute_factor_values(row, h5, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — 值执行失败：衰减/PIT 双降级
            h5_error = _h5_error_text(exc)
            logger.warning(
                "[%s] h5 因子值执行失败（衰减/PIT 降级）：%s",
                factor_id,
                exc,
                exc_info=True,
            )
        if values is not None and len(values):
            try:
                h5_ic = _decay_stage(values, tfb, universe=universe)
                h5_error = None
            except Exception as exc:  # noqa: BLE001 — 单节失败不拦另一节
                h5_error = _h5_error_text(exc)
                logger.warning(
                    "[%s] 衰减节失败（降级）：%s", factor_id, exc, exc_info=True
                )
            try:
                pit = _pit_stage(row, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] PIT 探针失败（降级）：%s", factor_id, exc)

    report = validation.build_validation_report(
        factor_id=factor_id,
        market=market,
        generated_at=_now_iso(),
        tfb=tfb,
        h5_ic=h5_ic,
        series=series,
        orthogonality=_as_dict(row.get("orthogonality")),
        pit=pit,
        h5_error=h5_error,
    )
    recorded = await validation_store.finish_validation(
        factor_id,
        market,
        status=report["status"],
        report=report,
        tfb_run_id=report.get("tfb_run_id"),
    )
    if not recorded:
        logger.warning("[%s] 验证收口未生效（行不在 running）", factor_id)
    return report


# ── 入口 ──────────────────────────────────────────────────────────────


async def _ensure_tables() -> None:
    """三表就绪：因子主表（物化器同款契约）→ T-FB 台账 → 验证专表。"""
    from backend.services.engine.factor_backtest import store as fb_store
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
        RDAgentFactorPersistence,
    )

    await RDAgentFactorPersistence().ensure_tables()
    await fb_store.ensure_tables()
    await validation_store.ensure_table()


async def _settle_failed(factor_id: str, market: str, exc: BaseException) -> None:
    """未预期异常的兜底收口（failed）；收口本身失败只告警。"""
    from backend.services.engine.routers.alpha_agent import _format_backtest_error

    try:
        await validation_store.finish_validation(
            factor_id,
            market,
            status="failed",
            error=_format_backtest_error(exc)[-1500:],
        )
    except Exception as exc2:  # noqa: BLE001
        logger.error("[%s] failed 收口失败：%s", factor_id, exc2)


async def _run(args: argparse.Namespace) -> int:
    if args.dry_run:
        rows = await _query_factors(
            factor_ids=args.factor_ids, task_id=args.task_id, market=args.market
        )
        if not rows:
            logger.warning("无匹配因子")
        for row in rows:
            logger.info(
                "[dry-run] 将验证 factor_id=%s name=%s market=%s universe=%s",
                row.get("factor_id"),
                row.get("factor_name"),
                row.get("market"),
                row.get("universe") or "(默认池)",
            )
        return 0

    handle = _acquire_run_lock()
    try:
        logger.info("已取得验证锁：%s", _lock_path())
        await _ensure_tables()
        rows = await _query_factors(
            factor_ids=args.factor_ids, task_id=args.task_id, market=args.market
        )
        if not rows:
            logger.warning(
                "无匹配因子（factor_ids=%s task_id=%s market=%s）",
                args.factor_ids,
                args.task_id,
                args.market,
            )
            return 0
        failures = 0
        for row in rows:
            factor_id = str(row.get("factor_id") or "")
            market = str(row.get("market") or DEFAULT_MARKET)
            if not str(row.get("factor_code") or "").strip():
                logger.warning("[%s] 因子代码为空，跳过", factor_id)
                continue
            try:
                report = await validate_one(
                    row, h5_path=args.h5_path, timeout=args.timeout
                )
                logger.info(
                    "验证完成 %s status=%s unavailable=%s tfb_run=%s",
                    factor_id,
                    report.get("status"),
                    [u.get("section") for u in report.get("unavailable") or []],
                    report.get("tfb_run_id"),
                )
            except Exception as exc:  # noqa: BLE001 — 单因子失败不拦整批
                failures += 1
                logger.exception("[%s] 验证失败", factor_id)
                await _settle_failed(factor_id, market, exc)
        return 1 if failures else 0
    finally:
        handle.close()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="T-MV-09 机构级验证流水线（挖掘因子 → 六节验证报告）"
    )
    parser.add_argument("--task-id", help="按挖掘任务 ID 批量（metadata_json.task_id）")
    parser.add_argument("--factor-ids", help="逗号分隔的 factor_id 列表")
    parser.add_argument(
        "--market",
        default=DEFAULT_MARKET,
        help=f"市场键（默认 {DEFAULT_MARKET}；非 a_share 时 h5 阶段自动降级）",
    )
    parser.add_argument(
        "--h5-path", help="挖掘同源 daily_pv.h5（缺省走共享缓存/现场生成）"
    )
    parser.add_argument(
        "--timeout", type=int, default=_EXEC_TIMEOUT_S, help="因子代码执行超时秒数"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只列出将验证的因子，不写库不执行"
    )
    parser.add_argument("--verbose", action="store_true", help="调试日志")
    args = parser.parse_args(argv)
    args.factor_ids = [
        s.strip() for s in str(args.factor_ids or "").split(",") if s.strip()
    ]
    if not args.factor_ids and not args.task_id:
        parser.error("至少提供 --factor-ids 或 --task-id")
    from backend.services.engine.factor_backtest.profiles import get_market_profile

    try:
        get_market_profile(args.market)
    except KeyError:
        parser.error(f"未知市场：{args.market}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
