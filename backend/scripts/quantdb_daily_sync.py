#!/usr/bin/env python3
"""
QuantDB 每日数据同步 — A 股唯一数据同步入口
=============================================

数据流:
  1. QuantDB SDK sync_dataset() → data/quantdb/ (parquet 增量更新)
  2. 从 parquet 批量填充 PG stock_daily_latest (DuckDB join + execute_values)
  3. 增量更新 Qlib 缓存 (从 parquet 生成 → data/quantdb/.qlib_cache/cn_data)

用法:
  # 每日增量同步 (推荐 cron 任务)
  python backend/scripts/quantdb_daily_sync.py

  # 全量重灌 (从 2016-01-04 起)
  python backend/scripts/quantdb_daily_sync.py --full

  # 仅同步 parquet (不更新 PG/Qlib)
  python backend/scripts/quantdb_daily_sync.py --parquet-only

  # 指定数据集
  python backend/scripts/quantdb_daily_sync.py --datasets daily_forward,valuation

  # 查看状态
  python backend/scripts/quantdb_daily_sync.py --status
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from collections.abc import Callable
from typing import Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("quantdb_daily_sync")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
QUANTDB_DATA_DIR = Path(os.getenv("QM_QUANTDB_DATA_DIR", str(PROJECT_ROOT / "data" / "quantdb")))

# V2 分区数据集 (按交易日分区, sync_dataset 增量同步)
V2_DATASETS = [
    {"category_id": "1", "sub_category": "daily_unadjusted", "dir": "1_kline_data"},
    {"category_id": "1", "sub_category": "daily_forward", "dir": "1_kline_data"},
    {"category_id": "1", "sub_category": "daily_backward", "dir": "1_kline_data"},
    {"category_id": "1", "sub_category": "index_daily", "dir": "1_kline_data"},
    {"category_id": "2", "sub_category": "margin_trading", "dir": "2_base_sector"},
    {"category_id": "5", "sub_category": "valuation", "dir": "5_technical_derived"},
    {"category_id": "5", "sub_category": "technical_indicators", "dir": "5_technical_derived"},
    {"category_id": "5", "sub_category": "market_sentiment", "dir": "5_technical_derived"},
    {"category_id": "6", "sub_category": "features_daily", "dir": "6_ml_datasets"},
    {"category_id": "6", "sub_category": "l1_factors", "dir": "6_ml_datasets"},
    {"category_id": "6", "sub_category": "l2_factors", "dir": "6_ml_datasets"},
    {"category_id": "6", "sub_category": "l1_l2_factors", "dir": "6_ml_datasets"},
]

# 每天全量重写的分区数据集：上游每次 release 都会整段重算（如前复权/后复权
# K 线，除权除息后历史价格全部回溯改写）。对这些数据集，本地不能只看
# "key 已存在"就跳过，必须比对服务端 sha256 —— 变化则覆盖重下，否则会
# 停留在上次 release 的旧复权基准（表现为"前复权只同步了一天"）。
FULL_REWRITE_V2_DATASETS = {
    "daily_forward",
    "daily_backward",
    "daily_unadjusted",
    "index_daily",
}

# V1 非分区数据集 (全量 ETag 增量)
V1_DATASETS = [
    {"category_id": "2", "sub_category": "sector_concept", "dir": "2_base_sector"},
    {"category_id": "2", "sub_category": "instrument_detail", "dir": "2_base_sector"},
    {"category_id": "2", "sub_category": "index_weights", "dir": "2_base_sector"},
    {"category_id": "2", "sub_category": "trading_calendar", "dir": "2_base_sector"},
    {"category_id": "3", "sub_category": "balance", "dir": "3_financial_data"},
    {"category_id": "3", "sub_category": "income", "dir": "3_financial_data"},
    {"category_id": "3", "sub_category": "cashflow", "dir": "3_financial_data"},
    {"category_id": "3", "sub_category": "capital", "dir": "3_financial_data"},
    {"category_id": "3", "sub_category": "pershare_index", "dir": "3_financial_data"},
    {"category_id": "3", "sub_category": "dividend_factors", "dir": "3_financial_data"},
    {"category_id": "3", "sub_category": "holder_num", "dir": "3_financial_data"},
]

# DB config
DB_HOST = os.getenv("DB_HOST", os.getenv("DB_MASTER_HOST", "127.0.0.1"))
DB_PORT = int(os.getenv("DB_PORT", os.getenv("DB_MASTER_PORT", "5432")))
DB_NAME = os.getenv("DB_NAME", "quantmind")
DB_USER = os.getenv("DB_USER", "quantmind")
DB_PASS = os.getenv("DB_PASSWORD", "quantmind")


# ---------------------------------------------------------------------------
# SDK client
# ---------------------------------------------------------------------------
def _make_client():
    from backend.shared.runtime_secrets import get_secret
    from quantdb_sdk import QuantDBClient
    # 任务执行时动态读取：管理台换 key 后，下一次同步（含 Celery 定时任务）
    # 无需重启进程即用新 key
    api_key = get_secret("QUANTDB_API_KEY")
    if not api_key:
        raise RuntimeError("QUANTDB_API_KEY 未配置")
    return QuantDBClient(api_key=api_key, timeout=(15, 300), max_retries=3)


# ---------------------------------------------------------------------------
# Phase 1: sync_dataset() → parquet
# ---------------------------------------------------------------------------
SYNC_WORKERS = 8

# SDK 把同步状态库放在 ~/.quantdb_state/，文件名由 save_dir 路径生成。
# 容器内需挂载此目录（docker-compose 里已配置 volume）。
_STATE_DIR = Path(os.getenv("QUANTDB_STATE_DIR", str(Path.home() / ".quantdb_state")))


def _state_dir() -> Path:
    """状态库目录：调用时读 env（测试可隔离），缺省回退模块级常量。"""
    return Path(os.getenv("QUANTDB_STATE_DIR", str(_STATE_DIR)))


def _state_path(root: Path | None = None) -> Path:
    """状态库文件路径：文件名由数据根目录路径生成（换目录即换库）。

    root 缺省用 QUANTDB_DATA_DIR；本地扫描等外部调用可传入其它数据根
    以复用同一命名规则。
    """
    resolved = str((root or QUANTDB_DATA_DIR).resolve())
    name = "quantdb_sync_" + resolved.replace("/", "_").replace("\\", "_").strip("_") + ".sqlite"
    return _state_dir() / name


def _open_state():
    import sqlite3

    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_state_path()), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS objects ("
        "key TEXT PRIMARY KEY, etag TEXT, sha256 TEXT, size INTEGER,"
        " path TEXT, layout TEXT, dataset TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS releases (dataset TEXT PRIMARY KEY, release_id TEXT NOT NULL)"
    )
    return conn


def sha256_of(path: Path) -> str:
    """分块读文件计算 sha256（内存友好）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def md5_of(path: Path) -> str:
    """分块读文件计算 md5（内存友好）。"""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _is_patch_key(key: str) -> bool:
    """patches/ 是上游重算的中间态快照，与 dt=* 分区重复且复权基准可能过期。

    实测 20260727.2/20260801.3 等 patch 在自身末日违反「前复权末日价==不复权价」
    不变式（如 300806.SZ 报 41.05、当日实际 57.48），而 dt 分区 5529/5529 全部满足。
    dt 分区已吸收更晚 release 的重算结果，故跳过 patches 可省流量且避免引入过期基准。
    """
    return "/patches/" in key or key.startswith("releases/")


def _is_partition_file(path: Path) -> bool:
    """判断是否为 V2 分区文件 (dt=YYYYMMDD/data.parquet)。"""
    return "/dt=" in str(path) and path.name == "data.parquet"


def _download_object(client, dataset: str, cat_id: str, key: str, target: Path, layout: str):
    """下载单个对象，返回 (sha256, md5, size)。不校验服务端声明的 size。"""
    params = {"category_id": cat_id, "sub_category": dataset, "layout": layout}
    if layout == "v2":
        params["object_key"] = key
    else:
        params["symbol"] = ""
    resp = client._download_stream(params)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    h_sha, h_md5 = hashlib.sha256(), hashlib.md5()
    try:
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(1 << 20):
                if chunk:
                    fh.write(chunk)
                    h_sha.update(chunk)
                    h_md5.update(chunk)
        size = tmp.stat().st_size
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return h_sha.hexdigest(), h_md5.hexdigest(), size


def _sync_v2_dataset(client, state, cat_id: str, dataset: str, progress_cb: Callable | None = None, should_cancel: Callable[[], bool] | None = None) -> tuple[int, int]:
    """同步 V2 分区数据集。跳过 patches，不校验服务端 size 声明。

    先走 releases 增量，再用 manifest 补漏——服务端可能已将新数据写入 manifest
    但尚未发布 release，导致 releases-only 同步漏掉最新交易日。
    """
    # ---- Phase 1: releases 增量 ----
    data = client._get("/api/v1/data/releases", {"datasets": dataset, "after_release": ""})
    releases = data.get("releases", [])

    # 同一 key 可被多份 manifest 引用，以 cursor 序列中最新一份为准。
    latest: dict[str, dict] = {}
    for rel in releases:
        for obj in rel.get("objects", []):
            key = client._normalise_release_key(obj["key"])
            if _is_patch_key(key):
                continue
            latest[key] = obj

    # ---- Phase 2: manifest 补漏 ----
    try:
        manifest = client.query_manifest(category_id=cat_id, sub_category=dataset)
        if manifest:
            for obj in manifest:
                key = obj.get("key", "")
                if not key or _is_patch_key(key):
                    continue
                if key not in latest:
                    latest[key] = obj
            if manifest and len(latest) > (sum(len(r.get("objects", [])) for r in releases) if releases else 0):
                log.info("[V2] %s: manifest 补漏，releases %d → 合计 %d",
                         dataset,
                         sum(len(r.get("objects", [])) for r in releases),
                         len(latest))
    except Exception as exc:
        log.warning("[V2] %s: manifest 补漏查询失败（不影响 releases 数据）: %s", dataset, str(exc)[:120])

    if not latest:
        return 0, 0

    pending = []
    verify = []
    is_full_rewrite = dataset in FULL_REWRITE_V2_DATASETS
    for key, obj in latest.items():
        rel_path = obj.get("relative_path") or key
        target = QUANTDB_DATA_DIR / rel_path
        row = state.execute("SELECT path FROM objects WHERE key=?", (key,)).fetchone()
        if row and Path(row[0]).exists() and Path(row[0]).stat().st_size > 0:
            # 每天全量重写的数据集（前复权等）：已登记也要比对服务端 sha，
            # 服务端每次 release 重算都会改历史分区，sha 变了必须覆盖重下。
            if is_full_rewrite:
                verify.append((key, obj, target))
            continue
        # 状态库无登记但文件已在磁盘：与云端 sha256 对上就登记跳过，
        # 绝不整库重下（2026-08-17 状态库丢失曾把 1.3 万分区全量重拉）
        if (
            target.exists()
            and target.stat().st_size > 0
            and str(obj.get("sha256") or "").strip()
        ):
            verify.append((key, obj, target))
        else:
            pending.append((key, obj, target))

    if verify:
        verified_count = 0

        def verify_work(item):
            key, obj, target = item
            expected = str(obj.get("sha256") or "").strip().lower()
            actual = sha256_of(target)
            return key, obj, target, actual if (actual and actual == expected) else None

        with ThreadPoolExecutor(max_workers=SYNC_WORKERS) as pool:
            for key, obj, target, actual in pool.map(verify_work, verify):
                if actual is None:
                    pending.append((key, obj, target))
                    continue
                verified_count += 1
                state.execute(
                    "INSERT OR REPLACE INTO objects(key,etag,sha256,size,path,layout,dataset)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (key, obj.get("etag", ""), actual, target.stat().st_size,
                     str(target), "v2_daily_partition", dataset),
                )
        state.commit()
        log.info("[V2] %s: %d 个已有文件与云端哈希一致，登记跳过（免重下）",
                 dataset, verified_count)

    if not pending:
        return 0, 0

    log.info("[V2] %s: 需下载 %d / %d", dataset, len(pending), len(latest))
    done = errors = 0

    def work(item):
        key, obj, target = item
        sha, _md5, size = _download_object(client, dataset, cat_id, key, target, "v2")
        return key, obj, target, sha, size

    with ThreadPoolExecutor(max_workers=SYNC_WORKERS) as pool:
        futures = {pool.submit(work, it): it[0] for it in pending}
        for fut in as_completed(futures):
            try:
                key, obj, target, sha, size = fut.result()
            except Exception as exc:
                errors += 1
                log.warning("[V2] %s: %s 失败 %s", dataset, futures[fut], str(exc)[:90])
                continue
            state.execute(
                "INSERT OR REPLACE INTO objects(key,etag,sha256,size,path,layout,dataset)"
                " VALUES(?,?,?,?,?,?,?)",
                (key, obj.get("etag", ""), sha, size, str(target), "v2_daily_partition", dataset),
            )
            done += 1
            if done % 200 == 0:
                log.info("[V2] %s: %d/%d", dataset, done, len(pending))
                if progress_cb:
                    progress_cb("file", dataset=dataset, done=done, total=len(pending))
            # 协作式取消：当前文件已落盘登记，再停止后续新任务。
            if should_cancel and should_cancel():
                pool.shutdown(wait=False, cancel_futures=True)
                log.info("[V2] %s: 收到取消信号，当前文件完成后停止 (%d/%d)", dataset, done, len(pending))
                state.commit()
                return done, errors

    if releases:
        state.execute(
            "INSERT OR REPLACE INTO releases(dataset,release_id) VALUES(?,?)",
            (dataset, releases[-1]["release_id"]),
        )
    state.commit()
    log.info("[V2] %s: 下载 %d, 失败 %d", dataset, done, errors)
    return done, errors


def _sync_v1_dataset(client, state, cat_id: str, dataset: str, progress_cb: Callable | None = None, should_cancel: Callable[[], bool] | None = None) -> tuple[int, int]:
    """同步 V1 全量数据集。用 etag(md5) 校验，跳过服务端 size 声明。"""
    manifest = client.query_manifest(category_id=cat_id, sub_category=dataset)
    if not manifest:
        return 0, 0

    pending = []
    for obj in manifest:
        key = obj["key"]
        remote_etag = (obj.get("etag") or "").strip('"')
        rel_path = obj.get("relative_path") or key
        target = QUANTDB_DATA_DIR / rel_path
        row = state.execute("SELECT etag, path FROM objects WHERE key=?", (key,)).fetchone()
        if row:
            local_etag = (row[0] or "").strip('"')
            local_path = row[1]
            # ETag 未变且文件存在 → 跳过
            if local_etag and local_etag == remote_etag and local_path and Path(local_path).exists() and Path(local_path).stat().st_size > 0:
                continue
        # 状态库无登记但文件已在磁盘：md5 与云端 etag 对上就登记跳过，
        # 绝不整库重下（与 V2 同因 2026-08-17 全量重拉事故）
        if (
            target.exists()
            and target.stat().st_size > 0
            and remote_etag
            and "-" not in remote_etag
            and md5_of(target) == remote_etag
        ):
            state.execute(
                "INSERT OR REPLACE INTO objects(key,etag,sha256,size,path,layout,dataset)"
                " VALUES(?,?,?,?,?,?,?)",
                (key, f'"{remote_etag}"', sha256_of(target), target.stat().st_size,
                 str(target), "v1_symbol", dataset),
            )
            continue
        pending.append((key, obj, target))

    if not pending:
        return 0, 0

    log.info("[V1] %s: 需下载 %d / %d", dataset, len(pending), len(manifest))
    done = errors = 0

    def work(item):
        key, obj, target = item
        params = {
            "category_id": cat_id,
            "sub_category": dataset,
            "layout": "v1",
            "symbol": obj.get("symbol", ""),
        }
        resp = client._download_stream(params)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        h_sha, h_md5 = hashlib.sha256(), hashlib.md5()
        try:
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(1 << 20):
                    if chunk:
                        fh.write(chunk)
                        h_sha.update(chunk)
                        h_md5.update(chunk)
            size = tmp.stat().st_size
            expected = (obj.get("etag") or "").strip('"')
            if expected and "-" not in expected and h_md5.hexdigest() != expected:
                raise RuntimeError(f"MD5 不符 期望{expected[:10]} 实际{h_md5.hexdigest()[:10]}")
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return key, obj, target, h_sha.hexdigest(), h_md5.hexdigest(), size

    with ThreadPoolExecutor(max_workers=SYNC_WORKERS) as pool:
        futures = {pool.submit(work, it): it[0] for it in pending}
        for fut in as_completed(futures):
            try:
                key, obj, target, sha, md5, size = fut.result()
            except Exception as exc:
                errors += 1
                log.warning("[V1] %s: %s 失败 %s", dataset, futures[fut], str(exc)[:90])
                continue
            state.execute(
                "INSERT OR REPLACE INTO objects(key,etag,sha256,size,path,layout,dataset)"
                " VALUES(?,?,?,?,?,?,?)",
                (key, f'"{md5}"', sha, size, str(target), "v1_symbol", dataset),
            )
            done += 1
            if progress_cb and done % 100 == 0:
                progress_cb("file", dataset=dataset, done=done, total=len(pending))
            # 协作式取消：当前文件已落盘登记，再停止后续新任务。
            if should_cancel and should_cancel():
                pool.shutdown(wait=False, cancel_futures=True)
                log.info("[V1] %s: 收到取消信号，当前文件完成后停止 (%d/%d)", dataset, done, len(pending))
                state.commit()
                return done, errors

    state.commit()
    if progress_cb:
        progress_cb("file", dataset=dataset, done=done, total=len(pending))
    log.info("[V1] %s: 下载 %d, 失败 %d", dataset, done, errors)
    return done, errors


def reseed_state(datasets: list[dict] | None = None) -> dict:
    """用本地已有文件的哈希重建 SDK 状态库。

    状态库按 save_dir 路径命名，换目录（如 NAS→本地）后是空库，
    不重建会导致已有数据被全量重下。
    """
    if datasets is None:
        datasets = V2_DATASETS + V1_DATASETS

    client = _make_client()
    state = _open_state()
    summary = {"seeded": 0, "per_dataset": {}}

    for ds in datasets:
        sub, cat_id = ds["sub_category"], ds["category_id"]
        is_v2 = ds in V2_DATASETS
        rows = []

        if is_v2:
            data = client._get("/api/v1/data/releases", {"datasets": sub, "after_release": ""})
            releases = data.get("releases", [])
            latest = {}
            for rel in releases:
                for obj in rel.get("objects", []):
                    key = client._normalise_release_key(obj["key"])
                    if _is_patch_key(key):
                        continue
                    latest[key] = obj

            def check_v2(item, _sub=sub):
                key, obj = item
                target = QUANTDB_DATA_DIR / (obj.get("relative_path") or key)
                if not target.exists() or target.stat().st_size == 0:
                    return None
                expected = (obj.get("sha256") or "").lower()
                actual = sha256_of(target)
                if expected and actual != expected:
                    return None
                return (key, obj.get("etag", ""), actual, target.stat().st_size,
                        str(target), "v2_daily_partition", _sub)

            with ThreadPoolExecutor(max_workers=SYNC_WORKERS) as pool:
                rows = [r for r in pool.map(check_v2, latest.items()) if r]

        if releases:
            state.execute(
                "INSERT OR REPLACE INTO releases(dataset,release_id) VALUES(?,?)",
                (sub, releases[-1]["release_id"]),
            )
        else:
            manifest = client.query_manifest(category_id=cat_id, sub_category=sub)

            def check_v1(obj, _sub=sub):
                key = obj["key"]
                target = QUANTDB_DATA_DIR / (obj.get("relative_path") or key)
                if not target.exists() or target.stat().st_size == 0:
                    return None
                expected = (obj.get("etag") or "").strip('"')
                actual_md5 = md5_of(target)
                if expected and "-" not in expected and actual_md5 != expected:
                    return None
                return (key, f'"{actual_md5}"', sha256_of(target), target.stat().st_size,
                        str(target), "v1_symbol", _sub)

            with ThreadPoolExecutor(max_workers=SYNC_WORKERS) as pool:
                rows = [r for r in pool.map(check_v1, manifest) if r]

        if rows:
            state.executemany(
                "INSERT OR REPLACE INTO objects(key,etag,sha256,size,path,layout,dataset)"
                " VALUES(?,?,?,?,?,?,?)",
                rows,
            )
            state.commit()
        summary["per_dataset"][sub] = len(rows)
        summary["seeded"] += len(rows)
        log.info("[RESEED] %s: 登记 %d 个已验证文件", sub, len(rows))

    state.close()
    log.info("状态库重建完成: 共 %d 个对象", summary["seeded"])
    return summary


def sync_parquet(datasets: list[dict] | None = None, *, dry_run: bool = False, progress_cb: Callable | None = None, should_cancel: Callable[[], bool] | None = None) -> dict:
    """增量同步 QuantDB parquet 数据。

    不走 SDK 的 sync_dataset()：服务端 manifest 的 size 声明与实际文件不符
    （如 trading_calendar 声明 15224、实际 15203），SDK 会因 size 校验失败
    整个数据集中断。这里改为自行下载 + 哈希校验，并跳过 patches 对象。
    """
    if datasets is None:
        datasets = V2_DATASETS + V1_DATASETS

    if dry_run:
        log.info("dry-run: 跳过实际下载")
        return {"synced": 0, "up_to_date": 0, "errors": [], "total_downloaded": 0}

    client = _make_client()
    state = _open_state()
    results = {"synced": 0, "up_to_date": 0, "errors": [], "total_downloaded": 0}

    for idx, ds in enumerate(datasets):
        sub, cat_id = ds["sub_category"], ds["category_id"]
        # 协作式取消：开始下一数据集前检查，取消则跳过剩余全部数据集。
        if should_cancel and should_cancel():
            results["cancelled"] = True
            log.info("[quantdb] 收到取消信号，停止剩余数据集（已处理 %d/%d）", idx, len(datasets))
            break
        is_v2 = ds in V2_DATASETS
        if progress_cb:
            progress_cb("dataset_start", dataset=sub, index=idx, total=len(datasets))
        try:
            if is_v2:
                done, errs = _sync_v2_dataset(client, state, cat_id, sub, progress_cb=progress_cb, should_cancel=should_cancel)
            else:
                done, errs = _sync_v1_dataset(client, state, cat_id, sub, progress_cb=progress_cb, should_cancel=should_cancel)
            if progress_cb:
                progress_cb("dataset_done", dataset=sub, synced=done, errors=errs)
            if done:
                results["synced"] += 1
                results["total_downloaded"] += done
            else:
                results["up_to_date"] += 1
                log.info("[OK] %s: 已最新", sub)
            if errs:
                results["errors"].append(f"{sub}: {errs} 个对象下载失败")
        except Exception as exc:
            results["errors"].append(f"{sub}: {exc}")
            log.warning("[FAIL] %s: %s", sub, exc)
            client = _make_client()

    state.close()
    log.info(
        "Parquet sync: %d synced, %d up-to-date, %d errors, %d files downloaded",
        results["synced"], results["up_to_date"], len(results["errors"]),
        results["total_downloaded"],
    )
    return results


# ---------------------------------------------------------------------------
# Phase 2: parquet → PG fill
# ---------------------------------------------------------------------------
def _get_engine():
    from sqlalchemy import create_engine
    from urllib.parse import quote_plus as _q
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        db_url = f"postgresql+psycopg2://{DB_USER}:{_q(DB_PASS)}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    elif "asyncpg" in db_url:
        db_url = db_url.replace("asyncpg", "psycopg2")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return create_engine(db_url, pool_pre_ping=True)


def _to_internal(symbol: str) -> str:
    """600036.SH -> SH600036 (PG internal format)"""
    s = symbol.strip().upper()
    if "." in s:
        code, ex = s.split(".", 1)
        return f"{ex}{code}"
    return s


QUANTDB_EPOCH = date(2016, 1, 4)

# PG stock_daily_latest <- QuantDB 视图列映射
_KLINE_COLS = ("open", "high", "low", "close", "volume", "amount")

# features_daily = technical_indicators + valuation 合并表，覆盖 2016~今全序列。
# 不用 qdb_technical_indicators：它只有 595 天（2018-06~2026-07 整段缺失）。
#
# ⚠️ 复权口径（2026-08-17 修）：features_daily 的 close/ma5~ma60/vol_atr_14 是
# **后复权**（对应 qdb_daily_backward 口径，如 002832 的 146.90），而 OHLCV 取
# 自 qdb_daily_forward（前复权 26.08）→ 同一行两个价格体系，risk 评分卡和报告
# 误判「跌破均线」「极端波动」。价格类指标（ma5/10/20/60、ma_gap_*、vol_atr_14）
# 改为基于 forward close 用窗口函数重算，与 OHLCV 同口径；features_daily 只保留
# 非价格类字段（估值/波动率%等）。
_FEATURE_COLS = {
    "pe_ttm": "pe_ttm",
    "pb": "pb",
    "total_mv": "total_mv",
    "float_mv": "float_mv",
    "return_1d": "return_1d", "return_3d": "return_3d", "return_5d": "return_5d",
    "return_10d": "return_10d", "return_20d": "return_20d", "return_60d": "return_60d",
    "vol_std_5": "vol_std_5", "vol_std_20": "vol_std_20", "vol_std_60": "vol_std_60",
    "rsi_14": "rsi_14", "rsi_6": "rsi_6",
    "macd_hist": "macd_hist", "kdj_k": "kdj_k", "beta_20": "beta_20",
    "volume_ma_3": "volume_ma_5", "amount_ma_5": "amount_ma_5",
    "pct_change": "pct_change",
    "vol_to_ma5": "volume_ratio_5", "vol_to_ma20": "volume_ratio_20",
}
# 价格类指标：基于 qdb_daily_forward close 用 DuckDB 窗口函数重算
# （源表是后复权口径，与 OHLCV 混用导致风险评分/报告误判）
_PRICE_DERIVED_COLS = ("ma5", "ma10", "ma20", "ma60", "ma_gap_5", "ma_gap_10",
                       "ma_gap_20", "vol_atr_14")
# PG volume_trend_3d 是 boolean（量能是否上升），QuantDB 同名列是数值趋势，语义不同，不映射


def _trade_dates(hub, start: date, end: date) -> list[date]:
    """从 daily_forward 分区目录枚举可用交易日。"""
    root = Path(hub.data_dir) / "1_kline_data" / "daily_forward"
    out = []
    for p in sorted(root.glob("dt=*")):
        try:
            d = datetime.strptime(p.name[3:], "%Y%m%d").date()
        except ValueError:
            continue
        if start <= d <= end:
            out.append(d)
    return out


def _add_price_derived_cols(df: pd.DataFrame) -> pd.DataFrame:
    """基于前复权 forward OHLCV 重算价格派生指标（与 OHLCV 同口径）。

    - ma5/10/20/60：close.rolling(n).mean()
    - ma_gap_N：(close/maN − 1) × 100（百分数口径，与 features_daily 一致）
    - vol_atr_14：TR = max(high−low, |high−prev_close|, |low−prev_close|)，
      Wilder 平滑（ewm alpha=1/14，与 features_daily 的 6.4185 实测一致）

    调用方需先拉取回看窗口数据（本函数只负责计算），再按区间裁剪。
    """
    if df.empty:
        return df
    df = df.sort_values("trade_date")
    derived = pd.DataFrame(index=df.index)
    for sym, grp in df.groupby("symbol", sort=False):
        close = grp["close"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat(
            [
                grp["high"].astype(float) - grp["low"].astype(float),
                (grp["high"].astype(float) - prev_close).abs(),
                (grp["low"].astype(float) - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        # Wilder 平滑：ATR_t = (ATR_{t-1} × 13 + TR_t) / 14
        atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
        for n, col in ((5, "ma5"), (10, "ma10"), (20, "ma20"), (60, "ma60")):
            derived.loc[grp.index, col] = close.rolling(n).mean()
        for n in (5, 10, 20):
            ma = close.rolling(n).mean()
            derived.loc[grp.index, f"ma_gap_{n}"] = (close / ma - 1) * 100
        derived.loc[grp.index, "vol_atr_14"] = atr
    for c in _PRICE_DERIVED_COLS:
        df[c] = derived[c]
    return df


def _pg_latest_trade_date() -> date | None:
    """返回 PG stock_daily_latest 已写入的最大交易日；探测失败时返回 None。"""
    try:
        engine = _get_engine()
        from sqlalchemy import text as sql_text
        with engine.begin() as conn:
            row = conn.execute(
                sql_text("SELECT MAX(trade_date) FROM stock_daily_latest")
            ).scalar()
        return row
    except Exception as exc:
        log.warning("查询 PG MAX(trade_date) 失败: %s", exc)
        return None


def fill_pg_from_parquet(
    symbols: list[str] | None = None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    batch_days: int = 20,
) -> dict:
    """从 QuantDB parquet 批量填充 PG stock_daily_latest。

    按交易日分批，单条 SQL join kline+valuation+technical_indicators，
    用 execute_values 批量 upsert（逐行 execute 在全量场景下不可用）。
    """
    from psycopg2.extras import execute_values

    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    hub = QuantDBDataHub(QUANTDB_DATA_DIR)
    if not hub.available:
        return {"status": "skipped", "reason": "QuantDB data dir not available"}

    # 增量模式（未显式传 start_date）：探测 PG 已写入的最大交易日并往前回溯若干
    # 自然日作为重灌起点，避免每次都把 QUANTDB_EPOCH(2016) 以来的数据全量重灌。
    # 重叠部分由 ON CONFLICT DO UPDATE 的 upsert 语义保证幂等安全。
    if start_date is not None:
        start = start_date
    else:
        latest = _pg_latest_trade_date()
        if latest is not None:
            # 回溯 7 自然日（覆盖约 5 个交易日），确保最近批次被重叠重写
            start = latest - timedelta(days=7)
            log.info("PG 增量起点: %s（PG 最大 %s，回溯 7 自然日）", start, latest)
        else:
            log.info("PG stock_daily_latest 为空或探测失败，PG 阶段从 %s 全量重灌", QUANTDB_EPOCH)
            start = QUANTDB_EPOCH
    end = end_date or date.today()
    days = _trade_dates(hub, start, end)
    if not days:
        return {"status": "skipped", "reason": f"no trade dates in {start}~{end}"}

    feat_cols = list(_FEATURE_COLS)
    pg_cols = (
        ["trade_date", "symbol", "adj_factor"]
        + list(_KLINE_COLS)
        + [_FEATURE_COLS[c] for c in feat_cols]
        + list(_PRICE_DERIVED_COLS)
    )
    non_pk = [c for c in pg_cols if c not in ("trade_date", "symbol")]
    update_set = ", ".join(f"{c}=EXCLUDED.{c}" for c in non_pk)
    insert_sql = (
        f"INSERT INTO stock_daily_latest ({', '.join(pg_cols)}) VALUES %s "
        f"ON CONFLICT (trade_date, symbol) DO UPDATE SET {update_set}"
    )

    conn_duck = hub._get_duck_conn()
    engine = _get_engine()
    has_feat = hub._view_exists("qdb_features_daily")
    if not has_feat:
        log.warning("qdb_features_daily 视图缺失，仅写入 OHLCV")

    sym_filter = ""
    if symbols:
        qdb_syms = {s for s in symbols} | {
            f"{s[2:]}.{s[:2]}" for s in symbols if len(s) > 2 and s[:2] in ("SH", "SZ", "BJ")
        }
        quoted = ", ".join("'" + s.replace("'", "''") + "'" for s in sorted(qdb_syms))
        sym_filter = f" AND k.symbol IN ({quoted})"

    total_rows = 0
    failed_days: list[str] = []

    for i in range(0, len(days), batch_days):
        chunk = days[i:i + batch_days]
        lo, hi = chunk[0], chunk[-1]
        feat_sel = "".join(
            f", f.{src} AS {dst}" for src, dst in _FEATURE_COLS.items()
        ) if has_feat else "".join(
            f", NULL AS {dst}" for dst in _FEATURE_COLS.values()
        )
        feat_join = (
            " LEFT JOIN qdb_features_daily f ON f.symbol = k.symbol AND f.dt = k.dt"
            if has_feat else ""
        )
        # 回看 160 自然日（≈110 交易日），覆盖 ma60 窗口与 ATR Wilder 预热，
        # 产出时再裁到本批 [lo, hi]
        lookback = (lo - timedelta(days=160)).strftime("%Y%m%d")
        sql = (
            f"SELECT k.dt, k.symbol, k.open, k.high, k.low, k.close, "
            f"k.volume, k.amount{feat_sel} "
            f"FROM qdb_daily_forward k{feat_join} "
            f"WHERE k.dt >= {lookback} AND k.dt <= {hi:%Y%m%d}{sym_filter}"
        )

        try:
            df = conn_duck.execute(sql).fetchdf()
        except Exception as exc:
            log.warning("duckdb query failed %s~%s: %s", lo, hi, exc)
            failed_days.append(f"{lo}~{hi}")
            continue

        if df.empty:
            continue

        df["symbol"] = df["symbol"].map(lambda s: _to_internal(str(s)))
        df["adj_factor"] = 1.0

        for c in _KLINE_COLS:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        # dt 为 Hive 分区整数 YYYYMMDD -> PG DATE
        df["trade_date"] = pd.to_datetime(
            df["dt"].astype("int64").astype(str), format="%Y%m%d"
        ).dt.date
        df = df.drop(columns=["dt"])

        # 价格派生指标：基于 forward close（前复权，与 OHLCV 同口径）重算，
        # 与 features_daily 的后复权 ma*/ma_gap*/vol_atr_14 口径对齐问题见
        # 模块顶部注释（2026-08-17 修复）
        df = _add_price_derived_cols(df)

        df = df.replace([float("inf"), float("-inf")], None)
        df = df.astype(object).where(pd.notna(df), None)
        # 裁掉回看窗口，只写本批区间
        df = df[df["trade_date"] >= lo]
        if df.empty:
            continue

        records = [tuple(r) for r in df[pg_cols].itertuples(index=False, name=None)]
        try:
            raw = engine.raw_connection()
            try:
                with raw.cursor() as cur:
                    execute_values(cur, insert_sql, records, page_size=5000)
                raw.commit()
            finally:
                raw.close()
            total_rows += len(records)
            log.info("PG fill %s~%s: %d rows (total %d)", lo, hi, len(records), total_rows)
        except Exception as exc:
            log.warning("PG upsert failed %s~%s: %s", lo, hi, exc)
            failed_days.append(f"{lo}~{hi}")

    log.info("PG fill done: %d rows, %d failed batches", total_rows, len(failed_days))
    return {
        "status": "ok" if not failed_days else "partial",
        "rows": total_rows,
        "trade_dates": len(days),
        "failed_batches": failed_days,
    }


# ---------------------------------------------------------------------------
# Phase 3: Qlib cache incremental update
# ---------------------------------------------------------------------------
def update_qlib_cache() -> dict:
    """增量更新 Qlib 缓存 (从 QuantDB parquet 生成)。"""
    try:
        from backend.services.engine.qlib_data_builder import ensure_qlib_cache
        provider_uri = ensure_qlib_cache(QUANTDB_DATA_DIR)
        return {"status": "ok", "provider_uri": provider_uri}
    except Exception as exc:
        log.warning("Qlib cache update failed: %s", exc)
        return {"status": "error", "reason": str(exc)}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def show_status() -> dict:
    """显示 QuantDB 数据同步状态。"""
    status = {
        "quantdb_dir": str(QUANTDB_DATA_DIR),
        "quantdb_exists": QUANTDB_DATA_DIR.is_dir(),
    }

    if QUANTDB_DATA_DIR.is_dir():
        # 统计各数据集文件数
        for ds in V2_DATASETS + V1_DATASETS:
            sub = ds["sub_category"]
            parent = ds["dir"]
            p = QUANTDB_DATA_DIR / parent / sub
            count = sum(1 for _ in p.rglob("*.parquet")) if p.exists() else 0
            status[f"{parent}/{sub}"] = count

    # Qlib cache（统一走 qlib_paths 解析，固定目录 /data/qlib/cn_data）
    try:
        from backend.shared.qlib_paths import resolve_qlib_provider_uri
        qlib_cache = Path(resolve_qlib_provider_uri("CN"))
    except Exception:
        qlib_cache = QUANTDB_DATA_DIR / ".qlib_cache" / "cn_data"
    if qlib_cache.is_dir():
        cal_file = qlib_cache / "calendars" / "day.txt"
        if cal_file.exists():
            lines = cal_file.read_text().strip().splitlines()
            status["qlib_cache_calendar"] = len(lines)
            if lines:
                status["qlib_cache_range"] = f"{lines[0]} ~ {lines[-1]}"
        feat_dir = qlib_cache / "features"
        if feat_dir.exists():
            status["qlib_cache_symbols"] = sum(1 for d in feat_dir.iterdir() if d.is_dir())

    # PG status
    try:
        engine = _get_engine()
        from sqlalchemy import text as sql_text
        with engine.begin() as conn:
            row = conn.execute(
                sql_text("SELECT MAX(trade_date), COUNT(DISTINCT symbol) FROM stock_daily_latest")
            ).fetchone()
            if row and row[0]:
                status["pg_latest_date"] = row[0].isoformat()
                status["pg_symbol_count"] = row[1]
    except Exception:
        pass

    return status


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _sync_extra_sources(
    *,
    days: int = 5,
    dry_run: bool = False,
    datasets: list[str] | None = None,
) -> dict:
    """同步额外数据源（北向资金/北向日频/南向资金）。

    datasets 为 None（每日同步路径）时同步所有已勾选的数据源；
    否则只同步被请求的数据集（后台管理逐数据集触发路径）。
    hsgt_north_daily 是 2017~2024 历史回填，仅显式请求时才跑，不进每日同步。
    """
    out: dict = {}
    try:
        from backend.shared.data_source_config import is_source_enabled

        north_enabled = is_source_enabled("A", "hsgt_north")
        south_enabled = is_source_enabled("A", "hsgt_south")
    except Exception:  # noqa: BLE001
        north_enabled = True
        south_enabled = True

    want_north = (datasets is None or "hsgt_north" in datasets) and north_enabled
    want_north_daily = datasets is not None and "hsgt_north_daily" in datasets and north_enabled
    want_south = (datasets is None or "hsgt_south" in datasets) and south_enabled
    log.info(
        "额外数据源: 北向=%s 北向日频=%s 南向=%s (datasets=%s)",
        want_north, want_north_daily, want_south, datasets,
    )

    if want_north:
        try:
            from backend.scripts.quantdb_north_sync import sync as north_sync

            # 北向个股 2024-08-19 起改季度披露，按季度末+第5交易日同步最新季度
            r = north_sync(latest=True, dry_run=dry_run)
            out["hsgt_north"] = r
            log.info("北向资金同步完成: %s", r)
        except Exception as exc:  # noqa: BLE001
            log.warning("北向资金同步失败: %s", exc)
            out["hsgt_north"] = {"status": "error", "error": str(exc)}

    if want_north_daily:
        try:
            from backend.scripts.akshare_north_daily import sync as north_daily_sync

            r = north_daily_sync(dry_run=dry_run)
            out["hsgt_north_daily"] = r
            log.info("北向资金日频同步完成: %s", r)
        except Exception as exc:  # noqa: BLE001
            log.warning("北向资金日频同步失败: %s", exc)
            out["hsgt_north_daily"] = {"status": "error", "error": str(exc)}

    if want_south:
        try:
            from backend.scripts.quanthk_south_sync import sync as south_sync

            r = south_sync(days=days, dry_run=dry_run)
            out["hsgt_south"] = r
            log.info("南向资金同步完成: %s", r)
        except Exception as exc:  # noqa: BLE001
            log.warning("南向资金同步失败: %s", exc)
            out["hsgt_south"] = {"status": "error", "error": str(exc)}

    return out


def run_daily_sync(
    *,
    parquet_only: bool = False,
    datasets: list[str] | None = None,
    skip_parquet: bool = False,
    skip_pg: bool = False,
    skip_qlib: bool = False,
    skip_snapshot: bool = False,
    full: bool = False,
    dry_run: bool = False,
    progress_cb: Callable | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict:
    """执行每日同步流程。"""
    result = {
        "started": datetime.now().isoformat(),
        "parquet": None,
        "l1_ohlcv_backfill": None,
        "pg_fill": None,
        "qlib_cache": None,
        "feature_snapshot": None,
    }

    # Phase 1: sync parquet
    if not skip_parquet:
        log.info("=== Phase 1: Sync QuantDB parquet ===")
        ds_list = None
        if datasets:
            all_ds = V2_DATASETS + V1_DATASETS
            ds_list = [ds for ds in all_ds if ds["sub_category"] in datasets]
        result["parquet"] = sync_parquet(ds_list, dry_run=dry_run, progress_cb=progress_cb, should_cancel=should_cancel)

        # 上游历史 L1 分区只有因子，不含 OHLCV。每次同步后修复最近窗口，
        # 防止增量文件重新覆盖后让训练标签再次为空；历史全量由专用脚本执行。
        includes_l1 = datasets is None or "l1_factors" in datasets
        if includes_l1 and not dry_run and not (should_cancel and should_cancel()):
            try:
                from backend.scripts.backfill_l1_ohlcv import backfill_l1_ohlcv

                result["l1_ohlcv_backfill"] = backfill_l1_ohlcv(
                    QUANTDB_DATA_DIR,
                    start=date.today() - timedelta(days=10),
                )
            except Exception as exc:
                log.warning("L1 OHLCV backfill failed: %s", exc)
                result["l1_ohlcv_backfill"] = {"status": "error", "reason": str(exc)}

    # Phase 1.5: 额外数据源（北向/南向，按数据源勾选控制；逐数据集路径只同步请求项）
    result["sources"] = _sync_extra_sources(dry_run=dry_run, datasets=datasets)

    if parquet_only:
        result["finished"] = datetime.now().isoformat()
        return result

    # Phase 2: fill PG from parquet
    if not skip_pg:
        log.info("=== Phase 2: Fill PG from parquet ===")
        if full:
            result["pg_fill"] = fill_pg_from_parquet(start_date=QUANTDB_EPOCH)
        else:
            result["pg_fill"] = fill_pg_from_parquet()

    # Phase 3: update Qlib cache
    if not skip_qlib:
        log.info("=== Phase 3: Update Qlib cache ===")
        result["qlib_cache"] = update_qlib_cache()

    # Phase 4 is legacy-only.  New training/inference reads the three raw
    # QuantDB factor sources and must never materialise model_features_*.parquet.
    # Keep an explicit break-glass flag solely for historical parquet models.
    legacy_snapshot_enabled = os.getenv("QM_ENABLE_LEGACY_FEATURE_SNAPSHOT", "").lower() in {"1", "true", "yes"}
    if not skip_snapshot and legacy_snapshot_enabled:
        log.info("=== Phase 4: Generate feature snapshot ===")
        try:
            from backend.scripts.generate_feature_snapshots import _build_snapshot
            year = date.today().year
            snap = _build_snapshot(year)
            if snap:
                result["feature_snapshot"] = snap
                log.info("Feature snapshot: year=%d rows=%s", year, snap.get("row_count"))
            else:
                result["feature_snapshot"] = {"status": "skipped", "reason": "no_data"}
        except Exception as exc:
            log.warning("Feature snapshot failed: %s", exc)
            result["feature_snapshot"] = {"status": "error", "reason": str(exc)}
    elif not skip_snapshot:
        result["feature_snapshot"] = {
            "status": "skipped",
            "reason": "direct QuantDB factor reader is active; legacy snapshot generation disabled",
        }

    result["finished"] = datetime.now().isoformat()
    log.info("Daily sync complete")
    return result


def repair_partitions(datasets: list[dict] | None = None, recent_days: int = 30) -> dict:
    """扫描并修复残缺的分区文件。

    对每个 V2 分区数据集，只检查最近 recent_days 天的分区文件，
    如果行数 < 同数据集最近 5 日平均行数的 60%，标记为残缺并删除状态库记录，
    下次同步时重下。历史数据不修（早期股票数量少是正常的）。
    """
    import duckdb
    from datetime import timedelta

    if datasets is None:
        datasets = V2_DATASETS

    cutoff = date.today() - timedelta(days=recent_days)
    cutoff_int = int(cutoff.strftime("%Y%m%d"))

    state = _open_state()
    results = {"scanned": 0, "repaired": 0, "details": []}

    for ds in datasets:
        sub = ds["sub_category"]
        ds_dir = QUANTDB_DATA_DIR / ds["dir"] / sub
        if not ds_dir.exists():
            continue

        # 收集最近 recent_days 的分区文件
        partitions = []
        for p in sorted(ds_dir.glob("dt=*")):
            try:
                dt_val = int(p.name[3:])
            except ValueError:
                continue
            if dt_val < cutoff_int:
                continue
            pq = p / "data.parquet"
            if pq.exists() and pq.stat().st_size > 0:
                partitions.append((p.name, pq))

        if len(partitions) < 3:
            continue

        results["scanned"] += len(partitions)

        # 用最近 5 个分区的行数作为基准
        recent = partitions[-5:]
        try:
            con = duckdb.connect()
            recent_counts = []
            for _, pq in recent:
                r = con.execute(f"SELECT count(*) FROM read_parquet('{pq}')").fetchone()
                recent_counts.append(r[0])
            baseline = sum(recent_counts) / len(recent_counts)
            con.close()
        except Exception as exc:
            log.warning("[REPAIR] %s: 无法读取基准行数: %s", sub, exc)
            continue

        threshold = baseline * 0.6

        for part_name, pq_path in partitions:
            try:
                con = duckdb.connect()
                count = con.execute(f"SELECT count(*) FROM read_parquet('{pq_path}')").fetchone()[0]
                con.close()
            except Exception:
                continue

            if count < threshold:
                log.warning("[REPAIR] %s/%s: 残缺 (rows=%d < baseline=%.0f * 0.6=%.0f)",
                            sub, part_name, count, baseline, threshold)
                state.execute("DELETE FROM objects WHERE key LIKE ?", (f"%{part_name}%",))
                pq_path.unlink(missing_ok=True)
                results["repaired"] += 1
                results["details"].append({"dataset": sub, "partition": part_name,
                                           "rows": count, "baseline": baseline})

    state.commit()
    state.close()
    log.info("[REPAIR] 扫描 %d, 修复 %d", results["scanned"], results["repaired"])
    return results


def main():
    parser = argparse.ArgumentParser(description="QuantDB 每日数据同步")
    parser.add_argument("--parquet-only", action="store_true", help="仅同步 parquet")
    parser.add_argument("--skip-parquet", action="store_true", help="跳过 parquet 同步")
    parser.add_argument("--skip-pg", action="store_true", help="跳过 PG 填充")
    parser.add_argument("--skip-qlib", action="store_true", help="跳过 Qlib 缓存更新")
    parser.add_argument("--skip-snapshot", action="store_true", help="跳过特征快照生成")
    parser.add_argument("--full", action="store_true", help="全量重灌 PG (从 2016-01-04 起)")
    parser.add_argument("--datasets", type=str, help="指定数据集 (逗号分隔)")
    parser.add_argument("--dry-run", action="store_true", help="仅检查，不下载")
    parser.add_argument("--status", action="store_true", help="查看同步状态")
    parser.add_argument(
        "--reseed-state",
        action="store_true",
        help="用本地已有文件重建 SDK 状态库（换数据目录后必须先跑，否则会全量重下）",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="扫描并修复残缺的分区文件（删除后下次同步时重下）",
    )
    args = parser.parse_args()

    if args.status:
        status = show_status()
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return 0

    datasets = args.datasets.split(",") if args.datasets else None

    if args.reseed_state:
        ds_list = None
        if datasets:
            all_ds = V2_DATASETS + V1_DATASETS
            ds_list = [d for d in all_ds if d["sub_category"] in datasets]
        summary = reseed_state(ds_list)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    if args.repair:
        ds_list = None
        if datasets:
            all_ds = V2_DATASETS + V1_DATASETS
            ds_list = [d for d in all_ds if d["sub_category"] in datasets]
        result = repair_partitions(ds_list)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    result = run_daily_sync(
        parquet_only=args.parquet_only,
        datasets=datasets,
        skip_parquet=args.skip_parquet,
        skip_pg=args.skip_pg,
        skip_qlib=args.skip_qlib,
        skip_snapshot=args.skip_snapshot,
        full=args.full,
        dry_run=args.dry_run,
    )

    log.info("Result: %s", json.dumps(result, indent=2, ensure_ascii=False, default=str))

    # 落一条系统事件（data_sync），失败不阻断主流程
    try:
        from backend.shared.system_events import record_system_event

        record_system_event(
            event_type="data_sync",
            level="info",
            source="sync",
            title="QuantDB 数据同步完成",
            message=(
                "parquet 同步完成，PG 填充" + ("完成" if not args.skip_pg else "跳过") +
                "，Qlib 缓存" + ("完成" if not args.skip_qlib else "跳过")
            ),
            meta={"market": "quantdb", "datasets": datasets, "dry_run": args.dry_run},
        )
    except Exception as exc:  # noqa: BLE001 - 事件记录非关键路径
        log.warning("记录数据同步事件失败: %s", exc)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(1)
    except Exception as e:
        log.error("FATAL: %s", e, exc_info=True)
        # 同步异常也要落一条 error 系统事件
        try:
            from backend.shared.system_events import record_system_event

            record_system_event(
                event_type="data_sync",
                level="error",
                source="sync",
                title="QuantDB 数据同步失败",
                message=str(e),
                meta={"market": "quantdb", "error": str(e)},
            )
        except Exception as exc:  # noqa: BLE001 - 事件记录非关键路径
            log.warning("记录数据同步失败事件失败: %s", exc)
        sys.exit(1)
