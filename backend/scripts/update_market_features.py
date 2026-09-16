#!/usr/bin/env python3
"""多市场特征工程 — 从各 Quant 平台 parquet 计算 OHLCV 特征并保存为 parquet。

支持加密货币、港股、美股市场。复用 update_feature_parquet.py 中的
compute_features_for_group() 计算纯 OHLCV 特征，跳过 A 股特有列。

用法:
    python update_market_features.py --market crypto
    python update_market_features.py --market hong_kong
    python update_market_features.py --market us_stock
    python update_market_features.py --market crypto --rebuild
    python update_market_features.py --market crypto --dry-run
"""

import argparse
import os
import sys
import warnings
from datetime import date, datetime
from pathlib import Path

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=RuntimeWarning)

# 容器内 vs 主机：优先用脚本位置推断，回退到 /app
_script_root = Path(__file__).resolve().parents[2]
if (_script_root / "db").is_dir():
    PROJECT_ROOT = _script_root
elif Path("/app/db").is_dir():
    PROJECT_ROOT = Path("/app")
else:
    PROJECT_ROOT = _script_root

FEATURE_SNAPSHOT_DIR = PROJECT_ROOT / "db" / "feature_snapshots"
FEATURE_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

# 市场 → 输出 parquet 文件名
MARKET_PARQUET_NAMES = {
    "crypto": "model_features_crypto.parquet",
    "hong_kong": "model_features_hk.parquet",
    "us_stock": "model_features_us.parquet",
    "futures": "model_features_futures.parquet",
}


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def load_crypto_parquet() -> pd.DataFrame:
    """从 QuantBC daily_forward parquet 加载加密货币日线（替代旧 H5）。"""
    from backend.services.engine.data_platform.quantbc_hub import QuantBCDataHub

    hub = QuantBCDataHub()
    data_dir = hub.data_dir
    fwd_dir = data_dir / "1_kline_data" / "daily_forward"
    if not fwd_dir.is_dir():
        raise FileNotFoundError(f"QuantBC 数据目录不可用: {fwd_dir}")

    _log(f"读取 QuantBC parquet: {fwd_dir}")
    import glob as _glob

    files = sorted(_glob.glob(str(fwd_dir / "dt=*" / "data.parquet")))
    if not files:
        raise FileNotFoundError(f"QuantBC 无日K分区: {fwd_dir}")
    _log(f"  日K分区: {len(files)} 个")

    frames = []
    for f in files:
        try:
            chunk = pd.read_parquet(f, engine="pyarrow")
            frames.append(chunk)
        except Exception as e:  # noqa: BLE001
            _log(f"  跳过分区 {f}: {e}")
    if not frames:
        raise RuntimeError("QuantBC 日K 全部读取失败")

    df = pd.concat(frames, ignore_index=True)
    _log(f"  合并 {len(df):,} 行")

    # symbol -> instrument（如 BTCUSDT）
    if "symbol" in df.columns:
        df = df.rename(columns={"symbol": "instrument"})
    elif "instrument" not in df.columns:
        raise RuntimeError("QuantBC parquet 缺少 symbol/instrument 列")

    if "time" in df.columns:
        df["trade_date"] = pd.to_datetime(df["time"], errors="coerce").dt.date
        df = df.drop(columns=["time"])
    if "trade_date" not in df.columns:
        raise RuntimeError("QuantBC parquet 缺少 time/trade_date 列")

    df = df.dropna(subset=["trade_date", "close"])

    if "amount" not in df.columns:
        df["amount"] = df["close"] * df["volume"]
    if "adj_factor" not in df.columns:
        df["adj_factor"] = 1.0
    if "turnover_rate" not in df.columns:
        df["turnover_rate"] = 0.0

    # A 股特有列填 0 / NaN
    for col in ["pe_ttm", "pb", "roe", "bp", "ep_ttm", "float_mv", "total_mv"]:
        if col not in df.columns:
            df[col] = 0.0
    if "ln_mv_total" not in df.columns:
        df["ln_mv_total"] = np.log(df["amount"].clip(lower=1))
    for col in ["industry", "is_st", "listing_market"]:
        if col not in df.columns:
            df[col] = ""
    for col in ["return_1d", "return_5d", "return_20d", "ma5", "ma20", "ma60",
                "rsi_14", "kdj_k", "macd_hist", "vol_std_20", "vol_atr_14",
                "beta_20", "flow_net_amount", "volume_ma_5", "amount_ma_5"]:
        if col not in df.columns:
            df[col] = np.nan

    _log(f"  加载 {len(df):,} 行, {df['instrument'].nunique()} 个标的")
    _log(f"  日期范围: {df['trade_date'].min()} ~ {df['trade_date'].max()}")
    return df


def load_futures_parquet() -> pd.DataFrame:
    """从 QuantFutures parquet 加载期货数据（无 H5，直接读 daily_forward 分区）。

    期货日K 是标准 10 列（symbol/time/open/high/low/close/volume/amount），
    符号如 RB0.CN / CL.FUT / Au99.99。统一为 instrument + trade_date 列。
    """
    from backend.services.engine.data_platform.quantfutures_hub import QuantFuturesDataHub

    hub = QuantFuturesDataHub.get_instance()
    data_dir = hub.data_dir
    fwd_dir = data_dir / "1_kline_data" / "daily_forward"
    if not fwd_dir.is_dir():
        raise FileNotFoundError(f"QuantFutures 数据目录不可用: {fwd_dir}")

    _log(f"读取 QuantFutures parquet: {fwd_dir}")
    import glob as _glob

    files = sorted(_glob.glob(str(fwd_dir / "dt=*" / "data.parquet")))
    if not files:
        raise FileNotFoundError(f"QuantFutures 无日K分区: {fwd_dir}")

    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f, engine="pyarrow"))
        except Exception as e:  # noqa: BLE001
            _log(f"  跳过分区 {f}: {e}")
    if not frames:
        raise RuntimeError("QuantFutures 日K 全部读取失败")

    df = pd.concat(frames, ignore_index=True)

    # symbol -> instrument（保持期货原生代码）
    if "symbol" in df.columns:
        df = df.rename(columns={"symbol": "instrument"})
    elif "instrument" not in df.columns:
        raise RuntimeError("期货 parquet 缺少 symbol/instrument 列")

    # time -> trade_date
    if "time" in df.columns:
        df["trade_date"] = pd.to_datetime(df["time"], errors="coerce").dt.date
        df = df.drop(columns=["time"])
    if "trade_date" not in df.columns:
        raise RuntimeError("期货 parquet 缺少 time/trade_date 列")

    df = df.dropna(subset=["trade_date", "close"])

    # amount 兜底
    if "amount" not in df.columns:
        df["amount"] = df["close"] * df["volume"]
    if "adj_factor" not in df.columns:
        df["adj_factor"] = 1.0
    if "turnover_rate" not in df.columns:
        df["turnover_rate"] = 0.0

    # A 股特有列填 0 / NaN
    for col in ["pe_ttm", "pb", "roe", "bp", "ep_ttm", "float_mv", "total_mv"]:
        if col not in df.columns:
            df[col] = 0.0
    if "ln_mv_total" not in df.columns:
        df["ln_mv_total"] = np.log(df["amount"].clip(lower=1))
    for col in ["industry", "is_st", "listing_market"]:
        if col not in df.columns:
            df[col] = ""
    for col in ["return_1d", "return_5d", "return_20d", "ma5", "ma20", "ma60",
                "rsi_14", "kdj_k", "macd_hist", "vol_std_20", "vol_atr_14",
                "beta_20", "flow_net_amount", "volume_ma_5", "amount_ma_5"]:
        if col not in df.columns:
            df[col] = np.nan

    _log(f"  加载 {len(df):,} 行, {df['instrument'].nunique()} 个标的")
    _log(f"  日期范围: {df['trade_date'].min()} ~ {df['trade_date'].max()}")
    return df


def load_us_parquet() -> pd.DataFrame:
    """从 QuantUS dt 分区 parquet 加载美股日线（2001 起，替代旧 H5）。

    dt 分区是"每日期截面"（symbol × time 全市场），需要转成长表
    (instrument, trade_date)，供 compute_features_for_group 按标的计算。
    """
    from backend.services.engine.data_platform.quantus_hub import QuantUSDataHub

    hub = QuantUSDataHub()
    data_dir = hub.data_dir
    fwd_dir = data_dir / "1_kline_data" / "daily_forward"
    if not fwd_dir.is_dir():
        raise FileNotFoundError(f"QuantUS 数据目录不可用: {fwd_dir}")

    _log(f"读取 QuantUS parquet: {fwd_dir}")
    import glob as _glob

    files = sorted(_glob.glob(str(fwd_dir / "dt=*" / "data.parquet")))
    if not files:
        raise FileNotFoundError(f"QuantUS 无日K分区: {fwd_dir}")
    _log(f"  日K分区: {len(files)} 个, {files[0].split('=')[-1]} ~ {files[-1].split('=')[-1]}")

    frames = []
    for f in files:
        try:
            chunk = pd.read_parquet(f, engine="pyarrow")
            frames.append(chunk)
        except Exception as e:  # noqa: BLE001
            _log(f"  跳过分区 {f}: {e}")
    if not frames:
        raise RuntimeError("QuantUS 日K 全部读取失败")

    df = pd.concat(frames, ignore_index=True)
    _log(f"  合并 {len(df):,} 行")

    # symbol -> instrument（美股 symbol 原样，如 AAPL）
    if "symbol" in df.columns:
        df = df.rename(columns={"symbol": "instrument"})
    elif "instrument" not in df.columns:
        raise RuntimeError("QuantUS parquet 缺少 symbol/instrument 列")

    # time -> trade_date
    if "time" in df.columns:
        df["trade_date"] = pd.to_datetime(df["time"], errors="coerce").dt.date
        df = df.drop(columns=["time"])
    if "trade_date" not in df.columns:
        raise RuntimeError("QuantUS parquet 缺少 time/trade_date 列")

    df = df.dropna(subset=["trade_date", "close"])

    # amount 兜底
    if "amount" not in df.columns:
        df["amount"] = df["close"] * df["volume"]
    if "adj_factor" not in df.columns:
        df["adj_factor"] = 1.0
    if "turnover_rate" not in df.columns:
        df["turnover_rate"] = 0.0

    # A 股特有列填 0 / NaN
    for col in ["pe_ttm", "pb", "roe", "bp", "ep_ttm", "float_mv", "total_mv"]:
        if col not in df.columns:
            df[col] = 0.0
    if "ln_mv_total" not in df.columns:
        df["ln_mv_total"] = np.log(df["amount"].clip(lower=1))
    for col in ["industry", "is_st", "listing_market"]:
        if col not in df.columns:
            df[col] = ""
    for col in ["return_1d", "return_5d", "return_20d", "ma5", "ma20", "ma60",
                "rsi_14", "kdj_k", "macd_hist", "vol_std_20", "vol_atr_14",
                "beta_20", "flow_net_amount", "volume_ma_5", "amount_ma_5"]:
        if col not in df.columns:
            df[col] = np.nan

    _log(f"  加载 {len(df):,} 行, {df['instrument'].nunique()} 个标的")
    _log(f"  日期范围: {df['trade_date'].min()} ~ {df['trade_date'].max()}")
    return df


def load_hk_parquet(start_year: int | None = None) -> pd.DataFrame:
    """从 QuantHK dt 分区 parquet 加载港股日线（替代旧 H5）。

    QuantHK parquet 与 QuantUS 结构相同：dt 分区"每日期截面"
    （symbol × time 全市场），symbol 如 0823.HK。转成长表 (instrument, trade_date)。

    start_year: 仅读取该年份及之后的分区（如 2010 只读 2010 起），
                避免 1980 年起全量读导致内存/耗时过大。
    """
    from backend.services.engine.data_platform.quanthk_hub import QuantHKDataHub

    hub = QuantHKDataHub()
    data_dir = hub.data_dir
    fwd_dir = data_dir / "1_kline_data" / "daily_forward"
    if not fwd_dir.is_dir():
        raise FileNotFoundError(f"QuantHK 数据目录不可用: {fwd_dir}")

    _log(f"读取 QuantHK parquet: {fwd_dir}")
    import glob as _glob

    files = sorted(_glob.glob(str(fwd_dir / "dt=*" / "data.parquet")))
    if not files:
        raise FileNotFoundError(f"QuantHK 无日K分区: {fwd_dir}")
    _log(f"  日K分区: {len(files)} 个, {files[0].split('=')[-1]} ~ {files[-1].split('=')[-1]}")

    if start_year:
        before = len(files)
        files = [f for f in files if int(f.split("dt=")[-1].split("/")[0][:4]) >= start_year]
        _log(f"  过滤 {start_year} 起: {before} -> {len(files)} 个分区")

    frames = []
    for f in files:
        try:
            chunk = pd.read_parquet(f, engine="pyarrow")
            frames.append(chunk)
        except Exception as e:  # noqa: BLE001
            _log(f"  跳过分区 {f}: {e}")
    if not frames:
        raise RuntimeError("QuantHK 日K 全部读取失败")

    df = pd.concat(frames, ignore_index=True)
    _log(f"  合并 {len(df):,} 行")

    # symbol -> instrument（港股 symbol 保留 .HK 后缀，如 0823.HK）
    if "symbol" in df.columns:
        df = df.rename(columns={"symbol": "instrument"})
    elif "instrument" not in df.columns:
        raise RuntimeError("QuantHK parquet 缺少 symbol/instrument 列")

    # time -> trade_date
    if "time" in df.columns:
        df["trade_date"] = pd.to_datetime(df["time"], errors="coerce").dt.date
        df = df.drop(columns=["time"])
    if "trade_date" not in df.columns:
        raise RuntimeError("QuantHK parquet 缺少 time/trade_date 列")

    df = df.dropna(subset=["trade_date", "close"])

    # amount 兜底
    if "amount" not in df.columns:
        df["amount"] = df["close"] * df["volume"]
    if "adj_factor" not in df.columns:
        df["adj_factor"] = 1.0
    if "turnover_rate" not in df.columns:
        df["turnover_rate"] = 0.0

    # A 股特有列填 0 / NaN
    for col in ["pe_ttm", "pb", "roe", "bp", "ep_ttm", "float_mv", "total_mv"]:
        if col not in df.columns:
            df[col] = 0.0
    if "ln_mv_total" not in df.columns:
        df["ln_mv_total"] = np.log(df["amount"].clip(lower=1))
    for col in ["industry", "is_st", "listing_market"]:
        if col not in df.columns:
            df[col] = ""
    for col in ["return_1d", "return_5d", "return_20d", "ma5", "ma20", "ma60",
                "rsi_14", "kdj_k", "macd_hist", "vol_std_20", "vol_atr_14",
                "beta_20", "flow_net_amount", "volume_ma_5", "amount_ma_5"]:
        if col not in df.columns:
            df[col] = np.nan

    _log(f"  加载 {len(df):,} 行, {df['instrument'].nunique()} 个标的")
    _log(f"  日期范围: {df['trade_date'].min()} ~ {df['trade_date'].max()}")
    return df


def _coerce_batch_types(df: pd.DataFrame) -> None:
    """统一分类列类型，避免 parquet 写入 mixed type 报错（is_st 空串→int、
    industry 对象混型→str）。"""
    for col in ("is_st", "listing_market"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    if "industry" in df.columns and df["industry"].dtype == object:
        mask = df["industry"].notna()
        df.loc[mask, "industry"] = df.loc[mask, "industry"].astype(str)
        df.loc[~mask, "industry"] = None


def compute_market_features(df: pd.DataFrame, market: str, batch_size: int = 100, final_path: Path | None = None) -> pd.DataFrame:
    """为所有标的计算特征。

    分批计算（每 batch_size 个标的写一次临时 parquet），避免全量累积
    导致 OOM —— 港股 2200+ 标的 × 全历史时内存峰值极大。

    final_path: 若给定，合并临时文件后直接写该路径并返回轻量占位，
                避免大表全量回读内存。
    """
    from backend.shared.feature_defs import compute_features_for_group

    # industry 在部分市场/时段是 object 混型（字符串+数字），统一规整为 str
    # 再进入 pyarrow 落盘，避免 ArrowTypeError
    if "industry" in df.columns and df["industry"].dtype == object:
        df = df.copy()
        mask = df["industry"].notna()
        df.loc[mask, "industry"] = df.loc[mask, "industry"].astype(str)
        df.loc[~mask, "industry"] = None

    instruments = df["instrument"].unique()
    total = len(instruments)
    _log(f"  计算特征（{total} 个标的，批大小 {batch_size}）...")

    temp_files: list[Path] = []
    results: list[pd.DataFrame] = []
    for i, (sym, group) in enumerate(df.groupby("instrument"), 1):
        try:
            feat = compute_features_for_group(group)
            results.append(feat)
        except Exception as e:
            _log(f"    跳过 {sym}: {e}")

        # 每批写盘释放内存
        if len(results) >= batch_size:
            try:
                batch_df = pd.concat(results, ignore_index=True)
                _coerce_batch_types(batch_df)
                tmp = FEATURE_SNAPSHOT_DIR / f".{market}_features_tmp_{len(temp_files)}.parquet"
                batch_df.to_parquet(str(tmp), index=False, engine="pyarrow")
                temp_files.append(tmp)
                _log(f"    批 {len(temp_files)}: 已写 {len(batch_df):,} 行")
            finally:
                results.clear()

        if i % 50 == 0:
            _log(f"    进度: {i}/{total}")

    # 收尾：剩余批次写盘
    if results:
        try:
            batch_df = pd.concat(results, ignore_index=True)
            _coerce_batch_types(batch_df)
            tmp = FEATURE_SNAPSHOT_DIR / f".{market}_features_tmp_{len(temp_files)}.parquet"
            batch_df.to_parquet(str(tmp), index=False, engine="pyarrow")
            temp_files.append(tmp)
            _log(f"    尾批: 已写 {len(batch_df):,} 行")
        finally:
            results.clear()

    if not temp_files:
        return pd.DataFrame()

    # 合并所有临时文件：流式写入，避免全量 pd.concat 导致 OOM
    # （27 个 ~300MB 临时文件 concat 时内存峰值可达 50GB+）
    _log(f"  合并 {len(temp_files)} 个临时文件（流式写入）...")
    import pyarrow as pa
    import pyarrow.parquet as pq_writer

    merged_path = FEATURE_SNAPSHOT_DIR / f".{market}_features_merged.parquet"

    # 先扫描所有临时文件的列集合，得到统一列序，避免 schema 不匹配
    all_columns: list[str] = []
    col_seen: set[str] = set()
    for t in temp_files:
        cols = pd.read_parquet(str(t), engine="pyarrow").columns
        for c in cols:
            if c not in col_seen:
                col_seen.add(c)
                all_columns.append(c)
    _log(f"  统一列序: {len(all_columns)} 列")

    writer = None
    first_schema = None
    try:
        for idx, t in enumerate(temp_files):
            chunk = pd.read_parquet(str(t), engine="pyarrow")
            # 按统一列序对齐，缺失列填 NaN/0
            for c in all_columns:
                if c not in chunk.columns:
                    chunk[c] = 0
            chunk = chunk[all_columns]
            _coerce_batch_types(chunk)
            table = pa.Table.from_pandas(chunk, preserve_index=False)
            if writer is None:
                first_schema = table.schema
                writer = pq_writer.ParquetWriter(str(merged_path), table.schema)
            else:
                # 各批列类型可能不一致（int64 vs double），统一 cast 到首批 schema
                try:
                    table = table.cast(first_schema, safe=False)
                except Exception:
                    # cast 失败则逐列转 string 兜底
                    table = table.cast(pa.schema([pa.field(n, pa.string()) for n in all_columns]), safe=False)
            writer.write_table(table)
            del chunk, table
            _log(f"    合并 {idx + 1}/{len(temp_files)}: {t.stat().st_size / 1024 / 1024:.0f}MB")
    finally:
        if writer is not None:
            writer.close()

    # 清理临时文件
    for t in temp_files:
        try:
            t.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    # 大表场景：合并临时文件后直接流式写最终 parquet，避免全量回读内存
    if final_path is not None:
        import shutil
        try:
            shutil.copy2(str(merged_path), str(final_path))
            os.remove(str(merged_path))
        except Exception:
            # copy 失败则回退到流式重写
            import pyarrow as pa
            import pyarrow.parquet as pq_writer
            pq_writer.write_table(pa.Table.from_pandas(pd.read_parquet(str(merged_path), engine="pyarrow"), preserve_index=False), str(final_path))
            os.remove(str(merged_path))
        # 返回轻量信息：读取 parquet 元数据验证可读性，而非全量数据
        try:
            import pyarrow.parquet as pq_file
            pq_file.ParquetFile(str(final_path))
            return pd.DataFrame({"__placeholder__": [0]})
        except Exception:
            return pd.DataFrame({"__placeholder__": [0]})

    all_feat = pd.read_parquet(str(merged_path), engine="pyarrow")

    # 确保分类列类型一致（避免 parquet 写入时 mixed type 报错）
    if "is_st" in all_feat.columns:
        all_feat["is_st"] = pd.to_numeric(all_feat["is_st"], errors="coerce").fillna(0).astype(int)

    # 清理 A 股特有列（保留但全为 0 的列可以删掉以减小文件）
    drop_cols = [
        "industry", "listing_market",
        "idx_all", "idx_hs300", "idx_zz1000", "idx_chinext", "idx_margin",
        "concept_ai", "concept_chip", "concept_new_energy", "concept_pv",
        "concept_military", "concept_medical", "concept_fintech",
        "concept_consumption", "concept_state_owned", "concept_lithium",
    ]
    for col in drop_cols:
        if col in all_feat.columns:
            # Convert to numeric before comparison to avoid mixed-type issues
            numeric_col = pd.to_numeric(all_feat[col], errors="coerce").fillna(0)
            if (numeric_col == 0).all():
                all_feat = all_feat.drop(columns=[col])

    return all_feat


def _verify_snapshot(parquet_path: Path) -> None:
    """验证最终 parquet：行数/列数/最新日期覆盖。轻量读取 metadata。"""
    try:
        import pyarrow.parquet as pq_file

        pf = pq_file.ParquetFile(str(parquet_path))
        _log(f"验证: {pf.metadata.num_rows:,} 行, {pf.metadata.num_columns} 列, "
             f"{parquet_path.stat().st_size / 1024 / 1024:.0f}MB")
    except Exception as exc:  # noqa: BLE001
        _log(f"  WARN: 验证失败: {exc}")


def main():
    parser = argparse.ArgumentParser(description="多市场特征工程")
    parser.add_argument("--market", required=True, choices=["crypto", "hong_kong", "us_stock", "futures"],
                        help="市场: crypto, hong_kong, us_stock, futures")
    parser.add_argument("--rebuild", action="store_true", help="重建全部特征")
    parser.add_argument("--dry-run", action="store_true", help="仅检查不写入")
    parser.add_argument("--start-year", type=int, default=None,
                        help="仅处理该年份及之后的数据（港股默认 2010 起，避免 1980 全量）")
    args = parser.parse_args()

    market = args.market
    parquet_name = MARKET_PARQUET_NAMES[market]
    parquet_path = FEATURE_SNAPSHOT_DIR / parquet_name

    _log(f"市场: {market}")
    _log(f"输出: {parquet_path}")

    # 加载数据（各市场走各自 Quant 平台 parquet 单源）
    if market == "futures":
        df = load_futures_parquet()
    elif market == "us_stock":
        df = load_us_parquet()
    elif market == "hong_kong":
        # 港股默认 2010 起，避免 1980 全量读取
        hk_start = args.start_year or 2010
        df = load_hk_parquet(start_year=hk_start)
    elif market == "crypto":
        df = load_crypto_parquet()
    else:
        _log(f"ERROR: 不支持的市场: {market}")
        sys.exit(1)

    if df.empty:
        _log(f"ERROR: {market} 数据为空")
        sys.exit(1)

    # 增量模式：检查现有 parquet
    existing = None
    if parquet_path.exists() and not args.rebuild:
        _log(f"读取现有 parquet: {parquet_path}")
        existing = pd.read_parquet(str(parquet_path), engine="pyarrow")
        existing["trade_date"] = pd.to_datetime(existing["trade_date"]).dt.date
        max_date = existing["trade_date"].max()
        _log(f"  现有数据: {len(existing):,} 行, 最新日期: {max_date}")

        # 只计算新数据
        new_dates = sorted(df["trade_date"].unique())
        new_dates = [d for d in new_dates if d > max_date]
        if not new_dates:
            _log("无需更新（parquet 已是最新）")
            return
        _log(f"  需要计算: {len(new_dates)} 天新数据")
        df = df[df["trade_date"].isin(new_dates)]

    if args.dry_run:
        _log(f"DRY RUN: 将计算 {len(df):,} 行, {df['instrument'].nunique()} 个标的")
        return

    # 计算特征（港股全量首次：直接流式写最终 parquet，避免全量回读 OOM）
    if market == "hong_kong" and existing is None and not args.dry_run:
        new_data = compute_market_features(df, market, final_path=parquet_path)
        _log(f"  计算完成: 已写最终 parquet -> {parquet_path.name}")
        _verify_snapshot(parquet_path)
        return
    new_data = compute_market_features(df, market)
    _log(f"  计算完成: {len(new_data):,} 行, {len(new_data.columns)} 列")

    if new_data.empty:
        _log("没有有效数据")
        return

    # 合并
    if existing is not None and not args.rebuild:
        # 对齐列
        all_cols = list(dict.fromkeys(list(existing.columns) + [c for c in new_data.columns if c not in existing.columns]))
        for c in all_cols:
            if c not in existing.columns:
                existing[c] = 0
            if c not in new_data.columns:
                new_data[c] = 0
        existing = existing[all_cols]
        new_data = new_data[all_cols]
        combined = pd.concat([existing, new_data], ignore_index=True)
    else:
        combined = new_data

    # 大表（港股全量数百万行）避免 sort_values 全量重排导致 OOM：
    # 跳过排序，写入阶段按需分组即可。
    if len(combined) > 2_000_000:
        _log(f"  跳过 sort_values（{len(combined):,} 行大表，避免 OOM）")
    else:
        combined = combined.sort_values(["trade_date", "instrument"]).reset_index(drop=True)

    # 宏观因子合并（美股全市场共用）
    if market == "us_stock":
        try:
            macro_path = PROJECT_ROOT / "data" / "quantus" / "5_technical_derived" / "macro_usa" / "macro_usa.parquet"
            if not macro_path.exists():
                macro_path = Path("/data/quantus/5_technical_derived/macro_usa/macro_usa.parquet")
            if macro_path.exists():
                macro = pd.read_parquet(str(macro_path), engine="pyarrow")
                macro["trade_date"] = pd.to_datetime(macro["trade_date"]).dt.date
                combined["_td"] = pd.to_datetime(combined["trade_date"]).dt.date
                combined = combined.merge(macro, left_on="_td", right_on="trade_date", how="left", suffixes=("", "_macro"))
                combined = combined.drop(columns=["trade_date_macro", "_td"])
                macro_cols = [c for c in macro.columns if c != "trade_date"]
                _log(f"  合并宏观因子: {len(macro_cols)} 列 {macro_cols}")
            else:
                _log("  WARN: 宏观因子 parquet 不存在，跳过")
        except Exception as exc:  # noqa: BLE001
            _log(f"  WARN: 宏观因子合并失败: {exc}")

    _log(f"合并后: {len(combined):,} 行, {len(combined.columns)} 列")
    _log(f"日期: {combined['trade_date'].min()} ~ {combined['trade_date'].max()}")

    # 写入（大表用 pyarrow 流式写，避免 pandas 全量驻留内存）
    if len(combined) > 2_000_000:
        import pyarrow as pa
        import pyarrow.parquet as pq_writer

        _log(f"  大表 {len(combined):,} 行，用 pyarrow 流式写入...")
        _coerce_batch_types(combined)
        table = pa.Table.from_pandas(combined, preserve_index=False)
        pq_writer.write_table(table, str(parquet_path), compression="snappy")
        _log(f"已写入: {parquet_path} ({parquet_path.stat().st_size / 1024 / 1024:.1f}MB)")
    else:
        combined.to_parquet(str(parquet_path), index=False, engine="pyarrow")
        _log(f"已写入: {parquet_path} ({parquet_path.stat().st_size / 1024 / 1024:.1f}MB)")

    # 验证
    verify = pd.read_parquet(str(parquet_path), engine="pyarrow")
    _log(f"验证: {len(verify):,} 行, {len(verify.columns)} 列")

    # 特征覆盖检查
    latest = verify[verify["trade_date"] == verify["trade_date"].max()]
    _log(f"最新日期 {len(latest)} 个标的:")
    for col_group, cols in [
        ("OHLCV", ["open", "high", "low", "close", "volume"]),
        ("动量", ["mom_ret_1d", "mom_ret_5d", "mom_rsi_14"]),
        ("波动率", ["vol_std_20", "vol_atr_14"]),
        ("流动性", ["liq_volume", "liq_amihud_20"]),
        ("资金流", ["flow_net_amount", "flow_vpin"]),
    ]:
        coverage = []
        for col in cols:
            if col in latest.columns:
                non_null = latest[col].notna().sum()
                coverage.append(f"{col}={non_null}")
        _log(f"  [{col_group}] {', '.join(coverage)}")

    _log("完成!")


if __name__ == "__main__":
    main()
