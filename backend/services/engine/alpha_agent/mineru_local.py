"""本地/局域网 MinerU（自托管 4.x 的 **V1 HTTP API**）客户端（T-FM-21）。

与云端 v4 是**两套协议**，不可以把 ``MINERU_BASE_URL`` 直接指到自托管服务：

    云端 v4                                    本地 V1（本模块）
    POST /api/v4/file-urls/batch               POST /v1/uploads
    PUT  预签名 URL（OSS，不带任何头）          PUT  同源 upload_url（带 Bearer）
    GET  /api/v4/extract-results/batch/{id}    POST /v1/uploads/{id}/complete → file_id
    GET  full_zip_url                          POST /v1/parse/jobs → job_id
                                               GET  /v1/parse/jobs/{job_id}
                                               GET  /v1/files/{fid}/content
    zip: full.md                               zip: markdown.md（解包层已做别名）

契约逐字段核对自 MinerU 4.0.11 官方源码（``mineru/parser/api_server.py``、
``tests/unittest/test_parser_api_contract.py``）与官方示例脚本
（``scripts/http_api_example.sh``），2026-10-09。

三条设计纪律：

1. **同源纪律**：Bearer 只发给与 API 基址同源（scheme+host+有效端口）的地址；
   302 跨源不重发凭证。upload_headers 始终原样保留。局域网部署最容易把
   这条做丢——丢了就是把内网 key 漏给任意重定向目标。
2. **不套云端 SSRF 闸**：``assert_safe_remote_url`` 拒绝 http/私网，而那
   正是本地模式的工作地址；本客户端以「运维显式配置的 MINERU_LOCAL_URL」
   换掉那两道闸，且请求/下载只走该基址（302 除外，见第 1 条）。
3. **状态枚举不猜测**：job 终态（completed/partial/failed/canceled）下
   queued/running 的部件一律映射 failed——否则编排层永远轮询挂死；未知
   job 状态响亮报错。V1 的 job 索引在服务进程内存里（服务重启即丢），
   404 必须定格失败而不是重试到超时。

与云端客户端共用重试骨架（``_run_with_retry``）与异常分类；响应是**平铺
JSON 模型**（无三层信封），所以 JSON 请求走本模块自己的 ``_json_plain``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx

from .mineru_client import (
    DEFAULT_ZIP_MAX_BYTES,
    MAX_BATCH_FILES,
    MineruAuthError,
    MineruBatchItem,
    MineruClient,
    MineruError,
    MineruFileSpec,
    _CHUNK,
    _file_chunks,
)

logger = logging.getLogger(__name__)

#: 创建 upload 的 MIME 推断（官方示例脚本同表；未知扩展名回落 octet-stream）
_MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".html": "text/html",
    ".htm": "text/html",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".epub": "application/epub+zip",
}
_DEFAULT_MIME = "application/octet-stream"

_JOB_ACTIVE = frozenset({"queued", "running"})
_JOB_DONE = frozenset({"completed", "partial"})
_JOB_DEAD = frozenset({"failed", "canceled"})
_JOB_ALL = _JOB_ACTIVE | _JOB_DONE | _JOB_DEAD
_FILE_RUNNING = frozenset({"queued", "running"})

_MAX_REDIRECTS = 5
_DEFAULT_HTTP_PORT = {"http": 80, "https": 443}


def count_pages_in_range(page_range: str | None) -> int | None:
    """V1 的 page_range 文本 → 页数；不认识的形态一律 None（绝不猜）。

    认：``"1-7"``、``"1-5,8"``（并集）、``"3"``；不认：``""``/``"all"``/
    ``"r3-r1"`` 等花式写法——数错页数会污染展示，宁缺毋错（本地模式
    页数记账已停用，此值只影响界面展示）。
    """
    raw = (page_range or "").strip()
    if not raw:
        return None
    total = 0
    for chunk in raw.split(","):
        part = chunk.strip()
        if not part:
            return None
        if "-" in part:
            lo_s, _, hi_s = part.partition("-")
            if not (lo_s.strip().isdigit() and hi_s.strip().isdigit()):
                return None
            lo, hi = int(lo_s), int(hi_s)
            if hi < lo:
                return None
            total += hi - lo + 1
        elif part.isdigit():
            total += 1
        else:
            return None
    return total or None


def _same_origin(a: str, b: str) -> bool:
    """scheme + host + 有效端口 全等（官方 SDK ``_same_origin_upload_headers`` 口径）。"""
    pa, pb = urlsplit(a), urlsplit(b)
    if pa.scheme.lower() not in ("http", "https") or pb.scheme.lower() not in (
        "http",
        "https",
    ):
        return False
    if not pa.hostname or not pb.hostname:
        return False

    def eff_port(p) -> int:
        return p.port or _DEFAULT_HTTP_PORT[p.scheme.lower()]

    return (
        pa.scheme.lower() == pb.scheme.lower()
        and pa.hostname.lower() == pb.hostname.lower()
        and eff_port(pa) == eff_port(pb)
    )


@dataclass
class _LocalUpload:
    upload_id: str
    url: str
    page_range: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class _LocalBatch:
    entries: list[_LocalUpload]
    #: job 级 OCR 开关：任一件声明 OCR → 整任务 ocr；否则 auto
    ocr_mode: str = "auto"


class MineruLocalClient(MineruClient):
    """本地/局域网 MinerU（V1）实现。编排层接口与云端客户端逐字节一致。"""

    def __init__(self, config, **kwargs) -> None:
        super().__init__(config, **kwargs)
        #: 批次令牌 → upload 清单（create 建、finalize 消费）
        self._local_batches: dict[str, _LocalBatch] = {}
        #: upload URL → 所属批次令牌（upload_file 靠它找服务端 upload_headers）
        self._local_by_url: dict[str, str] = {}

    # -- 内部：平铺 JSON 请求（V1 无三层信封） -------------------------

    def _auth_headers(self) -> dict[str, str]:
        key = self._config.token
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _unwrap_plain(self, resp: httpx.Response, context: str) -> dict:
        status = resp.status_code
        if status == 429 or status >= 500:
            raise MineruError(
                f"{context}: HTTP {status} {resp.text[:200]}", retryable=True
            )
        if status in (401, 403):
            raise MineruAuthError(
                f"{context}: 本地 MinerU 拒绝访问（HTTP {status}，"
                f"检查 MINERU_LOCAL_API_KEY）: {resp.text[:200]}",
                retryable=False,
            )
        if 300 <= status < 400:
            raise MineruError(
                f"{context}: HTTP {status} 重定向（协议异常，检查 MINERU_LOCAL_URL）",
                retryable=False,
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
            raise MineruError(f"{context}: 响应不是对象", retryable=False)
        return payload

    async def _json_plain(
        self, method: str, url: str, *, context: str, json_body: dict | None = None
    ) -> dict:
        async def op(_attempt: int) -> dict:
            resp = await self._client.request(
                method, url, json=json_body, headers=self._auth_headers()
            )
            return self._unwrap_plain(resp, context)

        return await self._run_with_retry(context, op)

    # -- 上传三段 ------------------------------------------------------

    async def create_upload_batch(
        self, files: list[MineruFileSpec], *, language: str = "ch"
    ) -> tuple[str, list[str]]:
        """逐个建 upload → (本地批次令牌, 同序的绝对 upload URL)。

        令牌是发给 finalize 的内部凭据（V1 建 upload 阶段没有批次概念，
        job 要等全部 complete 之后才建）；upload URL 是服务端返回的**相对**
        路径按基址解析后的绝对地址。``language`` 仅为与云端签名一致——
        V1 建 upload 不接受语言参数（auto 由服务端处理）。
        """
        if not files:
            raise ValueError("至少一个文件")
        if len(files) > MAX_BATCH_FILES:
            raise ValueError(f"单请求最多 {MAX_BATCH_FILES} 个文件，收到 {len(files)}")
        # V1 create 必填 bytes：先整批校验再发第一个请求（绝不打半截请求）
        for spec in files:
            if not spec.size_bytes or spec.size_bytes <= 0:
                raise MineruError(
                    f"本地 MinerU 需要文件大小（size_bytes）才能建上传: {spec.name!r}",
                    retryable=False,
                )

        entries: list[_LocalUpload] = []
        for spec in files:
            context = "本地 MinerU 建上传"
            data = await self._json_plain(
                "POST",
                f"{self._config.base_url}/v1/uploads",
                context=context,
                json_body={
                    "filename": spec.name,
                    "bytes": int(spec.size_bytes),
                    "mime_type": _MIME_BY_EXT.get(
                        Path(spec.name).suffix.lower(), _DEFAULT_MIME
                    ),
                    "purpose": "parse",
                },
            )
            upload_id = data.get("id")
            status = data.get("status")
            upload_url = data.get("upload_url")
            if not isinstance(upload_id, str) or not upload_id:
                raise MineruError(f"{context}: 响应缺少 upload id", retryable=False)
            # 我们不传 sha256sum，服务端不可能去重成 completed——出现即协议
            # 违例（无声跳过会丢文件），响亮失败。
            if status != "pending" or not isinstance(upload_url, str) or not upload_url:
                raise MineruError(
                    f"{context}: 期望 status=pending 且带 upload_url，"
                    f"实际 status={status!r}",
                    retryable=False,
                )
            headers = data.get("upload_headers")
            entries.append(
                _LocalUpload(
                    upload_id=upload_id,
                    url=urljoin(self._config.base_url + "/", upload_url),
                    page_range=spec.page_ranges,
                    headers={
                        str(k): str(v)
                        for k, v in (headers or {}).items()
                        if isinstance(k, str)
                    },
                )
            )

        token = f"local-{uuid4().hex}"
        self._local_batches[token] = _LocalBatch(
            entries=entries,
            ocr_mode="ocr" if any(s.is_ocr for s in files) else "auto",
        )
        for entry in entries:
            self._local_by_url[entry.url] = token
        return token, [e.url for e in entries]

    async def upload_file(self, url: str, content: bytes | Path) -> None:
        """PUT 字节到同源 upload_url：服务端 upload_headers 原样带，同源才加 Bearer。"""
        entry = self._entry_for_url(url)
        headers = dict(entry.headers)
        if _same_origin(url, self._config.base_url):
            headers.update(self._auth_headers())

        def factory() -> bytes | object:
            return _file_chunks(content) if isinstance(content, Path) else content

        async def op(_attempt: int) -> None:
            resp = await self._client.put(url, content=factory(), headers=headers)
            if 200 <= resp.status_code < 300:
                return
            if resp.status_code == 429 or resp.status_code >= 500:
                raise MineruError(
                    f"本地 MinerU 文件上传: HTTP {resp.status_code}", retryable=True
                )
            raise MineruError(
                f"本地 MinerU 文件上传: HTTP {resp.status_code} {resp.text[:200]}"
                "（上传会话可能已过期）",
                retryable=False,
            )

        await self._run_with_retry("本地 MinerU 文件上传", op)

    async def finalize_submission(self, batch_id: str) -> str:
        """complete 每个 upload → 建解析任务 → 返回 job_id（落库/轮询用）。"""
        batch = self._local_batches.pop(batch_id, None)
        if batch is None:
            raise MineruError(
                "本地批次不存在（本地服务重启或进程内状态已丢），请重新上传",
                retryable=False,
            )
        for entry in batch.entries:
            self._local_by_url.pop(entry.url, None)

        file_ids: list[str] = []
        for entry in batch.entries:
            context = "本地 MinerU 完成上传"
            data = await self._json_plain(
                "POST",
                f"{self._config.base_url}/v1/uploads/{entry.upload_id}/complete",
                context=context,
            )
            file_obj = data.get("file")
            file_id = file_obj.get("id") if isinstance(file_obj, dict) else None
            if data.get("status") != "completed" or not isinstance(file_id, str):
                raise MineruError(
                    f"{context}: 期望 status=completed 且带 file.id，"
                    f"实际 status={data.get('status')!r}",
                    retryable=False,
                )
            file_ids.append(file_id)

        job_files: list[dict] = []
        for file_id, entry in zip(file_ids, batch.entries, strict=True):
            item: dict = {"source": {"type": "file_id", "file_id": file_id}}
            if entry.page_range:
                item["page_range"] = entry.page_range
            job_files.append(item)

        body: dict = {
            "files": job_files,
            "output_formats": ["zip"],
            "ocr_mode": batch.ocr_mode,
        }
        if self._config.local_tier:
            body["tier"] = self._config.local_tier

        context = "本地 MinerU 建解析任务"
        data = await self._json_plain(
            "POST",
            f"{self._config.base_url}/v1/parse/jobs",
            context=context,
            json_body=body,
        )
        job_id = data.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise MineruError(f"{context}: 响应缺少 job_id", retryable=False)
        return job_id

    def _entry_for_url(self, url: str) -> _LocalUpload:
        token = self._local_by_url.get(url)
        if token is not None:
            batch = self._local_batches.get(token)
            if batch is not None:
                for entry in batch.entries:
                    if entry.url == url:
                        return entry
        raise MineruError(f"上传链接不属于任何本地批次: {url}", retryable=False)

    # -- 轮询与下载 ----------------------------------------------------

    async def get_batch_results(self, job_id: str) -> list[MineruBatchItem]:
        context = "本地 MinerU 查询解析任务"
        data = await self._json_plain(
            "GET",
            f"{self._config.base_url}/v1/parse/jobs/{job_id}",
            context=context,
        )
        status = str(data.get("status") or "")
        if status not in _JOB_ALL:
            raise MineruError(
                f"{context}: 未知任务状态 {status!r}（协议不匹配？）", retryable=False
            )
        raw_files = data.get("files")
        if not isinstance(raw_files, list):
            raise MineruError(f"{context}: 响应缺少 files", retryable=False)
        return [self._to_item(raw, job_status=status) for raw in raw_files]

    def _to_item(self, raw: object, *, job_status: str) -> MineruBatchItem:
        if not isinstance(raw, dict):
            raise MineruError("本地任务项不是对象", retryable=False)
        name = str(raw.get("name") or "")
        file_status = str(raw.get("status") or "")
        total_pages = count_pages_in_range(raw.get("page_range"))

        if file_status == "completed":
            zip_id = None
            out = raw.get("output_files")
            zip_ref = out.get("zip") if isinstance(out, dict) else None
            if isinstance(zip_ref, dict):
                zip_id = zip_ref.get("file_id")
            if not isinstance(zip_id, str) or not zip_id:
                # 已完成却没有 zip 产物 = 协议违例；绝不让下游拿到 None URL 崩
                return MineruBatchItem(
                    file_name=name,
                    state="failed",
                    err_msg="解析已完成但产物清单缺少 zip（响应结构异常）",
                    total_pages=total_pages,
                )
            return MineruBatchItem(
                file_name=name,
                state="done",
                full_zip_url=f"{self._config.base_url}/v1/files/{zip_id}/content",
                total_pages=total_pages,
            )

        if file_status == "failed":
            return MineruBatchItem(
                file_name=name,
                state="failed",
                err_msg=self._error_text(raw),
                total_pages=total_pages,
            )

        # queued/running：任务在跑 = 部件在跑；任务已进终态却还有部件在跑 =
        # 永远等不到——映射 failed，绝不把编排层挂死。
        if job_status in _JOB_DEAD or job_status in _JOB_DONE:
            reason = "任务已取消" if job_status == "canceled" else "任务已失败"
            return MineruBatchItem(
                file_name=name,
                state="failed",
                err_msg=f"{reason}（部件仍为 {file_status or 'queued'}）",
                total_pages=total_pages,
            )
        return MineruBatchItem(file_name=name, state="running", total_pages=total_pages)

    @staticmethod
    def _error_text(raw: dict) -> str:
        err = raw.get("error")
        if not isinstance(err, dict):
            return "解析失败（本地服务未返回错误详情）"
        message = str(err.get("message") or "").strip() or "解析失败"
        code = err.get("code")
        return f"[{code}] {message}" if code else message

    async def download_zip(
        self, url: str, dest: Path, *, max_bytes: int = DEFAULT_ZIP_MAX_BYTES
    ) -> Path:
        """流式下载产物；302 手动跟随（≤5 跳），凭证绝不跨源重发。"""

        async def op(_attempt: int) -> Path:
            current = url
            for _hop in range(_MAX_REDIRECTS + 1):
                headers = (
                    self._auth_headers()
                    if _same_origin(current, self._config.base_url)
                    else {}
                )
                async with self._client.stream("GET", current, headers=headers) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise MineruError(
                                f"本地 MinerU 下载产物: HTTP {resp.status_code} 无 Location",
                                retryable=False,
                            )
                        current = urljoin(current, location)
                        continue
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise MineruError(
                            f"本地 MinerU 下载产物: HTTP {resp.status_code}",
                            retryable=True,
                        )
                    if resp.status_code in (401, 403):
                        raise MineruAuthError(
                            f"本地 MinerU 下载产物: HTTP {resp.status_code}"
                            "（检查 MINERU_LOCAL_API_KEY）",
                            retryable=False,
                        )
                    if resp.status_code >= 400:
                        raise MineruError(
                            f"本地 MinerU 下载产物: HTTP {resp.status_code}"
                            "（产物可能已被清理）",
                            retryable=False,
                        )
                    cl = resp.headers.get("content-length")
                    if cl and cl.isdigit() and int(cl) > max_bytes:
                        raise MineruError(
                            f"产物 zip 声明大小 {cl} 超上限 {max_bytes}",
                            retryable=False,
                        )
                    total = 0
                    try:
                        with open(dest, "wb") as fh:
                            async for chunk in resp.aiter_bytes(_CHUNK):
                                total += len(chunk)
                                if total > max_bytes:
                                    raise MineruError(
                                        f"产物 zip 超过上限 {max_bytes}",
                                        retryable=False,
                                    )
                                fh.write(chunk)
                    except BaseException:
                        dest.unlink(missing_ok=True)
                        raise
                    return dest
            raise MineruError(
                f"本地 MinerU 下载产物: 重定向超过 {_MAX_REDIRECTS} 跳",
                retryable=False,
            )

        return await self._run_with_retry("本地 MinerU 下载产物", op)
