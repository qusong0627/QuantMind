"""L0.5 热集快照落盘（T-P6-04）：parquet 按日分区 + 质检 + 降冷 + 归档器。

**分区范式（沿用 QuantDB 约定，防事故）**：
- 布局 ``<base>/date=YYYYMMDD/part-<epoch_ms>-<seq>.parquet``；**文件内不写 dt/date 列**
  （DuckDB hive 分区列会被同名列遮蔽——2026-09 已备案事故；按日读取直接读当日目录，无须 hive）。
- 列：symbol(后缀式)/ts(epoch 秒)/五档/涨跌停/refresh_time/source；缺值为 null（不假填）。

**读取**：``read_day`` 走 DuckDB 读当日目录（glob），可带 symbols 过滤（分区裁剪天然生效）。

**质检**：``quality_report`` —— 单调性/缺口/覆盖/五档完整性，逐标的 + 总览 + flags。

**降冷**：``prune_old`` 只匹配 ``date=\\d{8}`` 目录、按 keep_days 删除（dry-run 可查），
非分区目录/文件一律不碰。

**归档器**：``SnapshotArchiver`` —— 订阅引擎帧的落盘 sink（内嵌 worker，零额外读取）；
缓冲按日分组，行数/时间双阈值触发 flush；单次写失败只计数不抛出（不阻断实时链）。

**容量**：实测 zstd 全字段行 ≈ 145 B/行（2026-09-17 合成标定，开盘后以 ``capacity_report`` 复测）；
3s 节拍 × 527 只 ≈ 367 MB/交易日 → 默认 keep_days=90 ≈ 33 GB。容量表与 EOD 质检/降冷走
``backend/scripts/l05_maintenance.py``。
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("l05_store")

CST = timezone(timedelta(hours=8))
DEFAULT_BASE_DIR = "/data/l05_snapshots"
_DATE_DIR_RE = re.compile(r"^date=(\d{8})$")

_FLOAT_FIELDS = (
    "price",
    "pre_close",
    "open",
    "high",
    "low",
    "volume",
    "amount",
    "limit_up",
    "limit_down",
    "seal_amount",
    *[f"bid{i}" for i in range(1, 6)],
    *[f"bid_vol{i}" for i in range(1, 6)],
    *[f"ask{i}" for i in range(1, 6)],
    *[f"ask_vol{i}" for i in range(1, 6)],
)
_BOOK_FIELDS = tuple(
    [f"bid{i}" for i in range(1, 6)]
    + [f"bid_vol{i}" for i in range(1, 6)]
    + [f"ask{i}" for i in range(1, 6)]
    + [f"ask_vol{i}" for i in range(1, 6)]
)


def _date_dir(base_dir: str, day: date) -> Path:
    return Path(base_dir) / f"date={day.strftime('%Y%m%d')}"


def _record_to_row(record: dict[str, Any]) -> dict[str, Any]:
    ts = record.get("ts")
    row: dict[str, Any] = {
        "symbol": str(record.get("symbol") or ""),
        "ts": int(ts) if ts is not None else None,
        "refresh_time": str(record.get("refresh_time") or ""),
        "source": str(record.get("source") or "tdx_aidata_sub"),
    }
    for field in _FLOAT_FIELDS:
        value = record.get(field)
        try:
            row[field] = float(value) if value is not None else None
        except (TypeError, ValueError):
            row[field] = None
    return row


def write_records(
    records: list[dict[str, Any]],
    *,
    base_dir: str = DEFAULT_BASE_DIR,
    tag: str = "",
) -> dict[str, Any]:
    """按本地日（CST）分组落盘；返回 {rows, dates, files}。

    ``tag``：多 worker 分片并存时写入文件名（part-<ms>-<tag>-<seq>.parquet），
    防同毫秒同名撞车（各分片只归档自己订阅到的帧，无重复行）。
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        ts = record.get("ts")
        if ts is None:
            continue
        day_key = datetime.fromtimestamp(int(ts), tz=CST).strftime("%Y%m%d")
        row = _record_to_row(record)
        if not row["symbol"]:
            continue
        grouped.setdefault(day_key, []).append(row)

    tag_part = f"-{str(tag).strip()}" if str(tag or "").strip() else ""
    files: list[str] = []
    rows = 0
    seq = 0
    for day_key, day_rows in grouped.items():
        day_dir = Path(base_dir) / f"date={day_key}"
        day_dir.mkdir(parents=True, exist_ok=True)
        seq += 1
        path = day_dir / f"part-{int(time.time() * 1000)}{tag_part}-{seq:04d}.parquet"
        # 按 (symbol, ts) 排序：同一 flush 内标的成组，行组统计可裁剪（单标的回放读更快）
        day_rows.sort(key=lambda r: (r["symbol"], r["ts"] or 0))
        table = pa.Table.from_pylist(day_rows)
        pq.write_table(table, path, compression="zstd")
        files.append(str(path))
        rows += len(day_rows)
    return {"rows": rows, "dates": sorted(grouped), "files": files}


def list_days(base_dir: str = DEFAULT_BASE_DIR) -> list[str]:
    """已落盘的分区日（YYYYMMDD 升序）；非 date= 目录一律不识别。"""
    base = Path(base_dir)
    if not base.is_dir():
        return []
    days = [
        m.group(1)
        for child in base.iterdir()
        if child.is_dir() and (m := _DATE_DIR_RE.match(child.name))
    ]
    return sorted(days)


def capacity_report(
    base_dir: str = DEFAULT_BASE_DIR, *, days: list[str] | None = None
) -> dict[str, Any]:
    """逐日容量统计（只读 parquet 元数据，不读数据）：行数/文件数/字节/行均字节。

    读不到元数据的文件列入 ``unreadable_files``（绝不静默）。
    """
    import pyarrow.parquet as pq

    day_list = days if days is not None else list_days(base_dir)
    per_day: dict[str, Any] = {}
    unreadable: list[str] = []
    total_rows = total_bytes = total_files = 0
    for day_key in day_list:
        day_dir = Path(base_dir) / f"date={day_key}"
        rows = files = size = 0
        for path in sorted(day_dir.glob("part-*.parquet")):
            try:
                rows += pq.ParquetFile(path).metadata.num_rows
                size += path.stat().st_size
                files += 1
            except Exception:  # noqa: BLE001 - 坏文件如实记录，不中断统计
                unreadable.append(str(path))
        per_day[day_key] = {
            "rows": rows,
            "files": files,
            "bytes": size,
            "bytes_per_row": round(size / rows, 1) if rows else None,
        }
        total_rows += rows
        total_bytes += size
        total_files += files
    return {
        "days": per_day,
        "totals": {
            "days": len(day_list),
            "rows": total_rows,
            "files": total_files,
            "bytes": total_bytes,
            "bytes_per_row": round(total_bytes / total_rows, 1) if total_rows else None,
            "bytes_per_day": round(total_bytes / len(day_list)) if day_list else 0,
        },
        "unreadable_files": unreadable,
    }


def read_day(
    day: date,
    *,
    base_dir: str = DEFAULT_BASE_DIR,
    symbols: list[str] | None = None,
):
    """读取某日全部（或指定标的）快照（pandas DataFrame，按 ts 升序）。"""
    import duckdb

    day_dir = _date_dir(base_dir, day)
    glob = str(day_dir / "part-*.parquet")
    conn = duckdb.connect()
    try:
        try:
            if symbols:
                placeholders = ",".join(["?"] * len(symbols))
                df = conn.execute(
                    f"SELECT * FROM read_parquet('{glob}') "
                    f"WHERE symbol IN ({placeholders}) ORDER BY ts",
                    list(symbols),
                ).fetchdf()
            else:
                df = conn.execute(
                    f"SELECT * FROM read_parquet('{glob}') ORDER BY ts"
                ).fetchdf()
        except duckdb.IOException:
            # 当日无文件：返回空表（保留列骨架）
            import pandas as pd

            df = pd.DataFrame(
                columns=["symbol", "ts", *_FLOAT_FIELDS, "refresh_time", "source"]
            )
        return df
    finally:
        conn.close()


def quality_report(table) -> dict[str, Any]:
    """质检：逐标的（行数/时间单调/缺口/五档完整性）+ 总览 + flags。

    ``table`` 支持 pyarrow.Table 或 pandas.DataFrame（来自 read_day）。
    """
    import pyarrow as pa

    if not isinstance(table, pa.Table):
        table = pa.Table.from_pandas(table, preserve_index=False)
    cols = table.to_pydict()
    symbols: dict[str, list[int]] = {}
    ts_list = cols.get("ts") or []
    symbol_list = cols.get("symbol") or []
    for sym, ts in zip(symbol_list, ts_list, strict=False):
        if sym is None or ts is None:
            continue
        symbols.setdefault(str(sym), []).append(int(ts))

    per: dict[str, Any] = {}
    flags: list[str] = []
    for sym, timestamps in symbols.items():
        ordered = sorted(timestamps)
        monotonic = all(
            b >= a for a, b in zip(timestamps, timestamps[1:], strict=False)
        )
        gaps = [b - a for a, b in zip(ordered, ordered[1:], strict=False)]
        max_gap = max(gaps) if gaps else 0
        # 五档完整性：按标的统计全列非空比例
        completeness = _book_completeness(table, sym)
        per[sym] = {
            "rows": len(timestamps),
            "ts_min": ordered[0] if ordered else None,
            "ts_max": ordered[-1] if ordered else None,
            "ts_monotonic": monotonic,
            "max_gap_s": max_gap,
            "median_gap_s": sorted(gaps)[len(gaps) // 2] if gaps else 0,
            "book_completeness": completeness,
        }
        if not monotonic:
            flags.append(f"{sym}: ts_monotonic")
        if completeness < 0.95:
            flags.append(f"{sym}: book_completeness={completeness:.2f}")

    return {
        "totals": {"rows": int(len(symbol_list)), "symbols": len(symbols)},
        "symbols": per,
        "flags": flags,
    }


def _book_completeness(table, symbol: str) -> float:
    import pyarrow.compute as pc

    mask = pc.equal(table["symbol"], symbol)
    sub = table.filter(mask)
    if sub.num_rows == 0:
        return 0.0
    present = 0
    for field in _BOOK_FIELDS:
        if field not in sub.column_names:
            continue
        if sub[field].null_count < sub.num_rows:
            present += 1
    return present / len(_BOOK_FIELDS)


def prune_old(
    base_dir: str = DEFAULT_BASE_DIR,
    *,
    keep_days: int,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[str]:
    """删除超过 keep_days 的 date= 分区目录；只匹配分区目录，其余一律不碰。"""
    if keep_days <= 0:
        return []
    base = Path(base_dir)
    if not base.is_dir():
        return []
    now = now or datetime.now(tz=CST)
    cutoff = (now.date() - timedelta(days=int(keep_days))).strftime("%Y%m%d")
    removed: list[str] = []
    for child in sorted(base.iterdir()):
        m = _DATE_DIR_RE.match(child.name)
        if not m or not child.is_dir():
            continue
        if m.group(1) < cutoff:
            removed.append(child.name)
            if not dry_run:
                try:
                    shutil.rmtree(child)
                except OSError as exc:
                    logger.warning("l05 降冷删除失败 %s: %s", child, exc)
                    removed.pop()
    return removed


class SnapshotArchiver:
    """订阅帧 → L0.5 parquet 的缓冲归档器（线程安全 append；flush 由引擎周期调用）。"""

    def __init__(
        self,
        *,
        base_dir: str = DEFAULT_BASE_DIR,
        flush_rows: int = 50_000,
        flush_seconds: float = 30.0,
        keep_days: int = 90,
        tag: str = "",
    ) -> None:
        self.base_dir = base_dir
        self.tag = str(tag or "").strip()
        self.flush_rows = max(1, int(flush_rows))
        self.flush_seconds = float(flush_seconds)
        self.keep_days = int(keep_days)
        self._buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()
        self._last_prune_day: str | None = None
        self.counters: dict[str, Any] = {
            "rows": 0,
            "flushes": 0,
            "flush_errors": 0,
            "last_error": None,
        }

    @property
    def pending_rows(self) -> int:
        with self._lock:
            return len(self._buffer)

    def append(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._buffer.append(record)
            due = len(self._buffer) >= self.flush_rows or (
                time.monotonic() - self._last_flush >= self.flush_seconds
            )
        if due:
            self.flush()

    def flush(self) -> dict[str, Any]:
        with self._lock:
            batch, self._buffer = self._buffer, []
            self._last_flush = time.monotonic()
        result: dict[str, Any] = {"rows": 0, "dates": []}
        if not batch:
            return result
        try:
            result = write_records(batch, base_dir=self.base_dir, tag=self.tag)
            self.counters["rows"] += int(result.get("rows") or 0)
            self.counters["flushes"] += 1
        except Exception as exc:  # noqa: BLE001 - 归档失败不阻断实时链
            self.counters["flush_errors"] += 1
            self.counters["last_error"] = f"flush: {exc}"
            logger.error("l05 flush failed (%d rows dropped-safe): %s", len(batch), exc)
            return result
        self._maybe_prune()
        return result

    def _maybe_prune(self) -> None:
        today = datetime.now(tz=CST).strftime("%Y%m%d")
        if self._last_prune_day == today:
            return
        self._last_prune_day = today
        try:
            removed = prune_old(self.base_dir, keep_days=self.keep_days)
            if removed:
                logger.info("l05 降冷删除: %s", removed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("l05 降冷失败: %s", exc)
