"""
merge_ohlcv_to_training_data.py
从 qlib_data 提取 OHLCV + 复权因子，合并写入 feature_snapshots 的每年 parquet。

用法（在服务器 engine 容器内运行）：
  python3 /app/scripts/merge_ohlcv_to_training_data.py

新增列：
  open, high, low, close, volume, factor
  若 parquet 中已存在同名列则跳过（幂等）。
"""

import struct
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── 路径配置 ────────────────────────────────────────────────────────────────
QLIB_DIR   = Path("/app/db/qlib_data")
DATA_DIR   = Path("/app/db/feature_snapshots")
CALENDAR   = QLIB_DIR / "calendars" / "day.txt"
FEATURES_DIR = QLIB_DIR / "features"
FIELDS     = ["open", "high", "low", "close", "volume", "factor"]


def load_calendar() -> list[str]:
    return [l.strip() for l in CALENDAR.read_text().splitlines() if l.strip()]


def read_bin(path: Path, calendar: list[str]) -> tuple[list[str], np.ndarray]:
    """读取 qlib bin 文件，返回 (dates, values)。"""
    raw = path.read_bytes()
    if len(raw) < 4:
        return [], np.array([], dtype=np.float32)
    start_idx = struct.unpack("<I", raw[:4])[0]
    arr = np.frombuffer(raw[4:], dtype="<f4").copy()
    # 对齐日历：截断超出范围的部分
    n = min(len(arr), len(calendar) - start_idx)
    if n <= 0:
        return [], np.array([], dtype=np.float32)
    dates = calendar[start_idx: start_idx + n]
    return dates, arr[:n]


def build_ohlcv_df(calendar: list[str]) -> pd.DataFrame:
    """遍历所有 symbol，构建完整 OHLCV DataFrame。"""
    symbols = sorted(os.listdir(FEATURES_DIR))
    logger.info("共 %d 个 symbol，开始解析 OHLCV...", len(symbols))

    chunks: list[pd.DataFrame] = []
    missing_count = 0

    for i, sym in enumerate(symbols):
        sym_dir = FEATURES_DIR / sym
        field_paths = {f: sym_dir / f"{f}.day.bin" for f in FIELDS}

        # 以 close 为基准确认该 symbol 的日期序列
        close_path = field_paths["close"]
        if not close_path.exists():
            missing_count += 1
            continue

        dates, close_arr = read_bin(close_path, calendar)
        if len(dates) == 0:
            missing_count += 1
            continue

        data = {
            "symbol": sym.upper(),           # 统一为大写
            "trade_date": pd.to_datetime(dates).date,  # 与 parquet 保持一致
            "close": close_arr.astype(np.float32),
        }

        for field in ["open", "high", "low", "volume", "factor"]:
            p = field_paths[field]
            if p.exists():
                _, arr = read_bin(p, calendar)
                # 对齐长度（以 close 为准）
                n = len(dates)
                if len(arr) >= n:
                    data[field] = arr[:n].astype(np.float32)
                else:
                    padded = np.full(n, np.nan, dtype=np.float32)
                    padded[:len(arr)] = arr
                    data[field] = padded
            else:
                data[field] = np.full(len(dates), np.nan, dtype=np.float32)

        chunks.append(pd.DataFrame(data))

        if (i + 1) % 500 == 0:
            logger.info("  进度: %d / %d symbols", i + 1, len(symbols))

    logger.info("解析完成，缺失 %d 个 symbol。合并中...", missing_count)
    df = pd.concat(chunks, ignore_index=True)

    # 过滤全 NaN 行（停牌/未上市）
    df = df.dropna(subset=["close"])
    logger.info("OHLCV DataFrame: %d 行, %d symbols", len(df), df["symbol"].nunique())
    return df


def merge_year(year: int, ohlcv_df: pd.DataFrame) -> dict:
    """将 OHLCV 数据合并进指定年份的 parquet，返回合并统计。"""
    parquet_path = DATA_DIR / f"train_ready_{year}.parquet"
    if not parquet_path.exists():
        logger.warning("parquet 不存在，跳过：%s", parquet_path)
        return {"year": year, "status": "skip", "reason": "parquet not found"}

    df = pd.read_parquet(parquet_path)
    original_cols = set(df.columns)

    # 检查是否已有所有 OHLCV 字段（幂等保护）
    new_fields = [f for f in FIELDS if f not in original_cols]
    if not new_fields:
        logger.info("年份 %d：所有 OHLCV 字段已存在，跳过。", year)
        return {"year": year, "status": "skip", "reason": "already merged"}

    logger.info("年份 %d：合并字段 %s（原始 %d 行）", year, new_fields, len(df))

    # 过滤当年 OHLCV 数据（加速 merge）
    year_ohlcv = ohlcv_df[
        pd.to_datetime(ohlcv_df["trade_date"]).dt.year == year
    ][["symbol", "trade_date"] + FIELDS].copy()

    # 统一 trade_date 类型为 date
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    year_ohlcv["trade_date"] = pd.to_datetime(year_ohlcv["trade_date"]).dt.date

    merged = df.merge(
        year_ohlcv[["symbol", "trade_date"] + new_fields],
        on=["symbol", "trade_date"],
        how="left",
    )

    # 统计合并命中率
    n_total = len(merged)
    n_hit = merged[new_fields[0]].notna().sum()
    hit_rate = n_hit / n_total if n_total > 0 else 0

    # 写回原路径（覆盖）
    merged.to_parquet(parquet_path, index=False, engine="pyarrow", compression="snappy")

    logger.info(
        "年份 %d：合并完成，命中率 %.1f%% (%d/%d 行)，新字段 %s",
        year, hit_rate * 100, n_hit, n_total, new_fields,
    )
    return {
        "year": year,
        "status": "merged",
        "new_fields": new_fields,
        "rows": n_total,
        "hit_rate": round(hit_rate, 4),
    }


def main():
    logger.info("=== OHLCV 合并任务启动 ===")
    logger.info("数据源: %s", QLIB_DIR)
    logger.info("目标:   %s", DATA_DIR)

    calendar = load_calendar()
    logger.info("日历: %d 天（%s ~ %s）", len(calendar), calendar[0], calendar[-1])

    # 构建全量 OHLCV（内存约 1-2 GB，可接受）
    ohlcv_df = build_ohlcv_df(calendar)

    # 按年合并
    years = sorted(
        int(p.stem.replace("train_ready_", ""))
        for p in DATA_DIR.glob("train_ready_*.parquet")
    )
    logger.info("待合并年份: %s", years)

    results = []
    for year in years:
        result = merge_year(year, ohlcv_df)
        results.append(result)

    logger.info("=== 合并任务完成 ===")
    for r in results:
        logger.info("  %s", r)


if __name__ == "__main__":
    main()
