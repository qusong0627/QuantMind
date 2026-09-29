#!/usr/bin/env python3
"""RD-Agent 挖掘因子物化器：``rd_agent_factors`` → ``rd_mined`` 库（CUSTOM 市场）。

研发链路（挖掘 → 训练直读）的最后一段：

1. 从 PG ``rd_agent_factors`` 取带代码的 a_share 因子（默认只碰 a_share）；
2. 在子进程里执行因子代码（输入 = RD-Agent 同款 ``daily_pv.h5``：
   QuantDB parquet 生成、qfq + ``$factor`` + 富化列），产出全历史因子值；
3. 值级查重：逐日截面秩相关（日均 |ρ|）对照 CUSTOM ``l1_factors`` +
   ``rd_mined`` 既有列，≥ 阈值（默认 0.9）判为重复因子拒入；
4. 经 ``merge_factor_into_source`` 按交易日写回
   ``$QM_QUANTCUSTOM_DATA_DIR/6_ml_datasets/rd_mined/dt=*/data.parquet``
   （列名 = ``feature_column_name``，``rd_`` 前缀 + SQL 安全）；
5. 收尾对齐各分区列集（训练读取层无 union_by_name，列漂移会响亮失败）；
6. ``--register`` 时刷新字段注册并发布/更新训练目录版本（列集未变则跳过）。

清单文件 ``_materialize_manifest.json`` 记录逐因子状态（materialized /
rejected_duplicate / error），支持断点续跑；``--force`` 重做。

典型用法::

    # 单任务（挖掘脚本自动挂接时用）
    python backend/scripts/rd_mined_materialize.py --task-id <id> --register

    # 存量全量回填（后台跑）
    python backend/scripts/rd_mined_materialize.py --register

    # 只对齐分区 schema / 预演
    python backend/scripts/rd_mined_materialize.py --align-only
    python backend/scripts/rd_mined_materialize.py --dry-run

宿主机无 PyTables 时单测自动跳过执行路径；真实链路在容器内跑。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.shared.factor_identity import (  # noqa: E402
    code_fingerprint,
    feature_column_name,
)

logger = logging.getLogger("rd_mined_materialize")

MANIFEST_NAME = "_materialize_manifest.json"
RD_MINED_SOURCE = "rd_mined"
LIB_MARKET = "CUSTOM"
DEFAULT_MARKET = "a_share"

_SAMPLE_DAYS = 60
_RECENT_WINDOW_DAYS = 120
_CORR_WARN = 0.8
_CORR_REJECT = 0.9
_MIN_PAIRS_PER_DAY = 20
_MIN_SAMPLE_DAYS = 5
_EXEC_TIMEOUT_S = 900


# ── 清单与筛选 ─────────────────────────────────────────────────────────


def _lib_root() -> Path:
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        market_data_dir,
    )

    return market_data_dir(LIB_MARKET) / "6_ml_datasets" / RD_MINED_SOURCE


def _quantdb_dir() -> Path:
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        market_data_dir,
    )

    return market_data_dir("CN")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_manifest(lib_root: Path) -> dict[str, Any]:
    path = Path(lib_root) / MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("清单文件损坏，按空清单处理：%s", path)
        return {}
    return data if isinstance(data, dict) else {}


def _save_manifest(lib_root: Path, data: Mapping[str, Any]) -> None:
    lib_root = Path(lib_root)
    lib_root.mkdir(parents=True, exist_ok=True)
    tmp = lib_root / f"{MANIFEST_NAME}.tmp"
    try:
        tmp.write_text(
            json.dumps(dict(data), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(lib_root / MANIFEST_NAME)
    finally:
        tmp.unlink(missing_ok=True)


def _eligible_row(row: Mapping[str, Any]) -> tuple[bool, str]:
    if str(row.get("market") or "") != DEFAULT_MARKET:
        return False, "market_unsupported"
    if not str(row.get("factor_code") or "").strip():
        return False, "no_code"
    return True, "ok"


def _should_materialize(
    row: Mapping[str, Any], manifest: Mapping[str, Any], *, force: bool
) -> tuple[bool, str]:
    factor_id = str(row.get("factor_id") or "")
    entry = manifest.get(factor_id) if factor_id else None
    if not entry:
        return True, "new"
    if force:
        return True, "force"
    status = str(entry.get("status") or "")
    if status in ("materialized", "rejected_duplicate"):
        # 同 factor_id 的代码被改写（同任务重跑会 UPDATE factor_code）时，
        # 旧值/旧判定都随之失效，必须按新代码重算。
        stored = str(entry.get("code_fp") or "")
        current = code_fingerprint(str(row.get("factor_code") or "")) or ""
        if stored and current and stored != current:
            return True, "code_changed"
        return False, (
            "already_materialized" if status == "materialized" else "rejected_duplicate"
        )
    return True, "retry"


def _column_owners(manifest: Mapping[str, Any]) -> dict[str, str]:
    """清单里已落库的「列名 → factor_id」占用表（含被拒/失败条目）。"""
    owners: dict[str, str] = {}
    for factor_id, entry in manifest.items():
        column = str((entry or {}).get("column") or "")
        if column:
            owners.setdefault(column, str(factor_id))
    return owners


def _owned_columns(owners: Mapping[str, str], factor_id: str) -> set[str]:
    """该因子已占用的列名（含失败重试时的旧列）——值级查重要排除自身旧值。"""
    return {col for col, fid in owners.items() if fid == factor_id}


def _disambiguate_column(base: str, factor_id: str, owners: Mapping[str, str]) -> str:
    """列名冲突消歧：同名不同因子（如 momentum_5d 与 Momentum_5D）各占一列。

    冲突时追加 factor_id 前 6 位（不够唯一则加长），截断时优先保后缀，
    保证不同因子永远不落同一列、不静默互相覆盖。factor_id 为空或耗尽的
    后缀退化为数字后缀，循环恒有界。
    """
    if owners.get(base, factor_id) == factor_id:
        return base

    def _claim(suffix: str) -> str | None:
        candidate = f"{base[: 80 - len(suffix)]}{suffix}"
        return candidate if owners.get(candidate, factor_id) == factor_id else None

    if factor_id:
        # 后缀随长度增长（6/8/10/…）；一旦后缀已含整个 factor_id 仍冲突，
        # 后面再增长也不会变，必须换数字后缀，否则死循环。
        for length in range(6, len(factor_id) + 2, 2):
            hit = _claim(f"_{factor_id[:length]}")
            if hit:
                return hit
    for n in range(2, 10000):
        hit = _claim(f"_{n}")
        if hit:
            return hit
    raise RuntimeError(f"列名消歧失败（占用过多）：{base}")  # pragma: no cover


# ── 值级查重（逐日截面秩相关，取日均） ─────────────────────────────────


def _pick_sample_days(
    days: list[str] | set[str] | tuple[str, ...], n: int = _SAMPLE_DAYS
) -> list[str]:
    seq = sorted({str(d) for d in days})
    if n <= 0 or len(seq) <= n:
        return seq
    idx = np.linspace(0, len(seq) - 1, n).round().astype(int)
    return [seq[i] for i in sorted(set(idx.tolist()))]


def _recent_sample_days(
    values: pd.Series, window: int = _RECENT_WINDOW_DAYS
) -> list[str]:
    """近端 window 个交易日里均匀取 ``_SAMPLE_DAYS`` 天（限制对照库读取量）。"""
    days = sorted({str(d) for d in values.index.get_level_values(0)})
    return _pick_sample_days(days[-window:], n=_SAMPLE_DAYS)


def _max_abs_corr(
    values: pd.Series,
    controls: pd.DataFrame,
    *,
    sample_days: set[str] | None = None,
    exclude: set[str] | None = None,
) -> tuple[float, str | None]:
    """新因子与对照列的最大日均 |Spearman ρ|。

    实现 = 逐日截面 rank 后按日求皮尔逊相关，再对日取均值（抓「逐日排序
    相同的换皮因子」，对显著但非单调的高相关列更稳健）；要求每天至少
    ``_MIN_PAIRS_PER_DAY`` 对、至少 ``_MIN_SAMPLE_DAYS`` 个有效日。
    索引须为 ``(trade_date, symbol)`` 两层（``_to_canonical`` 保证）。
    ``exclude`` 列（该因子自己的旧列）不参与比对——否则 --force 重做时
    拿旧值比新值，|ρ|=1 自我拒绝。
    无重叠/样本不足时返回 ``(0.0, None)``（宁可漏报不误报）。
    """
    if values is None or controls is None or len(values) == 0 or controls.empty:
        return 0.0, None
    new_col = values.rename("__new__")
    if isinstance(new_col, pd.DataFrame):
        new_col = new_col.iloc[:, 0]
    frame = pd.concat([new_col, controls], axis=1, join="inner")
    frame = frame[frame["__new__"].notna()]
    if exclude:
        drop = [c for c in exclude if c in frame.columns]
        if drop:
            frame = frame.drop(columns=drop)
    if sample_days is not None:
        frame = frame[frame.index.get_level_values(0).isin(sample_days)]
    if frame.empty:
        return 0.0, None
    ranks = frame.groupby(level=0).rank()
    new_rank = ranks["__new__"]
    other = ranks.drop(columns="__new__")
    if other.shape[1] == 0:
        return 0.0, None
    new_rank_c = new_rank - new_rank.groupby(level=0).transform("mean")
    other_c = other - other.groupby(level=0).transform("mean")
    pair_ok = other.notna()
    cov = other_c.mul(new_rank_c, axis=0).where(pair_ok).groupby(level=0).sum()
    var_other = (other_c**2).where(pair_ok).groupby(level=0).sum()
    var_new = (new_rank_c.where(pair_ok.any(axis=1)) ** 2).groupby(level=0).sum()
    n_pairs = pair_ok.groupby(level=0).sum()
    denom = var_other.mul(var_new, axis=0) ** 0.5
    rho_day = cov / denom.where(denom > 0)
    rho_day = rho_day.where(n_pairs >= _MIN_PAIRS_PER_DAY)
    rho_mean = rho_day.mean()
    rho_mean = rho_mean.where(rho_day.notna().sum() >= _MIN_SAMPLE_DAYS)
    abs_rho = rho_mean.abs().dropna()
    if abs_rho.empty:
        return 0.0, None
    return float(abs_rho.max()), str(abs_rho.idxmax())


def _long_to_wide(long_df: pd.DataFrame) -> pd.DataFrame:
    """``read_factor_source`` 长表 → ``(trade_date, symbol)`` 宽表（仅数值列）。"""
    if long_df is None or long_df.empty:
        return pd.DataFrame()
    df = long_df.copy()
    if "symbol" not in df.columns or "trade_date" not in df.columns:
        return pd.DataFrame()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df = df.dropna(subset=["trade_date", "symbol"])
    df["trade_date"] = df["trade_date"].dt.strftime("%Y-%m-%d")
    df = df.drop_duplicates(subset=["trade_date", "symbol"], keep="last")
    df = df.set_index(["trade_date", "symbol"]).sort_index()
    return df.select_dtypes(include=[np.number])


def _load_controls(lib_root: Path, sample_days: list[str]) -> pd.DataFrame:
    """值级查重对照帧：CUSTOM ``l1_factors`` + ``rd_mined`` 既有列。

    只读采样日所在区间（近端窗口），列含 OHLCV——价格/成交量的直接
    衍生因子同样应被拒。
    """
    from backend.shared.feature_source import read_factor_source

    if not sample_days:
        return pd.DataFrame()
    start, end = min(sample_days), max(sample_days)
    frames: list[pd.DataFrame] = []
    for lib in ("l1_factors", RD_MINED_SOURCE):
        if lib == RD_MINED_SOURCE and not any(Path(lib_root).glob("dt=*/*.parquet")):
            continue
        try:
            long_df = read_factor_source(lib, market=LIB_MARKET, start=start, end=end)
        except FileNotFoundError:
            logger.info("对照库 %s 无分区，跳过", lib)
            continue
        except Exception as exc:  # noqa: BLE001 - 对照库缺失不应中断物化
            logger.warning("读取对照库 %s 失败：%s", lib, exc)
            continue
        wide = _long_to_wide(long_df)
        if not wide.empty:
            logger.info("对照库 %s：%d 行 × %d 列", lib, len(wide), wide.shape[1])
            frames.append(wide)
    if not frames:
        return pd.DataFrame()
    controls = pd.concat(frames, axis=1)
    controls = controls.loc[:, ~controls.columns.duplicated()]
    keep = set(sample_days)
    return controls[controls.index.get_level_values(0).isin(keep)]


# ── 因子代码执行（子进程 + daily_pv.h5） ───────────────────────────────


_RUNNER_SOURCE = r"""
import os
import sys
import traceback

import pandas as pd

os.chdir(os.path.dirname(os.path.abspath(__file__)))
for _f in os.listdir("."):
    if _f.endswith(".h5") and _f != "daily_pv.h5":
        try:
            os.remove(_f)
        except OSError:
            pass

factor_code = os.environ["RDM_FACTOR_CODE"]
out_parquet = os.environ["RDM_OUT_PARQUET"]

try:
    exec(factor_code, globals())
    _results = [
        f for f in os.listdir(".") if f.endswith(".h5") and "result" in f.lower()
    ]
    if not _results:
        _fns = [
            v
            for k, v in globals().items()
            if k.startswith("calculate_") and callable(v)
        ]
        if _fns:
            _ret = _fns[0]()
            if _ret is not None and hasattr(_ret, "to_hdf"):
                if isinstance(_ret, pd.Series):
                    _ret = _ret.to_frame()
                _ret.to_hdf("result.h5", key="data", mode="w")
        _results = [
            f for f in os.listdir(".") if f.endswith(".h5") and "result" in f.lower()
        ]
    if not _results:
        _results = [
            f for f in os.listdir(".") if f.endswith(".h5") and f != "daily_pv.h5"
        ]
    if not _results:
        print("NO_RESULT_FILE")
        sys.exit(2)
    _df = pd.read_hdf(_results[0])
    if isinstance(_df, pd.Series):
        _df = _df.to_frame()
    if _df.empty:
        print("EMPTY_FACTOR")
        sys.exit(3)
    _df = _df.iloc[:, [0]]
    if not isinstance(_df.index, pd.MultiIndex) or _df.index.nlevels != 2:
        print("BAD_INDEX")
        sys.exit(4)
    _df.to_parquet(out_parquet)
    print("ROWS=%d" % len(_df))
except SystemExit:
    raise
except Exception as _exc:
    print("ERROR: %s" % _exc)
    traceback.print_exc()
    sys.exit(1)
"""


def _execute_factor_code(
    factor_code: str,
    h5_path: Path,
    out_parquet: Path,
    *,
    timeout: int = _EXEC_TIMEOUT_S,
) -> int:
    """在临时目录执行因子代码，产出单列因子值 parquet，返回行数。

    与 ``run_rd_agent.compute_factor_ic`` 同款约定：代码自行读
    ``daily_pv.h5`` 并写 ``result.h5``；无 __main__ 守卫时显式调用
    首个 ``calculate_*()`` 函数并落盘其返回值。
    """
    h5_path = Path(h5_path)
    out_parquet = Path(out_parquet)
    if not h5_path.is_file():
        raise RuntimeError(f"daily_pv.h5 不存在: {h5_path}")
    with tempfile.TemporaryDirectory(prefix="rd_mined_exec_") as tmp:
        tmp_dir = Path(tmp)
        link = tmp_dir / "daily_pv.h5"
        try:
            os.symlink(str(h5_path.resolve()), link)
        except OSError:
            shutil.copy2(h5_path, link)
        script = tmp_dir / "_run_factor.py"
        script.write_text(_RUNNER_SOURCE, encoding="utf-8")
        env = dict(os.environ)
        env["RDM_FACTOR_CODE"] = str(factor_code)
        env["RDM_OUT_PARQUET"] = str(out_parquet)
        try:
            proc = subprocess.run(
                [sys.executable, str(script)],
                cwd=str(tmp_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"因子代码执行超时（{timeout}s）") from exc
        output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        rows: int | None = None
        for line in output.splitlines():
            if line.startswith("ROWS="):
                try:
                    rows = int(line.split("=", 1)[1])
                except ValueError:
                    rows = None
        if proc.returncode != 0 or rows is None:
            tail = "\n".join(output.strip().splitlines()[-6:])
            raise RuntimeError(f"因子代码执行失败（rc={proc.returncode}）：{tail}")
        if not out_parquet.is_file():
            raise RuntimeError("执行声称成功但输出 parquet 缺失")
        return rows


def _to_canonical(frame: pd.DataFrame) -> pd.Series:
    """执行结果 → 单列 Series，``(trade_date str, symbol 前缀式)`` 两层索引。"""
    from backend.shared.stock_utils import StockCodeUtil

    series = frame.iloc[:, 0]
    if not isinstance(series.index, pd.MultiIndex) or series.index.nlevels != 2:
        raise ValueError("因子值索引不是 (datetime, instrument) 两层 MultiIndex")
    days = pd.to_datetime(series.index.get_level_values(0), errors="coerce")
    symbols = [
        StockCodeUtil.to_prefix(str(v)) for v in series.index.get_level_values(1)
    ]
    canonical = pd.Series(
        pd.to_numeric(pd.Series(series.to_numpy()), errors="coerce").to_numpy(),
        index=pd.MultiIndex.from_arrays(
            [days.strftime("%Y-%m-%d"), symbols], names=["trade_date", "symbol"]
        ),
    )
    canonical = canonical[~canonical.index.duplicated(keep="last")]
    # ±inf（除零产物）与 NaN 同罪：落库前统一清掉，避免 inf 进训练列
    canonical = canonical.replace([np.inf, -np.inf], np.nan)
    return canonical.dropna()


def _compute_factor_values(
    row: Mapping[str, Any], h5_path: Path, *, timeout: int
) -> pd.Series:
    fd, tmp_name = tempfile.mkstemp(suffix=".parquet", prefix="rd_mined_values_")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        _execute_factor_code(
            str(row.get("factor_code") or ""), h5_path, tmp, timeout=timeout
        )
        frame = pd.read_parquet(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    return _to_canonical(frame)


_HDF5_MAGIC = b"\x89HDF\r\n\x1a\n"


def _h5_is_valid(path: Path) -> bool:
    """HDF5 魔数校验（O(1)）：缓存被因子脚本当输出名写坏时能即刻发现。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(8) == _HDF5_MAGIC
    except OSError:
        return False


def _resolve_h5(h5_path: str | None) -> Path:
    """daily_pv.h5 解析：显式路径 → 共享缓存 → 现场生成（复用 RD-Agent 生成器）。"""
    if h5_path:
        path = Path(h5_path)
        if path.is_file():
            if _h5_is_valid(path):
                return path
            logger.warning("--h5-path 不是有效 HDF5（%s），回退共享缓存", path)
        else:
            # 自动挂接场景：任务目录可能已被清理，回退共享缓存/现场生成
            logger.warning("--h5-path 不存在（%s），回退共享缓存", path)
    cache = _quantdb_dir() / ".h5_cache" / "daily_pv_all.h5"
    if cache.is_file() and not _h5_is_valid(cache):
        logger.warning(
            "共享 h5 缓存非有效 HDF5（疑被因子代码写坏），删除重生成：%s", cache
        )
        cache.unlink(missing_ok=True)
    if not cache.is_file():
        _generate_shared_h5(cache)
    if not cache.is_file():
        raise RuntimeError("daily_pv.h5 生成失败（QuantDBDataHub 不可用或数据缺失）")
    if not _h5_is_valid(cache):
        raise RuntimeError("daily_pv.h5 非有效 HDF5（生成异常）")
    # 只读化：因子脚本误把 daily_pv.h5 当输出名时会响亮失败，而非毁掉共享缓存
    try:
        os.chmod(cache, 0o444)
    except OSError:  # pragma: no cover - 非 POSIX/权限受限时静默
        pass
    return cache


def _generate_shared_h5(cache_path: Path) -> None:
    from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper

    wrapper = RDLoopWrapper(market=DEFAULT_MARKET)
    # output_path 必须指向临时位置：生成器会把缓存硬链/软链到 output_path，
    # 若传缓存自身会「先删目标再链接」把缓存毁掉。
    with tempfile.TemporaryDirectory(prefix="rd_mined_h5_") as tmp:
        wrapper._generate_h5_from_parquet(  # noqa: SLF001 - 复用既有生成器
            str(_quantdb_dir()), str(Path(tmp) / "daily_pv.h5"), debug=False
        )
    logger.info("daily_pv.h5 已生成/复用：%s", cache_path)


def _write_factor(values: pd.Series, column: str) -> int:
    from backend.shared.feature_source import merge_factor_into_source

    # 整数因子（计数/序号等）若按 int64 落库，与其他分区的 float64 混读时
    # DuckDB 以**第一个文件**的类型为准静默把其余档取整（实测 1.5→2）——
    # 一律 float64，杜绝混合 dtype 分区
    factor_df = values.astype("float64").to_frame(name=column)
    return int(
        merge_factor_into_source(
            factor_df,
            column,
            source=RD_MINED_SOURCE,
            market=LIB_MARKET,
            create_missing=True,
        )
    )


# ── 分区 schema 对齐 ───────────────────────────────────────────────────

_NUMERIC_ARROW_RE = re.compile(r"^(u?int\d*|float\d*|double|halffloat|decimal)")


def _align_partition_schemas(lib_root: Path) -> dict[str, int]:
    """把 ``dt=*`` 分区对齐到全库列并集（补 NaN float64、统一列序/dtype、原子替换）。

    读取层 DuckDB 对 glob 分区无 union_by_name：列集漂移会响亮失败
    （schema mismatch）；dtype 漂移更糟——DuckDB 以**第一个文件**的类型
    为准静默把其余档转型（实测 int64 在前时 1.5 读成 2），所以数值列
    跨分区必须统一为 float64。
    """
    partitions = sorted(Path(lib_root).glob("dt=*/*.parquet"))
    if not partitions:
        return {"files": 0, "aligned": 0, "columns": 0}
    import pyarrow.parquet as pq

    schemas: list[tuple[str, ...]] = []
    file_types: list[dict[str, str]] = []
    union: list[str] = []
    seen: set[str] = set()
    col_types: dict[str, set[str]] = {}
    for path in partitions:
        arrow = pq.ParquetFile(str(path)).schema_arrow
        cols = tuple(str(c) for c in arrow.names)
        types = {str(n): str(t) for n, t in zip(arrow.names, arrow.types, strict=True)}
        schemas.append(cols)
        file_types.append(types)
        for col in cols:
            if col not in seen:
                seen.add(col)
                union.append(col)
            col_types.setdefault(col, set()).add(types[col])
    # 数值列（含跨分区已漂移成多种数值类型的）统一目标 double；
    # 非数值列跨分区类型不一致属异常，告警但不动（不做无据的猜测转型）
    numeric_cols: set[str] = set()
    for col, types in col_types.items():
        if all(_NUMERIC_ARROW_RE.match(t) for t in types):
            numeric_cols.add(col)
        elif len(types) > 1:
            logger.warning("列 %s 跨分区类型不一致（%s），未做统一", col, sorted(types))
    target_names = tuple(union)
    aligned = 0
    for path, cols, types in zip(partitions, schemas, file_types, strict=True):
        dtype_drift = any(
            col in numeric_cols and types.get(col) != "double" for col in cols
        )
        if cols == target_names and not dtype_drift:
            continue
        df = pd.read_parquet(path, engine="pyarrow")
        for col in union:
            if col not in df.columns:
                df[col] = np.full(len(df), np.nan, dtype="float64")
        for col in numeric_cols:
            if col in df.columns and str(df[col].dtype) != "float64":
                df[col] = df[col].astype("float64")
        # 临时名**不能**以 .parquet 结尾：pathlib 通配（含读取层 _files）
        # 会匹配 .tmp-data.parquet 这类名字，崩溃残留会毒化 glob 读取
        tmp = path.parent / f".{path.name}.tmp"
        try:
            df[union].to_parquet(tmp, index=False, engine="pyarrow")
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
        aligned += 1
    return {"files": len(partitions), "aligned": aligned, "columns": len(union)}


# ── DB 交互 ────────────────────────────────────────────────────────────


async def _query_candidates(
    *,
    factor_ids: list[str] | None = None,
    task_id: str | None = None,
    market: str | None = None,
    limit: int = 0,
    ensure: bool = True,
) -> list[dict[str, Any]]:
    """候选因子查询（显式参数版；CLI 与后台物化面板共用同一实现）。

    ``ensure=False`` 供只读状态面复用：跳过建表迁移（那是一串无条件执行的
    ``CREATE/ALTER TABLE``，在面板 10s 轮询下会反复取 ACCESS EXCLUSIVE 锁，
    还会要求 API 侧 DB 账号具备 DDL 权限）。真实运行保持默认 True。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    if ensure:
        from backend.services.engine.qlib_app.services.rd_agent_persistence import (
            RDAgentFactorPersistence,
        )

        await RDAgentFactorPersistence().ensure_tables()

    clauses: list[str] = []
    params: dict[str, Any] = {}
    if factor_ids:
        clauses.append("factor_id = ANY(:ids)")
        params["ids"] = list(factor_ids)
    if task_id:
        clauses.append("metadata_json->>'task_id' = :task_id")
        params["task_id"] = task_id
    if market:
        clauses.append("COALESCE(market, :default_market) = :market")
        params["default_market"] = DEFAULT_MARKET
        params["market"] = market
    where = " AND ".join(clauses) if clauses else "TRUE"
    sql = (
        "SELECT factor_id, factor_name, factor_code, factor_formulation, "
        "COALESCE(market, :default_market) AS market, status "
        "FROM rd_agent_factors "
        f"WHERE {where} ORDER BY created_at"
    )
    if limit and limit > 0:
        sql += " LIMIT :limit"
        params["limit"] = int(limit)
        params.setdefault("default_market", DEFAULT_MARKET)
    async with get_session(read_only=True) as session:
        rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(row) for row in rows]


async def _load_candidates(args: argparse.Namespace) -> list[dict[str, Any]]:
    return await _query_candidates(
        factor_ids=list(args.factor_ids or []),
        task_id=args.task_id,
        market=args.market,
        limit=int(args.limit or 0),
    )


async def _update_factor_meta(factor_id: str, entry: Mapping[str, Any]) -> None:
    if not factor_id:
        return
    try:
        from backend.services.engine.qlib_app.services.rd_agent_persistence import (
            RDAgentFactorPersistence,
        )

        await RDAgentFactorPersistence().update_factor_metrics(
            factor_id=factor_id, metadata={"materialization": dict(entry)}
        )
    except Exception as exc:  # noqa: BLE001 - 元数据回写失败不阻断物化
        logger.warning("回写 materialization 元数据失败 %s：%s", factor_id, exc)


async def _published_enabled_columns(session: Any) -> tuple[str | None, set[str]]:
    """rd_mined 最新已发布版本的 ``(version_id, enabled 映射列集)``。

    「目录是否最新」判据的唯一来源（注册幂等与后台物化面板共用同一口径，
    防止面板说「已最新」而注册又说要发新版）。无发布版本返回 ``(None, set())``。
    """
    from sqlalchemy import text

    row = (
        await session.execute(
            text(
                "SELECT version_id FROM qm_training_factor_catalog_version "
                "WHERE source_dataset = :src AND market = :mkt "
                "AND status = 'published' "
                "ORDER BY published_at DESC NULLS LAST LIMIT 1"
            ),
            {"src": RD_MINED_SOURCE, "mkt": LIB_MARKET},
        )
    ).first()
    if row is None:
        return None, set()
    version_id = str(row[0])
    enabled = set(
        (
            await session.execute(
                text(
                    "SELECT source_column FROM qm_training_factor_mapping "
                    "WHERE version_id = :vid AND enabled"
                ),
                {"vid": version_id},
            )
        )
        .scalars()
        .all()
    )
    return version_id, enabled


async def _register_library(lib_root: Path, *, force: bool = False) -> str | None:
    """刷新字段注册并发布/更新 rd_mined 训练目录版本。

    幂等判据 = 已发布版本的 enabled 映射列集 vs 当前扫描列集，相同则跳过
    （避免注册失败后「永远不再发」或每次空发新版）。
    """
    from backend.services.api.routers.admin.quantdb_factor_catalog import (
        _ensure_schema,
        create_catalog_draft,
        publish_catalog_version,
        record_source_fields,
        seed_catalog_mappings,
    )
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        KEY_COLUMNS,
        REQUIRED_COLUMNS,
        QuantDBFactorReader,
    )
    from backend.shared.database_manager_v2 import get_session

    if not any(Path(lib_root).glob("dt=*/*.parquet")):
        logger.info("rd_mined 无分区，跳过目录注册")
        return None
    reader = QuantDBFactorReader(market=LIB_MARKET)
    status = reader.describe(RD_MINED_SOURCE)
    factor_cols = [
        str(c)
        for c in status.columns
        if str(c) not in KEY_COLUMNS and str(c) not in set(REQUIRED_COLUMNS)
    ]
    if not factor_cols:
        logger.warning("rd_mined 分区存在但无因子列，跳过目录注册")
        return None
    status_dict = status.to_dict()
    async with get_session() as session:
        await _ensure_schema(session)
        if not force:
            _published_vid, enabled = await _published_enabled_columns(session)
            if _published_vid is not None and enabled == set(factor_cols):
                logger.info("rd_mined 目录已最新（%d 列），跳过", len(factor_cols))
                return None
        await record_source_fields(session, RD_MINED_SOURCE, status_dict, LIB_MARKET)
        version_id = await create_catalog_draft(
            session,
            RD_MINED_SOURCE,
            f"rd_mined 自动注册 {date.today().isoformat()}",
            LIB_MARKET,
            created_by="rd_mined_materialize",
        )
        seeded = await seed_catalog_mappings(
            session,
            {
                "version_id": version_id,
                "market": LIB_MARKET,
                "source_dataset": RD_MINED_SOURCE,
            },
        )
        await publish_catalog_version(session, version_id)
    logger.info(
        "rd_mined 目录已发布 %s（%d 因子列，播种 %s）",
        version_id,
        len(factor_cols),
        seeded,
    )
    return version_id


# ── 主流程 ─────────────────────────────────────────────────────────────


def _lock_path() -> Path:
    """锁文件路径。``RD_MINED_MATERIALIZE_LOCK`` 覆盖仅供测试隔离。

    默认路径是全局固定的；正在跑生产回填时单测若也用全局锁，取锁必然
    失败（测试随环境变红/变绿）。覆盖口读在调用时，测试指向 tmp_path
    即与任何在跑的物化进程互不干扰。
    """
    override = os.getenv("RD_MINED_MATERIALIZE_LOCK")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "_rd_mined_materialize.lock"


def _acquire_run_lock() -> Any | None:
    """非阻塞独占锁：同一时刻只允许一个物化进程碰这座库。

    挖掘完成自动挂接的物化可能撞上手工全量回填：并发会交错抢列名、
    互相覆盖清单、并写同批分区。拿不到锁的一方直接让位（返回 None，
    调用侧记日志退出 0）——成果由清单续跑兜底，不丢因子。

    返回持有锁的文件对象（须保持引用；fd 一关锁即释放），拿不到返回 None。
    """
    import fcntl  # POSIX-only：物化器只在容器/Ubuntu 上跑

    lock_path = _lock_path()
    handle = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


# ── 后台物化面板支撑（admin API 复用） ─────────────────────────────────


def project_root() -> Path:
    """仓库根目录（与模块内 ``_project_root`` 同源；API 侧拼子进程 cwd 用）。"""
    return _project_root


def _web_log_path() -> Path:
    """后台「开始物化」的统一日志文件。

    ``RD_MINED_MATERIALIZE_WEB_LOG`` 覆盖仅供测试隔离；容器内默认
    ``/data/rd_mined_materialize_ui.log``（宿主 ./data 可见）。
    """
    override = os.getenv("RD_MINED_MATERIALIZE_WEB_LOG")
    if override:
        return Path(override)
    return Path("/data/rd_mined_materialize_ui.log")


def build_run_command() -> list[str]:
    """后台触发的物化命令（固定 argv：全常量、无任何用户输入拼接）。

    与手工 ``python3 backend/scripts/rd_mined_materialize.py --register``
    等价；用 ``-m`` + ``cwd=project_root()`` 让 ``backend.*`` 绝对导入不依赖
    PYTHONPATH 是否设置。
    """
    return [
        sys.executable,
        "-m",
        "backend.scripts.rd_mined_materialize",
        "--register",
    ]


def probe_run_lock() -> bool:
    """是否有物化进程在运行（flock 试探：拿得到锁 = 没人跑，随即释放）。

    与真实运行共用同一把锁，只作面板展示与重复启动的快速拒绝。探测与启动
    之间若被人抢先，后启动的物化进程会自己取锁失败并安静退 0——并发安全
    最终由物化进程的独占锁兜底，不依赖本探测。锁文件不可写时按「未运行」
    处理并告警：物化子进程自带取锁兜底，误报「运行中」会让面板永久假卡死。
    """
    import fcntl

    try:
        handle = open(_lock_path(), "a", encoding="utf-8")
    except OSError as exc:
        logger.warning("物化锁探测失败（按未运行处理）：%s", exc)
        return False
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def tail_web_log(max_lines: int = 120, max_bytes: int = 256 * 1024) -> dict[str, Any]:
    """后台物化日志尾部（面板展示用）；文件不存在返回 exists=False。

    符号链接一律拒读：日志路径可被同挂载域的低权限写者摆成任意文件的链接，
    顺着读会把面板变成别人的文件浏览器（读到的内容进管理员浏览器）。
    """
    path = _web_log_path()
    if path.is_symlink():
        return {
            "path": str(path),
            "exists": False,
            "lines": [],
            "note": "路径是符号链接，拒绝读取",
        }
    if not path.is_file():
        return {"path": str(path), "exists": False, "lines": []}
    size = path.stat().st_size
    truncated = size > max_bytes
    with open(path, "rb") as fh:
        if truncated:
            start = size - max_bytes
            # 边界前一字节是换行 → 截断点恰好落在行首，首行是完整的，不能丢
            fh.seek(start - 1)
            boundary_aligned = fh.read(1) == b"\n"
            fh.seek(start)
        else:
            boundary_aligned = True
        raw = fh.read()
    content = raw.decode("utf-8", errors="replace")
    lines = content.splitlines()
    if truncated and not boundary_aligned and lines:
        lines = lines[1:]  # 截断点落在行中间：首行是半个行，丢弃残片
    return {
        "path": str(path),
        "exists": True,
        "size": size,
        "truncated": truncated,
        "lines": lines[-max(1, int(max_lines)) :],
    }


async def materialize_overview(*, market: str = DEFAULT_MARKET) -> dict[str, Any]:
    """后台物化面板的只读汇总：候选分桶 + 清单统计 + 库面/目录状态。

    候选分桶复用与真实物化**完全相同**的 ``_eligible_row`` /
    ``_should_materialize`` 判定——面板显示的「待物化」与点下按钮后的实际
    工作量同口径，不另写一套近似逻辑。无业务写入，也不跑建表迁移
    （``ensure=False``）；DB 抖动一律降级成 error 字段，不把状态接口打成
    500——运行态与日志尾恰恰是故障时最需要看的两段。
    """
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        KEY_COLUMNS,
        REQUIRED_COLUMNS,
        QuantDBFactorReader,
    )
    from backend.shared.database_manager_v2 import get_session

    lib_root = _lib_root()
    manifest = _load_manifest(lib_root)
    candidates: dict[str, Any] = {
        "total": 0,
        "pending": 0,
        "pending_reasons": {},
        "skipped": {},
    }
    try:
        rows = await _query_candidates(market=market, ensure=False)
    except Exception as exc:  # noqa: BLE001 - 候选查不出来也要能看运行态/日志
        candidates["error"] = str(exc)[:300]
        rows = []

    pending = 0
    pending_reasons: dict[str, int] = {}
    skipped: dict[str, int] = {}
    for row in rows:
        ok, reason = _eligible_row(row)
        if ok:
            ok, reason = _should_materialize(row, manifest, force=False)
        if ok:
            pending += 1
            pending_reasons[reason] = pending_reasons.get(reason, 0) + 1
        else:
            skipped[reason] = skipped.get(reason, 0) + 1
    candidates.update(
        total=len(rows),
        pending=pending,
        pending_reasons=pending_reasons,
        skipped=skipped,
    )

    manifest_stats: dict[str, int] = {}
    manifest_last_at = ""
    for entry in manifest.values():
        status = str((entry or {}).get("status") or "unknown")
        manifest_stats[status] = manifest_stats.get(status, 0) + 1
        at = str((entry or {}).get("at") or "")
        if at > manifest_last_at:
            manifest_last_at = at

    factor_cols: set[str] = set()
    library: dict[str, Any] = {"ready": False, "factor_columns": 0, "partitions": 0}
    try:
        status = QuantDBFactorReader(market=LIB_MARKET).describe(RD_MINED_SOURCE)
        reserved = set(KEY_COLUMNS) | set(REQUIRED_COLUMNS)
        factor_cols = {str(c) for c in status.columns if str(c) not in reserved}
        library = {
            "ready": bool(status.ready),
            "factor_columns": len(factor_cols),
            "partitions": int(status.files),
            "min_date": status.min_date,
            "max_date": status.max_date,
        }
    except Exception as exc:  # noqa: BLE001 - 面板要能显示「库读不出来」而不是 500
        library["error"] = str(exc)[:300]

    catalog: dict[str, Any] = {
        "published_version": None,
        "published_columns": 0,
        "up_to_date": False,
    }
    try:
        async with get_session() as session:
            version_id, enabled = await _published_enabled_columns(session)
        # 与 _register_library 的跳过判据同源：目录是否最新 = 已发布 enabled
        # 映射列集 == 当前盘上列集。
        catalog = {
            "published_version": version_id,
            "published_columns": len(enabled),
            "up_to_date": version_id is not None and enabled == factor_cols,
        }
    except Exception as exc:  # noqa: BLE001 - DB 抖动时面板降级展示，不整页失败
        catalog["error"] = str(exc)[:300]

    return {
        "candidates": candidates,
        "manifest": {
            "total": len(manifest),
            "by_status": manifest_stats,
            "last_at": manifest_last_at,
        },
        "library": library,
        "catalog": catalog,
    }


async def _run(args: argparse.Namespace) -> int:
    lib_root = _lib_root()
    lock = _acquire_run_lock()
    if lock is None:
        logger.warning("另一个物化进程正在运行，本次跳过（避免并发写同一座库）")
        return 0
    if args.align_only:
        report = _align_partition_schemas(lib_root)
        logger.info("分区 schema 对齐：%s", report)
        return 0

    rows = await _load_candidates(args)
    if not rows and not args.register:
        logger.info("无匹配因子（market=%s）", args.market)
        return 0
    if not rows:
        # --register 且没有候选：物化无事可做，但目录发布仍要照常刷新——
        # 面板「待物化 0」时点开始，承诺的就是这一步（否则按钮静默变哑巴）。
        logger.info("无匹配因子（market=%s），仅执行注册/发布", args.market)
    manifest = _load_manifest(lib_root)
    todo: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    for row in rows:
        ok, reason = _eligible_row(row)
        if ok:
            ok, reason = _should_materialize(row, manifest, force=args.force)
        if ok:
            todo.append(row)
        else:
            skipped[reason] = skipped.get(reason, 0) + 1
    logger.info("候选 %d：待物化 %d，跳过 %s", len(rows), len(todo), skipped or "无")
    owners = _column_owners(manifest)
    if args.dry_run:
        for row in todo[:50]:
            factor_id = str(row.get("factor_id") or "")
            base = feature_column_name(str(row.get("factor_name") or ""))
            column = _disambiguate_column(base, factor_id, owners)
            logger.info(
                "  [dry-run] %s %s → %s%s",
                factor_id[:8],
                str(row.get("factor_name"))[:40],
                column,
                "（列名冲突，已消歧）" if column != base else "",
            )
            owners[column] = factor_id
        if len(todo) > 50:
            logger.info("  ... 其余 %d 条", len(todo) - 50)
        return 0
    if not todo and not args.register:
        return 0

    stats = {
        "materialized": 0,
        "rejected_duplicate": 0,
        "errors": 0,
        "corr_unverified": 0,  # 值级查重没算出结果的因子数（门静默退化可见性）
    }
    dirty_library = False  # 有任一因子写过值 → 尾部必须跑 schema 对齐
    controls: pd.DataFrame | None = None
    sample_days: list[str] | None = None
    h5_path: Path | None = None
    for idx, row in enumerate(todo, 1):
        factor_id = str(row.get("factor_id") or "")
        name = str(row.get("factor_name") or "")
        base_column = feature_column_name(name)
        column = _disambiguate_column(base_column, factor_id, owners)
        logger.info(
            "[%d/%d] %s（%s）→ %s%s",
            idx,
            len(todo),
            name[:40],
            factor_id[:8],
            column,
            "（列名冲突，已消歧）" if column != base_column else "",
        )
        try:
            if h5_path is None:
                h5_path = _resolve_h5(args.h5_path)
                logger.info("daily_pv.h5 = %s", h5_path)
            values = _compute_factor_values(row, h5_path, timeout=args.timeout)
            if values.empty:
                raise RuntimeError("因子值为全空")
            rho: float | None = None
            matched: str | None = None
            if not args.no_corr_gate:
                if controls is None:
                    sample_days = _recent_sample_days(values)
                    controls = _load_controls(lib_root, sample_days)
                    if controls.empty:
                        logger.warning("无对照库数据，本轮值级查重不可用")
                    else:
                        logger.info(
                            "对照帧：%d 行 × %d 列，采样日 %d 天",
                            len(controls),
                            controls.shape[1],
                            len(sample_days),
                        )
                rho, matched = _max_abs_corr(
                    values,
                    controls,
                    sample_days=set(sample_days or []) or None,
                    exclude=_owned_columns(owners, factor_id) | {column},
                )
                if not controls.empty and matched is None and rho == 0.0:
                    # matched=None ⇒ 没算出任何可比对列（非「算出来是 0」）：
                    # 该因子本轮未经值级查重，计入汇总，别让门静默退化
                    logger.warning("  值级查重未生效（与对照库无足够重叠样本）")
                    stats["corr_unverified"] += 1
                if rho >= args.corr_threshold:
                    entry = {
                        "status": "rejected_duplicate",
                        "column": column,
                        "name": name,
                        "corr": round(float(rho), 4),
                        "corr_against": matched,
                        "code_fp": code_fingerprint(str(row.get("factor_code") or "")),
                        "at": _now_iso(),
                    }
                    manifest[factor_id] = entry
                    _save_manifest(lib_root, manifest)
                    await _update_factor_meta(factor_id, entry)
                    owners[column] = factor_id
                    stats["rejected_duplicate"] += 1
                    logger.warning(
                        "  值级重复（|ρ|=%.3f vs %s），拒绝入账", rho, matched
                    )
                    continue
                if rho >= _CORR_WARN:
                    logger.warning(
                        "  相关偏高：与 %s 的 |ρ|=%.3f（拒绝阈值 %.2f）",
                        matched,
                        rho,
                        args.corr_threshold,
                    )
            # 先置脏再写：_write_factor 可能半途失败留下部分分区，尾部对齐不可跳过
            dirty_library = True
            written = _write_factor(values, column)
            if written <= 0:
                raise RuntimeError("写回 0 个非空值")
            entry = {
                "status": "materialized",
                "column": column,
                "name": name,
                "values": int(written),
                "corr": None if rho is None else round(float(rho), 4),
                "corr_against": matched
                if rho is not None and rho >= _CORR_WARN
                else None,
                "code_fp": code_fingerprint(str(row.get("factor_code") or "")),
                "at": _now_iso(),
            }
            manifest[factor_id] = entry
            _save_manifest(lib_root, manifest)
            await _update_factor_meta(factor_id, entry)
            owners[column] = factor_id
            if controls is not None and sample_days:
                sub = values[values.index.get_level_values(0).isin(set(sample_days))]
                if not sub.empty:
                    controls[column] = sub
            stats["materialized"] += 1
            logger.info("  完成：%d 个非空值", written)
        except Exception as exc:  # noqa: BLE001 - 单因子失败记录后继续
            logger.exception("  失败：%s", exc)
            entry = {
                "status": "error",
                "column": column,
                "name": name,
                "error": str(exc)[:500],
                "at": _now_iso(),
            }
            manifest[factor_id] = entry
            _save_manifest(lib_root, manifest)
            await _update_factor_meta(factor_id, entry)
            owners[column] = factor_id  # 失败也占名：重试落回原列，不与后来者交错
            stats["errors"] += 1

    if dirty_library:
        report = _align_partition_schemas(lib_root)
        logger.info("分区 schema 对齐：%s", report)
    if args.register:
        try:
            await _register_library(lib_root, force=args.force_register)
        except Exception as exc:  # noqa: BLE001 - 注册失败不影响已物化数据
            logger.exception("目录注册失败：%s", exc)
            stats["errors"] += 1
    logger.info("汇总：%s", stats)
    return 0 if stats["errors"] == 0 else 1


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="RD-Agent 挖掘因子 → rd_mined 库物化器"
    )
    parser.add_argument("--task-id", help="只物化该挖掘任务产出的因子")
    parser.add_argument("--factor-ids", help="逗号分隔的 factor_id 列表")
    parser.add_argument(
        "--market",
        default=DEFAULT_MARKET,
        help="只物化该市场的因子（默认 a_share；v1 仅支持该市场）",
    )
    parser.add_argument("--limit", type=int, default=0, help="最多处理条数（0=不限）")
    parser.add_argument("--h5-path", help="daily_pv.h5 路径（缺省用共享缓存/现场生成）")
    parser.add_argument(
        "--timeout", type=int, default=_EXEC_TIMEOUT_S, help="单因子执行超时秒数"
    )
    parser.add_argument(
        "--corr-threshold", type=float, default=_CORR_REJECT, help="值级查重拒绝阈值"
    )
    parser.add_argument("--no-corr-gate", action="store_true", help="关闭值级查重")
    parser.add_argument(
        "--register", action="store_true", help="结束后发布/更新 rd_mined 训练目录版本"
    )
    parser.add_argument(
        "--force-register", action="store_true", help="目录列集未变也强制发布新版"
    )
    parser.add_argument("--force", action="store_true", help="重做已物化/已拒绝的因子")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的动作")
    parser.add_argument(
        "--align-only", action="store_true", help="只做分区 schema 对齐后退出"
    )
    parser.add_argument("--verbose", action="store_true", help="调试日志")
    args = parser.parse_args(argv)
    args.factor_ids = [
        s.strip() for s in str(args.factor_ids or "").split(",") if s.strip()
    ]
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
