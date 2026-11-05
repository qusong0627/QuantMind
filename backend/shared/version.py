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

**本部署 commit 的取值顺序（2026-11-05 修订）**：
1. 运行处工作树（`.git` 只读探针）—— 唯一能反映「此刻实际检出/热修」的来源，
   不依赖 update.sh 是否写过 version.json；bind mount 的开发部署也据此自动提示；
2. version.json（update.sh 落盘的声明）—— 仅当容器内 `.git` 不可达时回退；
3. 遗留 version.txt **故意不参与**：它的 commit 冻结在打包时刻，拿它对比上游
   会把「本地其实最新」误报成落后几百提交（2026-10-09 事故口径：宁可不提示，
   也不误报）。
上游对比分支默认 next（本项目开发主线；main 只读、推送走 next），可用
QUANTMIND_UPSTREAM_BRANCH 覆盖；实际优先跟随运行处 checkout 的分支。
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
# 缓存有效期（秒）：自动提醒按此 TTL 复用结果，避免每次页面轮询都请求上游。
# 15 分钟 = 用户最多晚一刻钟看到新提交；单部署每小时最多 4 次上游请求。
_CACHE_TTL = int(os.getenv("QUANTMIND_UPDATE_CHECK_TTL", str(15 * 60)))
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


def get_git_identity(
    code_root: Path | None = None, *, with_status: bool = True
) -> dict:
    """运行处工作树身份：commit/branch/脏标记（T7-3）。

    `.git` 不可达（未挂载/非仓库/无 git）→ available=False，脏标记为 None
    （未探到 ≠ 干净，别把不可知显示成 False）。脏面限定 `_STATUS_PATHS`。

    ``with_status=False`` 只跑两条 rev-parse，跳过 git status：更新检查每次
    请求都要拿 commit/branch，在大仓库上 ``git status`` 是几百毫秒级开销，
    而脏标记与「落后几个提交」无关。
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
    if not with_status:
        return {
            "available": True,
            "commit": commit,
            "short": commit[:8],
            "branch": branch,
            "dirty": None,
            "dirty_count": None,
            "dirty_sample": [],
        }
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


def get_git_remotes(code_root: Path | None = None) -> list[str]:
    """运行处 checkout 配置的远端名（git remote）；不可达时返回空列表。

    更新接口用它挑一个实际存在的远端（本仓没有 ``origin``，实际是
    gitee/upstream/quantmind2），避免把 ``origin`` 传给 update.sh 后
    落到那条「远端不存在」的回退路径上。
    """
    out = _git(["remote"], Path(code_root) if code_root is not None else _CODE_ROOT)
    return [line.strip() for line in (out or "").splitlines() if line.strip()]


def get_git_remote_url(name: str, code_root: Path | None = None) -> str:
    """远端 URL（git remote get-url）；不存在/失败返回空串。"""
    root = Path(code_root) if code_root is not None else _CODE_ROOT
    return _git(["remote", "get-url", name], root) or ""


def get_git_remote_ref(remote: str, branch: str, code_root: Path | None = None) -> str:
    """远端跟踪 ref（refs/remotes/<remote>/<branch>）指向的 commit；不存在返回空串。"""
    root = Path(code_root) if code_root is not None else _CODE_ROOT
    return (
        _git(
            ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"],
            root,
        )
        or ""
    )


def _known_upstream_base(branch: str) -> str:
    """本地已知的「上游基线」commit（用于 HEAD 尚未推送时的贪心回退）。

    本地 HEAD 带未推送提交时，上游 compare 会 404（"Compare not found"），
    整个提醒就消失了。此时用最近一次 fetch 留下的远端跟踪 ref 当基线：
    它指向的 commit 一定存在于上游，compare(base...branch) 仍能准确给出
    「上游还有多少提交不在本地」——未推送的本地提交不影响这个数。
    优先 gitee（更新检查的目标上游），再 origin/upstream。
    """
    for remote in ("gitee", "origin", "upstream", "quantmind2"):
        sha = get_git_remote_ref(remote, branch)
        if sha:
            return sha
    return ""


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


def _update_identity() -> dict:
    """更新检查用的「本部署身份」（commit / 对比分支 / 来源）。

    优先级见模块 docstring：运行处工作树 > version.json > 不检查。
    遗留 version.txt 不参与——它冻结在打包时刻，天然会误报。
    """
    runtime = get_git_identity(with_status=False)
    if runtime["available"] and runtime["commit"]:
        return {
            "commit": runtime["commit"],
            "branch": runtime["branch"] or _UPSTREAM_BRANCH,
            "source": "git-runtime",
        }
    if _VERSION_JSON.is_file():
        info = get_version_info()
        if info.get("commit"):
            return {
                "commit": info["commit"],
                "branch": info.get("branch") or _UPSTREAM_BRANCH,
                "source": "version-json",
            }
    return {"commit": "", "branch": _UPSTREAM_BRANCH, "source": "unknown"}


def _cache_matches(cache: dict | None, identity: dict) -> bool:
    """缓存必须绑定「同一个本地 commit + 同一个对比分支」。

    只按时间判新鲜是不够的：部署刚更新/热修换了 commit 后，旧缓存里的
    behind 会继续挂着，管理页就会建议一次并不存在的更新（update.sh 会在
    更新完成后删缓存，但热加载/手工 checkout 不走那条路径）。
    """
    return bool(
        cache
        and cache.get("current_commit") == identity["commit"]
        and cache.get("upstream_branch") == identity["branch"]
    )


def _cache_is_fresh(cache: dict | None) -> bool:
    return bool(cache) and time.time() - cache.get("checked_at", 0) < _CACHE_TTL


def _latest_commit_info(body: dict) -> dict:
    """从 gitee compare 响应里取「上游最新提交」摘要。

    gitee 的 commits 按**新→旧**排列（实测 2026-11-05），commits[0] 即上游 HEAD；
    落后 0 个提交时列表为空，base_commit 就是本地（也即上游）HEAD。
    """
    commits = body.get("commits") or []
    latest = commits[0] if commits else (body.get("base_commit") or {})
    sha = latest.get("sha") or ""
    message = ((latest.get("commit") or {}).get("message") or "").strip()
    author = (latest.get("commit") or {}).get("author") or {}
    return {
        "latest_commit": sha,
        "latest_short": sha[:8],
        "latest_subject": message.splitlines()[0] if message else "",
        "latest_committed_at": author.get("date"),
    }


async def _fetch_compare(base: str, branch: str) -> dict | None:
    """请求 gitee compare(base...branch)；网络/解析失败一律返回 None。"""
    url = (
        f"{_UPSTREAM_HOST.rstrip('/')}/api/v5/repos/"
        f"{_UPSTREAM_OWNER}/{_UPSTREAM_REPO}/compare/{base}...{branch}"
    )
    try:
        resp = await asyncio.to_thread(_httpx.get, url)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    return body if isinstance(body, dict) else None


async def check_updates(force: bool = False) -> dict | None:
    """向上游查询本部署是否落后，返回落后提交数等；无法判断时返回 None。

    返回字段：behind、behind_capped、upstream_branch、current_commit、
    latest_commit/latest_subject、checked_at、is_up_to_date、source。
    前端仅需 behind > 0 即提示「落后上游 N 个提交」。
    """
    identity = await asyncio.to_thread(_update_identity)
    commit, branch = identity["commit"], identity["branch"]
    if not commit:
        return None

    cache = _load_cache()
    if not force and _cache_matches(cache, identity) and _cache_is_fresh(cache):
        return cache

    # 幂等：同一批次并发请求只打一次上游。
    async with _lock:
        # 双检：等待锁期间可能已被别的协程填充缓存。
        cache = _load_cache()
        if not force and _cache_matches(cache, identity) and _cache_is_fresh(cache):
            return cache

        body = await _fetch_compare(commit, branch)
        compared_from = commit
        if body is None:
            # 本地 HEAD 带未推送提交时上游 compare 会 404；退回「最近一次 fetch
            # 留下的远端跟踪 ref」做基线。该 commit 一定在上游，算出的 behind
            # 仍是「上游还有多少提交不在本地」，不受未推送本地提交影响。
            fallback = await asyncio.to_thread(_known_upstream_base, branch)
            if fallback and fallback != commit:
                body = await _fetch_compare(fallback, branch)
                compared_from = fallback
        if body is None:
            return None

        commits = body.get("commits") or []
        behind = len(commits)
        result = {
            "behind": behind,
            "behind_capped": bool(body.get("truncated")),
            "upstream_branch": branch,
            "current_commit": commit,
            "current_short": commit[:8],
            # 对比基线：正常等于 current_commit；回退时是本地远端跟踪 ref 的 commit
            "compared_from": compared_from,
            "compared_from_short": compared_from[:8],
            **_latest_commit_info(body),
            "checked_at": int(time.time()),
            "is_up_to_date": behind == 0,
            "source": identity["source"],
        }
        _save_cache(result)
        return result
