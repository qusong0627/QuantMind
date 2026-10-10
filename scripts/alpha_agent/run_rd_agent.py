"""RD-Agent 多市场因子挖掘 runner 脚本

由 launcher 作为子进程调用。使用 RDLoopWrapper 运行 RD-Agent 因子挖掘，
提取发现的因子并持久化到 QuantMind 数据库。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("rd_agent_run")


async def persist_factors(factors: list[dict], task_id: str, user_id: str, market: str, universe: str = "csi300") -> int:
    """持久化因子到数据库"""
    if not factors:
        return 0

    import hashlib
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
        RDAgentFactorPersistence,
    )

    persistence = RDAgentFactorPersistence()
    await persistence.ensure_tables()

    count = 0
    for f in factors:
        try:
            raw_id = f"{task_id}:{f['name']}"
            factor_id = hashlib.md5(raw_id.encode()).hexdigest()

            metadata = {
                "source": "rd_agent",
                "market": market,
                "task_id": task_id,
                "category": f.get("category", market),
            }
            if f.get("formulation"):
                metadata["formulation"] = f["formulation"]
            if f.get("description"):
                metadata["description"] = f["description"]
            if f.get("feedback"):
                metadata["feedback"] = f["feedback"][:2000]

            await persistence.save_factor(
                factor_id=factor_id,
                factor_name=f["name"],
                factor_code=f.get("code", ""),
                user_id=user_id,
                metadata=metadata,
                market=market,
                universe=universe,
                factor_formulation=f.get("formulation", ""),
            )
            count += 1
            logger.info("Persisted factor: %s (market=%s, universe=%s, id=%s)", f["name"], market, universe, factor_id)
        except Exception as e:
            logger.warning("Failed to persist factor %s: %s", f["name"], e)

    return count


def _dedupe_against_corpus(
    corpus: list[dict], factors: list[dict], task_id: str
) -> set[str]:
    """落库前查重（廉价层）：返回应跳过的 factor_id 集合。

    与存量（同市场）+ 同批候选比对，口径 = 名称归一 / LaTeX 公式归一 /
    代码指纹（``backend.shared.factor_identity``）。本任务自己的存量行不
    参与比对——重跑同一任务应允许更新既有因子，而不是被自己挡住。
    值级（相关）复核在物化时另做。
    """
    import hashlib

    from backend.shared.factor_identity import partition_duplicates

    def cand_id(name: str) -> str:
        return hashlib.md5(f"{task_id}:{name}".encode()).hexdigest()

    candidates = [
        {
            "factor_id": cand_id(f["name"]),
            "factor_name": f["name"],
            "factor_formulation": f.get("formulation", ""),
            "factor_code": f.get("code", ""),
        }
        for f in factors
    ]
    own_ids = {str(c["factor_id"]) for c in candidates}
    external = [r for r in corpus if str(r.get("factor_id") or "") not in own_ids]
    _, duplicates = partition_duplicates(candidates, external)
    skipped: set[str] = set()
    for cand, verdict in duplicates:
        skipped.add(str(cand["factor_id"]))
        logger.warning(
            "因子与存量重复（%s，命中 %s / %s），跳过落库：%s",
            verdict.reason,
            verdict.matched_name or "?",
            str(verdict.matched_id or "?")[:8],
            cand["factor_name"],
        )
    if skipped:
        logger.info(
            "落库前查重：跳过 %d 个重复因子（候选 %d 个）", len(skipped), len(candidates)
        )
    return skipped


def _maybe_spawn_materialize(args, log_dir: str, data_path: str, persisted: int) -> None:
    """落库后自动物化：把本任务因子写入 rd_mined 库（训练直读链路）。

    ``RD_AGENT_AUTO_MATERIALIZE=false`` 可关闭；v1 只支持 a_share 市场。
    失败只告警——物化链可事后用同一脚本手工补跑（清单支持断点续跑）。
    """
    if persisted <= 0 or args.market != "a_share":
        return
    flag = os.getenv("RD_AGENT_AUTO_MATERIALIZE", "true").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        logger.info("自动物化已关闭（RD_AGENT_AUTO_MATERIALIZE=%s）", flag)
        return
    script = _project_root / "backend" / "scripts" / "rd_mined_materialize.py"
    if not script.is_file():
        logger.warning("物化器脚本不存在，跳过自动物化：%s", script)
        return
    cmd = [
        sys.executable,
        str(script),
        "--task-id",
        str(args.task_id),
        "--market",
        str(args.market),
        "--register",
    ]
    if data_path and Path(data_path).exists():
        cmd += ["--h5-path", str(data_path)]
    log_file = Path(log_dir) / "materialize.log"
    try:
        with open(log_file, "ab") as handle:
            subprocess.Popen(
                cmd,
                cwd=str(_project_root),
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        logger.info("自动物化已启动（后台）：log=%s", log_file)
    except Exception as exc:  # noqa: BLE001 - 自动链路失败不阻断挖掘结果
        logger.warning("自动物化启动失败：%s", exc)


def _maybe_spawn_validate(args, log_dir: str, data_path: str, persisted: int) -> None:
    """落库后自动验证（T-MV-09 六节报告）——后台子进程，不阻塞收尾。

    ``RD_AGENT_AUTO_VALIDATE=false`` 可关闭。全市场可触发：T-FB 阶段引擎
    五市场通吃，h5 阶段（衰减/PIT）非 a_share 自动降级，报告如实标 degraded。
    失败只告警——可事后用同一脚本手工补跑（--task-id/--factor-ids）。

    与物化器可并行跑（各自落不同表）；验证脚本自带全局 flock 串行化。
    """
    if persisted <= 0:
        return
    flag = os.getenv("RD_AGENT_AUTO_VALIDATE", "true").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        logger.info("自动验证已关闭（RD_AGENT_AUTO_VALIDATE=%s）", flag)
        return
    script = _project_root / "backend" / "scripts" / "mining_factor_validate.py"
    if not script.is_file():
        logger.warning("验证器脚本不存在，跳过自动验证：%s", script)
        return
    cmd = [
        sys.executable,
        str(script),
        "--task-id",
        str(args.task_id),
        "--market",
        str(args.market),
    ]
    if data_path and Path(data_path).exists():
        cmd += ["--h5-path", str(data_path)]
    log_file = Path(log_dir) / "validate.log"
    try:
        with open(log_file, "ab") as handle:
            subprocess.Popen(
                cmd,
                cwd=str(_project_root),
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        logger.info("自动验证已启动（后台）：log=%s", log_file)
    except Exception as exc:  # noqa: BLE001 - 自动链路失败不阻断挖掘结果
        logger.warning("自动验证启动失败：%s", exc)


def _near_one_year_window() -> tuple[str, str]:
    """近一年回测窗口（end=数据最新交易日，start=end 往前一年）。

    优先从 QuantDB 交易日历取最新交易日，失败则退回今天。
    """
    import pandas as pd

    end_ts = None
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        cal = QuantDBDataHub.get_instance().fetch_calendar()
        if cal is not None and not cal.empty:
            for col in ("trade_date", "date", "time", "cal_date", "TradingDate"):
                if col in cal.columns:
                    end_ts = pd.to_datetime(cal[col]).max()
                    break
    except Exception:
        end_ts = None
    if end_ts is None or pd.isna(end_ts):
        end_ts = pd.Timestamp.today().normalize()
    return (end_ts - pd.DateOffset(years=1)).strftime("%Y-%m-%d"), end_ts.strftime("%Y-%m-%d")


def compute_factor_ic(
    factor_code: str,
    data_path: str,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """执行因子代码并计算 IC 指标

    Args:
        factor_code: 因子源码（含 calculate_*）
        data_path: daily_pv.h5 路径
        start/end: 可选回测窗口（YYYY-MM-DD）；给定时只在该窗口内计算 IC

    Returns dict with: ic, rank_ic, icir, rank_icir (or empty dict on failure)
    """
    import tempfile
    import subprocess
    import sys

    if not factor_code or not Path(data_path).exists():
        return {}

    # Create a temporary script that executes the factor code and computes IC
    script = f"""
import pandas as pd
import numpy as np
import sys, os, tempfile, traceback

os.chdir(tempfile.gettempdir())

# 清理上一因子残留的结果文件，避免误读陈旧 result.h5
for _f in list(os.listdir('.')):
    if _f.endswith('.h5') and _f != 'daily_pv.h5':
        try:
            os.remove(_f)
        except OSError:
            pass

try:
    # Execute factor code（用 repr 内联，避免多行代码缩进破坏 try 结构）
    exec({factor_code!r}, globals())

    # 若因子代码未自执行（无 __main__ 守卫）或未产出 result.h5，则显式调用 calculate_*()
    _has_result = any(f.endswith('.h5') and 'result' in f.lower() for f in os.listdir('.'))
    if not _has_result:
        _fns = [v for k, v in globals().items() if k.startswith('calculate_') and callable(v)]
        if _fns:
            _res = _fns[0]()
            if _res is not None and hasattr(_res, 'to_hdf'):
                _res.to_hdf('result.h5', key='data', mode='w')

    # Find the result H5 file
    result_files = [f for f in os.listdir('.') if f.endswith('.h5') and 'result' in f.lower()]
    if not result_files:
        # Try to find any .h5 file that's not the input
        result_files = [f for f in os.listdir('.') if f.endswith('.h5') and f != 'daily_pv.h5']

    if not result_files:
        print("NO_RESULT_FILE")
        sys.exit(1)

    factor_df = pd.read_hdf(result_files[0])
    if factor_df.empty:
        print("EMPTY_FACTOR")
        sys.exit(1)

    # Load price data for returns
    price_df = pd.read_hdf("{data_path}")
    # 可选：切片到回测窗口（end=最新交易日，start=end-N年）
    _start = {start!r}
    _end = {end!r}
    if _start or _end:
        _di = price_df.index.get_level_values(0)
        if _start:
            price_df = price_df[_di >= pd.Timestamp(_start)]
            _di = price_df.index.get_level_values(0)
        if _end:
            price_df = price_df[_di <= pd.Timestamp(_end)]
        if price_df.empty:
            print("EMPTY_WINDOW")
            sys.exit(1)
    if 'close' in price_df.columns.get_level_values(0):
        close = price_df['close']
    elif '$close' in price_df.columns.get_level_values(0):
        close = price_df['$close']
    else:
        close = price_df.iloc[:, 0]

    # Compute forward returns
    returns = close.groupby(level=1).pct_change().shift(-1)

    # Align factor and returns
    # 因子结果为 MultiIndex(datetime, instrument) + 单列：直接取首列，
    # 不能用 stack()（会多出一层列名索引）。
    if isinstance(factor_df, pd.DataFrame):
        factor_values = factor_df.iloc[:, 0]
    else:
        factor_values = factor_df
    if factor_values.index.nlevels != 2:
        print("BAD_FACTOR_INDEX")
        sys.exit(1)
    factor_values.index.names = ['datetime', 'instrument']
    returns.index.names = ['datetime', 'instrument']

    # 统一 instrument 大小写：因子代码可能假设大写（SH600036），而 daily_pv.h5 用小写
    # （sh600036），不统一会导致对齐交集为空、IC 无法计算。
    def _upper_instrument(_s):
        _names = list(_s.index.names)
        if 'instrument' in _names:
            _lvl = _names.index('instrument')
            _lvs = _s.index.levels[_lvl]
            if _lvs.dtype == object:
                _s.index = _s.index.set_levels(_lvs.str.upper(), level=_lvl)
        return _s

    factor_values = _upper_instrument(factor_values)
    returns = _upper_instrument(returns)

    common_idx = factor_values.index.intersection(returns.index)
    if len(common_idx) < 100:
        print("INSUFFICIENT_DATA")
        sys.exit(1)

    f = factor_values.loc[common_idx]
    r = returns.loc[common_idx]

    # Remove NaN and inf
    mask = np.isfinite(f) & np.isfinite(r)
    f = f[mask]
    r = r[mask]

    if len(f) < 100:
        print("INSUFFICIENT_CLEAN_DATA")
        sys.exit(1)

    # Compute IC：向量化日度 Spearman（秩的 Pearson），避免逐日 spearmanr 过慢
    df_ic = pd.DataFrame({{"f": f.values, "r": r.values}})
    df_ic["date"] = f.index.get_level_values(0)
    df_ic = df_ic[np.isfinite(df_ic["f"]) & np.isfinite(df_ic["r"])]
    if len(df_ic) < 100:
        print("INSUFFICIENT_CLEAN_DATA")
        sys.exit(1)

    g = df_ic.groupby("date")
    df_ic["fr"] = g["f"].rank(method="average")
    df_ic["rr"] = g["r"].rank(method="average")
    g = df_ic.groupby("date")
    means = g[["fr", "rr"]].transform("mean")
    df_ic["fc"] = df_ic["fr"] - means["fr"]
    df_ic["rc"] = df_ic["rr"] - means["rr"]
    df_ic["fcr"] = df_ic["fc"] * df_ic["rc"]
    df_ic["fc2"] = df_ic["fc"] ** 2
    df_ic["rc2"] = df_ic["rc"] ** 2
    sums = g[["fcr", "fc2", "rc2"]].transform("sum")
    counts = g["fcr"].transform("count")
    n = (counts - 1).clip(lower=1)
    cov = sums["fcr"] / n
    var_f = sums["fc2"] / n
    var_r = sums["rc2"] / n
    denom = np.sqrt(var_f * var_r)
    df_ic["corr"] = np.where(
        denom > 1e-12, cov / np.where(denom > 1e-12, denom, 1.0), np.nan
    )
    ic_by_day = g["corr"].first().dropna()
    ic_by_day = ic_by_day[np.isfinite(ic_by_day)]

    if len(ic_by_day) == 0:
        print("NO_IC_VALUES")
        sys.exit(1)

    ic = float(ic_by_day.mean())
    rank_ic = float(ic_by_day.median())
    std = float(ic_by_day.std(ddof=1)) if len(ic_by_day) > 1 else 0.0
    icir = ic / (std + 1e-8)
    rank_icir = rank_ic / (std + 1e-8)

    print(f"IC={{ic:.4f}}")
    print(f"RANK_IC={{rank_ic:.4f}}")
    print(f"ICIR={{icir:.4f}}")
    print(f"RANK_ICIR={{rank_icir:.4f}}")
    print(f"OBSERVATIONS={{len(f)}}")
    print(f"IC_DATES={{len(ic_by_day)}}")

except Exception as e:
    print(f"ERROR: {{e}}")
    traceback.print_exc()
    sys.exit(1)
"""

    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, dir='/tmp') as f:
            f.write(script)
            script_path = f.name

        # Copy data file to /tmp for the script (factor code expects 'daily_pv.h5')
        import shutil
        tmp_data = '/tmp/daily_pv.h5'
        if not os.path.exists(tmp_data):
            shutil.copy2(data_path, tmp_data)
        elif os.path.getmtime(data_path) > os.path.getmtime(tmp_data):
            shutil.copy2(data_path, tmp_data)

        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True, text=True, timeout=120, cwd='/tmp'
        )

        os.unlink(script_path)

        output = result.stdout + result.stderr
        metrics = {}
        for line in output.strip().split('\n'):
            if line.startswith('IC='):
                metrics['ic'] = float(line.split('=')[1])
            elif line.startswith('RANK_IC='):
                metrics['rank_ic'] = float(line.split('=')[1])
            elif line.startswith('ICIR='):
                metrics['icir'] = float(line.split('=')[1])
            elif line.startswith('RANK_ICIR='):
                metrics['rank_icir'] = float(line.split('=')[1])

        return metrics

    except Exception as e:
        logger.warning("IC computation failed: %s", e)
        return {}


def main():
    parser = argparse.ArgumentParser(description="QuantMind RD-Agent Multi-Market Runner")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--market", default="a_share", help="Market: a_share, crypto, hong_kong, us_stock, futures")
    parser.add_argument("--universe", default="csi300", help="Stock universe: csi300, csi500, csi1000, sse50, gem, star, csi800, all_a")
    parser.add_argument("--loop-n", type=int, default=5)
    parser.add_argument("--log-dir", default="")
    parser.add_argument("--direction", default="")
    args = parser.parse_args()

    log_dir = args.log_dir or os.getenv("LOG_TRACE_PATH", "/tmp/rd_agent_logs")
    log_dir = str(Path(log_dir).resolve())
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("RD-Agent Runner starting")
    logger.info("  Task ID: %s", args.task_id)
    logger.info("  User ID: %s", args.user_id)
    logger.info("  Market:  %s", args.market)
    logger.info("  Universe: %s", args.universe)
    logger.info("  Loops:   %d", args.loop_n)
    logger.info("  Log dir: %s", log_dir)
    logger.info("  Direction: %s", args.direction or "(default)")
    logger.info("=" * 60)

    try:
        from backend.services.engine.alpha_agent.hw_lock import (
            HardwareLockError,
            assert_factor_mining_hardware,
        )

        assert_factor_mining_hardware()
    except HardwareLockError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc

    try:
        os.environ["LOG_TRACE_PATH"] = log_dir

        from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper

        wrapper = RDLoopWrapper(market=args.market)
        logger.info("[%s] Market adapter: %s (%s)", args.market, wrapper.market_name, args.market)

        t0 = time.time()
        result = asyncio.run(wrapper.run(
            loop_n=args.loop_n,
            task_log_dir=log_dir,
            direction=args.direction,
        ))
        elapsed = time.time() - t0

        factors = result.get("factors", [])
        error = result.get("error")
        if error:
            logger.error("Factor mining failed: %s", error)
            sys.exit(1)
        logger.info("Factor mining completed in %.1fs, found %d factors", elapsed, len(factors))

        for i, f in enumerate(factors, 1):
            logger.info("  Factor %d: %s (expr: %s)", i, f["name"],
                         f.get("formulation", "")[:80] or "N/A")

        # Persist 因子 + 回填 IC —— 必须在同一事件循环内完成，
        # 否则跨 asyncio.run 复用 DB engine 会报 "attached to a different loop"。
        # 数据路径优先本次任务实际生成的 daily_pv.h5（A股由 _ensure_data_file 生成）。
        market_data_paths = {
            "crypto": "/app/db/crypto_data/5min_pv.h5",
            "hong_kong": "/app/db/hk_data/daily_pv.h5",
            "us_stock": "/app/db/us_data/daily_pv.h5",
            "futures": "/app/db/futures_data/daily_pv.h5",
        }
        local_h5 = Path(log_dir) / "git_ignore_folder" / "factor_implementation_source_data" / "daily_pv.h5"
        if local_h5.exists():
            data_path = str(local_h5)
        else:
            data_path = market_data_paths.get(
                args.market,
                "/app/alphaagent/scenarios/qlib/experiment/factor_data_template/daily_pv_all.h5",
            )
        logger.info("IC data path resolved: %s (exists=%s)", data_path, Path(data_path).exists())

        # 回测窗口默认近一年（仅 A 股；其他市场数据日历不同，保持全样本）
        if args.market == "a_share":
            ic_start, ic_end = _near_one_year_window()
            logger.info("IC window (recent 1y): %s ~ %s", ic_start, ic_end)
        else:
            ic_start, ic_end = None, None

        import hashlib

        from backend.services.engine.qlib_app.services.rd_agent_persistence import (
            RDAgentFactorPersistence,
        )

        async def _persist_and_metrics() -> int:
            p = RDAgentFactorPersistence()
            await p.ensure_tables()
            corpus: list[dict] = []
            corpus_limit = 5000
            try:
                corpus = await p.list_factors(market=args.market, limit=corpus_limit)
            except Exception as exc:
                logger.warning(
                    "查重语料读取失败（跳过落库前查重，重复因子可能入库）：%s", exc
                )
            if len(corpus) >= corpus_limit:
                # list_factors 按 created_at DESC 截断：被丢的是**最早**的因子，
                # 恰恰是新因子最可能撞的那批——必须显式可见，别让廉价层静默变薄
                logger.warning(
                    "查重语料达上限 %d 条：更早的存量因子未参与本批查重", corpus_limit
                )
            dup_ids = _dedupe_against_corpus(corpus, factors, args.task_id)
            saved = 0
            for f in factors:
                try:
                    fid = hashlib.md5(f"{args.task_id}:{f['name']}".encode()).hexdigest()
                    if fid in dup_ids:
                        continue
                    metadata: dict = {
                        "source": "rd_agent",
                        "market": args.market,
                        "task_id": args.task_id,
                        "category": f.get("category", args.market),
                    }
                    if f.get("formulation"):
                        metadata["formulation"] = f["formulation"]
                    if f.get("description"):
                        metadata["description"] = f["description"]
                    if f.get("feedback"):
                        metadata["feedback"] = f["feedback"][:2000]

                    status = "pending"
                    ic_value = None
                    rank_ic = None
                    if Path(data_path).exists() and f.get("code"):
                        metrics = await asyncio.to_thread(
                            compute_factor_ic, f["code"], data_path, ic_start, ic_end
                        )
                        if metrics:
                            status = "completed"
                            ic_value = metrics.get("ic")
                            rank_ic = metrics.get("rank_ic")
                            metadata.update({
                                "icir": metrics.get("icir", 0),
                                "rank_icir": metrics.get("rank_icir", 0),
                                "data_source": "task_h5",
                            })
                            logger.info(
                                "  %s: IC=%.4f, RankIC=%.4f, ICIR=%.4f",
                                f["name"], ic_value or 0, rank_ic or 0,
                                metrics.get("icir", 0),
                            )
                        else:
                            logger.info("  %s: IC computation empty", f["name"])

                    await p.save_factor(
                        factor_id=fid,
                        factor_name=f["name"],
                        factor_code=f.get("code", ""),
                        user_id=args.user_id,
                        metadata=metadata,
                        market=args.market,
                        universe=args.universe,
                        factor_formulation=f.get("formulation", ""),
                    )
                    await p.update_factor_metrics(
                        factor_id=fid,
                        status=status,
                        ic_value=ic_value,
                        rank_ic=rank_ic,
                        metadata=metadata,
                    )
                    saved += 1
                except Exception as e:
                    logger.warning("Failed to persist factor %s: %s", f["name"], e)
            return saved

        count = asyncio.run(_persist_and_metrics())
        logger.info("Persisted %d factors to database", count)

        # Write result JSON for launcher to read
        result_file = Path(log_dir) / "result.json"
        result_file.write_text(json.dumps({
            "task_id": args.task_id,
            "market": args.market,
            "total_factors": len(factors),
            "persisted_factors": count,
            "elapsed_seconds": elapsed,
        }, indent=2))

        logger.info("=" * 60)
        logger.info("RD-Agent task complete! task_id=%s, market=%s", args.task_id, args.market)
        logger.info("  Found: %d factors, Persisted: %d", len(factors), count)
        logger.info("=" * 60)

        # 落库后自动物化（rd_mined 库，训练直读链路）——后台子进程，不阻塞收尾
        _maybe_spawn_materialize(args, log_dir, data_path, count)

        # 落库后自动验证（T-MV-09 六节报告）——后台子进程，不阻塞收尾
        _maybe_spawn_validate(args, log_dir, data_path, count)

    except Exception as e:
        logger.exception("RD-Agent runner failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
