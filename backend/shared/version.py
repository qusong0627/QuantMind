"""运行时代码版本读取与上游更新检查。

版本来源（优先级）：
1. backend/shared/version.json —— 由 deploy/update.sh 每次构建前写入
   （含 version/commit/branch 三个稳定字段，commit 为完整 HEAD SHA，
   describe 恰为 tag 而无短 SHA 时也可对比）
2. backend/shared/version.txt —— 旧的 describe 单行（遗留回退，可解析出短 SHA）
3. 缺省回退 "dev"（本地未走 update.sh 的开发环境）

更新检查：容器内没有 .git，也无法 git fetch，故改走上游平台（默认 gitee）的
compare HTTP API，比较「本地部署 commit」与「上游分支」算出落后提交数。
结果做本地磁盘缓存，避免每次请求都访问上游。

**只有走过 deploy/update.sh 的部署（version.json 在场）才参与更新检查**：
version.txt 是遗留回退，它的 commit 冻结在写入那一刻，开发机（bind mount
恒最新）拿它对比上游会把「本地其实最新」误报成落后几百提交。宁可不提示，
也不误报。上游对比分支默认 next（本项目开发主线；main 只读、推送走 next），
可用 QUANTMIND_UPSTREAM_BRANCH 覆盖。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx

_BASE_DIR = Path(__file__).resolve().parent
_VERSION_TXT = _BASE_DIR / "version.txt"
_VERSION_JSON = _BASE_DIR / "version.json"

# 上游检查配置：容器若能访问上游（默认 gitee），后端可自动提示落后提交数。
# 内部销售版可改指私有仓库；无法访问时 check_updates 返回 None，仅静默回退。
_UPSTREAM_HOST = os.getenv("QUANTMIND_UPSTREAM_HOST", "https://gitee.com")
_UPSTREAM_OWNER = os.getenv("QUANTMIND_UPSTREAM_OWNER", "qusong0627")
_UPSTREAM_REPO = os.getenv("QUANTMIND_UPSTREAM_REPO", "quantmind")
_UPSTREAM_BRANCH = os.getenv("QUANTMIND_UPSTREAM_BRANCH", "next")

# 检查结果缓存在运行时可写目录（STORAGE_ROOT 默认 /data，挂载持久化）。
_CACHE_FILE = os.getenv(
    "QUANTMIND_UPDATE_CACHE_FILE",
    os.path.join(os.getenv("STORAGE_ROOT", "/data"), "version_check.json"),
)
# 缓存有效期（秒）：避免每次页面加载都请求上游。
_CACHE_TTL = int(os.getenv("QUANTMIND_UPDATE_CHECK_TTL", str(6 * 3600)))
_CACHE_FILE = Path(os.getenv("QUANTMIND_UPDATE_CACHE_FILE", _CACHE_FILE))
_TIMEOUT = float(os.getenv("QUANTMIND_UPDATE_CHECK_TIMEOUT", "10"))

_httpx = httpx.Client(timeout=_TIMEOUT)
_lock = asyncio.Lock()


def _commit_from_describe(describe: str) -> str | None:
    """从 describe（如 v1.9.0-beta-629-g13e38771）解析提交 SHA；恰为 tag 时无 g 段。"""
    match = re.search(r"-g([0-9a-f]{7,40})$", describe.strip())
    return match.group(1) if match else None


def get_version_info() -> dict:
    """返回当前部署版本明细：version（describe）、commit（SHA）、branch（部署分支）。"""
    if _VERSION_JSON.is_file():
        try:
            data = json.loads(_VERSION_JSON.read_text(encoding="utf-8"))
            commit = data.get("commit") or ""
            branch = data.get("branch")
            version = data.get("version") or ""
            return {
                "version": version,
                "commit": commit,
                "branch": branch or _UPSTREAM_BRANCH,
            }
        except (OSError, ValueError):
            pass
    # 遗留回退：解析 version.txt 的 describe。
    try:
        describe = _VERSION_TXT.read_text(encoding="utf-8").strip()
    except OSError:
        describe = ""
    if not describe:
        return {"version": "dev", "commit": "", "branch": _UPSTREAM_BRANCH}
    return {
        "version": describe,
        "commit": _commit_from_describe(describe) or "",
        "branch": _UPSTREAM_BRANCH,
    }


def _load_cache():
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        return data
    except (OSError, ValueError):
        return None


def _save_cache(payload: dict) -> None:
    try:
        Path(_CACHE_FILE).parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass  # 目录不可写时静默降级为每次实查


async def check_updates(force: bool = False) -> dict | None:
    """向正则资源上游查询本部署是否落后，返回落后提交数等；失败/无需检查返回 None。

    返回字段：behind、behind_capped、upstream_branch、checked_at、is_up_to_date。
    前端仅需 behind > 0 即提示「落后上游 N 个提交」。
    """
    info = get_version_info()
    commit, branch = info.get("commit"), info.get("branch")
    # 只有走 update.sh 的部署（version.json 在场）才能对比：遗留 version.txt 的
    # commit 冻结在打包时刻，开发机拿它对比上游 = 把恒最新的本地误报成落后。
    if not commit or not _VERSION_JSON.is_file():
        return None

    cache = _load_cache()
    if not force and cache and time.time() - cache.get("checked_at", 0) < _CACHE_TTL:
        return cache

    # 幂等：同一批次并发请求只打一次上游。
    async with _lock:
        # 双检：等待锁期间可能已被别的协程填充缓存。
        cache = _load_cache()
        if (
            not force
            and cache
            and time.time() - cache.get("checked_at", 0) < _CACHE_TTL
        ):
            return cache

        url = (
            f"{_UPSTREAM_HOST.rstrip('/')}/api/v5/repos/"
            f"{_UPSTREAM_OWNER}/{_UPSTREAM_REPO}/compare/{commit}...{branch}"
        )
        try:
            resp = await asyncio.to_thread(_httpx.get, url)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError):
            return None

        commits = body.get("commits") or []
        behind = len(commits)
        result = {
            "behind": behind,
            "behind_capped": bool(body.get("truncated")),
            "upstream_branch": branch,
            "checked_at": int(time.time()),
            "is_up_to_date": behind == 0,
        }
        _save_cache(result)
        return result
