#!/usr/bin/env python3
"""魔搭（ModelScope）QuantDB 数据集 → 本地数据目录一键初始化。

从魔搭公开数据集仓库（默认 ``qusong0627/LightGBM_Alpha300``）分页枚举远端
parquet 清单（含 sha256 / size），并发流式下载到 ``QM_QUANTDB_DATA_DIR``，
逐文件 sha256 校验后原子覆盖，并按需重建同步状态库，使后续
``quantdb_daily_sync`` 的增量 fast-path 直接命中，避免全量重拉。

远端仓库根目录与本地数据集 ``rel_dir`` 一一对应（如
``6_ml_datasets/l1_l2_factors/dt=20260918/data.parquet``），无需映射表。

设计要点：
  - 只依赖 stdlib + httpx（requirements 已含），不引入 modelscope SDK。
  - 下载走 302 → CDN 签名 URL，httpx 自动跟随重定向。
  - 全量覆盖：逐文件下载并原地替换（os.replace），与魔搭社区完全对齐。
  - 断点续传：仅当本地存在 + size 一致 + 状态库登记 sha256 == 远端 sha256
    才跳过；否则重下覆盖。每批完成即增量登记状态，中断后重跑只补未完成项。
  - 状态库：用远端 sha256 写 objects（免 56GB 重哈希）。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional
from collections.abc import Callable
from urllib.parse import quote

import httpx

log = logging.getLogger("modelscope_dataset_sync")

DEFAULT_ENDPOINT = "https://www.modelscope.cn"
DEFAULT_REPO_ID = "qusong0627/LightGBM_Alpha300"
DEFAULT_REVISION = "master"

TREE_PAGE_SIZE = 1000
DEFAULT_WORKERS = 6
DOWNLOAD_RETRIES = 3
FILE_PROGRESS_STEP = 25
BATCH_FACTOR = 8  # 每批提交 workers * BATCH_FACTOR 个任务，便于响应取消

_USER_AGENT = "QuantMind-ModelScopeSync/1.0"


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _endpoint() -> str:
    return (os.getenv("MODELSCOPE_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")


def _repo_id() -> str:
    return os.getenv("MODELSCOPE_DATASET_REPO") or DEFAULT_REPO_ID


def _revision() -> str:
    return os.getenv("MODELSCOPE_DATASET_REVISION") or DEFAULT_REVISION


def _token() -> str | None:
    token = os.getenv("MODELSCOPE_TOKEN") or os.getenv("MODELSCOPE_API_TOKEN")
    return token or None


def _workers() -> int:
    try:
        return max(1, int(os.getenv("MODELSCOPE_SYNC_WORKERS", str(DEFAULT_WORKERS))))
    except ValueError:
        return DEFAULT_WORKERS


def resolve_data_root() -> Path:
    """目标数据目录：与 quantdb_daily_sync / 本地扫描同源（QM_QUANTDB_DATA_DIR）。"""
    from backend.scripts.quantdb_local_scan import _default_root

    return Path(os.path.abspath(_default_root()))


# ---------------------------------------------------------------------------
# 远端枚举
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RemoteFile:
    path: str  # 仓库内 posix 相对路径
    size: int
    sha256: str
    dataset: str | None  # 映射到的本地数据集；未知顶层目录为 None
    layout: str  # v2_daily_partition / v1_symbol


def _classify(rel_path: str) -> tuple[str | None, str]:
    """把仓库相对路径归类到数据集。返回 (dataset, layout_col)。"""
    from backend.shared.quantdb_datasets import DATASETS

    for spec in DATASETS:
        if rel_path.startswith(spec.rel_dir + "/"):
            layout = "v2_daily_partition" if spec.layout == "partition" else "v1_symbol"
            return spec.dataset, layout
    return None, "v2_manifest"


def _headers() -> dict[str, str]:
    headers = {"User-Agent": _USER_AGENT}
    token = _token()
    if token:
        # 私有仓库 / 提高限流阈值；公开数据集留空即可
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _file_url(endpoint: str, repo_id: str, revision: str, rel_path: str) -> str:
    return (
        f"{endpoint}/api/v1/datasets/{repo_id}/repo"
        f"?Revision={quote(revision, safe='')}&FilePath={quote(rel_path, safe='')}"
    )


def list_remote_files(
    *,
    endpoint: str | None = None,
    repo_id: str | None = None,
    revision: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
    progress_cb: Callable[..., None] | None = None,
) -> list[RemoteFile]:
    """分页枚举魔搭仓库全部 blob（递归），返回带 sha256/size 的清单。"""
    ep = (endpoint or _endpoint()).rstrip("/")
    repo = repo_id or _repo_id()
    rev = revision or _revision()
    url = f"{ep}/api/v1/datasets/{repo}/repo/tree"

    files: dict[str, RemoteFile] = {}
    page = 1
    seen_entries = 0
    total: int | None = None
    timeout = httpx.Timeout(30.0, read=120.0)
    with httpx.Client(
        follow_redirects=True, timeout=timeout, headers=_headers()
    ) as client:
        while True:
            if should_cancel is not None and should_cancel():
                break
            resp = client.get(
                url,
                params={
                    "Revision": rev,
                    "Recursive": "true",
                    "PageNumber": page,
                    "PageSize": TREE_PAGE_SIZE,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            if body.get("Code") != 200:
                raise RuntimeError(
                    f"ModelScope tree API 失败: {body.get('Message') or body.get('Code')}"
                )
            payload = body.get("Data") or {}
            entries = payload.get("Files") or []
            if total is None:
                total = int(payload.get("TotalCount") or 0)
            seen_entries += len(entries)
            for ent in entries:
                if ent.get("Type") != "blob":
                    continue
                raw_path = ent.get("Path") or ent.get("Name") or ""
                if not raw_path:
                    continue
                dataset, layout = _classify(raw_path)
                files[raw_path] = RemoteFile(
                    path=raw_path,
                    size=int(ent.get("Size") or 0),
                    sha256=(ent.get("Sha256") or "").lower(),
                    dataset=dataset,
                    layout=layout,
                )
            if progress_cb:
                progress_cb("enumerate", done=seen_entries, total=total or seen_entries)
            if not entries or (total is not None and seen_entries >= total):
                break
            page += 1
    log.info("[MODELSCOPE] 枚举 %d 个文件（%d 页）", len(files), page)
    return list(files.values())


# ---------------------------------------------------------------------------
# 本地落盘
# ---------------------------------------------------------------------------
def _target_path(root: Path, rel_path: str) -> Path:
    """把仓库相对路径安全地映射到本地绝对路径（防目录穿越）。"""
    pure = PurePosixPath(rel_path)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"非法远端路径: {rel_path}")
    target = (root / pure).resolve()
    root_resolved = root.resolve()
    if os.path.commonpath([str(root_resolved), str(target)]) != str(root_resolved):
        raise ValueError(f"路径越界: {rel_path}")
    return target


def _download_one(
    client: httpx.Client, url: str, remote: RemoteFile, target: Path
) -> str:
    """下载单个文件：.part → sha256 校验 → 原子覆盖。失败抛异常。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    last_exc: Exception | None = None
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                h = hashlib.sha256()
                size = 0
                with open(part, "wb") as fh:
                    for chunk in resp.iter_bytes(1 << 20):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        h.update(chunk)
                        size += len(chunk)
            if remote.size and size != remote.size:
                raise OSError(f"size 不符: 期望 {remote.size} 实得 {size}")
            if remote.sha256 and h.hexdigest() != remote.sha256:
                raise OSError("sha256 校验失败")
            os.replace(part, target)
            return "downloaded"
        except Exception as exc:  # noqa: BLE001 - 重试后统一抛出
            last_exc = exc
            try:
                if part.exists():
                    part.unlink()
            except OSError:
                pass
            if attempt < DOWNLOAD_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError("下载失败")


def _local_breakdown(root: Path, files: list[RemoteFile]) -> tuple[int, int, int]:
    """按本地状态拆分远端文件字节数，返回 (已存在, 缺失, 存在但size不同)。

    「缺失」才真正需要新增磁盘空间；「存在但size不同」是原地覆盖（os.replace），
    基本不产生净增量，「已存在」直接跳过。
    """
    present = missing = changed = 0
    for f in files:
        try:
            target = _target_path(root, f.path)
        except ValueError:
            missing += f.size
            continue
        try:
            if not target.is_file():
                missing += f.size
            elif f.size and target.stat().st_size != f.size:
                changed += f.size
            else:
                present += f.size
        except OSError:
            changed += f.size
    return present, missing, changed


def _split_resumable(
    root: Path, files: list[RemoteFile], state_shas: dict[str, str]
) -> tuple[list[RemoteFile], list[RemoteFile]]:
    """按「本地已完整下载」拆分为 (可跳过, 待下载)。

    判据：本地文件存在、size 与远端一致，且状态库登记的同 key sha256 == 远端 sha256。
    任一不满足则重下并原地覆盖（保证与魔搭对齐）。
    """
    skippable: list[RemoteFile] = []
    pending: list[RemoteFile] = []
    for f in files:
        try:
            target = _target_path(root, f.path)
            if (
                f.size
                and f.sha256
                and target.is_file()
                and target.stat().st_size == f.size
                and state_shas.get(f.path) == f.sha256
            ):
                skippable.append(f)
                continue
        except (OSError, ValueError):
            pass
        pending.append(f)
    return skippable, pending


# ---------------------------------------------------------------------------
# 状态库重建
# ---------------------------------------------------------------------------
def _open_state_db(path: Path):
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60)
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


def _write_state_meta(
    root: Path, per_dataset: dict[str, list[RemoteFile]]
) -> tuple[dict[str, str], int]:
    """用远端元数据直接写 objects 表（免重哈希），使增量 fast-path 命中。"""
    from backend.scripts.quantdb_daily_sync import _state_path

    targets: list[tuple[str, Path]] = [
        ("quantmind", _state_path(root)),
        ("sdk", root / "quantdb_sync.sqlite"),
    ]
    written = 0
    for _, db_path in targets:
        conn = _open_state_db(db_path)
        try:
            for dataset, files in per_dataset.items():
                conn.execute("DELETE FROM objects WHERE dataset=?", (dataset,))
                rows = [
                    (
                        f.path,
                        f.sha256,
                        f.sha256,
                        f.size,
                        str(_target_path(root, f.path)),
                        f.layout,
                        dataset,
                    )
                    for f in files
                ]
                conn.executemany(
                    "INSERT OR REPLACE INTO objects(key, etag, sha256, size, path, layout, dataset)"
                    " VALUES(?,?,?,?,?,?,?)",
                    rows,
                )
                written += len(rows)
            conn.commit()
        finally:
            conn.close()
    return {label: str(p) for label, p in targets}, written


def _state_db_paths(root: Path) -> tuple[Path, ...]:
    from backend.scripts.quantdb_daily_sync import _state_path

    return (_state_path(root), root / "quantdb_sync.sqlite")


def _load_state_shas(root: Path, datasets: list[str]) -> dict[str, str]:
    """读同步状态库：key → 已登记的 sha256（用于断点续传跳过已下载文件）。"""
    import sqlite3

    if not datasets:
        return {}
    db_path = _state_db_paths(root)[0]
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
    except sqlite3.Error:
        return {}
    try:
        placeholders = ",".join("?" for _ in datasets)
        rows = conn.execute(
            f"SELECT key, sha256 FROM objects WHERE dataset IN ({placeholders})",
            datasets,
        )
        return {key: sha for key, sha in rows if sha}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _upsert_state_rows(root: Path, rows: list[tuple]) -> None:
    """增量登记已下载对象到两个状态库（中断后续传据此跳过）。"""
    if not rows:
        return
    for db_path in _state_db_paths(root):
        conn = _open_state_db(db_path)
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO objects(key, etag, sha256, size, path, layout, dataset)"
                " VALUES(?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _progress(progress_cb: Callable[..., None] | None, event: str, **kw: Any) -> None:
    if progress_cb:
        try:
            progress_cb(event, **kw)
        except Exception:  # noqa: BLE001 - 进度回调异常不得中断同步
            log.debug("progress_cb 异常", exc_info=True)


def _group_remote(
    remote: list[RemoteFile], datasets: list[str] | None
) -> dict[str, list[RemoteFile]]:
    grouped: dict[str, list[RemoteFile]] = {}
    for f in remote:
        if f.dataset is None or not f.path.endswith(".parquet"):
            continue
        if datasets is not None and f.dataset not in datasets:
            continue
        grouped.setdefault(f.dataset, []).append(f)
    return grouped


def init_from_modelscope(
    datasets: list[str] | None = None,
    *,
    repo_id: str | None = None,
    revision: str | None = None,
    endpoint: str | None = None,
    progress_cb: Callable[..., None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """从魔搭全量拉取 QuantDB 数据并原地覆盖本地目录。

    目标是与魔搭社区数据集完全对齐：不比对、不增量，逐文件下载并覆盖
    已存在的文件；完成后用远端 sha256 重建同步状态库。
    datasets: 数据集名列表（DATASETS 规格）；None=仓库内可识别的全部。
    """
    from backend.shared.quantdb_datasets import DATASETS

    known = {spec.dataset for spec in DATASETS}
    if datasets is not None:
        unknown = set(datasets) - known
        if unknown:
            raise ValueError(f"未知数据集: {', '.join(sorted(unknown))}")

    root = resolve_data_root()
    root.mkdir(parents=True, exist_ok=True)
    started = time.time()

    _progress(progress_cb, "phase", phase="enumerate", message="枚举魔搭远端清单")
    remote = list_remote_files(
        endpoint=endpoint,
        repo_id=repo_id,
        revision=revision,
        should_cancel=should_cancel,
        progress_cb=progress_cb,
    )
    grouped = _group_remote(remote, datasets)

    if datasets is not None:
        missing = [d for d in datasets if d not in grouped]
        if missing:
            raise ValueError(
                f"魔搭仓库中不存在这些数据集: {', '.join(sorted(missing))}"
            )

    total_files = sum(len(v) for v in grouped.values())
    total_bytes = sum(f.size for v in grouped.values() for f in v)
    _progress(
        progress_cb,
        "enumerate_done",
        datasets=len(grouped),
        files=total_files,
        bytes=total_bytes,
    )

    workers = _workers()
    batch = max(workers, workers * BATCH_FACTOR)
    timeout = httpx.Timeout(30.0, read=300.0, write=120.0, pool=60.0)

    # 断点续传：已完整下载（size 一致且状态库 sha256 == 远端 sha256）的文件跳过
    state_shas = _load_state_shas(root, list(grouped))
    downloaded = skipped = errors = downloaded_bytes = 0
    processed_bytes = 0
    error_samples: list[str] = []
    per_dataset_result: dict[str, dict[str, Any]] = {}
    cancelled = False
    ep = endpoint or _endpoint()
    repo = repo_id or _repo_id()
    rev = revision or _revision()

    with httpx.Client(
        follow_redirects=True, timeout=timeout, headers=_headers()
    ) as client:
        for idx, (dataset, files) in enumerate(grouped.items()):
            if should_cancel is not None and should_cancel():
                cancelled = True
                break

            skippable, pending = _split_resumable(root, files, state_shas)
            skipped += len(skippable)
            processed_bytes += sum(f.size for f in skippable)
            _progress(
                progress_cb,
                "dataset_start",
                dataset=dataset,
                index=idx,
                total=len(grouped),
                files=len(files),
                pending=len(pending),
                skipped=len(skippable),
            )
            ds_downloaded = ds_errors = done_in_ds = 0

            for start in range(0, len(pending), batch):
                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break
                window = pending[start : start + batch]
                done_rows: list[tuple] = []
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {
                        pool.submit(
                            _download_one,
                            client,
                            _file_url(ep, repo, rev, f.path),
                            f,
                            _target_path(root, f.path),
                        ): f
                        for f in window
                    }
                    for fut in as_completed(futures):
                        f = futures[fut]
                        try:
                            fut.result()
                            ds_downloaded += 1
                            downloaded_bytes += f.size
                            processed_bytes += f.size
                            done_rows.append(
                                (
                                    f.path,
                                    f.sha256,
                                    f.sha256,
                                    f.size,
                                    str(_target_path(root, f.path)),
                                    f.layout,
                                    dataset,
                                )
                            )
                        except Exception as exc:  # noqa: BLE001
                            ds_errors += 1
                            if len(error_samples) < 20:
                                error_samples.append(f"{f.path}: {exc}")
                            log.warning("[MODELSCOPE] %s 下载失败: %s", f.path, exc)
                        done_in_ds += 1
                        if done_in_ds % FILE_PROGRESS_STEP == 0:
                            _progress(
                                progress_cb,
                                "file",
                                dataset=dataset,
                                done=done_in_ds,
                                total=len(pending),
                                processed=processed_bytes,
                                downloaded=downloaded_bytes,
                                errors=errors + ds_errors,
                            )
                # 每批完成即增量登记状态，保证中断后重跑可跳过
                _upsert_state_rows(root, done_rows)
                if cancelled:
                    break

            downloaded += ds_downloaded
            errors += ds_errors
            per_dataset_result[dataset] = {
                "files": len(files),
                "downloaded": ds_downloaded,
                "skipped": len(skippable),
                "errors": ds_errors,
                "bytes": sum(f.size for f in files),
            }
            _progress(
                progress_cb,
                "dataset_done",
                dataset=dataset,
                downloaded=ds_downloaded,
                skipped=len(skippable),
                errors=ds_errors,
            )
            log.info(
                "[MODELSCOPE] %s: 下载 %d 跳过 %d (共 %d) 失败 %d",
                dataset,
                ds_downloaded,
                len(skippable),
                len(files),
                ds_errors,
            )
            if cancelled:
                break

    # 用远端元数据重建同步状态库（本地已是魔搭内容，直接登记 sha256）
    state_info: dict[str, Any] = {"mode": "meta", "status": "skipped"}
    if not cancelled:
        _progress(progress_cb, "phase", phase="state", message="写入同步状态库")
        try:
            per_ds_files = {
                d: [f for f in grouped[d] if _target_path(root, f.path).is_file()]
                for d in grouped
            }
            dbs, written = _write_state_meta(root, per_ds_files)
            state_info = {
                "mode": "meta",
                "status": "ok",
                "objects": written,
                "state_dbs": dbs,
            }
        except Exception as exc:  # noqa: BLE001
            state_info = {"mode": "meta", "status": "failed", "reason": str(exc)}
            log.error("[MODELSCOPE] 状态库写入失败: %s", exc, exc_info=True)

    return {
        "root": str(root),
        "repo_id": repo,
        "cancelled": cancelled,
        "datasets": per_dataset_result,
        "total_files": total_files,
        "downloaded": downloaded,
        "skipped": skipped,
        "errors": errors,
        "downloaded_bytes": downloaded_bytes,
        "error_samples": error_samples,
        "state": state_info,
        "elapsed_sec": round(time.time() - started, 1),
    }


def preflight_modelscope(
    datasets: list[str] | None = None,
    *,
    repo_id: str | None = None,
    revision: str | None = None,
    endpoint: str | None = None,
) -> dict[str, Any]:
    """初始化前预检：远端各数据集文件数/字节、本地目录与磁盘余量。"""
    from backend.shared.quantdb_datasets import DATASETS

    root = resolve_data_root()
    root.mkdir(parents=True, exist_ok=True)

    remote = list_remote_files(endpoint=endpoint, repo_id=repo_id, revision=revision)
    grouped = _group_remote(remote, datasets)

    # 续传判据（size 一致 + 状态库 sha256 一致）决定「已就绪(将跳过)」，
    # 与 init_from_modelscope 的 `_split_resumable` 保持同一套口径。
    state_shas = _load_state_shas(root, list(grouped))

    # 按 DATASETS 规格顺序输出（天然按 6 大类分组），不按标识字母排序
    items = []
    total_bytes = 0
    skip_files = 0
    skip_bytes = 0
    missing_bytes = 0
    changed_bytes = 0
    for spec in DATASETS:
        files = grouped.get(spec.dataset)
        if not files:
            continue
        ds_bytes = sum(f.size for f in files)
        skippable, _pending = _split_resumable(root, files, state_shas)
        # 缺失 = 路径不存在（真正占新增空间）；变更 = 存在但大小不同（原地覆盖）
        _present, ds_missing, ds_changed = _local_breakdown(root, files)
        ds_skip = sum(f.size for f in skippable)
        total_bytes += ds_bytes
        skip_files += len(skippable)
        skip_bytes += ds_skip
        missing_bytes += ds_missing
        changed_bytes += ds_changed
        items.append(
            {
                "dataset": spec.dataset,
                "name": spec.name,
                "group": spec.group,
                "layout": spec.layout,
                "rel_dir": spec.rel_dir,
                "files": len(files),
                "bytes": ds_bytes,
                "skip_bytes": ds_skip,
            }
        )

    try:
        usage = shutil.disk_usage(str(root))
        disk = {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        disk = {"total": 0, "used": 0, "free": 0}

    # 全量覆盖下载：已存在的文件原地替换（同目录 .part → os.replace），几乎不占净增量；
    # 只有本地缺失的文件才真正需要新增磁盘空间。
    warnings: list[str] = []
    if disk["free"] and missing_bytes and disk["free"] < missing_bytes * 1.1:
        warnings.append(
            f"磁盘余量不足：本地缺失约 {missing_bytes / 1024**3:.1f} GB 需新增空间，"
            f"当前可用 {disk['free'] / 1024**3:.1f} GB。请先清理磁盘再执行。"
        )

    ep = (endpoint or _endpoint()).rstrip("/")
    repo = repo_id or _repo_id()
    return {
        "repo_id": repo,
        "repo_url": f"{ep}/datasets/{repo}",
        "revision": revision or _revision(),
        "root": str(root),
        "datasets": items,
        "total_files": sum(it["files"] for it in items),
        "total_bytes": total_bytes,
        "skip_files": skip_files,
        "skip_bytes": skip_bytes,
        "missing_bytes": missing_bytes,
        "changed_bytes": changed_bytes,
        "disk": disk,
        "warnings": warnings,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
