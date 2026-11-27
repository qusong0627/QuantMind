# -*- coding: utf-8 -*-
"""Parquet part 存储：不可变 part + 原子 manifest，崩溃/掉线可续。

设计要点：
* Parquet 不支持原地 append → 每 5 分钟/20 万行写一个 part；
* 先写 ``.tmp`` → fsync → ``os.replace`` 原子提交，再更新 manifest；
* 只有 manifest 中的 part 被视为有效；崩溃最多丢最后一个未提交批；
* pyarrow 不可用时降级为 ``.jsonl.gz`` spool（同一 manifest），桥继续跑；
* 所有写入在独立线程，有界队列，绝不阻塞行情/订单路径。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import queue
import shutil
import threading
import time
from datetime import datetime, timedelta

try:  # pragma: no cover - Windows 可能未装 pyarrow
    import pyarrow as pa
    import pyarrow.parquet as pq
except Exception:  # noqa: BLE001
    pa = None
    pq = None

from .models import CN_TZ, date_str, is_valid_date, normalize_symbol

KINDS = ("bar1m", "snapshot3s", "tick_hot")
_MANIFEST_VERSION = 1
_COMPRESSION = {"zstd": "zstd", "snappy": "snappy", "gzip": "gzip", "none": None}


def _pyarrow_ready() -> bool:
    return pa is not None and pq is not None


def _schema(kind: str):
    """固定 schema（列顺序与类型不再漂移）。"""
    if not _pyarrow_ready():
        return None
    if kind == "bar1m":
        return pa.schema(
            [
                ("date", pa.string()),
                ("symbol", pa.string()),
                ("minute_ts", pa.int64()),
                ("open", pa.float64()),
                ("high", pa.float64()),
                ("low", pa.float64()),
                ("close", pa.float64()),
                ("volume", pa.float64()),
                ("amount", pa.float64()),
                ("vwap", pa.float64()),
                ("cum_volume", pa.float64()),
                ("cum_amount", pa.float64()),
                ("source", pa.string()),
                ("final", pa.bool_()),
            ]
        )
    if kind == "snapshot3s":
        return pa.schema(
            [
                ("date", pa.string()),
                ("ts_ms", pa.int64()),
                ("symbol", pa.string()),
                ("last", pa.float64()),
                ("open", pa.float64()),
                ("high", pa.float64()),
                ("low", pa.float64()),
                ("pre_close", pa.float64()),
                ("volume", pa.float64()),
                ("amount", pa.float64()),
                ("bid1", pa.float64()),
                ("ask1", pa.float64()),
                ("bid_vol1", pa.float64()),
                ("ask_vol1", pa.float64()),
                ("source", pa.string()),
                ("quality", pa.string()),
            ]
        )
    if kind == "tick_hot":
        return pa.schema(
            [
                ("date", pa.string()),
                ("ts_ms", pa.int64()),
                ("symbol", pa.string()),
                ("last", pa.float64()),
                ("volume", pa.float64()),
                ("amount", pa.float64()),
                ("bid1", pa.float64()),
                ("ask1", pa.float64()),
                ("source", pa.string()),
            ]
        )
    raise ValueError("unknown kind: %r" % (kind,))


class PartStore:
    """Parquet part 存储（线程安全；单写线程）。"""

    def __init__(
        self,
        base_dir: str,
        *,
        part_interval_s: float = 300.0,
        part_max_rows: int = 200000,
        retention_days: int = 30,
        kind_retention: dict | None = None,
        write_queue_max: int = 200,
        compress: str = "zstd",
        spool_fallback: bool = True,
        disk_min_free_gb: float = 5.0,
        logger=None,
        clock=time.time,
    ):
        self.base_dir = os.path.abspath(base_dir)
        self.part_interval_s = max(30.0, float(part_interval_s))
        self.part_max_rows = max(1000, int(part_max_rows))
        self.retention_days = max(1, int(retention_days))
        self.kind_retention = {"bar1m": self.retention_days}
        for kind, days in (kind_retention or {}).items():
            if kind in KINDS:
                self.kind_retention[kind] = max(1, int(days))
        self.compress = compress if compress in _COMPRESSION else "zstd"
        self.spool_fallback = bool(spool_fallback)
        self.disk_min_free_gb = max(0.0, float(disk_min_free_gb))
        self._clock = clock
        self._log = logger or (lambda msg: None)

        self._queue: queue.Queue = queue.Queue(maxsize=max(1, int(write_queue_max)))
        self._lock = threading.RLock()
        self._manifest_lock = threading.Lock()
        self._pending: dict[str, list[dict]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._parts = 0
        self._bytes = 0
        self._dropped = 0
        self._last_flush = 0.0
        # pyarrow 不可用即视为降级（即使能写 jsonl.gz spool，也不是 Parquet）。
        self._degraded = not _pyarrow_ready()
        self._started = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._started:
            return
        os.makedirs(self.base_dir, exist_ok=True)
        self._cleanup_tmp()
        self._scan_existing()
        self._started = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="today_cache_writer"
        )
        self._thread.start()
        self._log(
            "today_cache store start dir=%s format=%s parts=%d"
            % (self.base_dir, self._format_name(), self._parts)
        )

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None
        self.flush()
        self._started = False

    def _format_name(self) -> str:
        if _pyarrow_ready():
            return "parquet"
        if self.spool_fallback:
            return "jsonl.gz"
        return "disabled"

    # ------------------------------------------------------------------ write API
    def append(self, kind: str, rows) -> None:
        """非阻塞入队；队列满丢最旧（由调用方指标记录）。"""
        if kind not in KINDS:
            raise ValueError("unknown kind: %r" % (kind,))
        if not rows:
            return
        try:
            self._queue.put_nowait((kind, list(rows)))
        except queue.Full:
            with self._lock:
                self._dropped += 1
            self._log("today_cache write queue full, dropped %d rows" % len(rows))

    def flush(self) -> None:
        """把队列中已有数据立即写盘（测试/停止时用）。"""
        os.makedirs(self.base_dir, exist_ok=True)
        with self._lock:
            self._drain_locked()
            self._flush_pending_locked()

    def _loop(self) -> None:
        last_flush = time.time()
        while not self._stop.is_set():
            with self._lock:
                self._drain_locked()
                now = time.time()
                big = any(len(v) >= self.part_max_rows for v in self._pending.values())
                if self._pending and (now - last_flush >= self.part_interval_s or big):
                    self._flush_pending_locked()
                    last_flush = now
            self._stop.wait(0.5)
        with self._lock:
            self._drain_locked()
            self._flush_pending_locked()

    def _drain_locked(self) -> None:
        while True:
            try:
                kind, rows = self._queue.get_nowait()
            except queue.Empty:
                break
            self._pending.setdefault(kind, []).extend(rows)

    def _flush_pending_locked(self) -> None:
        for kind, rows in list(self._pending.items()):
            if not rows:
                self._pending.pop(kind, None)
                continue
            # 按 (date, hour) 分组：跨日/跨小时批次不能写进同一个 part 目录。
            groups: dict[tuple[str, int], list[dict]] = {}
            for row in rows:
                date = str(row.get("date") or "")
                if not is_valid_date(date):
                    ts = self._ts_of(kind, row)
                    date = date_str(ts) if ts else date_str(int(self._clock() * 1000))
                groups.setdefault((date, self._hour_of(kind, row)), []).append(row)
            for (date, hour), group in groups.items():
                for start in range(0, len(group), self.part_max_rows):
                    self._write_part_locked(
                        kind,
                        group[start : start + self.part_max_rows],
                        date=date,
                        hour=hour,
                    )
            self._pending.pop(kind, None)
        self._last_flush = time.time()

    # ------------------------------------------------------------------ part write
    def _write_part_locked(
        self, kind: str, rows: list[dict], *, date: str | None = None,
        hour: int | None = None,
    ) -> None:
        if not rows:
            return
        if self._disk_low():
            self._degraded = True
            self._dropped += len(rows)
            self._log("today_cache disk low, dropped %d rows" % len(rows))
            return
        date = str(date or rows[0].get("date") or "")
        if not is_valid_date(date):
            date = date_str(self._ts_of(kind, rows[0]) or int(self._clock() * 1000))
        hour = int(hour if hour is not None else self._hour_of(kind, rows[0]))
        target_dir = os.path.join(
            self.base_dir, "date=%s" % date, "type=%s" % kind, "hour=%02d" % hour
        )
        os.makedirs(target_dir, exist_ok=True)
        seq = self._next_seq()
        fmt = "parquet" if _pyarrow_ready() else ("jsonl.gz" if self.spool_fallback else "")
        if not fmt:
            self._degraded = True
            self._dropped += len(rows)
            return
        if fmt != "parquet":
            self._degraded = True
        name = "part-%06d.%s" % (seq, "parquet" if fmt == "parquet" else "jsonl.gz")
        path = os.path.join(target_dir, name)
        tmp = path + ".tmp"
        normalized = [self._normalize_row(kind, row) for row in rows]
        try:
            if fmt == "parquet":
                self._write_parquet(tmp, kind, normalized)
            else:
                self._write_jsonl(tmp, normalized)
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001 - 缓存失败不能拖垮桥
            self._degraded = True
            self._dropped += len(rows)
            self._log("today_cache write part failed: %r" % (exc,))
            try:
                os.remove(tmp)
            except OSError:
                pass
            return
        size = os.path.getsize(path)
        entry = {
            "type": kind,
            "hour": hour,
            "file": os.path.relpath(path, os.path.join(self.base_dir, "date=%s" % date)),
            "rows": len(normalized),
            "min_ts": min(self._ts_of(kind, row) or 0 for row in normalized),
            "max_ts": max(self._ts_of(kind, row) or 0 for row in normalized),
            "sha256": _sha256(path),
            "format": fmt,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            self._update_manifest(date, entry)
        except Exception as exc:  # noqa: BLE001
            self._degraded = True
            self._log("today_cache manifest update failed: %r" % (exc,))
            return
        with self._lock:
            self._parts += 1
            self._bytes += size

    def _write_parquet(self, path: str, kind: str, rows: list[dict]) -> None:
        table = pa.Table.from_pylist(rows, schema=_schema(kind))
        pq.write_table(
            table,
            path,
            compression=_COMPRESSION.get(self.compress),
            row_group_size=min(65536, max(1, len(rows))),
        )

    @staticmethod
    def _write_jsonl(path: str, rows: list[dict]) -> None:
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, default=str, sort_keys=True))
                fh.write("\n")

    # ------------------------------------------------------------------ manifest
    def _date_dir(self, date: str) -> str:
        return os.path.join(self.base_dir, "date=%s" % date)

    def _manifest_path(self, date: str) -> str:
        return os.path.join(self._date_dir(date), "manifest.json")

    def _load_manifest(self, date: str) -> dict:
        path = self._manifest_path(date)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("parts"), list):
                return data
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._log("today_cache manifest read failed: %r" % (exc,))
        return {"version": _MANIFEST_VERSION, "date": date, "parts": []}

    def _update_manifest(self, date: str, entry: dict) -> None:
        with self._manifest_lock:
            data = self._load_manifest(date)
            data.setdefault("version", _MANIFEST_VERSION)
            data["date"] = date
            data["parts"].append(entry)
            data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            path = self._manifest_path(date)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)

    # ------------------------------------------------------------------ read API
    def read(
        self,
        kind: str,
        date: str,
        *,
        symbols=None,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict]:
        if kind not in KINDS or not is_valid_date(date):
            return []
        wanted = {normalize_symbol(s) for s in symbols or [] if normalize_symbol(s)}
        rows: list[dict] = []
        for entry in self._load_manifest(date).get("parts", []):
            if entry.get("type") != kind:
                continue
            base = self._date_dir(date)
            path = os.path.abspath(
                os.path.join(base, str(entry.get("file") or ""))
            )
            # manifest 是本机文件，但仍拒绝越界路径（防篡改/误写）。
            if not path.startswith(os.path.abspath(base) + os.sep):
                continue
            if not os.path.isfile(path):
                continue
            try:
                if entry.get("format") == "parquet":
                    table = pq.read_table(path)
                    part_rows = table.to_pylist()
                else:
                    part_rows = _read_jsonl(path)
            except Exception as exc:  # noqa: BLE001
                self._log("today_cache read part failed %s: %r" % (path, exc))
                continue
            for row in part_rows:
                symbol = normalize_symbol(row.get("symbol"))
                if wanted and symbol not in wanted:
                    continue
                ts = self._ts_of(kind, row)
                if start_ms is not None and ts is not None and ts < int(start_ms):
                    continue
                if end_ms is not None and ts is not None and ts > int(end_ms):
                    continue
                rows.append(row)
        return rows

    # ------------------------------------------------------------------ maintenance
    def cleanup(self, today: str | None = None) -> int:
        """按 kind 保留期删除过期数据；返回删除的 date 目录数。"""
        today = today or date_str(int(self._clock() * 1000))
        cutoffs: dict[str, str] = {}
        for kind, days in self.kind_retention.items():
            try:
                cutoffs[kind] = _minus_days(today, days)
            except ValueError:
                continue
        removed = 0
        try:
            names = os.listdir(self.base_dir)
        except OSError:
            return 0
        for name in names:
            if not name.startswith("date="):
                continue
            date = name[5:]
            if not is_valid_date(date):
                continue
            date_dir = os.path.join(self.base_dir, name)
            for sub in os.listdir(date_dir) if os.path.isdir(date_dir) else []:
                if not sub.startswith("type="):
                    continue
                kind = sub[5:]
                cutoff = cutoffs.get(kind)
                if cutoff is None or date >= cutoff:
                    continue
                try:
                    shutil.rmtree(os.path.join(date_dir, sub))
                except OSError as exc:
                    self._log("today_cache cleanup failed %s: %r" % (sub, exc))
            try:
                if not os.path.isdir(date_dir):
                    continue
                remaining = [
                    sub for sub in os.listdir(date_dir) if sub.startswith("type=")
                ]
                if not remaining:
                    manifest = os.path.join(date_dir, "manifest.json")
                    if os.path.isfile(manifest):
                        os.remove(manifest)
                    os.rmdir(date_dir)
                    removed += 1
            except OSError:
                pass
        return removed

    def status(self) -> dict:
        with self._lock:
            return {
                "started": self._started,
                "format": self._format_name(),
                "degraded": bool(self._degraded),
                "queue": self._queue.qsize(),
                "pending_rows": sum(len(v) for v in self._pending.values()),
                "dropped": int(self._dropped),
                "last_flush_age_s": (
                    round(time.time() - self._last_flush, 1) if self._last_flush else -1
                ),
                "parts": int(self._parts),
                "bytes": int(self._bytes),
                "disk_free_gb": self._disk_free_gb(),
                "dir": self.base_dir,
            }

    # ------------------------------------------------------------------ helpers
    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _scan_existing(self) -> None:
        max_seq = 0
        parts = 0
        total = 0
        for root, _dirs, files in os.walk(self.base_dir):
            for name in files:
                if not name.startswith("part-"):
                    continue
                parts += 1
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
                try:
                    max_seq = max(max_seq, int(name.split("-", 1)[1].split(".", 1)[0]))
                except (IndexError, ValueError):
                    continue
        with self._lock:
            self._seq = max_seq
            self._parts = parts
            self._bytes = total

    def _cleanup_tmp(self) -> None:
        for root, _dirs, files in os.walk(self.base_dir):
            for name in files:
                if not name.endswith(".tmp"):
                    continue
                path = os.path.join(root, name)
                try:
                    if time.time() - os.path.getmtime(path) > 3600:
                        os.remove(path)
                except OSError:
                    pass

    def _disk_free_gb(self) -> float:
        try:
            return round(shutil.disk_usage(self.base_dir).free / 1e9, 2)
        except OSError:
            return -1.0

    def _disk_low(self) -> bool:
        free = self._disk_free_gb()
        return free >= 0 and free < self.disk_min_free_gb

    @staticmethod
    def _ts_of(kind: str, row: dict) -> int | None:
        key = "minute_ts" if kind == "bar1m" else "ts_ms"
        value = row.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _hour_of(self, kind: str, row: dict) -> int:
        ts = self._ts_of(kind, row)
        if ts is None:
            return 0
        return datetime.fromtimestamp(ts / 1000.0, CN_TZ).hour

    @staticmethod
    def _normalize_row(kind: str, row: dict) -> dict:
        out = dict(row)
        out["date"] = str(out.get("date") or "")
        out["symbol"] = normalize_symbol(out.get("symbol")) or str(out.get("symbol") or "")
        if kind == "bar1m":
            out["minute_ts"] = int(out.get("minute_ts") or 0)
            for key in (
                "open", "high", "low", "close", "volume", "amount", "vwap",
                "cum_volume", "cum_amount",
            ):
                out[key] = float(out.get(key) or 0.0)
            out["final"] = bool(out.get("final", True))
            out["source"] = str(out.get("source") or "qmt")
            if not is_valid_date(out["date"]):
                out["date"] = date_str(out["minute_ts"])
        elif kind == "tick_hot":
            out["ts_ms"] = int(out.get("ts_ms") or 0)
            for key in ("last", "volume", "amount", "bid1", "ask1"):
                out[key] = float(out.get(key) or 0.0)
            out["source"] = str(out.get("source") or "qmt")
            if not is_valid_date(out["date"]):
                out["date"] = date_str(out["ts_ms"])
        elif kind == "snapshot3s":
            out["ts_ms"] = int(out.get("ts_ms") or 0)
            for key in (
                "last", "open", "high", "low", "pre_close", "volume", "amount",
                "bid1", "ask1", "bid_vol1", "ask_vol1",
            ):
                out[key] = float(out.get(key) or 0.0)
            out["source"] = str(out.get("source") or "qmt_snapshot")
            out["quality"] = str(out.get("quality") or "ok")
            if not is_valid_date(out["date"]):
                out["date"] = date_str(out["ts_ms"])
        return out


def _read_jsonl(path: str) -> list[dict]:
    rows: list[dict] = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _minus_days(date: str, days: int) -> str:
    if not is_valid_date(date):
        raise ValueError("bad date")
    dt = datetime(
        int(date[:4]), int(date[4:6]), int(date[6:8]), tzinfo=CN_TZ
    ) - timedelta(days=int(days))
    return dt.strftime("%Y%m%d")
