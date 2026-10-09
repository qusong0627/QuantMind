"""文档解析编排（T-FM-07）—— MinerU 批次状态机 + 重启续轮询 + 留存 GC。

与挖掘链的「重启即 failed」不同，本链的可靠性目标是**续跑**：`parsing` 行
是 MinerU 队列里的活任务，PG 是权威状态（doc_store），engine 进程重启后
``resume_pending()`` 按表把轮询重新挂起来，直到 done 落盘或超时定格。

四条必须守住的纪律：

1. **done 即下载**：MinerU 的 ``full_zip_url`` 没有 TTL 承诺，拿到 done
   先流式下载 + 白名单解包落盘，再写 ``parsed``——顺序反了就是「库里已
   解析、盘上没有」的空壳文档。
2. **记账在页数已知之后**（doc_quota 的契约）：done 且产物落盘成功才
   ``record``；复用（sha256 命中）不烧 MinerU 配额、一分不记。
3. **失败要能读懂**：残留 MinerU 原文 + 常见错误码提示（-60006 页数超限
   这类必须变成人话），见 ``parse_failure_message``。
4. **GС 只扫定型行**：parsed/organized/parse_failed 超留存期（默认 90 天）
   才删目录转 expired；parsing/uploaded 有人在写，不碰。

轮询循环对**瞬态与意外异常**都是「记录 + 按节拍再试，受总时限约束」：
下载重试不会烧 MinerU（重轮询结果幂等），把一次磁盘抖动变成永久失败才是
真正的损失；总时限到了统一定格「解析超时」。永久性 MinerUError 立即定格。

多文件（T-FM-19a）：一份文档 = 一个 MinerU 批次里的多个文件，data_id
``{doc_id}_p{i}`` 把批次项钉回部件序号（不拿文件名猜）。任一部件 failed →
整份定格并点名部件（记账按实际烧掉的页：已 done 部件照常 settle，在途部件
预留转已用）；全 done → 逐件下载解包 → ``doc_merge`` 合并成一份 full.md
（图片去重/改名、引用重写）→ 单次 settle。单文件路径与历史批次逐字节同口径
（``decode_original_paths`` 把单文件行合成为一件）。

时钟/睡眠可注入（测试用 FakeClock 把 2 小时超时压成毫秒级）。
"""

from __future__ import annotations

import asyncio
import errno
import io
import logging
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from backend.services.engine.alpha_agent.doc_alerts import maybe_alert_quota_low
from backend.services.engine.alpha_agent.doc_credentials import (
    TOKEN_SRC_ENV,
    TOKEN_SRC_USER,
    resolve_effective_mineru_config,
)
from backend.services.engine.alpha_agent.doc_merge import MergePart, merge_parts
from backend.services.engine.alpha_agent.doc_quota import (
    DocQuota,
    QuotaStatus,
    get_doc_quota,
)
from backend.services.engine.alpha_agent.doc_store import (
    DocStore,
    decode_original_paths,
    get_doc_store,
)
from backend.services.engine.alpha_agent.mineru_client import (
    MODE_CLOUD,
    MODE_LOCAL,
    MineruBatchItem,
    MineruClient,
    MineruConfig,
    MineruError,
    MineruFileSpec,
    extract_err_code,
    extract_zip_whitelist,
    mineru_unconfigured_message,
    resolve_mineru_config,
    run_pdf_job,
)
from backend.services.engine.alpha_agent.mineru_local import MineruLocalClient

logger = logging.getLogger(__name__)

ENV_DOCS_DIR = "RD_AGENT_DOCS_DIR"
ENV_POLL_S = "RD_AGENT_DOC_PARSE_POLL_S"
ENV_PARSE_TIMEOUT_S = "RD_AGENT_DOC_PARSE_TIMEOUT_S"
ENV_RETENTION_DAYS = "RD_AGENT_DOC_RETENTION_DAYS"

DEFAULT_DOCS_DIR = "/data/rd_agent_docs"
DEFAULT_POLL_S = 5.0
DEFAULT_PARSE_TIMEOUT_S = 7200.0  # 2 小时：200 页 VLM 解析的宽松上界
DEFAULT_RETENTION_DAYS = 90

#: 产物下载/解包的并发闸（MinerU 出件是带宽活，别把 event loop 的线程池打满）
MAX_CONCURRENT_PARSES = 3
#: 重启对账：uploaded 行超龄多少分钟翻 parse_failed（提交前一刻崩溃的孤儿）
STALE_UPLOADED_MINUTES = 30

#: 扫描件探测：抽前 N 页找文本层；超过此大小不做探测（直接按 OCR 送）
SCAN_SAMPLE_PAGES = 3
SCAN_DETECT_MAX_BYTES = 64 * 1024 * 1024

#: 用户解析设置（mode+端点+凭据）的解析缓存 TTL：轮询每 5s 一跳，不能每跳
#: 读一次 Redis/设置存储；负结果（用户清掉了）同样缓存——清掉后 ≤TTL 内定格。
USER_CONFIG_CACHE_TTL_S = 300.0
#: 两个内存缓存的容量护栏（超过就整体清空，防止多用户长跑无界增长）
USER_CONFIG_CACHE_MAX = 256
USER_CLIENT_CACHE_MAX = 64

#: 多文件部件 data_id 前缀截断长度（MinerU data_id 上限 128，给 ``_p{i}`` 留位）
MULTI_DATA_ID_MAX = 120

_DOC_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

#: MinerU 常见失败码 → 人话（原文照旧拼接在前，排查锚点不丢）
_CODE_HINTS: dict[str, str] = {
    "-60005": "文件超过 MinerU 200MB 限制",
    "-60006": "文件超过 MinerU 200 页限制，请拆分后重试",
    "-60018": "MinerU 平台今日解析额度已用尽，请明日再试",
    "-60019": "HTML 解析额度已用尽",
}


# ── 纯函数 ──────────────────────────────────────────────────────────


def part_data_id(doc_id: str, index: int, total: int) -> str:
    """部件 data_id：单文件保持裸 ``doc_id``（历史批次口径不变）。

    多文件 ``{doc_id 截断}_p{i}``（i 从 1 起，与「第 i/N 部分」同序）——
    提交与轮询共用本函数，批次项因此能精确钉回部件，不靠文件名猜。
    """
    if total <= 1:
        return doc_id
    return f"{doc_id[:MULTI_DATA_ID_MAX]}_p{index}"


def parse_failure_message(item: MineruBatchItem) -> str:
    """失败项 → 可读文案：MinerU 原文保留 + 常见码补人话提示。"""
    raw = (item.err_msg or "").strip()
    code = item.err_code if item.err_code is not None else extract_err_code(raw)
    hint = _CODE_HINTS.get(str(code)) if code is not None else None
    if raw and hint:
        return f"{raw}（{hint}）"
    if raw:
        return raw
    if hint:
        return f"解析失败：{hint}"
    return "解析失败：MinerU 未返回错误详情"


def detect_scanned_pdf(content: bytes) -> bool | None:
    """PDF 是否有文本层：抽前 N 页全文为空 → True（扫描件，需要 OCR）。

    读不动返回 None——**不猜**；保守策略由调用方决定（submit 侧 None → OCR）。
    """
    if not content:
        return None
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            return None
        pages = reader.pages[:SCAN_SAMPLE_PAGES]
        text = "".join((page.extract_text() or "") for page in pages)
    except Exception:  # noqa: BLE001 —— 解析库对畸形 PDF 抛什么的都有
        return None
    return not text.strip()


def user_facing_error(exc: BaseException) -> str:
    """异常 → **可存库**的错误文案（安全审查 L1）：error 字段会被 API 回吐。

    MinerU 文案不含本地磁盘布局（HTTP 状态码/平台错误码），原样保留；其余
    异常只留类型与通用说明——FileNotFoundError/OSError 的 str 带
    ``/data/rd_agent_docs/<id>/original.pdf`` 这类绝对路径，绝不落库。
    """
    if isinstance(exc, MineruError):
        return str(exc)
    if isinstance(exc, OSError) and exc.errno is not None:
        code = errno.errorcode.get(exc.errno, str(exc.errno))
        return f"文件处理失败（{code}），详情见服务日志"
    return f"处理失败（{type(exc).__name__}），详情见服务日志"


def _format_duration(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:g} 小时"
    if seconds >= 60:
        return f"{seconds / 60:g} 分钟"
    return f"{seconds:g} 秒"


def _link_or_copy(src: str, dst: str) -> str:
    """复用复制器：同盘硬链接（零拷贝），跨设备回落真实复制。"""
    try:
        os.link(src, dst)
        return dst
    except OSError:
        return shutil.copy2(src, dst)


def build_mineru_client(cfg: MineruConfig) -> MineruClient:
    """配置 → 客户端（默认工厂）：本地/局域网模式走 V1 HTTP 版实现。

    注入 ``client_factory`` 的调用方（测试/定制）拿到的仍是同一 ``cfg``；
    云端与本地两条协议在同一接口后面，编排层不感知差异。
    """
    if cfg.mode == MODE_LOCAL:
        return MineruLocalClient(cfg)
    return MineruClient(cfg)


def doc_accounting(doc: Mapping[str, Any]) -> bool | None:
    """行上的通道 → 是否计平台云配额（本地通道不计）。

    行上没定格模式（新列之前的存量行）→ ``None`` = 用部署级默认
    （实例 ``page_accounting``，与它们提交当时的判据一致）。
    """
    mode = str(doc.get("mineru_mode") or "").strip().lower()
    if mode == MODE_LOCAL:
        return False
    if mode == MODE_CLOUD:
        return True
    return None


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r 不是数字，回落默认 %s", name, raw, default)
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，回落默认 %d", name, raw, default)
        return default
    return value if value > 0 else default


class DocParseService:
    def __init__(
        self,
        *,
        store: DocStore | None = None,
        quota: DocQuota | None = None,
        client: MineruClient | None = None,
        client_factory: Callable[[Any], MineruClient] | None = None,
        docs_root: Path | str | None = None,
        poll_interval_s: float | None = None,
        parse_timeout_s: float | None = None,
        max_concurrent: int | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store or get_doc_store()
        self._quota = quota or get_doc_quota()
        self._client = client
        self._client_factory = client_factory or build_mineru_client
        self._built_client: MineruClient | None = None
        self._docs_root = Path(docs_root) if docs_root is not None else None
        self._poll_interval_s = (
            poll_interval_s
            if poll_interval_s is not None
            else _env_float(ENV_POLL_S, DEFAULT_POLL_S)
        )
        self._parse_timeout_s = (
            parse_timeout_s
            if parse_timeout_s is not None
            else _env_float(ENV_PARSE_TIMEOUT_S, DEFAULT_PARSE_TIMEOUT_S)
        )
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._parse_sem = asyncio.Semaphore(max_concurrent or MAX_CONCURRENT_PARSES)
        self._tasks: dict[str, asyncio.Task] = {}
        # 用户解析设置：解析结果与按配置建的客户端各带缓存（TTL/容量见常量）
        self._user_config_cache: dict[
            tuple[str, str], tuple[float, MineruConfig | None]
        ] = {}
        self._config_clients: dict[tuple, MineruClient] = {}

    # -- 路径 ----------------------------------------------------------

    @property
    def docs_root(self) -> Path:
        if self._docs_root is None:
            raw = (os.getenv(ENV_DOCS_DIR) or "").strip() or DEFAULT_DOCS_DIR
            self._docs_root = Path(raw)
        return self._docs_root

    def doc_dir(self, doc_id: str) -> Path:
        """单个文档的独占目录（<root>/<doc_id>/）。doc_id 必须是无路径分量的安全串。"""
        if not _DOC_ID_RE.fullmatch(doc_id):
            raise ValueError(f"非法 doc_id: {doc_id!r}")
        return self.docs_root / doc_id

    # -- 提交 ----------------------------------------------------------

    def _resolve_client(self) -> MineruClient:
        if self._client is not None:
            return self._client
        if self._built_client is None:
            config = resolve_mineru_config()
            if config is None:
                raise MineruError(mineru_unconfigured_message(), retryable=False)
            self._built_client = self._client_factory(config)
        return self._built_client

    def ensure_ready(self) -> None:
        """预检解析通道（env 已配 / 注入了替身）。缺配置抛 MineruError。

        上传端点在**落盘之前**调这里：先收文件再发现通道不可用，会留下
        一堆「上传成功但永远解析不了」的孤儿目录与行。
        """
        self._resolve_client()

    def _client_for_config(self, cfg: MineruConfig) -> MineruClient:
        """按完整配置建/取客户端（用户设置路径；配置即缓存键）。"""
        key = (cfg.mode, cfg.base_url, cfg.model_version, cfg.token, cfg.local_tier)
        client = self._config_clients.get(key)
        if client is None:
            if len(self._config_clients) >= USER_CLIENT_CACHE_MAX:
                self._config_clients.clear()
            client = self._client_factory(cfg)
            self._config_clients[key] = client
        return client

    async def _client_for_submit(
        self, doc: Mapping[str, Any]
    ) -> tuple[MineruClient, str, str | None]:
        """提交期建客户端 → ``(client, 凭据来源, 通道模式)``。

        后两项写进行上的 ``mineru_token_src`` / ``mineru_mode``（重启续轮询
        按来源重建同一客户端；记账按模式判是否计云配额）。注入替身（测试）
        直接短路，模式未知回 None。任何来源都没配置 → 抛 MineruError
        （``submit_parse`` 的 except 会定格行 + 退预留）。
        """
        if self._client is not None:
            return self._client, TOKEN_SRC_ENV, None
        cfg, src = await resolve_effective_mineru_config(
            str(doc.get("user_id") or ""), str(doc.get("tenant_id") or ""), strict=False
        )
        if cfg is None:
            raise MineruError(mineru_unconfigured_message(), retryable=False)
        if src == TOKEN_SRC_USER:
            return self._client_for_config(cfg), TOKEN_SRC_USER, cfg.mode
        return self._resolve_client(), TOKEN_SRC_ENV, cfg.mode

    async def _cached_user_config(self, doc: Mapping[str, Any]) -> MineruConfig | None:
        """行上来源为 user 的文档：解析用户设置（TTL 缓存）。

        strict 解析：设置存储读不到 → DocMiningSettingsError 上抛，轮询层
        按可重试处理——「读不到」绝不当成「用户清掉了」定格失败。
        """
        user_id = str(doc.get("user_id") or "")
        tenant_id = str(doc.get("tenant_id") or "")
        if not user_id or not tenant_id:
            return None
        key = (user_id, tenant_id)
        now = self._clock()
        entry = self._user_config_cache.get(key)
        if entry is not None and now - entry[0] < USER_CONFIG_CACHE_TTL_S:
            return entry[1]
        cfg, src = await resolve_effective_mineru_config(
            user_id, tenant_id, strict=True
        )
        # 只认 user 来源：批次建在用户配置的通道上，换 env 配置查不到结果
        effective = cfg if src == TOKEN_SRC_USER else None
        if len(self._user_config_cache) >= USER_CONFIG_CACHE_MAX:
            self._user_config_cache.clear()
        self._user_config_cache[key] = (now, effective)
        return effective

    async def _client_for_doc(self, doc: Mapping[str, Any]) -> MineruClient:
        """按行上记录的凭据来源重建客户端（重启续轮询也走这条）。

        生产单例不注入 client——以前轮询直接 ``self._client.get_batch_results``
        会 AttributeError，被轮询循环的兜底 except 当瞬态异常吞掉重试到 2h
        超时（每份文档都失败，日志只说「意外异常」）。轮询/下载统一走本方法。
        """
        if self._client is not None:
            return self._client
        if str(doc.get("mineru_token_src") or "") == TOKEN_SRC_USER:
            cfg = await self._cached_user_config(doc)
            if cfg is not None:
                row_mode = str(doc.get("mineru_mode") or "").strip().lower()
                if row_mode and cfg.mode != row_mode:
                    # 批次建在旧通道上：本地/云端互切后批次号在新通道查不到，
                    # 与其拿 404 当「任务消失」，不如点名说清是通道换了。
                    raise MineruError(
                        "解析通道在解析期间被更改，无法继续查询该批次："
                        "请在「因子挖掘 → 文档解析设置」确认通道后重新上传",
                        retryable=False,
                    )
                return self._client_for_config(cfg)
            raise MineruError(
                "用户 MinerU 解析设置已失效（可能已被清除或更改），解析无法继续："
                "请在「因子挖掘 → 文档解析设置」重新配置后重新上传",
                retryable=False,
            )
        return self._resolve_client()

    async def _detect_is_ocr_for(self, ext: str, path: Path) -> bool:
        """非 PDF → 不 OCR（图片/office 各有直解通路）；PDF 探测文本层。

        探测读不动或文件过大 → **保守送 OCR**：漏 OCR 的扫描件会产出空壳
        文本（静默的坏结果），多 OCR 电子件只是慢一点。
        """
        ext = str(ext or "").lower()
        if ext and not ext.startswith("."):
            ext = f".{ext}"
        if ext != ".pdf":
            return False
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size > SCAN_DETECT_MAX_BYTES:
            return True
        try:
            content = await asyncio.to_thread(path.read_bytes)
        except OSError:
            return False
        # pypdf 走专用有界执行器（M2）：畸形 PDF 不许占满默认线程池/堵死事件循环
        verdict = await run_pdf_job(detect_scanned_pdf, content)
        return verdict if verdict is not None else True

    async def _detect_is_ocr(self, doc: Mapping[str, Any]) -> bool:
        """单文件行入口（兼容保留）；提交链统一走 :meth:`_detect_is_ocr_for`。"""
        return await self._detect_is_ocr_for(
            str(doc.get("ext") or ""), Path(str(doc.get("original_path") or ""))
        )

    async def submit_parse(self, doc: Mapping[str, Any]) -> None:
        """提交解析：建批次（全部部件一次成批）→ 上传原件 → parsing 落库 → 挂轮询。

        单文件与多文件同一条部件流水线（``decode_original_paths`` 把单文件
        行合成为一件）；data_id 单文件=裸 doc_id、多文件 ``{doc_id}_p{i}``。

        任何提交期异常都把行定格 parse_failed（带原文），再向上抛——调用方
        （端点）据此返回错误，用户看到的是「这份文档为什么没解析」。
        """
        doc_id = str(doc["doc_id"])
        parts = decode_original_paths(doc)
        specs: list[MineruFileSpec] = []
        for idx, part in enumerate(parts, 1):
            part_path = Path(str(part.get("path") or ""))
            try:  # 本地 V1 协议建上传单必填字节数；stat 不动内容
                size_bytes: int | None = part_path.stat().st_size
            except OSError:
                size_bytes = None  # 文件不在：upload_file 会以 ENOENT 定格失败
            specs.append(
                MineruFileSpec(
                    name=str(part.get("name") or "document"),
                    data_id=part_data_id(doc_id, idx, len(parts)),
                    is_ocr=await self._detect_is_ocr_for(
                        str(part.get("ext") or ""),
                        part_path,
                    ),
                    size_bytes=size_bytes,
                )
            )
        mode: str | None = None
        try:
            if not specs:
                raise MineruError(
                    "缺少原件路径（提交未完成），请重新上传", retryable=False
                )
            # 凭据解析放在 try 内：任何来源都没配置也要走 except 定格
            # parse_failed + 退预留——留在 try 外会留下「uploaded 但永远
            # 解析不了」且预留被占死的孤儿行。
            client, token_src, mode = await self._client_for_submit(doc)
            batch_id, urls = await client.create_upload_batch(specs)
            if len(urls) < len(specs):
                raise MineruError(
                    "MinerU 未返回上传链接"
                    if len(specs) == 1
                    else f"MinerU 未返回全部上传链接（{len(urls)}/{len(specs)}）",
                    retryable=False,
                )
            # 上面已保证 len(urls) >= len(specs)；多余链接忽略即可
            for part, url in zip(parts, urls, strict=False):
                await client.upload_file(url, Path(str(part.get("path") or "")))
            # 收尾钩子：本地 V1 上传完成后才建解析任务（云 v4 是恒等——
            # 批次提交即任务），返回的号才是落库与轮询用的号。
            batch_id = await client.finalize_submission(batch_id)
        except Exception as exc:
            await self._store.update_doc(
                doc_id,
                status="parse_failed",
                parse_state="upload_failed",
                error=user_facing_error(exc),
            )
            self._release_quota(
                doc, accounting=None if mode is None else mode != MODE_LOCAL
            )
            raise
        # H2：从落盘到 MinerU 上传完成可以很久，用户可能已在这个窗口里删除。
        # 行已删 → 写 no-op（返回值 False），放弃解析，不留产物。
        hit = await self._store.update_doc(
            doc_id,
            status="parsing",
            mineru_batch_id=batch_id,
            parse_state="pending",
            error=None,
            mineru_token_src=token_src,
            mineru_mode=mode,
        )
        if not hit:
            # 但 MinerU **已经拿到文件**（批次已建、上传已完成），会照常解析
            # 并计费——预留转已用，不许全退（退=账面与真实账单脱钩）。
            shutil.rmtree(self.doc_dir(doc_id), ignore_errors=True)
            self._commit_quota(doc)
            logger.info("doc %s 上传期间已被删除，放弃解析", doc_id)
            return
        self.start_poll(doc_id)
        logger.info(
            "doc %s 已提交 MinerU（batch=%s, %s）",
            doc_id,
            batch_id,
            f"is_ocr={specs[0].is_ocr}"
            if len(specs) == 1
            else f"{len(specs)} 个文件, is_ocr={[s.is_ocr for s in specs]}",
        )

    # -- 轮询 ----------------------------------------------------------

    def start_poll(self, doc_id: str) -> None:
        existing = self._tasks.get(doc_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._poll_loop(doc_id), name=f"doc-poll-{doc_id}")

        def _discard(done: asyncio.Task, key: str = doc_id) -> None:
            if self._tasks.get(key) is done:
                self._tasks.pop(key, None)

        task.add_done_callback(_discard)
        self._tasks[doc_id] = task

    @staticmethod
    def _match_item(
        items: list[MineruBatchItem], doc: Mapping[str, Any]
    ) -> MineruBatchItem | None:
        doc_id = doc.get("doc_id")
        for item in items:
            if item.data_id and item.data_id == doc_id:
                return item
        filename = doc.get("filename")
        for item in items:
            if not item.data_id and item.file_name == filename:
                return item
        return None

    @staticmethod
    def _match_part_items(
        items: list[MineruBatchItem],
        doc: Mapping[str, Any],
        parts: list[dict[str, str]],
    ) -> list[MineruBatchItem | None]:
        """批次项 → 部件序列对齐（与上传顺序同长，缺项为 None）。

        data_id 精确匹配优先（我们提交时写了 ``{doc_id}_p{i}``）；老批次
        没有 data_id 时按文件名兜底——同名多件按出现顺序一一认领。
        """
        doc_id = str(doc.get("doc_id") or "")
        total = len(parts)
        by_data: dict[str, MineruBatchItem] = {}
        by_name: dict[str, list[MineruBatchItem]] = {}
        for item in items:
            did = str(item.data_id or "")
            if did:
                by_data.setdefault(did, item)
            elif item.file_name:
                by_name.setdefault(str(item.file_name), []).append(item)
        matched: list[MineruBatchItem | None] = []
        for idx, part in enumerate(parts, 1):
            hit = by_data.get(part_data_id(doc_id, idx, total))
            if hit is None:
                queued = by_name.get(str(part.get("name") or ""))
                if queued:
                    hit = queued.pop(0)
            matched.append(hit)
        return matched

    def _release_quota(
        self, doc: Mapping[str, Any], *, accounting: bool | None = None
    ) -> None:
        """失败/删除路径：全额退回预留（没预留过 = no-op）。

        记账开关默认按行上定格的通道（本地通道从没预留过，天然 no-op）；
        提交失败且模式未知时由调用方显式传（None → 部署级默认）。
        """
        try:
            self._quota.release(
                str(doc["doc_id"]),
                str(doc.get("user_id") or ""),
                accounting=doc_accounting(doc) if accounting is None else accounting,
            )
        except Exception as exc:  # noqa: BLE001 —— 释放失败只告警（TTL 会回收）
            logger.warning("doc %s 配额预留释放失败: %s", doc.get("doc_id"), exc)

    def _commit_quota(self, doc: Mapping[str, Any]) -> None:
        """页数不可知但费用已发生（MinerU done / 文件已上传）→ 预留转已用。"""
        try:
            self._quota.commit_reservation(
                str(doc["doc_id"]), accounting=doc_accounting(doc)
            )
        except Exception as exc:  # noqa: BLE001 —— 记账失败只告警不回滚
            logger.warning("doc %s 配额预留转已用失败: %s", doc.get("doc_id"), exc)

    def _settle_quota(self, doc: Mapping[str, Any], pages: int) -> QuotaStatus | None:
        """产物落定后的结算：预留 → 实际（多退少补）；返回结算后状态供告警判。

        结算是用量**真正落地**的唯一时点——平台余量告警挂在结算之后的
        调用方（失败路径 release 全退后条件可能不再成立，不在那里判）。
        本地通道（行上 mode=local）不计平台云配额，settle 直接 no-op。
        """
        try:
            return self._quota.settle(
                str(doc["doc_id"]),
                str(doc.get("user_id") or ""),
                int(pages or 0),
                accounting=doc_accounting(doc),
            )
        except Exception as exc:  # noqa: BLE001 —— 产物已落盘，记账失败只告警不回滚
            logger.warning("doc %s 配额结算失败: %s", doc.get("doc_id"), exc)
            return None

    async def _alert_quota_low(self, status: QuotaStatus | None) -> None:
        """结算后判告警。走 to_thread：管理员 fanout 是同步 psycopg 落库，
        直接在事件循环里跑会在 DB 慢时卡住整个 engine（轮询/API 一起等）。

        旁路纪律不变：任何异常只记日志，绝不上抛（``maybe_alert_quota_low``
        自身承诺不抛；to_thread 调度层出错也在这里兜住）。
        """
        if status is None:
            return
        try:
            await asyncio.to_thread(maybe_alert_quota_low, status, quota=self._quota)
        except Exception as exc:  # noqa: BLE001 —— 告警是锦上添花，绝不弄断主链
            logger.warning("doc 配额告警旁路异常（已忽略）: %s", exc)

    async def _fail(
        self,
        doc: Mapping[str, Any],
        message: str,
        *,
        parse_state: str = "failed",
        release_quota: bool = True,
    ) -> None:
        await self._store.update_doc(
            str(doc["doc_id"]),
            status="parse_failed",
            parse_state=parse_state,
            error=message,
        )
        if release_quota:
            self._release_quota(doc)
        logger.warning("doc %s 解析失败：%s", doc.get("doc_id"), message)

    async def _fail_parts(
        self,
        doc: Mapping[str, Any],
        failed_part: Mapping[str, str],
        failed_item: MineruBatchItem,
        matched: list[MineruBatchItem | None],
    ) -> None:
        """多文件：某部件 failed → 整份定格（点名是哪一件）。

        记账按**实际烧掉的页**：同批已 done 部件 MinerU 照常计费（settle
        页数，页数不可知则预留转已用）；还有在途部件时按最坏情况把预留转为
        已用（它可能随后完成并计费）；全员失败才全退（与单文件同口径）。
        """
        name = str(failed_part.get("name") or "文件")
        message = f"「{name}」：{parse_failure_message(failed_item)}"
        await self._store.update_doc(
            str(doc["doc_id"]),
            status="parse_failed",
            parse_state="failed",
            error=message,
        )
        done_items = [
            item
            for item in matched
            if item is not None and (item.state or "").strip().lower() == "done"
        ]
        inflight = any(
            item is None or (item.state or "").strip().lower() not in ("done", "failed")
            for item in matched
        )
        if inflight:
            self._commit_quota(doc)
        elif done_items:
            pages = sum(int(item.total_pages or 0) for item in done_items)
            await self._settle_or_commit_quota(doc, pages)
        else:
            self._release_quota(doc)
        logger.warning("doc %s 解析失败：%s", doc.get("doc_id"), message)

    async def _poll_once(self, doc_id: str) -> str:
        """一次轮询：parsed / parse_failed / active / pending / gone。

        瞬态/永久 MineruError 原样上抛，由 ``_poll_loop`` 决定重试还是定格。
        """
        doc = await self._store.get_doc(doc_id)
        if not doc or doc.get("status") != "parsing":
            return "gone"
        batch_id = doc.get("mineru_batch_id")
        if not batch_id:
            await self._fail(doc, "缺少 MinerU 批次号（提交未完成），请重新上传")
            return "parse_failed"

        client = await self._client_for_doc(doc)
        items = await client.get_batch_results(str(batch_id))
        parts = decode_original_paths(doc)
        if len(parts) > 1:
            return await self._poll_once_multi(doc, items, parts)

        item = self._match_item(items, doc)
        if item is None:
            return "pending"  # 自己那条还没出现在结果里：只等待，不动状态

        state = (item.state or "").strip().lower()
        if state == "done":
            return await self._finish_done(doc, item)
        if state == "failed":
            await self._fail(doc, parse_failure_message(item))
            return "parse_failed"

        fields: dict[str, Any] = {"parse_state": state or "running"}
        if item.total_pages:
            fields["page_count"] = int(item.total_pages)
        await self._store.update_doc(doc_id, **fields)
        return "active"

    async def _poll_once_multi(
        self,
        doc: Mapping[str, Any],
        items: list[MineruBatchItem],
        parts: list[dict[str, str]],
    ) -> str:
        """多文件轮询：任一部件 failed → 整份定格；缺件 → 等；全 done → 合并落盘。"""
        matched = self._match_part_items(
            items, doc, parts
        )  # 与 parts 同长，缺项为 None
        for part, item in zip(parts, matched, strict=True):
            if item is None:
                continue
            if (item.state or "").strip().lower() == "failed":
                await self._fail_parts(doc, part, item, matched)
                return "parse_failed"
        if any(item is None for item in matched):
            return "pending"  # 有部件还没出现在结果里：只等待，不动状态

        if all((item.state or "").strip().lower() == "done" for item in matched):
            return await self._finish_done_multi(doc, parts, matched)

        fields: dict[str, Any] = {"parse_state": "running"}
        pages = sum(int(item.total_pages or 0) for item in matched)
        if pages:
            fields["page_count"] = pages
        await self._store.update_doc(str(doc["doc_id"]), **fields)
        return "active"

    async def _settle_or_commit_quota(self, doc: Mapping[str, Any], pages: int) -> None:
        """页数已知 → settle（多退少补）+ 告警判；不可知 → 预留转已用。

        MinerU 报了 done 却没给页数（上游契约外）时 settle(0) 会把预留全退——
        费用明明已发生，账面却归零。改为把预留留在账上（保守上界）。
        """
        if pages:
            status = self._settle_quota(doc, pages)
        else:
            self._commit_quota(doc)
            status = None
        await self._alert_quota_low(status)

    async def _finish_done(self, doc: Mapping[str, Any], item: MineruBatchItem) -> str:
        """done → 立即下载落盘（链接无 TTL 承诺）→ 白名单解包 → parsed + 结算。"""
        doc_id = str(doc["doc_id"])
        if not item.full_zip_url:
            await self._fail(doc, "解析完成但未返回 full_zip_url（响应结构异常）")
            return "parse_failed"

        pages = int(item.total_pages or doc.get("page_count") or 0)
        doc_dir = self.doc_dir(doc_id)
        doc_dir.mkdir(parents=True, exist_ok=True)
        zip_path = doc_dir / "mineru.zip"
        dest = doc_dir / "parsed"
        async with self._parse_sem:
            try:
                client = await self._client_for_doc(doc)
                await client.download_zip(item.full_zip_url, zip_path)
                result = await asyncio.to_thread(extract_zip_whitelist, zip_path, dest)
            except MineruError as exc:
                # MinerU 真扣了页才给 zip（done），按实际页数结算而不是全退
                await self._settle_or_commit_quota(doc, pages)
                await self._fail(
                    doc,
                    f"产物下载/解包失败：{exc}",
                    release_quota=False,
                )
                return "parse_failed"

        fields: dict[str, Any] = {
            "status": "parsed",
            "parse_state": "done",
            "error": None,
            "md_path": str(result.md_path),
        }
        if result.content_list_paths:
            fields["content_list_path"] = str(result.content_list_paths[0])
        if pages:
            fields["page_count"] = pages
        hit = await self._store.update_doc(doc_id, **fields)
        if not hit:
            # H2：下载/解包期间用户删了文档（写被 deleted 守卫拦下）。
            # 产物刚被写回已删目录 → 再清一次；删除端点对已建批次的行不退
            # 预留（MinerU 照常计费），此处也不再结算——预留留在账上
            # （保守上界，24h TTL 自回收），账面与真实账单一致。
            shutil.rmtree(doc_dir, ignore_errors=True)
            logger.info("doc %s 完成前已被删除，丢弃产物", doc_id)
            return "gone"

        await self._settle_or_commit_quota(doc, pages)
        logger.info(
            "doc %s 解析完成（%d 页，%d 张图）", doc_id, pages, result.image_count
        )
        return "parsed"

    async def _finish_done_multi(
        self,
        doc: Mapping[str, Any],
        parts: list[dict[str, str]],
        items: list[MineruBatchItem],
    ) -> str:
        """全部 done → 逐件下载解包 → 合并成一份 full.md → parsed + 单次结算。

        中间物（``mineru_p{i}.zip`` / ``parts/p{i}/``）成功后清掉；进函数先清
        一次旧残留——轮询层对意外异常按可重试处理，重试必须幂等：同一份产物
        合并同一遍，绝不让两次尝试的产出混起来。
        """
        doc_id = str(doc["doc_id"])
        doc_dir = self.doc_dir(doc_id)
        doc_dir.mkdir(parents=True, exist_ok=True)
        dest = doc_dir / "parsed"
        parts_root = doc_dir / "parts"
        shutil.rmtree(parts_root, ignore_errors=True)
        shutil.rmtree(dest, ignore_errors=True)
        for idx in range(1, len(parts) + 1):
            (doc_dir / f"mineru_p{idx}.zip").unlink(missing_ok=True)

        for part, item in zip(parts, items, strict=True):  # 调用方保证同长（matched）
            if not item.full_zip_url:
                await self._fail(
                    doc,
                    f"「{part.get('name')}」解析完成但未返回 full_zip_url"
                    "（响应结构异常）",
                )
                return "parse_failed"

        pages = sum(int(item.total_pages or 0) for item in items)
        merge_inputs: list[MergePart] = []
        async with self._parse_sem:
            try:
                client = await self._client_for_doc(doc)
                for idx, (part, item) in enumerate(zip(parts, items, strict=True), 1):
                    zip_path = doc_dir / f"mineru_p{idx}.zip"
                    part_dir = parts_root / f"p{idx}"
                    try:
                        await client.download_zip(str(item.full_zip_url), zip_path)
                        await asyncio.to_thread(
                            extract_zip_whitelist, zip_path, part_dir
                        )
                    except MineruError as exc:
                        raise MineruError(
                            f"「{part.get('name')}」{exc}", retryable=exc.retryable
                        ) from exc
                    merge_inputs.append(
                        MergePart(
                            name=str(part.get("name") or f"第 {idx} 部分"),
                            parsed_dir=part_dir,
                        )
                    )
                result = await asyncio.to_thread(merge_parts, merge_inputs, dest)
            except MineruError as exc:
                # MinerU 真扣了页才给 zip（done），按实际页数结算而不是全退
                await self._settle_or_commit_quota(doc, pages)
                await self._fail(
                    doc,
                    f"产物下载/解包失败：{exc}",
                    release_quota=False,
                )
                return "parse_failed"

        fields: dict[str, Any] = {
            "status": "parsed",
            "parse_state": "done",
            "error": None,
            "md_path": str(result.md_path),
            # 多文件的 content_list 不合并：整理链只认 full.md，合并 json 无消费方
            "content_list_path": None,
        }
        if pages:
            fields["page_count"] = pages
        hit = await self._store.update_doc(doc_id, **fields)
        if not hit:
            # H2：下载/合并期间用户删了文档（写被 deleted 守卫拦下）。
            # 产物刚被写回已删目录 → 再清一次；删除端点对已建批次的行不退
            # 预留（MinerU 照常计费），预留留在账上（保守上界）不再结算。
            shutil.rmtree(doc_dir, ignore_errors=True)
            logger.info("doc %s 完成前已被删除，丢弃产物", doc_id)
            return "gone"

        # 中间物清掉：合并产物已定型在 parsed/
        shutil.rmtree(parts_root, ignore_errors=True)
        for idx in range(1, len(parts) + 1):
            (doc_dir / f"mineru_p{idx}.zip").unlink(missing_ok=True)
        await self._settle_or_commit_quota(doc, pages)
        logger.info(
            "doc %s 多文件解析完成（%d 件 / %d 页 / %d 张图）",
            doc_id,
            len(parts),
            pages,
            result.image_count,
        )
        return "parsed"

    async def _poll_loop(self, doc_id: str) -> None:
        deadline = self._clock() + self._parse_timeout_s
        while True:
            if self._clock() >= deadline:
                await self._mark_timeout(doc_id)
                return
            try:
                state = await self._poll_once(doc_id)
            except MineruError as exc:
                if not exc.retryable:
                    await self._store.update_doc(
                        doc_id,
                        status="parse_failed",
                        parse_state="failed",
                        error=str(exc),
                    )
                    return
                logger.warning("doc %s 轮询暂态失败（%s），按节拍重试", doc_id, exc)
                state = "retry"
            except Exception as exc:  # noqa: BLE001
                # 意外异常（磁盘抖动等）按可恢复处理：重轮询幂等，把一次抖动
                # 变成永久失败才是真的损失；总时限兜底。
                logger.exception("doc %s 轮询意外异常，继续等待: %s", doc_id, exc)
                state = "retry"
            if state in ("parsed", "parse_failed", "gone"):
                return
            await self._sleep(self._poll_interval_s)

    async def _mark_timeout(self, doc_id: str) -> None:
        doc = await self._store.get_doc(doc_id)
        if not doc or doc.get("status") != "parsing":
            return
        await self._fail(
            doc,
            f"解析超时（超过 {_format_duration(self._parse_timeout_s)} 未完成），请稍后重新提交",
        )

    # -- sha256 幂等复用 ------------------------------------------------

    async def maybe_reuse(self, doc: Mapping[str, Any]) -> bool:
        """命中同用户同 sha256 的已解析文档 → 复制产物、置 parsed、不烧配额。

        donor 行在但产物没了（被 GC）：返回 False 回落真解析——假装 parsed
        等于把用户引向一个打不开的文档。
        """
        user_id = str(doc.get("user_id") or "")
        sha = str(doc.get("sha256") or "")
        if not user_id or not sha:
            return False
        donor = await self._store.find_reusable_parsed(user_id, sha)
        if not donor or donor.get("doc_id") == doc.get("doc_id"):
            return False
        donor_md_raw = donor.get("md_path") or ""
        if not donor_md_raw:
            return False
        donor_md = Path(str(donor_md_raw))
        if not donor_md.is_file():
            return False

        doc_id = str(doc["doc_id"])
        dst_dir = self.doc_dir(doc_id) / "parsed"
        await asyncio.to_thread(
            shutil.copytree,
            donor_md.parent,
            dst_dir,
            copy_function=_link_or_copy,
            dirs_exist_ok=True,
        )
        fields: dict[str, Any] = {
            "status": "parsed",
            "parse_state": "done",
            "error": None,
            "md_path": str(dst_dir / donor_md.name),
        }
        donor_cl = donor.get("content_list_path")
        if donor_cl:
            fields["content_list_path"] = str(dst_dir / Path(str(donor_cl)).name)
        if donor.get("page_count") is not None:
            fields["page_count"] = int(donor["page_count"])
        if donor.get("files_count") is not None:
            # 件数要抄到新行（列表页展示用）；original_paths 是各文档自己的
            # 磁盘事实，绝不跨行复制（新行保留自己刚上传的部件清单）
            fields["files_count"] = int(donor["files_count"])
        hit = await self._store.update_doc(doc_id, **fields)
        if not hit:
            # H2：复制产物期间行已被删除 → 清掉刚复制的目录，回落真解析路径
            # （upload 端点会再查一次行状态；即便走到 submit 也会被守卫拦下）
            shutil.rmtree(dst_dir, ignore_errors=True)
            logger.info("doc %s 复用期间已被删除，放弃复用", doc_id)
            return False
        logger.info(
            "doc %s 复用已解析产物（sha256 命中 %s），不消耗 MinerU 配额",
            doc_id,
            donor.get("doc_id"),
        )
        return True

    # -- 续跑 / GC ------------------------------------------------------

    async def resume_pending(self) -> int:
        """进程启动对账：超龄 uploaded 翻失败 + 全部 parsing 行重新挂轮询。"""
        flipped = await self._store.fail_stale_uploaded(
            older_than_minutes=STALE_UPLOADED_MINUTES
        )
        rows = await self._store.list_parsing()
        for row in rows:
            self.start_poll(str(row["doc_id"]))
        if rows or flipped:
            logger.info(
                "文档解析续跑：接管 %d 个 parsing，对账定格 %d 个滞留 uploaded",
                len(rows),
                flipped,
            )
        return len(rows)

    async def gc_expired(self) -> int:
        """留存 GC：定型行超留存期 → 删目录 + 转 expired。返回处理条数。"""
        days = _env_int(ENV_RETENTION_DAYS, DEFAULT_RETENTION_DAYS)
        rows = await self._store.list_expired_candidates(retention_days=days)
        for row in rows:
            doc_id = str(row["doc_id"])
            shutil.rmtree(self.doc_dir(doc_id), ignore_errors=True)
            await self._store.mark_expired(doc_id)
        if rows:
            logger.info("文档留存 GC：清理 %d 份超 %d 天文档", len(rows), days)
        return len(rows)

    async def cancel(self, doc_id: str) -> bool:
        """取消在途轮询并等它**完全停下**。返回是否找到任务。

        删除端点必须先调它再 rmtree：直接删目录的话，轮询下一拍落地
        ``_finish_done`` 会把行从 deleted 写回 parsed、把产物写回已删目录
        （「删完又复活」）。等 gather 返回 = 任务已在任一点让出，之后
        rmtree 才是最后一次触碰这份目录。
        """
        task = self._tasks.pop(doc_id, None)
        if task is None:
            return False
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def shutdown(self) -> None:
        """取消全部轮询任务（端点停机/测试收尾）。"""
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


_service: DocParseService | None = None


def get_doc_parse_service() -> DocParseService:
    global _service
    if _service is None:
        _service = DocParseService()
    return _service
