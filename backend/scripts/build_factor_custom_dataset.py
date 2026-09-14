#!/usr/bin/env python3
"""构建「筛选保留（去L2）273 因子」合并数据集 → 自定义市场（quantcustom）。

把筛选保留集里分布在 5 个库（alpha_library/alpha360/jq110/tdxgs/l1_factors）
的因子按 (trade_date × symbol) 合并为一份 parquet 分区，供**单次直读训练**使用
（平台直读训练限定单数据源，自定义市场是官方通道）。

产物：<quantcustom>/6_ml_datasets/l1_factors/dt=YYYYMMDD/data.parquet
    列：symbol + date + 273 因子列 + close（前复权，训练标签用）

口径（与回测一致）：
- 股票池：全 A 非 ST/退市（instrument_detail 静态口径，同 build_universe）；
- 可交易性：当日涨停（按板别阈值 9.8/19.8/29.8%）或次日跌停 → 该 (日,股) 行剔除
  （买入买不进/卖出卖不出，避免把不可成交的账面收益当样本）；
- 因子取值缺失写 NaN（训练端截面预处理负责填充）；
- close 用 daily_forward（前复权）。

用法（仓库根）:
    python3 backend/scripts/build_factor_custom_dataset.py --start 2020-01-01
    python3 backend/scripts/build_factor_custom_dataset.py --smoke 300    # 冒烟
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

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


def _limit_threshold(code: str) -> float:
    """按板块返回近似涨跌停阈值（复权价口径）。"""
    raw = str(code).split(".")[0]
    if raw.startswith(("300", "301", "688", "689")):
        return 0.198
    if raw.startswith(("4", "8", "92")):
        return 0.298
    return 0.098


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2020-01-01", help="起始日（含）")
    ap.add_argument("--smoke", type=int, default=0, help="冒烟：只取前 N 只股票")
    args = ap.parse_args()
    t0 = time.time()
    root = resolve_quantdb_dir()

    # 1) 筛选保留集（去 L2）
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
    total_cols = sum(len(v) for v in cols_by_lib.values())
    print(f"[1/5] 保留集(去L2)合并列数 {total_cols}: { {k: len(v) for k, v in cols_by_lib.items()} }")

    # 2) 股票池：全 A 非 ST/退市（静态口径）
    instr = pd.read_parquet(
        root / "2_base_sector" / "instrument_detail" / "instrument_detail.parquet",
        columns=["Symbol", "IsSTGP", "IsQuitGP"],
    )
    ok_sym = instr[
        (instr["IsSTGP"].astype(str) == "0") & (instr["IsQuitGP"].astype(str) == "0")
    ]["Symbol"].tolist()
    print(f"[2/5] 股票池（非 ST/退市）: {len(ok_sym)} 只")
    if args.smoke:
        ok_sym = ok_sym[: args.smoke]
    sym_index = pd.Index(ok_sym)
    threshold = sym_index.map(_limit_threshold).to_numpy(dtype=np.float64)

    # 3) 逐日合并（5 库 × 单日分区 → 1 个自定分区）
    dates = [
        d
        for d in sorted(
            p.name[3:] for p in (root / "1_kline_data" / "daily_forward").glob("dt=*")
        )
        if d >= args.start.replace("-", "")
    ]
    custom_root = Path(os.getenv("QM_QUANTCUSTOM_DATA_DIR") or "/data/quantcustom")
    out_root = custom_root / "6_ml_datasets" / "l1_factors"
    print(f"[3/5] 交易日 {len(dates)} 个（{dates[0]} ~ {dates[-1]}）→ {out_root}")

    _OHLCV = ("open", "high", "low", "close", "volume", "amount")

    def _ohlcv_of(dt: str) -> pd.DataFrame:
        d = pd.read_parquet(
            root / "1_kline_data" / "daily_forward" / f"dt={dt}" / "data.parquet",
            columns=["symbol", *_OHLCV],
        )
        return d.set_index("symbol").reindex(sym_index)[list(_OHLCV)]  # NaN 保留

    written = 0
    prev_close = None
    for i, dt in enumerate(dates):
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
        for c in ("open", "high", "low", "close", "volume", "amount"):
            cur[c] = ohlcv[c]
        if prev_close is not None:
            # 可交易性（标签 = 训练端由 close 计算的前向收益，置 NaN 即样本剔除）：
            #  当日涨停（买不进）或当日跌停（前一日标签卖不出）→ close(t) 置 NaN；
            #  同时使 t-1 的对应样本不可算（保守剔除，无虚假收益）。
            ret = ohlcv["close"] / prev_close - 1
            blocked = (ret.abs() >= threshold) | ohlcv["close"].isna() | prev_close.isna()
            cur["close"] = ohlcv["close"].where(~blocked)
        else:
            cur["close"] = ohlcv["close"]
        prev_close = ohlcv["close"]
        cur.insert(0, "date", pd.Timestamp(dt).strftime("%Y-%m-%d"))  # reader 需 YYYY-MM-DD
        cur = cur.reset_index().rename(columns={"index": "symbol"})
        cur = cur.replace([np.inf, -np.inf], np.nan)
        d_out = out_root / f"dt={dt}"
        d_out.mkdir(parents=True, exist_ok=True)
        cur.to_parquet(d_out / "data.parquet", index=False)
        written += 1
        if i % 200 == 0:
            print(f"      {dt} 已有 {written} 个分区（{time.time() - t0:.0f}s）")

    meta = {
        "dataset": "quantcustom/6_ml_datasets/l1_factors",
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "window": [dates[0], dates[-1]] if dates else None,
        "n_days": len(dates),
        "universe": "全 A 非 ST/退市（静态口径）",
        "n_factors": total_cols,
        "factors_by_source": {k: len(v) for k, v in cols_by_lib.items()},
        "exclusions": "当日涨停行 close 置 NaN（标签不可算 = 样本剔除）；次日跌停由标签自然不可实现未单列",
        "conventions": "close=daily_forward 前复权；因子缺失 NaN 由训练端截面预处理（中位数填充+1%/99%缩尾+Z-score）处理",
    }
    (out_root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    print(f"[5/5] 完成 → {out_root}（{written} 分区，总 {time.time() - t0:.0f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
