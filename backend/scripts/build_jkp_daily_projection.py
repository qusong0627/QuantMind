#!/usr/bin/env python3
"""JKP 月频因子库 → 日频投影库（月末值前向填充），供日频训练直读。

源：``<quantcustom>/6_ml_datasets/jkp_ashare_monthly``（月频，403 因子 + 标记列）
产：``<quantdb>/6_ml_datasets/jkp_ashare_daily``（日频，逐交易日落盘）

为什么产出落 **CN 市场根**（``/data/quantdb``）而不是 quantcustom：
「因子研究」页的市场白名单只有 CN/HK/US/FUTURES，扫的是各市场根目录下的
``6_ml_datasets``；放 quantcustom（训练页专用域）它天然看不见。该目录下现有
的 alpha360/alpha_library/jq110/tdxgs 等同样都是产出库，不是厂商原始数据。

口径
----
1. **前向填充不回填**：交易日 t 取「最近一个 ≤ t 的月末」的因子值。
   月末值由该日收盘数据算出，用于 t 当天的特征不会引入未来信息
   （标签仍由训练端用 close 前向收益构造）。
2. **陈旧上限**：距月末超过 ``--max-stale-days``（默认 45 自然日）即置空。
   正常月间隔 ≤ 23 个交易日；超限只可能是缺月，此时宁可缺特征也不给旧值。
3. **OHLCV 与训练集同口径**：取 ``daily_forward``（前复权），并复刻
   ``build_factor_custom_dataset`` 的可交易性剔除 —— 当日涨停/跌停或前收盘缺失
   时把 ``close`` 置 NaN（标签不可算 = 该样本剔除）。
4. ``date`` 写 ``YYYY-MM-DD`` 字符串（FactorReader 契约）。

用法（仓库根）
--------------
    python3 backend/scripts/build_jkp_daily_projection.py              # 增量
    python3 backend/scripts/build_jkp_daily_projection.py --full       # 全量重建
    python3 backend/scripts/build_jkp_daily_projection.py --smoke 60   # 只跑最近 60 个交易日
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.shared.quantdb_paths import resolve_quantdb_dir  # noqa: E402

logger = logging.getLogger("jkp_daily")

SRC_NAME = "jkp_ashare_monthly"
OUT_NAME = "jkp_ashare_daily"
OHLCV = ("open", "high", "low", "close", "volume", "amount")
FLAG_COLS = ("is_financial", "is_st")
#: 月频到日频的最大允许陈旧天数（自然日）。正常月间隔 ≤ 23 个交易日 ≈ 31–35 天；
#: 45 天留出春节长假与月末提前的余量，超过就只可能是缺月。
DEFAULT_MAX_STALE_DAYS = 45


def _custom_root() -> Path:
    import os

    env = os.environ.get("QM_QUANTCUSTOM_DATA_DIR")
    if env:
        return Path(env)
    cand = Path("/data/quantcustom")
    if cand.is_dir():
        return cand
    return _ROOT / "data" / "quantcustom"


def _cn_root() -> Path:
    """CN 市场根（因子研究 / 训练直读都扫这里）。"""
    return resolve_quantdb_dir()


def _month_blocks(src: Path) -> list[tuple[int, Path]]:
    out = []
    for p in sorted(src.glob("dt=*")):
        f = p / "data.parquet"
        if f.is_file():
            out.append((int(p.name[3:]), f))
    return out


def _factor_cols(path: Path) -> list[str]:
    names = pq.read_schema(path).names
    return [c for c in names if c not in ("symbol", "date", *OHLCV, *FLAG_COLS)]


def _trading_days(kline: Path) -> list[str]:
    return sorted(p.name[3:] for p in kline.glob("dt=*") if (p / "data.parquet").is_file())


def _limit_thresholds(symbols: pd.Index, dt: str) -> np.ndarray:
    """复用训练集唯一事实源（板别/创业板改革日/ST 的涨跌停比例）。"""
    try:
        from backend.scripts.build_factor_custom_dataset import _limit_thresholds as impl

        return impl(symbols, dt)
    except Exception as exc:  # noqa: BLE001 - 失败时退化为不剔除，并**响亮记录**
        logger.warning("涨跌停阈值不可用（%s），本日不做可交易性剔除", exc)
        return np.full(len(symbols), np.inf)


def build(
    *,
    out_root: str | None = None,
    full: bool = False,
    smoke: int = 0,
    start: str | None = None,
    max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
) -> int:
    qdb = resolve_quantdb_dir()
    src = _custom_root() / "6_ml_datasets" / SRC_NAME
    out = (_cn_root() if not out_root else Path(out_root)) / "6_ml_datasets" / OUT_NAME
    if not src.is_dir():
        raise SystemExit(f"源库不存在: {src}")

    blocks = _month_blocks(src)
    if not blocks:
        raise SystemExit(f"源库没有分区: {src}")
    factors = _factor_cols(blocks[0][1])
    logger.info("源 %d 个月末分区，%d 个因子列", len(blocks), len(factors))

    days = _trading_days(qdb / "1_kline_data" / "daily_forward")
    if start:
        s = start.replace("-", "")
        days = [d for d in days if d >= s]
    if smoke > 0:
        days = days[-smoke:]
    if not days:
        raise SystemExit("没有可写交易日")

    out.mkdir(parents=True, exist_ok=True)
    month_dates = [dt for dt, _ in blocks]
    block_cache: dict[int, pd.DataFrame] = {}
    prev_close: pd.Series | None = None
    written = skipped = 0
    t0 = time.time()

    for i, dt in enumerate(days):
        d_out = out / f"dt={dt}" / "data.parquet"
        if d_out.is_file() and not full:
            skipped += 1
            # 仍需维护 prev_close，否则后续交易日的涨跌停判定用的是隔日收盘
            prev_close = _read_close(qdb, days[i - 1]) if i > 0 else None
            continue

        k = _block_index(month_dates, int(dt))
        if k is None:
            prev_close = _read_close(qdb, dt) if i >= 0 else None
            continue
        if k not in block_cache:
            block_cache = {k: pd.read_parquet(blocks[k][1])}
        mblock = block_cache[k]
        mdate = month_dates[k]
        stale = (pd.Timestamp(dt) - pd.Timestamp(str(mdate))).days
        if stale > max_stale_days:
            logger.warning("%s 距最近月末 %s 已 %d 天，超过陈旧上限，跳过", dt, mdate, stale)
            prev_close = _read_close(qdb, dt)
            continue

        ohlcv = _read_ohlcv(qdb, dt)
        if ohlcv is None or ohlcv.empty:
            prev_close = None
            continue

        keep = [c for c in (*factors, *FLAG_COLS) if c in mblock.columns]
        cur = mblock.set_index("symbol")[keep].reindex(ohlcv.index)
        for c in OHLCV:
            cur[c] = ohlcv[c]
        if prev_close is not None:
            ret = ohlcv["close"] / prev_close.reindex(ohlcv.index) - 1
            thr = _limit_thresholds(ohlcv.index, dt)
            blocked = (
                (ret.abs() >= thr)
                | ohlcv["close"].isna()
                | prev_close.reindex(ohlcv.index).isna()
            )
            cur["close"] = ohlcv["close"].where(~blocked)
        cur.insert(0, "date", pd.Timestamp(dt).strftime("%Y-%m-%d"))
        cur = cur.reset_index().rename(columns={"index": "symbol"})
        cur = cur.replace([np.inf, -np.inf], np.nan)

        d_out.parent.mkdir(parents=True, exist_ok=True)
        cur.to_parquet(d_out, index=False)
        written += 1
        prev_close = ohlcv["close"]

        if written % 200 == 0:
            logger.info("  已写 %d 个分区（%d 跳过，%.0fs）", written, skipped, time.time() - t0)

    logger.info("完成：写 %d，跳过 %d，用时 %.0fs → %s", written, skipped, time.time() - t0, out)
    _write_meta(out, src, factors, len(days), max_stale_days)
    return 0


def _block_index(month_dates: list[int], dt: int) -> int | None:
    """最近一个 ≤ dt 的月末分区下标。"""
    lo, hi = 0, len(month_dates) - 1
    ans = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if month_dates[mid] <= dt:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def _read_ohlcv(qdb: Path, dt: str) -> pd.DataFrame | None:
    f = qdb / "1_kline_data" / "daily_forward" / f"dt={dt}" / "data.parquet"
    if not f.is_file():
        return None
    df = pd.read_parquet(f, columns=["symbol", *OHLCV])
    if df.empty:
        return None
    return df.set_index("symbol")


def _read_close(qdb: Path, dt: str) -> pd.Series | None:
    f = qdb / "1_kline_data" / "daily_forward" / f"dt={dt}" / "data.parquet"
    if not f.is_file():
        return None
    df = pd.read_parquet(f, columns=["symbol", "close"])
    return df.set_index("symbol")["close"] if not df.empty else None


def _write_meta(out: Path, src: Path, factors: list[str], days: int, stale: int) -> None:
    import json
    from datetime import datetime

    (out / "meta.json").write_text(
        json.dumps(
            {
                "source": str(src),
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "frequency": "daily",
                "fill": "月末值前向填充（不回填）",
                "max_stale_days": stale,
                "factor_columns": len(factors),
                "trading_days": days,
                "ohlcv": "daily_forward 前复权；涨跌停/前收缺失日 close=NaN（同 build_factor_custom_dataset）",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="忽略已存在分区，全部重写")
    ap.add_argument("--smoke", type=int, default=0, help="只跑最近 N 个交易日")
    ap.add_argument("--start", default=None, help="起始日 YYYY-MM-DD")
    ap.add_argument("--max-stale-days", type=int, default=DEFAULT_MAX_STALE_DAYS)
    ap.add_argument("--out-root", default=None, help="产出市场根，缺省 CN（/data/quantdb）")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    return build(out_root=args.out_root, full=args.full, smoke=args.smoke,
                 start=args.start, max_stale_days=args.max_stale_days)


if __name__ == "__main__":
    raise SystemExit(main())
