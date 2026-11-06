"""Admission bounds and chunk sizes for private factor materialization.

The sandbox accepts one panel at a time (32 MiB pickled input, 1_000_000
output rows). Jobs are admitted on universe size times trading days, and each
sandbox call is cut so it stays under that input budget.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

# Training factors start here when the caller omits a start date. Model
# training reads this history; a one-year backtest window is not a substitute.
TRAINING_MATERIALIZATION_START = "2018-01-02"
# Output cells. Chunking keeps each sandbox call under the pickle cap. This
# still admits an all-A window from 2018 through the early 2040s, and rejects
# a longer full-market span before any price partition is loaded.
MAX_MATERIALIZATION_CELLS = 40_000_000
# Stay under the 32 MiB sandbox pickle cap after the index overhead.
MAX_CHUNK_INPUT_BYTES = 20 * 1024 * 1024
PANEL_FLOAT_COLUMNS = 8
BYTES_PER_VALUE = 8
MAX_SANDBOX_ROWS = 1_000_000

# Upper bounds used when the constituent list is not frozen yet.
_UNIVERSE_SYMBOL_CAP = {
    "sse50": 50,
    "star": 50,
    "gem": 100,
    "csi300": 300,
    "csi500": 500,
    "csi800": 800,
    "csi1000": 1000,
    "all": 6000,
    "all_a": 6000,
}

_WINDOW_CALL = re.compile(
    r"(?:rolling|shift|ewm|pct_change)\(\s*(?:window\s*=\s*|periods\s*=\s*)?(\d+)"
)


def weekday_count(start: str, end: str) -> int:
    """Inclusive weekday count. Holidays make this a slight over-estimate."""
    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    if end_day < start_day:
        raise ValueError("start_date must not be after end_date")
    count = 0
    cursor = start_day
    while cursor <= end_day:
        if cursor.weekday() < 5:
            count += 1
        cursor += timedelta(days=1)
    return count


def universe_symbol_cap(universe: str, symbols: list | None = None) -> int:
    if symbols is not None:
        return len(symbols)
    return _UNIVERSE_SYMBOL_CAP.get(str(universe or "").strip(), 6000)


def shared_coverage_bounds(source: dict) -> tuple[str, str]:
    facts = [source.get("kline") or {}, *(source.get("datasets") or {}).values()]
    starts = [
        fact["start"] for fact in facts if isinstance(fact, dict) and fact.get("start")
    ]
    ends = [fact["end"] for fact in facts if isinstance(fact, dict) and fact.get("end")]
    return (max(starts) if starts else "", min(ends) if ends else "")


def infer_functional_warmup(code: str) -> int:
    """Largest pandas rolling/shift window declared by a calculate_* factor."""
    if not isinstance(code, str) or not code:
        return 0
    found = [int(match) for match in _WINDOW_CALL.findall(code)]
    if not found:
        return 0
    return min(max(found), 500)


def max_output_days_per_chunk(n_symbols: int, warmup_bars: int) -> int:
    if type(n_symbols) is not int or n_symbols < 1:
        raise ValueError("物化股票池为空")
    if type(warmup_bars) is not int or warmup_bars < 0:
        raise ValueError("物化暖机窗口无效")
    day_bytes = n_symbols * PANEL_FLOAT_COLUMNS * BYTES_PER_VALUE
    budget_days = MAX_CHUNK_INPUT_BYTES // day_bytes
    output_days = budget_days - warmup_bars
    if output_days < 1:
        raise ValueError(
            f"暖机 {warmup_bars} 日与 {n_symbols} 只股票无法装入单次计算输入上限，请缩小股票池"
        )
    row_cap = max(1, MAX_SANDBOX_ROWS // n_symbols)
    return min(output_days, row_cap)


def assert_materialization_fits(
    n_symbols: int, n_days: int, warmup_bars: int = 0
) -> None:
    if type(n_symbols) is not int or type(n_days) is not int:
        raise ValueError("物化规模无法核验")
    if n_symbols < 1 or n_days < 1:
        raise ValueError("物化区间内没有交易日或股票池为空")
    cells = n_symbols * n_days
    if cells > MAX_MATERIALIZATION_CELLS:
        raise ValueError(
            f"物化规模超出上限：{n_symbols} 只股票 × {n_days} 个交易日 = {cells}，"
            f"上限 {MAX_MATERIALIZATION_CELLS}。请缩短日期或缩小股票池"
        )
    max_output_days_per_chunk(n_symbols, warmup_bars)


def chunk_output_bounds(
    n_output_days: int, n_symbols: int, warmup_bars: int
) -> list[tuple[int, int]]:
    if n_output_days <= 0:
        return []
    size = max_output_days_per_chunk(n_symbols, warmup_bars)
    bounds: list[tuple[int, int]] = []
    cursor = 0
    while cursor < n_output_days:
        end = min(n_output_days, cursor + size)
        bounds.append((cursor, end))
        cursor = end
    return bounds


def materialization_date_chunks(
    start: str, end: str, n_symbols: int, warmup_bars: int
) -> list[tuple[str, str]]:
    """Split a training window into ranges that each fit one price load."""
    n_days = weekday_count(start, end)
    size = max_output_days_per_chunk(n_symbols, warmup_bars)
    if n_days <= size:
        return [(start, end)]
    chunks: list[tuple[str, str]] = []
    cursor = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    while cursor <= end_day:
        remaining = weekday_count(cursor.isoformat(), end)
        if remaining <= size:
            chunks.append((cursor.isoformat(), end))
            break
        counted = 0
        chunk_end = cursor
        while counted < size:
            if chunk_end.weekday() < 5:
                counted += 1
            if counted == size:
                break
            chunk_end += timedelta(days=1)
        chunks.append((cursor.isoformat(), chunk_end.isoformat()))
        cursor = chunk_end + timedelta(days=1)
    return chunks
