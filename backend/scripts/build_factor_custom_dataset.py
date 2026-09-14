#!/usr/bin/env python3
"""构建「筛选保留（去L2）273 因子」合并数据集 → 自定义市场（quantcustom）。

把筛选保留集里分布在 5 个库（alpha_library/alpha360/jq110/tdxgs/l1_factors）
的因子按 (trade_date × symbol) 合并为一份 parquet 分区，供**单次直读训练**使用
（平台直读训练限定单数据源，自定义市场是官方通道）。

产物：<quantcustom>/6_ml_datasets/l1_factors/dt=YYYYMMDD/data.parquet
    列：symbol + date + 273 因子列 + 全套 OHLCV（close 前复权，训练标签用）

口径（与回测一致）：
- 股票池：全 A 非 ST/退市（instrument_detail 静态口径，同 build_universe）；
- 可交易性：当日涨停（按板别阈值 9.8/19.8/29.8%）或次日跌停 → 该 (日,股) 行剔除
  （买入买不进/卖出卖不出，避免把不可成交的账面收益当样本）；
- 因子取值缺失写 NaN（训练端截面预处理负责填充）；
- close 用 daily_forward（前复权）。

增量模式（默认）：meta.json 记录筛选指纹 + 股票池 + 参数，指纹一致时只补写
缺失分区（供每日定时重建）；筛选集 / --start / --min-coverage 任一变化会自动
退回全量重建。`--full` 强制全量。

用法（仓库根）:
    python3 backend/scripts/build_factor_custom_dataset.py --start 2020-01-01
    python3 backend/scripts/build_factor_custom_dataset.py --smoke 300    # 冒烟
    python3 backend/scripts/build_factor_custom_dataset.py --full         # 强制全量

定时重建：由 Celery 市场同步调度以 market="CUSTOM" 挂载（默认每天 03:00，
见 backend/services/engine/tasks/market_sync_scheduler.py）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from collections.abc import Callable

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.shared.quantdb_paths import resolve_quantdb_dir  # noqa: E402

# 五个来源库 → 数据目录相对路径（key 与 screening 的 library 一致）
LIB_DIRS = {
    "alpha_library": ("6_ml_datasets", "alpha_library"),
    "alpha360": ("6_ml_datasets", "alpha360"),
    "jq110": ("6_ml_datasets", "jq110"),
    "tdxgs": ("6_ml_datasets", "tdxgs"),
    "l1_factors": ("6_ml_datasets", "l1_factors"),
}
DROP_COLS = {"symbol", "time", "dt", "date", "open", "high", "low", "volume", "amount"}

DEFAULT_START = "2020-01-01"
DEFAULT_MIN_COVERAGE = 0.9
_OHLCV = ("open", "high", "low", "close", "volume", "amount")


def _limit_threshold(code: str) -> float:
    """按板块返回近似涨跌停阈值（复权价口径）。"""
    raw = str(code).split(".")[0]
    if raw.startswith(("300", "301", "688", "689")):
        return 0.198
    if raw.startswith(("4", "8", "92")):
        return 0.298
    return 0.098


def _load_kept_columns(root: Path) -> dict[str, list[str]]:
    """读取筛选保留集（去 L2），按来源库归组为待合并列（列名排序保证确定性）。"""
    sel = json.loads(
        (root / "factor_research" / "screening" / "factor_selection.json").read_text(
            encoding="utf-8"
        )
    )
    cols_by_lib: dict[str, list[str]] = {}
    for k in sel.get("kept", []):
        lib, name = str(k.get("library") or ""), str(k.get("name") or "")
        if lib in LIB_DIRS and lib != "l2_factors" and name:
            cols_by_lib.setdefault(lib, []).append(name)
    for lib in cols_by_lib:
        cols_by_lib[lib] = sorted(set(cols_by_lib[lib]))
    return cols_by_lib


def _selection_fingerprint(cols_by_lib: dict[str, list[str]]) -> str:
    """筛选集指纹：库→列清单的稳定哈希，用于增量模式的一致性守卫。"""
    payload = json.dumps(
        {lib: sorted(cols) for lib, cols in cols_by_lib.items()},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _missing_dates(dates: list[str], out_root: Path) -> list[str]:
    """尚无输出分区的交易日（增量重建的补写清单）。"""
    return [d for d in dates if not (out_root / f"dt={d}" / "data.parquet").exists()]


def rebuild(
    *,
    start: str = DEFAULT_START,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    smoke: int = 0,
    incremental: bool = True,
    force_full: bool = False,
    log: Callable[..., None] = print,
) -> dict[str, Any]:
    """合并五库因子为自定义市场数据集；返回摘要 dict（供调度任务记录）。"""
    t0 = time.time()
    root = resolve_quantdb_dir()

    # 1) 筛选保留集（去 L2）
    cols_by_lib = _load_kept_columns(root)
    total_cols = sum(len(v) for v in cols_by_lib.values())
    log(
        f"[1/5] 保留集(去L2)合并列数 {total_cols}: { {k: len(v) for k, v in cols_by_lib.items()} }"
    )
    fingerprint = _selection_fingerprint(cols_by_lib)

    # 2) 股票池：全 A 非 ST/退市（静态口径）
    instr = pd.read_parquet(
        root / "2_base_sector" / "instrument_detail" / "instrument_detail.parquet",
        columns=["Symbol", "IsSTGP", "IsQuitGP"],
    )
    ok_sym = instr[
        (instr["IsSTGP"].astype(str) == "0") & (instr["IsQuitGP"].astype(str) == "0")
    ]["Symbol"].tolist()
    dates = [
        d
        for d in sorted(
            p.name[3:] for p in (root / "1_kline_data" / "daily_forward").glob("dt=*")
        )
        if d >= start.replace("-", "")
    ]
    if not dates:
        log(f"[2/5] 源数据窗口为空（start={start}），无事可做")
        return {"written": 0, "mode": "noop", "reason": "empty source window"}

    custom_root = Path(os.getenv("QM_QUANTCUSTOM_DATA_DIR") or "/data/quantcustom")
    out_root = custom_root / "6_ml_datasets" / "l1_factors"
    meta_path = out_root / "meta.json"

    prev_meta: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            prev_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log(f"[!] meta.json 读取失败（按全量处理）: {exc}")

    # 增量守卫：筛选集/参数一致且已有股票池时才允许增量补写
    can_incremental = bool(
        incremental
        and not force_full
        and not smoke
        and prev_meta.get("symbols")
        and prev_meta.get("selection_fingerprint") == fingerprint
        and float(prev_meta.get("min_coverage") or 0) == float(min_coverage)
        and str(prev_meta.get("start") or "") == start
    )
    if incremental and not force_full and not can_incremental:
        log("[i] 元数据缺失或不一致（筛选集 / start / min-coverage 变化）→ 全量重建")

    log(f"[2/5] 股票池（非 ST/退市）: {len(ok_sym)} 只，窗口 {len(dates)} 日")
    if smoke:
        ok_sym = ok_sym[:smoke]
    elif can_incremental:
        # 复用登记在 meta 的股票池；与当前静态口径取交集（新 ST/退市即时剔除）
        static_ok = set(ok_sym)
        ok_sym = [s for s in prev_meta["symbols"] if s in static_ok]
        log(f"      增量模式复用股票池: {len(ok_sym)} 只")
    elif min_coverage > 0:
        # 覆盖过滤：窗口内有效收盘占比 ≥ min_coverage，剔除长期停牌/次新等
        # （数据驱动、无未来函数；把训练体量压到内存安全范围）。
        cnt: dict[str, int] = {}
        for dt in dates:
            d = pd.read_parquet(
                root / "1_kline_data" / "daily_forward" / f"dt={dt}" / "data.parquet",
                columns=["symbol", "close"],
            )
            close = d.set_index("symbol")["close"].reindex(ok_sym)
            for sym, v in close.items():
                if pd.notna(v) and v > 0:
                    cnt[sym] = cnt.get(sym, 0) + 1
        floor = int(len(dates) * min_coverage)
        ok_sym = [sym for sym in ok_sym if cnt.get(sym, 0) >= floor]
        log(f"      覆盖率 ≥ {min_coverage:.0%}: 保留 {len(ok_sym)} 只")

    sym_index = pd.Index(ok_sym)
    threshold = sym_index.map(_limit_threshold).to_numpy(dtype=np.float64)

    # 3) 逐日合并（5 库 × 单日分区 → 1 个自定分区）
    if can_incremental:
        write_dates = _missing_dates(dates, out_root)
        mode = "incremental"
    else:
        write_dates = list(dates)
        mode = "full"
    log(
        f"[3/5] 交易日 {len(dates)} 个（{dates[0]} ~ {dates[-1]}）→ {out_root}；待写 {len(write_dates)} 个（{mode}）"
    )

    def _ohlcv_of(dt: str) -> pd.DataFrame:
        d = pd.read_parquet(
            root / "1_kline_data" / "daily_forward" / f"dt={dt}" / "data.parquet",
            columns=["symbol", *_OHLCV],
        )
        return d.set_index("symbol").reindex(sym_index)[list(_OHLCV)]  # NaN 保留

    # 增量补写时首日的前收需要从源数据（前一个交易日）取，涨跌停判定才连续
    prev_close = None
    if write_dates and write_dates[0] != dates[0]:
        i0 = dates.index(write_dates[0])
        prev_close = _ohlcv_of(dates[i0 - 1])["close"]

    written = 0
    for i, dt in enumerate(write_dates):
        frames: list[pd.DataFrame] = []
        for lib, dirs in LIB_DIRS.items():
            cols = cols_by_lib.get(lib)
            if not cols:
                continue
            f = root.joinpath(*dirs) / f"dt={dt}" / "data.parquet"
            if not f.exists():
                continue
            df = pd.read_parquet(f, columns=["symbol", *cols])
            frames.append(df.set_index("symbol").reindex(sym_index)[cols])
        if not frames:
            continue
        cur = pd.concat(frames, axis=1)
        ohlcv = _ohlcv_of(dt)
        # 直读 REQUIRED_COLUMNS = symbol,date,open,high,low,close,volume,amount → 附全套 OHLCV
        for c in _OHLCV:
            cur[c] = ohlcv[c]
        if prev_close is not None:
            # 可交易性（标签 = 训练端由 close 计算的前向收益，置 NaN 即样本剔除）：
            #  当日涨停（买不进）或当日跌停（前一日标签卖不出）→ close(t) 置 NaN；
            #  同时使 t-1 的对应样本不可算（保守剔除，无虚假收益）。
            ret = ohlcv["close"] / prev_close - 1
            blocked = (
                (ret.abs() >= threshold) | ohlcv["close"].isna() | prev_close.isna()
            )
            cur["close"] = ohlcv["close"].where(~blocked)
        else:
            cur["close"] = ohlcv["close"]
        prev_close = ohlcv["close"]
        cur.insert(
            0, "date", pd.Timestamp(dt).strftime("%Y-%m-%d")
        )  # reader 需 YYYY-MM-DD
        cur = cur.reset_index().rename(columns={"index": "symbol"})
        cur = cur.replace([np.inf, -np.inf], np.nan)
        d_out = out_root / f"dt={dt}"
        d_out.mkdir(parents=True, exist_ok=True)
        cur.to_parquet(d_out / "data.parquet", index=False)
        written += 1
        if i % 200 == 0:
            log(f"      {dt} 已有 {written} 个分区（{time.time() - t0:.0f}s）")

    # 4) 元数据（增量守卫依赖 symbols / fingerprint / 参数；冒烟运行不落 meta）
    if not smoke:
        meta = {
            "dataset": "quantcustom/6_ml_datasets/l1_factors",
            "built_at": datetime.now().isoformat(timespec="seconds"),
            "window": [dates[0], dates[-1]] if dates else None,
            "n_days": len(dates),
            "n_written": written,
            "mode": mode,
            "start": start,
            "min_coverage": float(min_coverage),
            "selection_fingerprint": fingerprint,
            "symbols": ok_sym,
            "universe": "全 A 非 ST/退市（静态口径）",
            "n_factors": total_cols,
            "factors_by_source": {k: len(v) for k, v in cols_by_lib.items()},
            "exclusions": "当日涨停行 close 置 NaN（标签不可算 = 样本剔除）；次日跌停由标签自然不可实现未单列",
            "conventions": "close=daily_forward 前复权；因子缺失 NaN 由训练端截面预处理（中位数填充+1%/99%缩尾+Z-score）处理",
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    elapsed = time.time() - t0
    log(f"[5/5] 完成 → {out_root}（{mode}：写 {written} 分区，总 {elapsed:.0f}s）")
    return {
        "written": written,
        "mode": mode,
        "window": [dates[0], dates[-1]],
        "pool": len(ok_sym),
        "n_factors": total_cols,
        "elapsed_s": round(elapsed, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START, help="起始日（含）")
    ap.add_argument("--smoke", type=int, default=0, help="冒烟：只取前 N 只股票")
    ap.add_argument(
        "--min-coverage",
        type=float,
        default=DEFAULT_MIN_COVERAGE,
        help="窗口内有效收盘覆盖率下限（剔除长期停牌/次新/垃圾股；0=不过滤）",
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help="强制全量重建（忽略 meta 的增量元数据）",
    )
    args = ap.parse_args()
    summary = rebuild(
        start=args.start,
        min_coverage=args.min_coverage,
        smoke=args.smoke,
        force_full=args.full,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
