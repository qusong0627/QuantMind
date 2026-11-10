"""数据加载（P2 由 train.py 拆出，逐行搬运）。

本地 parquet / QuantDB 直读、ST 与北交所过滤、行业合并、泄漏列剔除、
标签构建（T+1 执行口径，见 data.splits._EXECUTION_LAG_DAYS）。
"""

from __future__ import annotations

import atexit
import logging
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from data.memprobe import log_rss, rss_stage
from data.splits import _EXECUTION_LAG_DAYS

logger = logging.getLogger("quantmind.train")


# ══ 特征溢出（memmap）═══════════════════════════════════════════════════════
# 2026-10-16 全窗（2016-01 起 ≈ 10.72M 行 × 300 特征）内存改造：取行结果的宽帧
# 特征列按行块流式写入 C 序 memmap（临时文件、进程退出即删），返回的帧特征块架在
# 该映射上——列读数穿页、页缓存可回收（对 MemAvailable 友好），PSI/切分/训练后
# 预测等下游全部按普通 DataFrame 使用、代码零改动；薄列（symbol/trade_date/
# label）驻内存。输出与旧 `.loc[valid_index, keep_cols].reset_index(drop=True)`
# 逐位一致（2026-10-16 探针锁定）：行序=pos 顺序、列序=[symbol, trade_date,
# label]+features、dtype 原样。任何不可行情形（非 f32、索引非唯一、异常）回退旧写法。
_SPILL_REFS: list = []  # 保活 memmap 对象（帧块的 base 链之外再钉一道）
_SPILL_PATHS: list = []
_SPILL_CHUNK_ROWS = 262_144


def _spill_enabled() -> bool:
    v = os.environ.get("QM_TRAIN_FEATURE_SPILL", "0").strip().lower()
    return v not in ("0", "false", "no", "off")


def _spill_cleanup() -> None:
    for _p in _SPILL_PATHS:
        try:
            os.remove(_p)
        except OSError:
            pass
    _SPILL_PATHS.clear()


atexit.register(_spill_cleanup)


def _clip_rows_via_spill(
    df: pd.DataFrame, pos, features: list[str]
) -> pd.DataFrame | None:
    """按位置取行并把特征列落 memmap；任何不可行情形返回 None（调用方回退）。

    pos：df 内的行位置数组（valid_index 经 get_indexer 反查，须全部 ≥0）。
    旧写法 `.loc[valid_index, keep_cols].reset_index(drop=True)` 在 10.72M 行时
    同时驻留 3 份帧级拷贝（源帧 + 取行副本 + reset_index 内部再拷）必 OOM；
    本实现逐行块 take + 写 memmap，瞬态只多一个行块的量。
    """
    n, nf = len(pos), len(features)
    if n == 0 or nf == 0:
        return None
    _bad = [c for c in features if df[c].dtype != np.float32]
    if _bad:
        logger.warning(
            "Feature spill skipped: %d feature column(s) not float32 (e.g. %s)",
            len(_bad),
            _bad[:5],
        )
        return None
    path = None
    try:
        fd, path = tempfile.mkstemp(prefix="qm_feat_spill_", suffix=".f32")
        os.close(fd)
        _SPILL_PATHS.append(path)  # 先登记，异常路径也能被 atexit 清掉
        mat = np.memmap(path, dtype=np.float32, mode="w+", shape=(n, nf))
        for start in range(0, n, _SPILL_CHUNK_ROWS):
            idx = pos[start : start + _SPILL_CHUNK_ROWS]
            sub = df.take(idx)
            mat[start : start + len(idx)] = sub[features].to_numpy(
                dtype=np.float32, copy=False
            )
            del sub
        mat.flush()
        out = pd.DataFrame(mat, columns=list(features), copy=False)
        out.insert(0, "label", df["label"].take(pos).to_numpy())
        out.insert(0, "trade_date", df["trade_date"].take(pos).to_numpy())
        out.insert(0, "symbol", df["symbol"].take(pos).to_numpy())
        _SPILL_REFS.append(mat)
        logger.info(
            "[mem] 特征溢出到 memmap: %d 行 × %d 列 → %s（%.2fG，页缓存可回收）",
            n,
            nf,
            path,
            n * nf * 4 / 1e9,
        )
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("Feature spill failed (%s), falling back to in-memory clip", exc)
        return None


def _to_prefix_symbol(sym: str) -> str:
    """转前缀式（SH600036）：后缀式取点后拼接；其余原样大写。"""
    s = str(sym or "").strip().upper()
    if "." in s:
        code, exch = s.split(".", 1)
        if code and exch:
            return exch + code
    return s


def _load_local_parquet(
    local_dir: Path,
    year: int,
    required_columns: list[str],
    clip_start: pd.Timestamp | None = None,
    clip_end: pd.Timestamp | None = None,
) -> pd.DataFrame | None:
    file_path = local_dir / f"model_features_{year}.parquet"
    if not file_path.exists():
        return None
    try:
        logger.info(f"Local data hit: {file_path}")

        schema_cols = set(pq.ParquetFile(file_path).schema_arrow.names)
        selected_cols = [c for c in required_columns if c in schema_cols]
        if "trade_date" not in selected_cols or "symbol" not in selected_cols:
            logger.warning(
                "Skip parquet missing required base columns trade_date/symbol: %s",
                file_path,
            )
            return None
        df = pd.read_parquet(file_path, columns=selected_cols, engine="pyarrow")

        # 先按日期裁剪每年数据，避免把无关年份全量堆进内存
        if "trade_date" in df.columns and (
            clip_start is not None or clip_end is not None
        ):
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
            mask = pd.Series(True, index=df.index)
            if clip_start is not None:
                mask &= df["trade_date"] >= clip_start
            if clip_end is not None:
                mask &= df["trade_date"] <= clip_end
            df = df.loc[mask].copy()

        # 数值列统一降为 float32，降低内存峰值
        for col in df.columns:
            if col in {"trade_date", "symbol"}:
                continue
            if pd.api.types.is_numeric_dtype(df[col]):
                df[col] = df[col].astype(np.float32, copy=False)

        return df
    except Exception as exc:
        logger.warning(f"  ⚠ Failed to read local parquet {file_path}: {exc}")
        return None


# ── 数据加载 ──────────────────────────────────────────────────────────────────
_MARKET_PARQUET_FILES: dict[str, str] = {
    "HK": "model_features_hk.parquet",
    "US": "model_features_us.parquet",
    "CRYPTO": "model_features_crypto.parquet",
    "FUTURES": "model_features_futures.parquet",
}
# 各市场 6_ml_datasets 数据根目录（训练容器内环境变量，由编排器挂载设置）
_MARKET_DATA_DIR_ENV: dict[str, str] = {
    "CN": "QUANTDB_DATA_DIR",
    "HK": "QUANTHK_DATA_DIR",
    "US": "QUANTUS_DATA_DIR",
    "CRYPTO": "QUANTBC_DATA_DIR",
    "FUTURES": "QUANTFUTURES_DATA_DIR",
    "CUSTOM": "QM_QUANTCUSTOM_DATA_DIR",
}


def load_data(
    train_start: str,
    train_end: str,
    features: list[str],
    target_horizon_days: int = 1,
    target_mode: str = "return",
    label_transform: str = "rank",
    label_top_pct: float = 0.2,
    execution_price: str = "close",
    cache_dir: str | None = None,
    valid_end: str | None = None,
    test_end: str | None = None,
    source_mode: str = "LOCAL",
    local_dir: str | None = None,
    market: str = "CN",
    industry_as_feature: bool = False,
    factor_source: str | None = None,
    quantdb_dir: str | None = None,
    factor_field_sources: dict[str, str] | None = None,
    pool_symbols: list[str] | None = None,
) -> tuple:
    local_root = Path(local_dir).expanduser() if local_dir else None
    if local_root is None:
        raise RuntimeError(
            "local_dir must be provided; COS data download has been removed"
        )

    market_upper = str(market or "CN").upper()

    # 仅读取训练必需列，避免整表加载导致 OOM
    horizon = max(1, int(target_horizon_days or 1))
    horizon_col = f"mom_ret_{horizon}d"
    required_columns = list(
        dict.fromkeys(
            [
                "trade_date",
                "symbol",
                "mom_ret_1d",
                horizon_col,
                "is_st",
                "volume",
                "open",
            ]
            + list(features)
        )
    )
    # features_daily.return_Nd 是未来 N 日收益（return_1d[T] == pct_change[T+1]），
    # 曾被别名映射为 mom_ret_Nd 当特征使用，导致标签泄漏与虚高 RankIC。
    # 现只读取 l1_factors 提供的 mom_ret_Nd（过去收益），不做任何回退映射。
    _read_columns = list(required_columns)
    logger.info(
        "Memory-optimized read: selected %d columns (horizon=%s, market=%s)",
        len(required_columns),
        horizon,
        market_upper,
    )

    # 给标签构建预留边界，避免裁剪过早影响 shift/rolling
    range_start = pd.Timestamp(train_start) - pd.Timedelta(days=max(7, horizon + 3))
    upper_bound = test_end or valid_end or train_end
    range_end = pd.Timestamp(upper_bound) + pd.Timedelta(days=max(7, horizon + 3))

    direct_factor_source = str(factor_source or "").strip()
    _train_cache_key = None
    if direct_factor_source and market_upper in _MARKET_DATA_DIR_ENV:
        # Direct QuantDB mode: one factor source only, never materialise or merge snapshots.
        # 与 A 股一致：直接读该市场 6_ml_datasets 下的因子分区
        # （HK: l1_factors/ccass_factors/south_factors）。
        from backend.services.engine.data_platform.quantdb_factor_reader import (
            QuantDBFactorReader,
        )

        reader = QuantDBFactorReader(
            quantdb_dir or os.getenv(_MARKET_DATA_DIR_ENV[market_upper]) or None,
            market=market_upper,
        )
        # 标签构建缓冲(range_start)可能早于数据可用起点(如 train_start 恰为数据首日)。
        # 数据缺失部分无法提供，钳制到数据起点即可，避免 assert_ready 越界抛错。
        _status = reader.describe(direct_factor_source)
        if _status.min_date:
            range_start = max(range_start, pd.Timestamp(_status.min_date))
        if _status.max_date:
            range_end = min(range_end, pd.Timestamp(_status.max_date))

        # D3 训练缓存：源指纹+特征+窗口+池全等时直接复用最终窄表（含标签）。
        # cache_dir 由 train.py 传入（本地编排取 /workspace/cache；远端为空）。
        _train_cache_key = None
        if cache_dir:
            try:
                from backend.shared.train_cache import (
                    build_cache_key,
                    read_cache,
                    source_stamp,
                )

                _src_stamp = source_stamp(
                    _status, reader.source_path(direct_factor_source)
                )
                _train_cache_key = build_cache_key(
                    market=market_upper,
                    factor_source=direct_factor_source,
                    source_stamp_value=_src_stamp,
                    train_start=train_start,
                    train_end=train_end,
                    valid_end=valid_end,
                    test_end=test_end,
                    horizon=horizon,
                    target_mode=target_mode,
                    features=features,
                    factor_field_sources=factor_field_sources,
                    pool_symbols=pool_symbols,
                    industry_as_feature=industry_as_feature,
                )
                _cached = read_cache(cache_dir, _train_cache_key)
                if _cached is not None:
                    _cached_df, _cached_features = _cached
                    logger.info(
                        "Train cache hit: source=%s key=%s rows=%d features=%d",
                        direct_factor_source,
                        _train_cache_key[:12],
                        len(_cached_df),
                        len(_cached_features),
                    )
                    return _cached_df, _cached_features
            except Exception as exc:  # noqa: BLE001 - 缓存失败不阻断训练
                logger.warning("训练缓存检查失败（忽略）: %s", exc)
                _train_cache_key = None

        logger.info(
            "Direct QuantDB read %s: requesting %s .. %s",
            direct_factor_source,
            range_start.date(),
            range_end.date(),
        )
        with rss_stage("读全量因子", f"{len(features)} 特征"):
            df = reader.read_range(
                direct_factor_source,
                features=features,
                feature_sources=factor_field_sources,
                start=range_start.date(),
                end=range_end.date(),
            )
        logger.info(
            "Direct QuantDB factor source %s: %d rows, %s to %s",
            direct_factor_source,
            len(df),
            df["trade_date"].min() if not df.empty else "N/A",
            df["trade_date"].max() if not df.empty else "N/A",
        )
        log_rss("读出帧", f"{len(df):,} 行 × {len(df.columns)} 列")
        # 池尽早过滤（P3）：直读返回全市场（1053 万行），114 列 float64 在
        # 后续复制展开期峰值会触发容器 mem_limit（Exit 137）；池内成分先过滤，
        # 后续 downcast/漂移/标签全量受益。直读 symbol 为前缀式。
        if pool_symbols and market_upper == "CN" and "symbol" in df.columns:
            _wanted_early = {
                _to_prefix_symbol(s) for s in pool_symbols if str(s).strip()
            }
            if not _wanted_early:
                raise RuntimeError("pool_symbols 非空但无法解析出任何代码")
            _before_early = len(df)
            df = df[df["symbol"].astype(str).str.upper().isin(_wanted_early)].copy()
            logger.info(
                "After early pool filter: %d rows (pool=%d symbols, before=%d)",
                len(df),
                len(_wanted_early),
                _before_early,
            )
            if df.empty:
                raise RuntimeError(
                    "股票池过滤后无数据：池内标的在训练区间/数据源内无记录"
                    "（请检查池成分与训练时间窗是否匹配）"
                )
            log_rss("池过滤后", f"{len(df):,} 行（过滤前 {_before_early:,}）")
        # 与 core parquet 分支一致：数值列统一降为 float32，降低内存峰值。
        # 直读 reader 已在 DuckDB 侧 CAST AS FLOAT，多数列本来就是 float32 —— 此时
        # **必须整段跳过**：逐列 `df[col] = astype(...)` 会把连续块重新拆成数百个
        # 碎片块（2026-09-15 实测：修复 reader 去碎片后，又被本循环拆回 279 块，
        # 后续 .loc 取行「合并碎片 + 取行」双份 9.5GB，峰值冲到 48G 限额 OOM 137）。
        # 仅在确有非 float32 列时做一次批量转换（单次赋值不碎片化）。
        _need_cast = [
            col
            for col in df.columns
            if col not in {"trade_date", "symbol"}
            and pd.api.types.is_numeric_dtype(df[col])
            and df[col].dtype != np.float32
        ]
        if _need_cast:
            df[_need_cast] = df[_need_cast].astype(np.float32)
            logger.info("Direct QuantDB cast %d columns to float32", len(_need_cast))
        else:
            logger.info(
                "Direct QuantDB columns already float32 (reader-side cast), skip"
            )
    elif market_upper in _MARKET_PARQUET_FILES:
        # ── 非 A 股市场：从单一 parquet 文件加载 ──
        parquet_name = _MARKET_PARQUET_FILES[market_upper]
        parquet_path = local_root / parquet_name
        if not parquet_path.exists():
            raise RuntimeError(
                f"市场 {market_upper} parquet 文件不存在: {parquet_path}"
            )
        logger.info("Loading market-specific parquet: %s", parquet_path)

        # 非 A 股文件使用 'instrument' 列而非 'symbol'
        # 先检查 parquet schema，过滤掉不存在的列（如 mom_ret_2d）
        schema_cols = set(pq.ParquetFile(parquet_path).schema_arrow.names)
        # symbol/instrument 列名兼容
        has_instrument = "instrument" in schema_cols
        valid_cols = []
        missing_cols = []
        for c in _read_columns:
            if c in schema_cols:
                valid_cols.append(c)
            elif c == "symbol" and has_instrument:
                valid_cols.append("instrument")
            else:
                missing_cols.append(c)
        if missing_cols:
            logger.warning("Columns not in parquet (skipped): %s", missing_cols)

        try:
            df = pd.read_parquet(parquet_path, columns=valid_cols, engine="pyarrow")
        except Exception:
            df = pd.read_parquet(parquet_path, columns=valid_cols, engine="pyarrow")
        if "instrument" in df.columns and "symbol" not in df.columns:
            df = df.rename(columns={"instrument": "symbol"})

        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
        df = df[df["trade_date"].notna()].copy()
        # 日期裁剪
        mask = (df["trade_date"] >= range_start) & (df["trade_date"] <= range_end)
        df = df.loc[mask].copy()
        logger.info(
            "Market %s raw data: %d rows, date range: %s to %s",
            market_upper,
            len(df),
            df["trade_date"].min() if not df.empty else "N/A",
            df["trade_date"].max() if not df.empty else "N/A",
        )
    else:
        # ── A 股：优先使用 core parquet（78列），回退到年度 parquet 文件 ──
        core_parquet_path = local_root / "model_features_core.parquet"

        if core_parquet_path.exists():
            # 使用精简版 core parquet（78列，内存友好）
            logger.info("Using core parquet (78 factors): %s", core_parquet_path)

            schema_cols = set(pq.ParquetFile(core_parquet_path).schema_arrow.names)
            valid_cols = [c for c in _read_columns if c in schema_cols]
            missing_cols = [c for c in _read_columns if c not in schema_cols]
            if missing_cols:
                logger.warning(
                    "Columns not in core parquet (skipped): %s", missing_cols
                )

            if "trade_date" not in valid_cols or "symbol" not in valid_cols:
                raise RuntimeError(
                    "Core parquet missing required columns: trade_date or symbol"
                )

            df = pd.read_parquet(
                core_parquet_path, columns=valid_cols, engine="pyarrow"
            )
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
            df = df[df["trade_date"].notna()].copy()

            # 日期裁剪
            mask = (df["trade_date"] >= range_start) & (df["trade_date"] <= range_end)
            df = df.loc[mask].copy()

            # 数值列统一降为 float32
            for col in df.columns:
                if col in {"trade_date", "symbol"}:
                    continue
                if pd.api.types.is_numeric_dtype(df[col]):
                    df[col] = df[col].astype(np.float32, copy=False)

            logger.info(
                "Core parquet loaded: %d rows, date range: %s to %s",
                len(df),
                df["trade_date"].min() if not df.empty else "N/A",
                df["trade_date"].max() if not df.empty else "N/A",
            )
        else:
            # 回退到年度 parquet 文件（197列，内存占用大）
            logger.warning(
                "Core parquet not found, falling back to yearly parquet files"
            )
            start_year = pd.Timestamp(train_start).year
            ends = [train_end]
            if valid_end:
                ends.append(valid_end)
            if test_end:
                ends.append(test_end)
            end_year = max(pd.Timestamp(e).year for e in ends)

            chunks = []
            for year in range(max(start_year - 1, 2016), end_year + 1):
                df_year = _load_local_parquet(
                    local_root,
                    year,
                    required_columns=_read_columns,
                    clip_start=range_start,
                    clip_end=range_end,
                )
                if df_year is not None:
                    if not df_year.empty:
                        chunks.append(df_year)
                else:
                    logger.warning(
                        f"No data file found for year {year} in {local_root}, skipping"
                    )

            if not chunks:
                raise RuntimeError("No data loaded from local storage")

            df = pd.concat(chunks, axis=0, ignore_index=True)
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
            df = df[df["trade_date"].notna()].copy()
            logger.info(
                f"Raw concat size: {len(df)} rows. Date range: {df['trade_date'].min()} to {df['trade_date'].max()}"
            )

        # 过滤 ST/*ST 股票
        if "is_st" in df.columns:
            before = len(df)
            df["is_st"] = (
                pd.to_numeric(df["is_st"], errors="coerce").fillna(0).astype(int)
            )
            df = df[df["is_st"] == 0].copy()
            logger.info(
                f"After ST filter: {len(df)} rows (removed {before - len(df)} ST rows)"
            )

        # 行业条件化：合并 ind_code_l1（CSRC 一级行业编码）
        if industry_as_feature or "ind_code_l1" in features:
            try:
                # 优先从 QuantDB 全量挂载目录查找，回退到 feature_snapshots 子目录
                _qdb_mount = os.getenv("QUANTDB_DATA_DIR", "/tmp/quantdb_data")
                _sector_dirs = [
                    Path(_qdb_mount)
                    / "2_base_sector",  # local_docker_orchestrator 挂载的 QuantDB 全量数据
                    local_root
                    / "2_base_sector",  # feature_snapshots 内的子目录（兼容旧部署）
                ]
                ind_detail_path = None
                for _d in _sector_dirs:
                    for _name in (
                        "instrument_list.parquet",
                        "instrument_detail.parquet",
                    ):
                        _p = _d / "instrument_detail" / _name
                        if _p.exists():
                            ind_detail_path = _p
                            break
                    if ind_detail_path is not None:
                        break
                if ind_detail_path is not None:
                    ind_df = pd.read_parquet(ind_detail_path, engine="pyarrow")
                    sym_col = (
                        "symbol"
                        if "symbol" in ind_df.columns
                        else "wind_code"
                        if "wind_code" in ind_df.columns
                        else None
                    )
                    if sym_col and "rs_hycode_sim" in ind_df.columns:
                        ind_map = ind_df[[sym_col, "rs_hycode_sim"]].dropna()
                        ind_map = ind_map.rename(
                            columns={sym_col: "symbol", "rs_hycode_sim": "ind_code_l1"}
                        )
                        ind_map["symbol"] = ind_map["symbol"].astype(str).str.zfill(6)
                        ind_map["ind_code_l1"] = pd.Categorical(
                            ind_map["ind_code_l1"]
                        ).codes.astype(np.float32)
                        df["symbol"] = df["symbol"].astype(str).str.zfill(6)
                        df = df.merge(ind_map, on="symbol", how="left")
                        # 缺失行业映射到 max(code)+1 的独立类别 id（而非 fillna(-1) 或 0）：
                        # 负 id 会被 CatBoost 拒绝；并入 0 会与第一个真实行业混淆。
                        _ind_max = (
                            float(ind_map["ind_code_l1"].max())
                            if len(ind_map)
                            else -1.0
                        )
                        df["ind_code_l1"] = (
                            df["ind_code_l1"].fillna(_ind_max + 1).astype(np.float32)
                        )
                        logger.info(
                            "Industry mapping merged: %d/%d rows have ind_code_l1",
                            (df["ind_code_l1"] >= 0).sum(),
                            len(df),
                        )
                    else:
                        logger.warning(
                            "instrument_detail.parquet missing symbol/wind_code or rs_hycode_sim columns"
                        )
                else:
                    logger.warning(
                        "instrument_detail.parquet not found (searched: %s)",
                        ", ".join(str(d) for d in _sector_dirs),
                    )
            except Exception as e:
                logger.warning("Failed to merge industry data (non-fatal): %s", e)

    # ── 过滤北交所（4/8 开头，仅 A 股） ──
    # 必须在所有加载分支之后：此前缩在 CN-parquet 分支内，直读因子源模式
    # 会把北交所带进训练集。双口径（前缀式/6 位数字）判定。
    if market_upper == "CN" and "symbol" in df.columns:
        _bj_sym6 = (
            df["symbol"]
            .astype(str)
            .str.upper()
            .str.replace(r"^(SH|SZ|BJ)", "", regex=True)
        )
        _bj_mask = ~_bj_sym6.str.startswith(("4", "8"))
        _bj_removed = int((~_bj_mask).sum())
        if _bj_removed:
            df = df[_bj_mask].copy()
        logger.info(f"After BJ filter: {len(df)} rows (removed {_bj_removed} BJ rows)")

    # ── 全局股票池过滤（P3）：编排器已把池解析成 6 位代码列表随 config.yaml 传入 ──
    # 必须在所有加载分支之后（直读因子源 / core parquet / 年度 parquet 都走这里）：
    # 此前缩在 CN-parquet 分支内，直读模式（l1_factors）会静默训成全市场模型。
    # 严格语义：池非空但零命中时直接报错，避免静默错配。
    if pool_symbols and market_upper == "CN":
        wanted = {str(s).split(".")[0].zfill(6) for s in pool_symbols if str(s).strip()}
        if not wanted:
            raise RuntimeError("pool_symbols 非空但无法解析出任何代码")
        if "symbol" not in df.columns:
            raise RuntimeError("股票池过滤需要 symbol 列，但当前数据无该列")
        before_pool = len(df)
        # 双口径匹配：直读分支 symbol 为前缀式（SH600036），parquet 分支为
        # 6 位数字——zfill 对 8 位前缀是空操作，单用 6 位集合会零命中误杀。
        _sym_up = df["symbol"].astype(str).str.upper()
        _sym6 = _sym_up.str.replace(r"^(SH|SZ|BJ)", "", regex=True)
        _wanted_prefix = {_to_prefix_symbol(s) for s in pool_symbols if str(s).strip()}
        df = df[_sym6.isin(wanted) | _sym_up.isin(_wanted_prefix)].copy()
        logger.info(
            "After pool filter: %d rows (pool=%d symbols, before=%d)",
            len(df),
            len(wanted),
            before_pool,
        )
        if df.empty:
            raise RuntimeError(
                f"股票池过滤后无数据：池内 {len(wanted)} 只标的在训练区间/数据源内无记录"
                "（请检查池成分与训练时间窗是否匹配）"
            )

    # ── 丢弃 features_daily.return_Nd：这些列是【未来 N 日收益】 ──
    # return_1d[T] == pct_change[T+1]，当特征使用会直接泄漏标签。
    # 历史上曾把它们重命名为 mom_ret_Nd，导致 RankIC 虚高到 0.7+。
    # 仅旧快照 parquet（A 股 features_daily 血统）携带该列；直读因子源
    # （l1/l2/ccass/south）的 return_Nd 为过去收益（pct_change 口径），
    # 不适用此剔除（HK l1_factors 的 return_1d 即过去 1 日收益）。
    _LEAKY_RETURN_COLS = (
        "return_1d",
        "return_3d",
        "return_5d",
        "return_10d",
        "return_20d",
        "return_60d",
    )
    if not direct_factor_source:
        _leaky_present = [c for c in _LEAKY_RETURN_COLS if c in df.columns]
        if _leaky_present:
            df = df.drop(columns=_leaky_present, errors="ignore")
            logger.warning(
                "Dropped forward-looking return columns (label leakage): %s",
                _leaky_present,
            )

    # 如果仍缺 mom_ret_1d，尝试从 pct_change 或 close 构建
    if "mom_ret_1d" not in df.columns:
        if "pct_change" in df.columns:
            df["mom_ret_1d"] = pd.to_numeric(df["pct_change"], errors="coerce") / 100.0
            logger.info("Built mom_ret_1d from pct_change column")
        elif "close" in df.columns:
            df["mom_ret_1d"] = df.groupby("symbol")["close"].pct_change(1)
            logger.info("Built mom_ret_1d from close column pct_change")
        else:
            raise RuntimeError(
                "Column 'mom_ret_1d' not found and cannot be constructed (no pct_change or close)"
            )

    # ── 行选择与标签构建全部在**窄表**上完成（2026-09-15）────────────────────
    # 宽表（8.5M×279 float32 ≈ 9.5GB）每整表复制一次就 +9.5GB；旧实现依次做
    # 假日过滤 / 排序 / 标签过滤 / 裁剪 / 选列共 4~5 次整表复制，2016 起窗口
    # 首训实测在容器 OOM(SIGKILL 137)。改为：窄表（4~6 列，~0.2GB）上完成全部
    # 行级运算，宽表只保留一次 .loc 取行（含选列），语义逐行等价。
    _horizon = max(1, int(target_horizon_days or 1))
    _mom_col = f"mom_ret_{_horizon}d"
    _narrow_cols = list(
        dict.fromkeys(
            c
            for c in (
                "symbol",
                "trade_date",
                "volume",
                "close",
                "open",
                "mom_ret_1d",
                _mom_col,
                "total_cap",
            )
            if c in df.columns
        )
    )
    narrow = df[_narrow_cols]

    # 剔除节假日填充行：QuantDB parquet 含约 6.6% 的假交易日
    # （close>0、mom_ret_1d=0，但全市场 volume==0），如春节/清明/劳动节。
    # 必须在 label 构造前剔除：shift(-N) 按行位移，若序列含假日，
    # "未来 N 个交易日收益" 实际只跨 N-k 个真实交易日，导致标签时间尺度不一致。
    if "volume" in narrow.columns:
        _day_vol = narrow.groupby("trade_date")["volume"].max()
        _real_days = _day_vol[_day_vol > 0].index
        _dropped_days = len(_day_vol) - len(_real_days)
        if _dropped_days > 0:
            _rows_before = len(narrow)
            narrow = narrow[narrow["trade_date"].isin(_real_days)]
            logger.info(
                "Dropped %d non-trading days (holiday fill rows): %d -> %d rows",
                _dropped_days,
                _rows_before,
                len(narrow),
            )
    else:
        logger.warning(
            "Column 'volume' unavailable — cannot filter holiday fill rows; "
            "labels may span fewer real trading days than target_horizon_days"
        )

    # 标签：基于 target_horizon_days 构建 N 日远期收益
    # 注：mom_ret_{N}d 列是过去 N 日收益（backward-looking），如 mom_ret_5d[T] = (close[T]-close[T-5])/close[T-5]
    # shift(-N) 后，行 T 得到行 T+N 的值 = (close[T+N]-close[T])/close[T]，即正确的 N 日远期收益
    # 等价于: label = next_N_day_return = pct_change(N).shift(-N)
    # 保持原始行索引（不 reset），供后续宽表按索引一次性取行
    narrow = narrow.sort_values(["symbol", "trade_date"])
    _exec_price = str(execution_price or "close").strip().lower()
    if _exec_price not in ("open", "close"):
        raise ValueError(
            f"execution_price must be one of: open, close (got {execution_price!r})"
        )
    if direct_factor_source:
        # Raw factor sources carry open/close, so labels are true forward returns.
        # execution_price=open：t+1 开盘买（严格实盘口径）；close：t+1 收盘买（旧口径）。
        _lag = _EXECUTION_LAG_DAYS
        _entry_col = "open" if _exec_price == "open" else "close"
        execution_close = narrow.groupby("symbol")[_entry_col].shift(-_lag)
        future_close = narrow.groupby("symbol")["close"].shift(-(_lag + _horizon))
        narrow["label"] = future_close / execution_close - 1.0
    elif _exec_price == "open":
        raise RuntimeError(
            "execution_price=open 仅支持直读 QuantDB 因子源（需要 open 列）；"
            "本地快照/动量回退路径不支持开盘价标签"
        )
    elif _horizon == 1:
        # mom_ret_1d[T+2] = close[T+2] / close[T+1] - 1，匹配 T+1 执行。
        narrow["label"] = narrow.groupby("symbol")["mom_ret_1d"].shift(
            -(_horizon + _EXECUTION_LAG_DAYS)
        )
    elif _mom_col in narrow.columns:
        # mom_ret_H[T+1+H] = close[T+1+H] / close[T+1] - 1。
        narrow["label"] = narrow.groupby("symbol")[_mom_col].shift(
            -(_horizon + _EXECUTION_LAG_DAYS)
        )
    else:
        # 回退：通过滚动累乘 1d 收益构造 N 日远期收益
        narrow["label"] = (
            narrow.groupby("symbol")["mom_ret_1d"]
            .transform(lambda s: (1 + s).rolling(_horizon).apply(np.prod, raw=True) - 1)
            .shift(-(_horizon + _EXECUTION_LAG_DAYS))
        )
    logger.info(
        "Label built with target_horizon_days=%s execution_price=%s (%s)",
        _horizon,
        _exec_price,
        "direct close"
        if direct_factor_source
        else _mom_col
        if _mom_col in narrow.columns
        else "rolling",
    )

    # 标签有效性 + 裁剪到请求区间：一次布尔掩码定行（缓冲区行只为标签服务，不输出）
    valid_count_before = len(narrow)
    _label_ok = narrow["label"].notna()
    logger.info(
        f"After label shift & dropna: {int(_label_ok.sum())} rows (dropped {valid_count_before - int(_label_ok.sum())} rows with missing labels)"
    )
    _clip_end = test_end or valid_end or train_end
    _in_range = (narrow["trade_date"] >= train_start) & (
        narrow["trade_date"] <= _clip_end
    )
    valid_index = narrow.index[_label_ok & _in_range]

    # 校验特征列（选列发生在取行之前，先把缺失特征剔掉）
    missing = [f for f in features if f not in df.columns]
    if missing:
        logger.warning(f"Features not found in parquet (ignored): {missing}")
        features = [f for f in features if f in df.columns]
    if not features:
        raise RuntimeError("No valid feature columns found")

    # 宽表一次性取回（行=已排序有效行，列=最终保留列），label 从窄表对齐写入
    df["label"] = narrow["label"]
    keep_cols = ["symbol", "trade_date", "label"] + features
    # 优先走 memmap 溢出（见 _clip_rows_via_spill：全窗下旧写法的 3 份帧级拷贝
    # 必 OOM）；不可行时回退旧写法（当前窗口记忆里安全，全窗下则是已知风险路径）。
    _spilled = None
    if _spill_enabled() and df.index.is_unique:
        try:
            _pos = df.index.get_indexer(valid_index)
        except Exception:  # noqa: BLE001
            _pos = None
            logger.warning(
                "Feature spill: get_indexer failed, falling back", exc_info=True
            )
        if _pos is not None and (_pos >= 0).all():
            _spilled = _clip_rows_via_spill(df, _pos, features)
    if _spilled is not None:
        df = _spilled
    else:
        df = df.loc[valid_index, keep_cols].reset_index(drop=True)
    logger.info(f"After date range clip ({train_start} to {_clip_end}): {len(df)} rows")

    # 保留原始收益标签：label 随后会被截面 rank（回归）或二值化（分类）改写，
    # 评估报告的分层收益 / 多空净值需要真实收益口径（train 端按需读取）。
    df["label_return"] = df["label"].astype(np.float32)

    # 分类目标保留为 0/1，不能再做截面 rank；否则 binary objective 会收到
    # 连续标签而退化成语义不明确的回归任务。
    _target_mode = str(target_mode or "return").lower()
    if _target_mode == "classification":
        from preprocessing import binarize_labels

        df["label"] = binarize_labels(df["label"].to_numpy())
        _n_pos = int((df["label"] == 1).sum())
        _n_neg = int((df["label"] == 0).sum())
        logger.info(
            f"Classification target: positive={_n_pos}, negative={_n_neg} (ratio={_n_pos / max(1, _n_pos + _n_neg):.3f})"
        )

    # 收益预测默认使用截面 rank 目标（强调同日选股排序）；可选 zscore / raw。
    # 分类预测保持二元标签，不做截面变换。
    if _target_mode != "classification":
        from preprocessing import cross_sectional_label_transform

        _size_mode = str(label_transform or "").startswith("size_excess")
        _size_series = df["total_cap"] if "total_cap" in df.columns else None
        if _size_mode and _size_series is None:
            raise RuntimeError(
                "label_transform=size_excess_* requires a 'total_cap' feature column; "
                "add total_cap to the training feature list"
            )
        df["label"] = cross_sectional_label_transform(
            df["label"],
            df["trade_date"],
            label_transform,
            top_pct=label_top_pct,
            size=_size_series,
        ).to_numpy()
        logger.info("Label transform: %s", label_transform)

    logger.info(
        f"Data ready: {len(df):,} rows, {len(features)} features, "
        f"{df['trade_date'].min().date()} ~ {df['trade_date'].max().date()}"
    )

    # D3：只缓存直读因子源的最终窄表（含标签）；写失败只告警，不影响本次训练。
    if _train_cache_key and cache_dir and len(df) > 0:
        try:
            from backend.shared.train_cache import write_cache

            write_cache(
                cache_dir,
                _train_cache_key,
                df,
                features,
                meta_extra={
                    "factor_source": direct_factor_source,
                    "market": market_upper,
                    "train_start": str(train_start),
                    "train_end": str(train_end),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("训练缓存写入失败（忽略）: %s", exc)

    return df, features
