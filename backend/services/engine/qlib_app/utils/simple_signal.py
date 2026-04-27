import logging
from pathlib import Path
from typing import Optional

import pandas as pd
from qlib.backtest.signal import Signal
from qlib.data import D

logger = logging.getLogger(__name__)
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

task_logger = StructuredTaskLogger(logger, "SimpleSignal")


def _is_bj_instrument(code: str) -> bool:
    code_str = str(code or "").upper()
    return code_str.startswith("BJ")


def _exclude_bj_instruments(codes):
    return [c for c in codes if not _is_bj_instrument(c)]


class SimpleSignal(Signal):
    """
    A lightweight signal adapter that returns either a precomputed pred.pkl signal
    or falls back to a standard Qlib feature like `$close`.
    """

    def __init__(
        self,
        metric: str = "$close",
        universe: str = "all",
        pred_path: str | None = None,
        signal_lag_days: int = 1,
    ):
        self.metric = metric
        self.universe = universe
        self._pred_path = pred_path
        self.signal_lag_days = max(0, int(signal_lag_days or 0))
        self._pred_series: pd.Series | None = None
        self._universe_codes: set[str] | None = None
        self._daily_cache: dict[pd.Timestamp, pd.Series] = {}
        self._universe_instruments: list[str] | None = None

    @staticmethod
    def _project_root() -> Path:
        try:
            return Path(__file__).resolve().parents[5]
        except Exception:
            return Path.cwd()

    def _resolve_universe_file(self) -> Path | None:
        universe = str(self.universe or "").strip()
        if not universe:
            return None
        if "/" not in universe and not universe.lower().endswith(".txt"):
            return None

        raw = Path(universe)
        candidates = [
            raw,
            Path.cwd() / universe,
            self._project_root() / universe,
            self._project_root() / "db" / "qlib_data" / universe,
            Path("/data/qlib_data") / universe,
        ]
        seen: set[str] = set()
        for c in candidates:
            key = str(c)
            if key in seen:
                continue
            seen.add(key)
            if c.is_file():
                return c
        return None

    def _load_instruments_from_file(self, file_path: Path) -> list[str]:
        instruments: list[str] = []
        with file_path.open("r", encoding="utf-8") as fp:
            for line in fp:
                code = line.strip()
                if not code or code.startswith("#"):
                    continue
                if "\t" in code:
                    code = code.split("\t", 1)[0].strip()
                instruments.append(code)
        return _exclude_bj_instruments(instruments)

    def _get_universe_instruments(self) -> list[str]:
        if self._universe_instruments is not None:
            return self._universe_instruments
        if not self.universe:
            self._universe_instruments = []
            return self._universe_instruments

        universe_str = str(self.universe).strip()
        file_path = self._resolve_universe_file()
        if file_path is not None:
            try:
                instruments = self._load_instruments_from_file(file_path)
                self._universe_instruments = instruments
                task_logger.info(
                    "load_universe_file",
                    "SimpleSignal 从文件股票池加载成功",
                    universe=universe_str,
                    path=str(file_path),
                    count=len(instruments),
                )
                return self._universe_instruments
            except Exception as exc:
                task_logger.warning("read_universe_file_failed", "读取股票池文件失败", path=str(file_path), error=str(exc))

        try:
            qlib_instruments = D.list_instruments(D.instruments(universe_str), as_list=True)
            self._universe_instruments = _exclude_bj_instruments(qlib_instruments)
            return self._universe_instruments
        except Exception as exc:
            task_logger.warning("load_universe_failed", "加载股票池失败，股票池将置空", universe=universe_str, error=str(exc))
            self._universe_instruments = []
            return self._universe_instruments

    @staticmethod
    def _normalize_series(series: pd.Series) -> pd.Series:
        if not isinstance(series.index, pd.MultiIndex):
            series.index = pd.MultiIndex.from_product([series.index, [0]], names=["datetime", "instrument"])
        names = list(series.index.names)
        if names == ["instrument", "datetime"]:
            series = series.swaplevel(0, 1)
        if series.index.names != ["datetime", "instrument"]:
            series.index = series.index.set_names(["datetime", "instrument"])
        return series.sort_index()

    def _lag_series_by_trading_days(self, series: pd.Series) -> pd.Series:
        if self.signal_lag_days <= 0 or series.empty:
            return series
        if not isinstance(series.index, pd.MultiIndex) or "datetime" not in series.index.names:
            return series

        date_values = pd.to_datetime(series.index.get_level_values("datetime")).normalize()
        unique_dates = pd.Index(date_values.unique()).sort_values()
        shifted_dates = unique_dates.to_series(index=unique_dates).shift(-self.signal_lag_days)
        mapped_dates = date_values.map(shifted_dates)
        valid_mask = ~pd.isna(mapped_dates)
        if not valid_mask.any():
            return series.iloc[0:0]

        result = series.loc[valid_mask].copy()
        arrays = []
        for name in result.index.names:
            if name == "datetime":
                arrays.append(pd.DatetimeIndex(mapped_dates[valid_mask]))
            else:
                arrays.append(result.index.get_level_values(name))
        result.index = pd.MultiIndex.from_arrays(arrays, names=result.index.names)
        return result.sort_index()

    def _slice_time(self, series: pd.Series, start_time, end_time) -> pd.Series:
        if start_time is None and end_time is None:
            return series
        start = pd.to_datetime(start_time).normalize() if start_time else None
        end = pd.to_datetime(end_time).normalize() if end_time else None

        # 回测主路径通常是逐日请求，走缓存避免重复切片开销
        if start is not None and end is not None and start == end:
            cached = self._daily_cache.get(start)
            if cached is not None:
                return cached
            try:
                daily = series.xs(start, level="datetime", drop_level=False)
            except KeyError:
                daily = pd.Series(dtype=series.dtype)
            self._daily_cache[start] = daily
            return daily

        try:
            return series.loc[pd.IndexSlice[slice(start, end), :]]
        except Exception:
            idx = series.index.get_level_values("datetime")
            mask = pd.Series(True, index=series.index)
            if start is not None:
                mask &= idx >= start
            if end is not None:
                mask &= idx <= end
            return series[mask]

    def _get_universe_code_set(self) -> set[str] | None:
        if self._universe_codes is not None:
            return self._universe_codes
        if not self.universe or str(self.universe).lower() == "all":
            self._universe_codes = set()
            return self._universe_codes

        instruments = self._get_universe_instruments()
        self._universe_codes = set(map(str, instruments))
        return self._universe_codes

    def _load_pred_series(self, start_time=None, end_time=None) -> pd.Series | None:
        if not self._pred_path:
            return None
        if self._pred_series is not None:
            return self._slice_time(self._pred_series, start_time, end_time)

        try:
            from backend.services.engine.qlib_app.utils.qlib_utils import np_patch

            with np_patch():
                try:
                    if self._pred_path.endswith(".parquet"):
                        raw = pd.read_parquet(self._pred_path, engine="pyarrow")
                        score_col = "pred" if "pred" in raw.columns else raw.columns[-1]
                        df = (
                            raw[["trade_date", "symbol", score_col]]
                            .rename(columns={"trade_date": "datetime", "symbol": "instrument", score_col: "score"})
                            .assign(datetime=lambda d: pd.to_datetime(d["datetime"]))
                            .set_index(["datetime", "instrument"])
                            .sort_index()
                        )
                        task_logger.info("parquet_loaded", "SimpleSignal: pred.parquet 加载成功", rows=len(df))
                    else:
                        df = pd.read_pickle(self._pred_path)
                except Exception as e:
                    task_logger.warning("load_pred_file_failed", "加载预测文件失败", error=str(e))
                    return None

            if isinstance(df, pd.Series):
                series = df.copy()
            else:
                if "score" not in df.columns and df.shape[1] == 1:
                    df = df.rename(columns={df.columns[0]: "score"})
                series = df.get("score")

            if series is None:
                task_logger.warning("pred_missing_score", "pred.pkl 中找不到 score 列", metric=self.metric)
                return None

            series = self._normalize_series(series)
            series = self._lag_series_by_trading_days(series)
            series = self._align_instrument_case(series)
            universe_codes = self._get_universe_code_set()
            if universe_codes:
                inst = series.index.get_level_values("instrument").astype(str)
                before = len(series)
                series = series.loc[inst.isin(universe_codes)]
                task_logger.info(
                    "prefilter_signal_by_universe",
                    "SimpleSignal 按 universe 预过滤信号",
                    universe=self.universe,
                    before=before,
                    after=len(series),
                )
            self._pred_series = series
            return self._slice_time(series, start_time, end_time)
        except Exception as exc:
            task_logger.warning("load_pred_pickle_failed", "读取 pred.pkl 失败", path=self._pred_path, error=str(exc))
            return None

    def _align_instrument_case(self, series: pd.Series) -> pd.Series:
        """
        对齐 pred 信号股票代码大小写，避免与 qlib 数据集代码风格不一致导致 0 成交。
        """
        try:
            if not isinstance(series.index, pd.MultiIndex):
                return series
            if "instrument" not in series.index.names:
                return series

            inst_values = series.index.get_level_values("instrument")
            if len(inst_values) == 0:
                return series

            # 基于当前 universe 的可交易代码判断目标大小写风格
            qlib_instruments = self._get_universe_instruments()
            if not qlib_instruments:
                task_logger.warning("universe_empty_skip_case_align", "Universe 返回空股票列表，跳过大小写对齐", universe=self.universe)
                return series
            qlib_set = set(map(str, qlib_instruments))
            pred_set = set(map(str, inst_values))

            def to_qlib_code(code: str) -> str:
                code_u = str(code or "").strip().upper()
                if len(code_u) == 8 and code_u[:2] in {"SH", "SZ", "BJ"}:
                    return code_u
                if len(code_u) == 9 and "." in code_u:
                    left, right = code_u.split(".", 1)
                    if len(left) == 6 and right in {"SH", "SZ", "BJ"}:
                        return right + left
                if len(code_u) == 6 and code_u.isdigit():
                    if code_u.startswith(("6", "9")):
                        return "SH" + code_u
                    if code_u.startswith(("0", "2", "3")):
                        return "SZ" + code_u
                    if code_u.startswith(("4", "8")):
                        return "BJ" + code_u
                return code_u

            candidates = [
                ("raw", lambda s: str(s)),
                ("lower", lambda s: str(s).lower()),
                ("upper", lambda s: str(s).upper()),
                ("qlib_upper", to_qlib_code),
                ("qlib_lower", lambda s: to_qlib_code(s).lower()),
            ]

            best_name = "raw"
            best_overlap = -1
            for name, fn in candidates:
                mapped_set = {fn(v) for v in pred_set}
                overlap = len(mapped_set & qlib_set)
                if overlap > best_overlap:
                    best_name = name
                    best_overlap = overlap

            raw_overlap = len(pred_set & qlib_set)
            if best_overlap <= raw_overlap:
                return series

            transform = dict(candidates)[best_name]
            new_inst = inst_values.map(transform)
            series.index = pd.MultiIndex.from_arrays(
                [series.index.get_level_values("datetime"), new_inst],
                names=["datetime", "instrument"],
            )
            task_logger.info(
                "align_instrument_case",
                "SimpleSignal 已自动对齐 pred 股票代码格式",
                best_name=best_name,
                raw_overlap=raw_overlap,
                best_overlap=best_overlap,
            )
            return series.sort_index()
        except Exception as exc:
            task_logger.warning("align_instrument_case_failed", "股票代码大小写对齐失败，保持原始 pred 索引", error=str(exc))
            return series

    def get_signal(self, start_time, end_time):
        if self._pred_path:
            series = self._load_pred_series(start_time, end_time)
            if series is None:
                task_logger.warning(
                    "pred_signal_unavailable",
                    "预测信号不可用，返回空信号而不是回退到行情特征",
                    start_time=start_time,
                    end_time=end_time,
                    pred_path=self._pred_path,
                )
                return pd.Series(dtype=float)
            if series.empty:
                task_logger.info(
                    "pred_signal_empty",
                    "当前请求区间没有可交易预测信号，返回空信号",
                    start_time=start_time,
                    end_time=end_time,
                    pred_path=self._pred_path,
                )
                return pd.Series(dtype=float)

            dates = series.index.get_level_values("datetime")
            target_date = dates.max() if len(dates) > 1 else dates.iloc[0]
            daily = series.xs(target_date, level="datetime")
            if daily.empty:
                task_logger.info(
                    "pred_signal_daily_empty",
                    "预测信号切片为空，返回空信号",
                    start_time=start_time,
                    end_time=end_time,
                    target_date=str(target_date),
                )
                return pd.Series(dtype=float)
            task_logger.debug(
                "return_pred_signal",
                "SimpleSignal returning pred signal",
                entries=len(daily),
                start_time=start_time,
                end_time=end_time,
                target_date=str(target_date),
                min=float(daily.min()),
                max=float(daily.max()),
                std=float(daily.std(ddof=0)),
            )
            return daily

        try:
            instruments = self._get_universe_instruments()
            if not instruments:
                task_logger.warning("universe_empty_return_blank", "Universe 股票池为空，返回空信号", universe=self.universe)
                return pd.Series(dtype=float)
            query_start = start_time
            if self.signal_lag_days > 0 and start_time is not None:
                query_start = pd.to_datetime(start_time) - pd.Timedelta(days=max(10, self.signal_lag_days * 10))
            df = D.features(instruments, [self.metric], start_time=query_start, end_time=end_time)
            if df is not None and not df.empty:
                series = df[self.metric]
                series = self._normalize_series(series)
                series = self._lag_series_by_trading_days(series)
                if isinstance(series.index, pd.MultiIndex):
                    if "datetime" in series.index.names:
                        dates = series.index.get_level_values("datetime")
                        target_date = pd.to_datetime(end_time) if end_time else dates.max()
                        uniq_dates = pd.Index(dates.unique()).sort_values()
                        if target_date not in uniq_dates:
                            pos = uniq_dates.searchsorted(target_date, side="right") - 1
                            if pos < 0:
                                return pd.Series(dtype=float)
                            target_date = uniq_dates[pos]
                        return series.xs(target_date, level="datetime")
                    return series.droplevel(level=1) if series.index.nlevels > 1 else series
                return series
            task_logger.warning("metric_empty_return_blank", "指标返回空数据，返回空信号", metric=self.metric)
            return pd.Series(dtype=float)
        except Exception as exc:
            task_logger.warning("get_signal_failed", "Error getting signal", metric=self.metric, error=str(exc))
            return pd.Series(dtype=float)
