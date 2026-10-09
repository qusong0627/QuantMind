"""MinerU 在线 API v4 客户端 —— 文档解析链的**唯一出网口**。

契约（逐字段实测核对，2026-10-09）：

    POST /api/v4/file-urls/batch   提交批次（拿预签名上传链接）
    PUT  file_urls[i]              上传文件二进制（**不带** Content-Type / Authorization）
    GET  /api/v4/extract-results/batch/{batch_id}   轮询（state + 页进度）
    GET  full_zip_url              下载产物 zip（full.md + images/ + *_content_list.json）

三条纪律，每条都对应一类会静默骗人的故障：

1. **HTTP 200 ≠ 成功**。信封有三层：`code≠0`、`success:false/msgCode`、
   批次项 `state=failed`。漏一层，失败就会被编排层当成「还在跑」。
2. **错误分类决定行为**：暂态码（-60007 等）指数退避重试；永久码立即失败并
   带可读文案；配额码（-60018/-60019）与 token 码（A0202/A0211）单独成类——
   它们要求「告诉用户去做什么」，不是「再试一次」。未知码按永久处理：
   宁可让用户看到原因，也不拿未知码烧重试和共享配额。
3. **zip 是不可信输入**：只白名单提取（`full.md` / `*_content_list.json` /
   `images/**`），`..`、绝对路径、符号链接条目一律拒绝（Zip-Slip）；条目数
   与解压总量双上限，声明值超限先拒、流式解压再兜底（Zip-Bomb）。失败清理
   整个 dest 目录，绝不留半成品。

结果链接**无 TTL**，done 后必须尽快 `download_zip` 落盘（编排层负责）。
限速按官方：提交 ≤300/min、查询 ≤1000/min（`MinIntervalLimiter`）。
本模块不做业务状态机（那是 doc_parse_service 的事），只做「一次可重试的
原子动作」。
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
import os
import re
import socket
import stat
import time
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ── 常量（env 只经 resolve_mineru_config 读取） ─────────────────────

ENV_MINERU_TOKEN = "MINERU_API_TOKEN"
ENV_MINERU_MODEL_VERSION = "MINERU_MODEL_VERSION"
ENV_MINERU_BASE_URL = "MINERU_BASE_URL"

#: 解析通道模式（T-FM-21）：cloud=云端 v4 协议（默认）；local=本地/局域网
#: MinerU 4.x 自托管 V1 HTTP API（mineru-kit api-server，数据不出内网）。
ENV_MINERU_MODE = "MINERU_MODE"
MODE_CLOUD = "cloud"
MODE_LOCAL = "local"
VALID_MODES = frozenset({MODE_CLOUD, MODE_LOCAL})

ENV_MINERU_LOCAL_URL = "MINERU_LOCAL_URL"
ENV_MINERU_LOCAL_API_KEY = "MINERU_LOCAL_API_KEY"
ENV_MINERU_LOCAL_TIER = "MINERU_LOCAL_TIER"

DEFAULT_BASE_URL = "https://mineru.net"
DEFAULT_MODEL_VERSION = "vlm"

SUBMIT_RATE_PER_MIN = 300
QUERY_RATE_PER_MIN = 1000

MAX_BATCH_FILES = 50
MAX_PAGES_PER_FILE = 200  # 平台限制：单文件 200 页
MAX_FILE_MB = 200  # 平台限制：单文件 200MB（端点侧的展示常量，与本模块同源）

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_S = 1.0

DEFAULT_MAX_ZIP_ENTRIES = 2000
DEFAULT_MAX_UNZIP_BYTES = 512 * 1024 * 1024  # 解压总量上限（Zip-Bomb 第二道闸）
DEFAULT_ZIP_MAX_BYTES = 1024 * 1024 * 1024  # 下载 zip 本身大小上限

_CHUNK = 1024 * 1024

#: 暂态码 —— 指数退避重试。含 -60009 队列满（等一会儿再来）。
TRANSIENT_CODES = frozenset({-60007, -60008, -60010, -60001, -60011, -60009})
#: 永久码 —— 重试没有意义，立即失败 —— -60005 超大小 / -60006 超页数也在其列。
PERMANENT_CODES = frozenset({-60005, -60006, -60002, -60003, -60004, -60015, -60016})
#: 配额码 —— 日上限/HTML 额度：当天再试也没有，要让用户看到「为什么」。
QUOTA_CODES = frozenset({-60018, -60019})
#: token 码 —— 配置问题，运维面处理。
AUTH_CODES = frozenset({"A0202", "A0211"})

ACTIVE_STATES = frozenset({"waiting-file", "pending", "running", "converting"})
TERMINAL_STATES = frozenset({"done", "failed"})

_ERR_CODE_RE = re.compile(r"A\d{4}|-6\d{4}")


# ── 异常与错误分类 ──────────────────────────────────────────────────


class MineruError(Exception):
    """MinerU 链路统一异常。`retryable` 决定编排层能否退避重试。"""

    def __init__(
        self,
        message: str,
        *,
        code: int | str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class MineruAuthError(MineruError):
    """A0202/A0211 —— token 失效/未授权。文案要指向「检查 MINERU_API_TOKEN」。"""


class MineruQuotaError(MineruError):
    """-60018/-60019 —— 配额类，当天重试无意义。"""


class MineruZipError(MineruError):
    """zip 结构/安全校验失败。永不重试：同样的字节重试只是再失败一次。"""


def _normalize_code(code: object) -> int | str | None:
    if isinstance(code, bool):
        return None
    if isinstance(code, (int, float)):
        return int(code)
    if isinstance(code, str):
        s = code.strip()
        if s.upper() in AUTH_CODES:
            return s.upper()
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def classify_error_code(code: object) -> tuple[bool, type[MineruError]]:
    """错误码 → (可重试否, 异常类)。未知码按永久处理（见模块 docstring）。"""
    c = _normalize_code(code)
    if c in AUTH_CODES:
        return False, MineruAuthError
    if c in QUOTA_CODES:
        return False, MineruQuotaError
    if c in TRANSIENT_CODES:
        return True, MineruError
    if c in PERMANENT_CODES:
        return False, MineruError
    return False, MineruError


def extract_err_code(err_msg: str | None) -> int | str | None:
    """从批次项 err_msg 文本里捞错误码（信封不总是给结构化 code）。"""
    if not err_msg:
        return None
    m = _ERR_CODE_RE.search(err_msg)
    if not m:
        return None
    token = m.group(0)
    if token.startswith("A"):
        return token
    return int(token)


def raise_for_code(code: object, msg: str | None, context: str) -> None:
    retryable, exc_cls = classify_error_code(code)
    code_repr = f"[{code}] " if code is not None else ""
    raise exc_cls(
        f"{context}: {code_repr}{(msg or 'unknown').strip()}",
        code=code if isinstance(code, (int, str)) else None,
        retryable=retryable,
    )


# ── 配置 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MineruConfig:
    token: str
    base_url: str = DEFAULT_BASE_URL
    model_version: str = DEFAULT_MODEL_VERSION
    #: cloud（默认）或 local；本地模式下 token 承载 MINERU_LOCAL_API_KEY（可空）
    mode: str = MODE_CLOUD
    #: 本地服务解析档位（flash|basic|standard|advanced）；None=用服务端默认
    local_tier: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"未知 MinerU 模式: {self.mode!r}")
        if self.mode == MODE_LOCAL:
            if not self.base_url.startswith(("http://", "https://")):
                raise ValueError(
                    f"本地 MinerU 服务地址必须是 http(s) URL: {self.base_url!r}"
                )
            return
        if not self.token:
            raise ValueError("MinerU token 不能为空")


def is_local_mode() -> bool:
    """当前是否本地/局域网解析模式（**调用时读**，归一后精确匹配）。"""
    return (os.getenv(ENV_MINERU_MODE) or "").strip().lower() == MODE_LOCAL


def mineru_unconfigured_message() -> str:
    """通道未配置 → 指路「因子挖掘 → 文档解析设置」+ env 兜底（服务层与端点共用）。

    2026-10-09 起用户级配置入口在因子挖掘内（``doc_mining_settings``），
    不再提用户中心；文案同时保留部署级 env 兜底，两种配法都告诉用户。
    """
    if is_local_mode():
        return (
            "本地 MinerU 服务未配置，文档解析不可用：请在「因子挖掘 → 文档解析设置」"
            "填写本地/局域网 MinerU 服务地址，或在服务器 .env 配置 MINERU_LOCAL_URL"
        )
    return (
        "MinerU 解析通道未配置，文档解析不可用：请在「因子挖掘 → 文档解析设置」"
        "填写 MinerU API Token，或在服务器 .env 配置 MINERU_API_TOKEN"
    )


def resolve_mineru_config() -> MineruConfig | None:
    """env → 配置；不可用返回 None（端点据此转 503）。**调用时读**。

    本地模式（``MINERU_MODE=local``）：读 ``MINERU_LOCAL_URL`` /
    ``MINERU_LOCAL_API_KEY`` / ``MINERU_LOCAL_TIER``——**绝不**回落到云端
    配置（没配 URL 就是未配置；云端 token 绝不渗进本地客户端，防漏给内网
    服务）。未知 mode 值按 cloud 处理（打日志），不臆造第三种通道。
    """
    mode_raw = (os.getenv(ENV_MINERU_MODE) or "").strip().lower()
    if mode_raw and mode_raw not in VALID_MODES:
        logger.warning("MINERU_MODE=%r 无法识别，按 %s 处理", mode_raw, MODE_CLOUD)
        mode_raw = MODE_CLOUD
    if mode_raw == MODE_LOCAL:
        base_url = (os.getenv(ENV_MINERU_LOCAL_URL) or "").strip()
        if not base_url:
            return None
        try:
            return MineruConfig(
                token=(os.getenv(ENV_MINERU_LOCAL_API_KEY) or "").strip(),
                base_url=base_url.rstrip("/"),
                mode=MODE_LOCAL,
                local_tier=(os.getenv(ENV_MINERU_LOCAL_TIER) or "").strip() or None,
            )
        except ValueError as e:
            logger.warning("MINERU_LOCAL_URL=%r 非法：%s", base_url, e)
            return None
    token = (os.getenv(ENV_MINERU_TOKEN) or "").strip()
    if not token:
        return None
    base_url = (os.getenv(ENV_MINERU_BASE_URL) or "").strip() or DEFAULT_BASE_URL
    model_version = (
        os.getenv(ENV_MINERU_MODEL_VERSION) or ""
    ).strip() or DEFAULT_MODEL_VERSION
    return MineruConfig(
        token=token,
        base_url=base_url.rstrip("/"),
        model_version=model_version,
    )


# ── 数据模型 ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MineruFileSpec:
    name: str
    data_id: str | None = None
    is_ocr: bool = False
    page_ranges: str | None = None
    #: 原件字节数：云端 v4 请求用不到；本地 V1 create 必填（T-FM-21）
    size_bytes: int | None = None


@dataclass(frozen=True)
class MineruBatchItem:
    file_name: str
    state: str
    full_zip_url: str | None = None
    err_msg: str | None = None
    data_id: str | None = None
    extracted_pages: int | None = None
    total_pages: int | None = None
    err_code: int | str | None = None
    retryable: bool | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


@dataclass(frozen=True)
class ZipExtractResult:
    md_path: Path
    content_list_paths: list[Path] = field(default_factory=list)
    image_count: int = 0
    total_bytes: int = 0


# ── 限速 ────────────────────────────────────────────────────────────


class MinIntervalLimiter:
    """≤N 次/分钟 → 相邻两次调用最小间隔 60/N 秒。按调用点各持一份。"""

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute 必须为正")
        self._interval = 60.0 / per_minute
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._next_at: float | None = None

    async def acquire(self) -> None:
        now = self._clock()
        if self._next_at is not None and now < self._next_at:
            await self._sleep(self._next_at - now)
            now = self._clock()  # 真实钟已流逝；可注入钟由 sleep 推进
        self._next_at = now + self._interval


# ── zip 白名单解包 ──────────────────────────────────────────────────

_S_IFMT = 0o170000
_S_IFLNK = 0o120000


def _validated_parts(filename: str) -> list[str]:
    """条目名 → 归一化路径组件；`..`/绝对路径直接抛（Zip-Slip）。"""
    name = filename.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise MineruZipError(f"zip 含绝对路径条目: {filename!r}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise MineruZipError(f"zip 含目录穿越条目: {filename!r}")
    return parts


#: md 产物两种命名：云端 zip 用 full.md；本地 4.x 自托管用 markdown.md
_MD_BASENAMES = ("full.md", "markdown.md")


def _classify_member(parts: list[str]) -> tuple[str, str] | None:
    """(类别, 目的相对路径) 或 None（白名单外，静默跳过）。"""
    if not parts:
        return None
    base = parts[-1]
    if base in _MD_BASENAMES:
        return "md", "full.md"
    if base.endswith("_content_list.json"):
        return "content_list", base
    if "images" in parts[:-1]:
        idx = parts.index("images")
        tail = parts[idx + 1 :]
        if not tail:
            return None
        return "image", "images/" + "/".join(tail)
    return None


def extract_zip_whitelist(
    zip_path: Path,
    dest_dir: Path,
    *,
    max_entries: int = DEFAULT_MAX_ZIP_ENTRIES,
    max_total_bytes: int = DEFAULT_MAX_UNZIP_BYTES,
) -> ZipExtractResult:
    """白名单解包。dest_dir 必须是本次 zip **独占**目录：失败时整目录清理。"""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as e:
        raise MineruZipError(f"不是有效 zip: {e}") from e

    try:
        infos = zf.infolist()
        if len(infos) > max_entries:
            raise MineruZipError(f"zip 条目数 {len(infos)} 超上限 {max_entries}")

        plan: list[tuple[zipfile.ZipInfo, str, str]] = []
        md_candidates: list[tuple[zipfile.ZipInfo, str, str]] = []
        for info in infos:
            parts = _validated_parts(info.filename)
            mode = (info.external_attr >> 16) & _S_IFMT
            if mode == _S_IFLNK:
                raise MineruZipError(f"zip 含符号链接条目: {info.filename!r}")
            if info.is_dir():
                continue
            hit = _classify_member(parts)
            if hit is None:
                continue
            kind, rel = hit
            if kind == "md":
                md_candidates.append((info, kind, rel))
                continue
            plan.append((info, kind, rel))

        if not md_candidates:
            raise MineruZipError(
                "解析产物里没有 full.md/markdown.md（MinerU markdown 产物缺失）"
            )
        # full.md（云端命名）与 markdown.md（本地 4.x 命名）并存时 full.md 优先；
        # 同名的多份取第一份（确定性）。落地一律归一为 full.md。
        md_chosen = next(
            (
                c
                for c in md_candidates
                if c[0].filename.replace("\\", "/").rsplit("/", 1)[-1] == "full.md"
            ),
            md_candidates[0],
        )
        plan.append(md_chosen)

        declared = sum(info.file_size for info, _, _ in plan)
        if declared > max_total_bytes:
            raise MineruZipError(
                f"zip 声明解压总量 {declared} 字节超上限 {max_total_bytes}（Zip-Bomb 防御）"
            )

        dest_dir.mkdir(parents=True, exist_ok=True)
        running = 0
        md_path: Path | None = None
        content_list_paths: list[Path] = []
        image_count = 0
        try:
            for info, kind, rel in plan:
                target = dest_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(_CHUNK)
                        if not chunk:
                            break
                        running += len(chunk)
                        if running > max_total_bytes:
                            raise MineruZipError(
                                f"解压总量超上限 {max_total_bytes}（Zip-Bomb 防御）"
                            )
                        dst.write(chunk)
                if kind == "md":
                    md_path = target
                elif kind == "content_list":
                    content_list_paths.append(target)
                else:
                    image_count += 1
        except BaseException:
            import shutil

            shutil.rmtree(dest_dir, ignore_errors=True)
            raise
        # plan 里已有 md_chosen，这里不可能为 None；断言式收口防未来改坏
        if md_path is None:  # pragma: no cover
            raise MineruZipError("内部错误：白名单计划含 full.md 但未解出")
        return ZipExtractResult(
            md_path=md_path,
            content_list_paths=content_list_paths,
            image_count=image_count,
            total_bytes=running,
        )
    finally:
        zf.close()


# ── PDF 页数预检 ────────────────────────────────────────────────────


def count_pdf_pages(content: bytes) -> int | None:
    """pypdf 数页。读不动返回 None —— 交给 MinerU 兜底（-60006），不猜不拦。"""
    if not content:
        return None
    try:
        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(content)).pages)
    except Exception:
        return None


#: pypdf 专用有界执行器（安全审查 M2）：畸形 PDF 能让解析跑很久，绝不允许它
#: 占满事件循环默认执行器（engine 其它 to_thread 端点会被拖排队）。超时/异常
#: 一律按「数不出页数」处理，由调用方按保守值兜底（宁多留不许多放）。
_PDF_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pdf-pages")
PDF_JOB_TIMEOUT_S = 20.0


async def run_pdf_job(
    func: Callable[..., T], *args, timeout_s: float | None = None
) -> T | None:
    """在有界执行器上跑 CPU 重的 PDF 解析，带硬超时；超时/抛错 → None。"""
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_PDF_EXECUTOR, func, *args),
            timeout=timeout_s if timeout_s is not None else PDF_JOB_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 —— 超时与畸形输入同待遇：按未知处理
        logger.warning(
            "PDF 解析任务失败/超时（按未知处理）: %s: %s", type(exc).__name__, exc
        )
        return None


def assert_safe_remote_url(url: str, *, context: str) -> None:
    """MinerU 返回的远端 URL 仅允许 https 且禁止私网/回环（安全审查 L4，SSRF 纵深）。

    正常链路里这些 URL 来自 MinerU 官方响应（TLS 之下）；此闸防的是
    「MinerU 端点被攻陷/MITM 后把服务端引向内网」。域名不解析（DNS 绑定
    不可控），只拦直写 IP 的私网/回环/链路本地/保留段与 localhost。

    2026-10-09 补数字别名旁路：``127.1`` / ``2130706433`` / ``0x7f000001``
    / ``0177.0.0.1`` 这类 inet_aton 形态 ``ipaddress`` 直接解析拒绝，会从
    「不是 IP」的 except 分支放行、而 HTTP 客户端照样连到 127.0.0.1。
    用 ``inet_aton`` 再归一（纯本地换算，不发 DNS——解析不出的才是真域名）。
    """
    parsed = urlsplit(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname:
        raise MineruError(f"{context}: 拒绝非 https 的远端地址", retryable=False)
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise MineruError(f"{context}: 拒绝指向本机的远端地址", retryable=False)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # inet_aton 认得的数字别名就地归一；真域名（解析不出的）按原策略放行
        try:
            ip = ipaddress.ip_address(socket.inet_aton(host))
        except (OSError, ValueError):
            return
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise MineruError(
            f"{context}: 拒绝指向内网/回环地址的远端 URL", retryable=False
        )


# ── 客户端 ──────────────────────────────────────────────────────────


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            yield chunk


class MineruClient:
    """一次可重试的原子动作集合。编排层（doc_parse_service）负责状态机。"""

    def __init__(
        self,
        config: MineruConfig,
        *,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._max_attempts = max(1, max_attempts)
        self._backoff_base_s = backoff_base_s
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or time.monotonic
        self._submit_limiter = MinIntervalLimiter(
            SUBMIT_RATE_PER_MIN, clock=self._clock, sleep=self._sleep
        )
        self._query_limiter = MinIntervalLimiter(
            QUERY_RATE_PER_MIN, clock=self._clock, sleep=self._sleep
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- 内部：重试骨架 ------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        return self._backoff_base_s * (2 ** (attempt - 1))

    async def _run_with_retry(
        self, context: str, op: Callable[[int], Awaitable[T]]
    ) -> T:
        """重试骨架：暂态 MineruError / httpx 传输错误退避重试，其余立即穿透。"""
        for attempt in range(1, self._max_attempts + 1):
            try:
                return await op(attempt)
            except MineruError as e:
                if not e.retryable or attempt == self._max_attempts:
                    raise
                logger.warning("%s 暂态失败（第 %d 次）: %s", context, attempt, e)
                await self._sleep(self._backoff(attempt))
            except httpx.TransportError as e:
                if attempt == self._max_attempts:
                    raise MineruError(
                        f"{context}: 网络错误重试耗尽: {e}", retryable=True
                    ) from e
                logger.warning("%s 网络错误（第 %d 次）: %s", context, attempt, e)
                await self._sleep(self._backoff(attempt))
        raise MineruError(f"{context}: 重试耗尽", retryable=True)  # pragma: no cover

    # -- 内部：JSON 请求与三层信封 -------------------------------------

    async def _json_once(
        self,
        method: str,
        url: str,
        *,
        context: str,
        limiter: MinIntervalLimiter,
        json_body: dict | None,
    ) -> dict:
        await limiter.acquire()
        resp = await self._client.request(
            method,
            url,
            json=json_body,
            headers={"Authorization": f"Bearer {self._config.token}"},
        )
        return self._unwrap(resp, context)

    def _unwrap(self, resp: httpx.Response, context: str) -> dict:
        status = resp.status_code
        if status == 429 or status >= 500:
            raise MineruError(
                f"{context}: HTTP {status} {resp.text[:200]}", retryable=True
            )
        if status >= 400:
            raise MineruError(
                f"{context}: HTTP {status} {resp.text[:200]}", retryable=False
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise MineruError(f"{context}: 响应不是 JSON", retryable=False) from e
        if not isinstance(payload, dict):
            raise MineruError(f"{context}: 响应信封不是对象", retryable=False)

        success = payload.get("success")
        code = payload.get("code")
        ok = success is True or (code in (0, 200, "0", None) and success is not False)
        if not ok:
            raw_code = payload.get("msgCode")
            if raw_code is None:
                raw_code = code
            msg = payload.get("msg") or payload.get("message") or ""
            raise_for_code(raw_code, str(msg), context)

        data = payload.get("data")
        if not isinstance(data, dict):
            raise MineruError(f"{context}: 信封缺少 data 对象", retryable=False)
        return data

    # -- 公开动作 ------------------------------------------------------

    async def create_upload_batch(
        self, files: list[MineruFileSpec], *, language: str = "ch"
    ) -> tuple[str, list[str]]:
        """提交批次 → (batch_id, file_urls[])。file_urls 顺序与 files 一致。"""
        if not files:
            raise ValueError("至少一个文件")
        if len(files) > MAX_BATCH_FILES:
            raise ValueError(f"单请求最多 {MAX_BATCH_FILES} 个文件，收到 {len(files)}")

        payload_files = []
        for spec in files:
            item: dict = {
                "name": spec.name,
                "data_id": spec.data_id or spec.name,
                "is_ocr": spec.is_ocr,
            }
            if spec.page_ranges:
                item["page_ranges"] = spec.page_ranges
            payload_files.append(item)

        body = {
            "files": payload_files,
            "model_version": self._config.model_version,
            "enable_formula": True,
            "enable_table": True,
            "language": language,
        }
        context = "MinerU 提交解析批次"
        data = await self._run_with_retry(
            context,
            lambda _a: self._json_once(
                "POST",
                f"{self._config.base_url}/api/v4/file-urls/batch",
                context=context,
                limiter=self._submit_limiter,
                json_body=body,
            ),
        )

        batch_id = data.get("batch_id")
        file_urls = data.get("file_urls")
        if not isinstance(batch_id, str) or not batch_id:
            raise MineruError(f"{context}: 响应缺少 batch_id", retryable=False)
        if not isinstance(file_urls, list) or len(file_urls) != len(files):
            raise MineruError(f"{context}: file_urls 数量与请求不一致", retryable=False)
        return batch_id, [str(u) for u in file_urls]

    async def finalize_submission(self, batch_id: str) -> str:
        """上传完成后的收尾钩子：返回要落库/轮询的「解析任务号」。

        云端 v4 建批次即定 batch_id，无需收尾（恒等返回）；本地 V1 服务
        需要上传完成后以 file_id 建解析任务，由 `mineru_local.MineruLocalClient`
        覆盖。调用点固定在 submit_parse 的上传循环之后、落库之前。
        """
        return batch_id

    async def upload_file(self, url: str, content: bytes | Path) -> None:
        """PUT 预签名链接。**不带 Content-Type / Authorization**（否则签名失败）。"""
        assert_safe_remote_url(url, context="MinerU 文件上传")

        def factory() -> bytes | AsyncIterator[bytes]:
            return _file_chunks(content) if isinstance(content, Path) else content

        async def op(_attempt: int) -> None:
            resp = await self._client.put(url, content=factory())
            if 200 <= resp.status_code < 300:
                return
            if resp.status_code == 429 or resp.status_code >= 500:
                raise MineruError(
                    f"MinerU 文件上传: HTTP {resp.status_code}", retryable=True
                )
            raise MineruError(
                f"MinerU 文件上传: HTTP {resp.status_code} {resp.text[:200]}（链接可能已过期）",
                retryable=False,
            )

        await self._run_with_retry("MinerU 文件上传", op)

    async def get_batch_results(self, batch_id: str) -> list[MineruBatchItem]:
        context = "MinerU 查询批次结果"
        data = await self._run_with_retry(
            context,
            lambda _a: self._json_once(
                "GET",
                f"{self._config.base_url}/api/v4/extract-results/batch/{batch_id}",
                context=context,
                limiter=self._query_limiter,
                json_body=None,
            ),
        )
        raw_items = data.get("extract_result")
        if not isinstance(raw_items, list):
            raise MineruError(f"{context}: 响应缺少 extract_result", retryable=False)
        return [self._to_item(raw) for raw in raw_items]

    @staticmethod
    def _to_item(raw: object) -> MineruBatchItem:
        if not isinstance(raw, dict):
            raise MineruError("批次项不是对象", retryable=False)
        progress = raw.get("extract_progress")
        extracted = total = None
        if isinstance(progress, dict):
            ep, tp = progress.get("extracted_pages"), progress.get("total_pages")
            extracted = int(ep) if isinstance(ep, (int, float)) else None
            total = int(tp) if isinstance(tp, (int, float)) else None
        err_msg = raw.get("err_msg")
        err_code = extract_err_code(err_msg if isinstance(err_msg, str) else None)
        retryable = classify_error_code(err_code)[0] if err_code is not None else None
        state = raw.get("state")
        return MineruBatchItem(
            file_name=str(raw.get("file_name") or ""),
            state=str(state) if state is not None else "",
            full_zip_url=raw.get("full_zip_url"),
            err_msg=err_msg if isinstance(err_msg, str) else None,
            data_id=raw.get("data_id"),
            extracted_pages=extracted,
            total_pages=total,
            err_code=err_code,
            retryable=retryable,
        )

    async def download_zip(
        self, url: str, dest: Path, *, max_bytes: int = DEFAULT_ZIP_MAX_BYTES
    ) -> Path:
        """流式下载产物 zip；超限/失败必删残包（下游绝不能拿到半截 zip）。"""
        assert_safe_remote_url(url, context="MinerU 下载产物")

        async def op(_attempt: int) -> Path:
            try:
                async with self._client.stream("GET", url) as resp:
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise MineruError(
                            f"MinerU 下载产物: HTTP {resp.status_code}", retryable=True
                        )
                    if resp.status_code >= 400:
                        raise MineruError(
                            f"MinerU 下载产物: HTTP {resp.status_code}（结果链接可能已失效）",
                            retryable=False,
                        )
                    cl = resp.headers.get("content-length")
                    if cl and cl.isdigit() and int(cl) > max_bytes:
                        raise MineruError(
                            f"产物 zip 声明大小 {cl} 超上限 {max_bytes}",
                            retryable=False,
                        )
                    total = 0
                    with open(dest, "wb") as fh:
                        async for chunk in resp.aiter_bytes(_CHUNK):
                            total += len(chunk)
                            if total > max_bytes:
                                raise MineruError(
                                    f"产物 zip 超过上限 {max_bytes}", retryable=False
                                )
                            fh.write(chunk)
                return dest
            except BaseException:
                dest.unlink(missing_ok=True)
                raise

        return await self._run_with_retry("MinerU 下载产物", op)
