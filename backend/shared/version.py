"""运行时代码版本读取与上游更新检查。

版本来源（优先级，get_version_info 口径——合规披露的版本号冻结于此）：
1. backend/shared/version.json —— 由 deploy/update.sh 每次构建前写入
   （含 version/commit/branch 三个稳定字段，commit 为完整 HEAD SHA，
   describe 恰为 tag 而无短 SHA 时也可对比）
2. backend/shared/version.txt —— 旧的 describe 单行（遗留回退，可解析出短 SHA）
3. 缺省回退 "dev"（本地未走 update.sh 的开发环境）

部署真相探针（T7-3，get_deploy_truth）：容器与服务器均为 bind mount 活代码
（compose 挂 ./.git 只读；deploy/update.sh「后端代码 bind mount 已生效」），
version.json 只是部署时刻的**声明**。排障第一眼「跑的是哪版代码」以运行处
工作树为准：.git 可达 → rev-parse/status 直读 commit/branch/脏标记，并与声明
对照报告分叉；探针全程只读（ro 挂载 + --no-optional-locks），绝不抛异常。

更新检查：读 commit 已有 .git 探针，但 fetch/compare 仍需网络（只读 .git 不足以
fetch），故走上游平台（默认 gitee）的 compare HTTP API，比较「本地部署 commit」
与「上游分支」算出落后提交数。
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
import subprocess
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


# ── 部署真相探针（T7-3 / H10，2026-10-10）─────────────────────────────────
# 容器与服务器均为 bind mount 活代码（docker-compose.yml 把 ./.git 只读挂到
# /app/.git；deploy/update.sh 重启进程即拾新码）。version.json 只是部署脚本落盘
# 时刻的**声明**，热修/并行改动会让它与运行工作树分叉——排障第一眼需要的
# 「跑的是哪版代码」必须来自工作树本身，而不是声明文件。
_CODE_ROOT = _BASE_DIR.parent.parent  # backend/shared → 仓库根（容器内即 /app）
_GIT_TIMEOUT = 5
# 脏面限定在容器实际挂载的代码路径：容器内 electron/ 等未挂载目录在 worktree
# 里「缺失」，全量 status 会把它们全部报成 deleted，脏标记恒真。
_STATUS_PATHS = ("backend", "config", "scripts", "strategy_templates", "alphaagent")
# 镜像侧身份戳：Dockerfile.oss 构建时由 QM_GIT_* build-arg 写入（bind mount
# 部署下以运行处工作树为准，此戳是「镜像由哪版代码构建」的兜底证据）。
_IMAGE_STAMP = Path("/app/deploy_stamp.json")
_DIRTY_SAMPLE_LIMIT = 10


def _git(args: list[str], code_root: Path) -> str | None:
    """跑一条 git 只读探针命令；git 缺失/非仓库/超时/失败一律 → None（绝不抛）。"""
    try:
        proc = subprocess.run(
            [
                "git",
                # 容器内常以 root 读宿主用户属主的 .git（只读探针，无写入面）
                "-c",
                "safe.directory=*",
                # .git 以 ro 挂载：禁掉 status 顺带刷新 index 的可选写锁
                "--no-optional-locks",
                "-C",
                str(code_root),
                *args,
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def get_git_identity(code_root: Path | None = None) -> dict:
    """运行处工作树身份：commit/branch/脏标记（T7-3）。

    `.git` 不可达（未挂载/非仓库/无 git）→ available=False，脏标记为 None
    （未探到 ≠ 干净，别把不可知显示成 False）。脏面限定 `_STATUS_PATHS`。
    """
    root = Path(code_root) if code_root is not None else _CODE_ROOT
    commit = _git(["rev-parse", "HEAD"], root)
    if not commit:
        return {
            "available": False,
            "commit": "",
            "short": "",
            "branch": "",
            "dirty": None,
            "dirty_count": None,
            "dirty_sample": [],
        }
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], root) or ""
    scope = [p for p in _STATUS_PATHS if (root / p).exists()]
    porcelain = _git(["status", "--porcelain", "--", *scope], root) if scope else ""
    lines = [ln for ln in (porcelain or "").splitlines() if ln.strip()]
    return {
        "available": True,
        "commit": commit,
        "short": commit[:8],
        "branch": branch,
        "dirty": bool(lines),
        "dirty_count": len(lines),
        # porcelain 行 = "XY <path>"（rename 为 "R  old -> new"）。_git 的 strip()
        # 会吃掉行首变化位空格（" M p"→"M p"），所以按 [2:]+lstrip 解析，
        # 对 strip 前后两种形态都给出同一路径。
        "dirty_sample": [ln[2:].lstrip() for ln in lines[:_DIRTY_SAMPLE_LIMIT]],
    }


def _read_image_stamp(stamp_path: Path | None = None) -> dict | None:
    """读镜像构建戳（Dockerfile.oss 写入）；缺失/损坏 → None。"""
    path = stamp_path if stamp_path is not None else _IMAGE_STAMP
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def get_deploy_truth() -> dict:
    """部署真相（T7-3）：运行处工作树 > 部署声明 > 镜像戳 > 遗留 txt > unknown。

    与 `get_version_info` 的分工：后者读的是部署脚本落盘的**声明**（合规披露的
    版本号口径冻结在彼，本函数不改它）；本函数优先走**运行处探针**，报告此刻
    实际在跑的工作树身份，并在声明与工作树分叉（热修/并行改动）时给出
    divergence。divergence 只在 version.json 在场时判定：version.txt 是遗留
    回退，其 commit 冻结在写入时刻（开发机实测停在 8 月），拿它比会把正常
    开发报成永久分叉。探针全程只读、绝不抛异常；探不到的字段如实给
    unknown/None，绝不粉饰成 0 或 False。
    """
    runtime = get_git_identity()
    declared = get_version_info()
    image = _read_image_stamp()

    if runtime["available"]:
        commit = runtime["commit"]
        short = runtime["short"]
        branch = runtime["branch"]
        dirty = runtime["dirty"]
        source = "git-runtime"
    elif _VERSION_JSON.is_file() and declared["commit"]:
        commit = declared["commit"]
        short = declared["commit"][:8]
        branch = declared["branch"]
        dirty = None
        source = "version-json"
    elif image and str(image.get("commit") or "") not in ("", "unknown"):
        commit = str(image["commit"])
        short = commit[:8]
        branch = str(image.get("branch") or declared["branch"])
        dirty = None
        source = "image-stamp"
    elif declared["commit"]:  # 只剩遗留 version.txt 的短 SHA
        commit = declared["commit"]
        short = declared["commit"][:8]
        branch = declared["branch"]
        dirty = None
        source = "version-txt"
    else:
        commit, short, branch, dirty, source = (
            "",
            "",
            declared["branch"],
            None,
            "unknown",
        )

    divergence = None
    if (
        runtime["available"]
        and _VERSION_JSON.is_file()
        and declared["commit"]
        and runtime["commit"][:8] != declared["commit"][:8]
    ):
        divergence = {
            "declared_commit": declared["commit"],
            "runtime_commit": runtime["commit"],
        }

    return {
        "commit": commit,
        "short": short,
        "branch": branch,
        "dirty": dirty,
        "source": source,
        "runtime": runtime,
        "declared": {
            "version": declared["version"],
            "commit": declared["commit"],
            "branch": declared["branch"],
        },
        "image": image,
        "divergence": divergence,
    }


def log_deploy_truth(logger) -> dict:
    """启动打点（T7-3）：一行 INFO 记录运行身份；脏/分叉各补一条 WARNING。

    在 `main_oss.main()` 最顶部调用（先于一切启动逻辑）——排障时
    `docker logs quantmind | head` 第一眼就知道「跑的是哪版代码」。
    """
    truth = get_deploy_truth()
    logger.info(
        "部署真相: commit=%s branch=%s dirty=%s source=%s",
        truth["short"] or "unknown",
        truth["branch"] or "?",
        "unavailable" if truth["dirty"] is None else str(truth["dirty"]).lower(),
        truth["source"],
    )
    if truth["dirty"]:
        logger.warning(
            "工作树有未提交改动（%d 个文件）: %s",
            truth["runtime"]["dirty_count"],
            ", ".join(truth["runtime"]["dirty_sample"]),
        )
    if truth["divergence"]:
        logger.warning(
            "部署声明与运行工作树分叉: version.json=%s 运行=%s（热修未落库？）",
            truth["divergence"]["declared_commit"][:12],
            truth["divergence"]["runtime_commit"][:12],
        )
    return truth


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
