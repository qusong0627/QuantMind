"""Client-side local cache for Big QMT market data.

Pull bars from Big QMT once over RPC, persist them on the client, then read them
back with ``get_local_data`` without touching Big QMT again — for offline / local
analysis. One file per (period, dividend_type, code); incremental merge + dedupe
by time. Default storage is Parquet (columnar, compressed, cross-language); falls
back to pickle when pyarrow is unavailable. A cache written in one format is read
+ migrated transparently if the configured format changes.
"""

import os


# Candidate time-column names produced by the RPC market-data path.
_TIME_COLS = ("stime", "time", "index", "date", "datetime", "timetag")


def _time_col(df):
    cols = list(getattr(df, "columns", []))
    for name in _TIME_COLS:
        if name in cols:
            return name
    return None


def _time_axis(df):
    """Return (name, use_index): where the time axis lives, or (None, False).

    RPC-shaped frames keep a stime/time column; MiniQMT-shaped frames carry
    time as the index (the client normalizer moves stime to the index and
    drops the column). The cache must slice/dedupe by whichever exists —
    otherwise a MiniQMT-shaped write silently disables date filtering, so
    get_local_data returns every cached day regardless of the window
    (issue #54 follow-up).

    A MiniQMT-compatible frame can also have *both* a date-shaped index and an
    epoch-ms ``time`` column.  Prefer the index in that case: parquet must keep
    it, and date-window comparisons must not compare ``20260818`` with
    ``1786982400000`` as strings.
    """
    index = getattr(df, "index", None)
    try:
        if index is not None and len(index):
            digits = "".join(ch for ch in str(index[0]) if ch.isdigit())
            if 8 <= len(digits) <= 14:
                return "__index__", True
    except Exception:
        pass
    name = _time_col(df)
    if name:
        return name, False
    return None, False


def _pad_end(value):
    text = str(value)
    return text + "9" * (14 - len(text)) if 0 < len(text) < 14 else text


def _align_start(value, sample):
    """Align a timestamp lower bound to an eight-digit daily cache axis."""
    text = str(value)
    sample_text = str(sample)
    if sample_text.isdigit() and len(sample_text) == 8 and text.isdigit() and len(text) > 8:
        return text[:8]
    return text


def _drop_placeholder_rows(df):
    """Big QMT fills dates it has no local data for with all-zero rows. A real bar
    never has close/open == 0, so drop those placeholders — the cache should hold
    only real bars, not 0-fill padding."""
    for col in ("close", "open", "price", "lastPrice"):
        if col in getattr(df, "columns", []):
            try:
                # 不 reset_index：时间轴可能在索引上（MiniQMT 形态），重置会丢掉它。
                return df[df[col] != 0]
            except Exception:
                return df
    return df


def _pyarrow_available():
    try:
        import pyarrow  # noqa: F401

        return True
    except Exception:
        return False


def _resolve_format(fmt):
    fmt = str(fmt or "auto").lower()
    if fmt in ("parquet", "pq"):
        return "parquet"
    if fmt in ("pkl", "pickle"):
        return "pkl"
    # auto / unknown
    return "parquet" if _pyarrow_available() else "pkl"


class LocalMarketCache:
    def __init__(self, cache_dir=None, fmt="auto"):
        self.cache_dir = str(cache_dir or os.path.join(os.path.expanduser("~"), ".bigqmt_cache"))
        self.fmt = _resolve_format(fmt)

    def _ext(self):
        return ".parquet" if self.fmt == "parquet" else ".pkl"

    def path(self, code, period, dividend_type="none"):
        safe_code = str(code or "").replace("/", "_").replace("\\", "_")
        div = str(dividend_type or "none")
        return os.path.join(self.cache_dir, str(period or "1d"), div, safe_code + self._ext())

    def _existing_path(self, code, period, dividend_type):
        """Return the on-disk file for this key in the configured format, else the
        other format (so switching format still finds + migrates the old cache)."""
        primary = self.path(code, period, dividend_type)
        if os.path.isfile(primary):
            return primary
        base = primary[: -len(self._ext())]
        for ext in (".parquet", ".pkl"):
            alt = base + ext
            if os.path.isfile(alt):
                return alt
        return None

    @staticmethod
    def _read_file(path):
        import pandas as pd

        # Read by actual file extension (an existing cache may be either format).
        if path.endswith(".pkl"):
            return pd.read_pickle(path)
        return pd.read_parquet(path)

    def _write_file(self, df, path):
        # Write in the configured format regardless of the path (the temp file ends
        # with ".tmp", not the format extension).
        # 时间轴在索引上的帧（MiniQMT 形态）必须带索引写盘，否则读回后日期窗口失效。
        _, on_index = _time_axis(df)
        if self.fmt == "parquet":
            df.to_parquet(path, index=on_index)
        else:
            df.to_pickle(path)

    def write(self, code, period, df, dividend_type="none"):
        """Merge ``df`` into the cache for (code, period, dividend_type).

        Dedupe is by time keeping the LAST write, so re-pulling a range overwrites
        stale values — which is exactly what front-adjusted (前复权) data needs after
        a new dividend re-scales history. Returns total rows stored.
        """
        if df is None or not hasattr(df, "shape") or df.shape[0] == 0:
            return 0
        import pandas as pd

        incoming = _drop_placeholder_rows(df.copy())
        primary = self.path(code, period, dividend_type)
        existing = self._existing_path(code, period, dividend_type)
        if incoming.shape[0] == 0:
            # Nothing real to add (all 0-fill placeholders); keep existing cache.
            if existing:
                try:
                    return self._read_file(existing).shape[0]
                except Exception:
                    return 0
            return 0
        directory = os.path.dirname(primary)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        merged = incoming
        _, incoming_on_index = _time_axis(incoming)
        if existing:
            try:
                old = self._read_file(existing)
                _, old_on_index = _time_axis(old)
                if incoming_on_index and old_on_index:
                    # 保留时间索引（ignore_index=True 会把 MiniQMT 形态的时间轴丢掉）。
                    merged = pd.concat([old, merged])
                elif incoming_on_index and not old_on_index:
                    # 老缓存无时间轴（旧版写出），行不可切片、合并还会毁掉新数据
                    # 的时间索引。老行丢弃——cache-through 每次都会重拉完整窗口。
                    merged = incoming
                else:
                    merged = pd.concat([old, merged], ignore_index=True)
            except Exception:
                pass
        axis, on_index = _time_axis(merged)
        if on_index:
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        elif axis:
            merged = merged.drop_duplicates(subset=[axis], keep="last").sort_values(axis).reset_index(drop=True)
        else:
            merged = merged.drop_duplicates().reset_index(drop=True)
        # Atomic-ish write (temp + replace) so a crash mid-write can't corrupt the file.
        tmp = primary + ".tmp"
        self._write_file(merged, tmp)
        os.replace(tmp, primary)
        # Migrated from the other format? drop the stale file.
        if existing and existing != primary:
            try:
                os.remove(existing)
            except Exception:
                pass
        return merged.shape[0]

    def read(self, code, period, start_time="", end_time="", count=-1, dividend_type="none"):
        """Return the cached DataFrame for (code, period, dividend_type), filtered."""
        existing = self._existing_path(code, period, dividend_type)
        if not existing:
            return None
        try:
            df = self._read_file(existing)
        except Exception:
            return None
        axis, on_index = _time_axis(df)
        if axis:
            # 每次过滤后 series 都要按当前 df 重算——上一步过滤已缩短 df，
            # 复用旧 mask 会长度不匹配（索引形态直接报错）。
            if start_time:
                series = df.index.astype(str) if on_index else df[axis].astype(str)
                start_bound = _align_start(start_time, next(iter(series))) if len(series) else str(start_time)
                df = df[series >= start_bound]
            if end_time:
                series = df.index.astype(str) if on_index else df[axis].astype(str)
                df = df[series <= _pad_end(end_time)]
            # 索引形态保留时间索引（MiniQMT 形态），列形态维持原 reset 行为。
            df = df.sort_index() if on_index else df.sort_values(axis).reset_index(drop=True)
        try:
            n = int(count)
        except (TypeError, ValueError):
            n = -1
        if n > 0 and df.shape[0] > n:
            # 索引形态保留时间索引；列形态维持原 reset 行为。
            df = df.tail(n) if _time_axis(df)[1] else df.tail(n).reset_index(drop=True)
        return df

    def covered(self, code, period, dividend_type="none"):
        """Return (first_time, last_time, rows) for the cache, or None if empty."""
        df = self.read(code, period, dividend_type=dividend_type)
        if df is None or df.shape[0] == 0:
            return None
        axis, on_index = _time_axis(df)
        if not axis:
            return (None, None, df.shape[0])
        series = list(df.index.astype(str)) if on_index else list(df[axis].astype(str))
        return (series[0], series[-1], df.shape[0])

    def stats(self):
        """Return (files, periods) currently cached across all dividend types."""
        files = 0
        periods = set()
        if os.path.isdir(self.cache_dir):
            for root, _dirs, fnames in os.walk(self.cache_dir):
                cached = [f for f in fnames if f.endswith(".parquet") or f.endswith(".pkl")]
                if cached:
                    files += len(cached)
                    rel = os.path.relpath(root, self.cache_dir)
                    periods.add(rel.split(os.sep)[0] if rel != "." else rel)
        return files, sorted(periods)
