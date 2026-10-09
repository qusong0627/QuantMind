"""本地/局域网 MinerU（V1 HTTP API）客户端 —— 内网解析通道（T-FM-21）。

契约锚点：MinerU 4.0.11 自托管（``mineru-kit api-server``）的 V1 HTTP API，
2026-10-09 从官方源码（``mineru/parser/api_server.py`` +
``tests/unittest/test_parser_api_contract.py``）与官方示例脚本
（``scripts/http_api_example.sh``）逐字段核对：

    POST /v1/uploads              {filename,bytes,mime_type,purpose:"parse"}
                                  → {id,status:"pending",upload_url,upload_headers}
    PUT  upload_url               （同源才带 Bearer；upload_headers 原样保留）
    POST /v1/uploads/{id}/complete → {status:"completed",file:{id}}
    POST /v1/parse/jobs           {files:[{source:{type:"file_id",file_id}}],
                                   tier?,ocr_mode,output_formats:["zip"]}
    GET  /v1/parse/jobs/{job_id}  → 快照（files[] 逐个 status/error/output_files）
    GET  /v1/files/{fid}/content  → 产物字节（可 302；凭证绝不跨源重发）

盯的四类问题：
1. **协议代际错配**：云端 v4（``MINERU_BASE_URL``）与本地 V1 是两套协议，
   直接指过去必然错；本地模式的每一步都按 V1 的字段名/状态枚举走。
2. **同源纪律**：Bearer 只发给与 API 基址同源（scheme+host+端口）的地址；
   302 跨源不重发凭证——局域网部署最容易把这条做丢。
3. **状态枚举不做猜测**：job 终态（completed/partial/failed/canceled）与
   per-file 状态严格映射；未知 job 状态=响亮报错，绝不当作「在跑」挂死。
4. **本地模式不走云端 SSRF 闸**：``assert_safe_remote_url`` 拒绝 http/私网，
   而那正是本地模式的工作地址；本客户端以「运维显式配置」换掉那两道闸，
   且**绝不**触碰云端 env（MINERU_API_TOKEN）——防止把云 token 漏给内网服务。

httpx 全部走 MockTransport（脚本化 V1 服务），不碰网络。
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent.mineru_client import (  # noqa: E402
    MODE_LOCAL,
    MineruAuthError,
    MineruConfig,
    MineruError,
    MineruFileSpec,
)
from backend.services.engine.alpha_agent.mineru_local import (  # noqa: E402
    MineruLocalClient,
)

BASE = "http://192.168.31.9:8000"  # 局域网 http —— SSRF 闸门在本地模式不适用
API_KEY = "lan-key-123"


# ── 脚本化 V1 服务 ──────────────────────────────────────────────────


class LocalServer:
    """内存版 mineru-kit api-server：只实现我们用到的那几条路由。

    刻意保留 V1 的真实形状：相对 upload_url、upload_headers、per-file
    status/error/output_files、job 快照——客户端映射逻辑直接对着它测。
    """

    def __init__(self, *, base: str = BASE, api_key: str = API_KEY) -> None:
        self.base = base
        self.api_key = api_key
        self.uploads: dict[str, dict] = {}  # id → {filename, bytes, mime, body}
        self.files: dict[str, str] = {}  # file_id → upload_id
        self.jobs: dict[str, dict] = {}  # job_id → 快照（测试可改）
        self.downloads: dict[
            str, object
        ] = {}  # file_id → bytes | ("redirect", url) | 状态码
        self.calls: list[tuple[str, str]] = []  # (method, path)
        self.auth_seen: list[str | None] = []  # 每次请求的 Authorization
        self.headers_seen: dict[tuple[str, str], dict] = {}
        self.jobs_created: list[dict] = []
        self.fail_once: dict[str, int] = {}  # "POST /v1/uploads" → 再失败几次(500)
        self.create_status = "pending"  # 可强制为 completed 制造协议违例
        self._n = 0

    # -- helpers -----------------------------------------------------

    def _mint(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}_{self._n}"

    def _json(self, payload: dict, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=payload)

    def _auth_ok(self, req: httpx.Request) -> bool:
        if not self.api_key:  # 匿名本地服务（未配 --api-key）
            return True
        return req.headers.get("authorization") == f"Bearer {self.api_key}"

    def set_job(self, job_id: str, **fields) -> dict:
        snap = self.jobs[job_id]
        snap.update(fields)
        return snap

    def job_files(self, job_id: str) -> list[dict]:
        return self.jobs[job_id]["files"]

    # -- routing -----------------------------------------------------

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path))
        self.auth_seen.append(req.headers.get("authorization"))
        self.headers_seen[(req.method, req.url.path)] = dict(req.headers)
        path = req.url.path

        boom = self.fail_once.get(f"{req.method} {path}", 0)
        if boom > 0:
            self.fail_once[f"{req.method} {path}"] = boom - 1
            return httpx.Response(500, json={"detail": "boom"})

        if req.method == "POST" and path == "/v1/uploads":
            if not self._auth_ok(req):
                return httpx.Response(401, json={"detail": "invalid api key"})
            body = json.loads(req.content)
            uid = self._mint("upload")
            self.uploads[uid] = {
                "filename": body["filename"],
                "bytes": body["bytes"],
                "mime": body["mime_type"],
                "body": None,
            }
            return self._json(
                {
                    "id": uid,
                    "object": "upload",
                    "bytes": body["bytes"],
                    "created_at": 0,
                    "expires_at": 3600,
                    "filename": body["filename"],
                    "purpose": "parse",
                    "mime_type": body["mime_type"],
                    "status": self.create_status,
                    "upload_url": (
                        None
                        if self.create_status == "completed"
                        else f"/v1/uploads/{uid}/content"
                    ),
                    "upload_method": None
                    if self.create_status == "completed"
                    else "PUT",
                    "upload_headers": {"X-Test-Gate": "1"},
                    "file": (
                        {
                            "id": f"file_{uid}",
                            "object": "file",
                            "bytes": body["bytes"],
                            "filename": body["filename"],
                            "purpose": "parse",
                        }
                        if self.create_status == "completed"
                        else None
                    ),
                }
            )

        if req.method == "PUT" and path.startswith("/v1/uploads/"):
            uid = path.split("/")[3]
            if uid not in self.uploads:
                return httpx.Response(404, json={"detail": "upload_not_found"})
            self.uploads[uid]["body"] = req.content
            return self._json({"ok": True})

        if req.method == "POST" and path.endswith("/complete"):
            if not self._auth_ok(req):
                return httpx.Response(401, json={"detail": "invalid api key"})
            uid = path.split("/")[3]
            rec = self.uploads.get(uid)
            if rec is None:
                return httpx.Response(404, json={"detail": "upload_not_found"})
            fid = f"file_{uid}"
            self.files[fid] = uid
            return self._json(
                {
                    "id": uid,
                    "object": "upload",
                    "bytes": rec["bytes"],
                    "created_at": 0,
                    "expires_at": 3600,
                    "filename": rec["filename"],
                    "purpose": "parse",
                    "mime_type": rec["mime"],
                    "status": "completed",
                    "file": {
                        "id": fid,
                        "object": "file",
                        "bytes": rec["bytes"],
                        "filename": rec["filename"],
                        "purpose": "parse",
                    },
                }
            )

        if req.method == "POST" and path == "/v1/parse/jobs":
            if not self._auth_ok(req):
                return httpx.Response(401, json={"detail": "invalid api key"})
            body = json.loads(req.content)
            self.jobs_created.append(body)
            jid = self._mint("job")
            files = []
            for entry in body["files"]:
                fid = entry["source"]["file_id"]
                uid = self.files[fid]
                files.append(
                    {
                        "file_id": fid,
                        "name": self.uploads[uid]["filename"],
                        "page_range": entry.get("page_range", ""),
                        "status": "queued",
                    }
                )
            snap = {
                "job_id": jid,
                "status": "queued",
                "created_at": "2026-10-09T00:00:00Z",
                "tier": body.get("tier") or "standard",
                "output_formats": body.get("output_formats", ["markdown"]),
                "access_level": "anonymous",
                "progress": {"completed": 0, "failed": 0, "total": len(files)},
                "files": files,
                "links": {
                    "self": f"/v1/parse/jobs/{jid}",
                    "cancel": f"/v1/parse/jobs/{jid}",
                },
            }
            self.jobs[jid] = snap
            return self._json(snap)

        if req.method == "GET" and path.startswith("/v1/parse/jobs/"):
            if not self._auth_ok(req):
                return httpx.Response(401, json={"detail": "invalid api key"})
            jid = path.split("/")[4]
            if jid not in self.jobs:
                return httpx.Response(
                    404,
                    json={
                        "detail": {
                            "code": "job_not_found",
                            "message": f"Job {jid} not found",
                        }
                    },
                )
            return self._json(self.jobs[jid])

        if req.method == "GET" and path.startswith("/v1/files/"):
            fid = path.split("/")[3]
            if fid not in self.downloads:
                return httpx.Response(404, json={"detail": "file_not_found"})
            item = self.downloads[fid]
            if isinstance(item, tuple) and item[0] == "redirect":
                return httpx.Response(302, headers={"location": item[1]})
            if isinstance(item, int):
                return httpx.Response(item, json={"detail": "err"})
            return httpx.Response(200, content=item)

        return httpx.Response(404, json={"detail": "no route"})


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def make_client(server: LocalServer, clock: FakeClock, **kwargs) -> MineruLocalClient:
    cfg = MineruConfig(
        token=server.api_key,
        base_url=server.base,
        mode=MODE_LOCAL,
        local_tier=kwargs.pop("local_tier", None),
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    return MineruLocalClient(
        cfg, client=http, sleep=clock.sleep, clock=clock.now, **kwargs
    )


def spec(name: str, *, size: int | None = 1024, **kw) -> MineruFileSpec:
    return MineruFileSpec(name=name, size_bytes=size, **kw)


async def submit_files(
    client: MineruLocalClient,
    server: LocalServer,
    files: list[tuple[MineruFileSpec, bytes]],
) -> tuple[str, str, list[str]]:
    """完整三段：建上传 → PUT 字节 → finalize（complete + 建 job）。"""
    specs = [s for s, _ in files]
    token, urls = await client.create_upload_batch(specs)
    for (_, payload), url in zip(files, urls, strict=True):
        await client.upload_file(url, payload)
    job_id = await client.finalize_submission(token)
    return token, job_id, urls


# ── 上传三段 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_cycle_create_put_complete_job() -> None:
    """一个文件走完 V1 全周期：路径、载荷、顺序、鉴权全部钉死。"""
    server = LocalServer()
    clock = FakeClock()
    client = make_client(server, clock)
    payload = b"%PDF-1.4 data"

    token, job_id, urls = await submit_files(
        client, server, [(spec("研报.pdf", size=len(payload)), payload)]
    )

    assert token.startswith("local-")
    assert job_id == "job_2"
    assert urls == [f"{BASE}/v1/uploads/upload_1/content"]
    assert server.calls == [
        ("POST", "/v1/uploads"),
        ("PUT", "/v1/uploads/upload_1/content"),
        ("POST", "/v1/uploads/upload_1/complete"),
        ("POST", "/v1/parse/jobs"),
    ]
    # create 载荷：V1 必填三件套（filename/bytes/mime_type），不抄云端字段
    body_put = server.uploads["upload_1"]
    assert body_put["filename"] == "研报.pdf"
    assert body_put["bytes"] == len(payload)
    assert body_put["mime"] == "application/pdf"
    assert body_put["body"] == payload
    # PUT 带服务端 upload_headers + 同源 Bearer
    put_headers = server.headers_seen[("PUT", "/v1/uploads/upload_1/content")]
    assert put_headers.get("x-test-gate") == "1"
    assert put_headers.get("authorization") == f"Bearer {API_KEY}"
    # 建 job：file_id 引用 + 只要 zip（zip 内含 markdown.md + images）
    assert server.jobs_created[0] == {
        "files": [{"source": {"type": "file_id", "file_id": "file_upload_1"}}],
        "output_formats": ["zip"],
        "ocr_mode": "auto",
    }


@pytest.mark.asyncio
async def test_multi_file_order_and_ocr_variant() -> None:
    """多文件：file_id 按上传顺序引用；任一 is_ocr → job 级 ocr_mode=ocr。"""
    server = LocalServer()
    clock = FakeClock()
    client = make_client(server, clock)

    _, job_id, _ = await submit_files(
        client,
        server,
        [
            (spec("a.pdf", is_ocr=True), b"A"),
            (spec("b.png"), b"B"),
        ],
    )

    assert job_id == "job_3"
    assert server.jobs_created[0]["files"] == [
        {"source": {"type": "file_id", "file_id": "file_upload_1"}},
        {"source": {"type": "file_id", "file_id": "file_upload_2"}},
    ]
    assert server.jobs_created[0]["ocr_mode"] == "ocr"


@pytest.mark.asyncio
async def test_page_ranges_and_tier_and_mime_fallback() -> None:
    server = LocalServer()
    clock = FakeClock()
    client = make_client(server, clock, local_tier="flash")

    await submit_files(client, server, [(spec("x.weird", page_ranges="1-5"), b"X")])

    assert server.uploads["upload_1"]["mime"] == "application/octet-stream"
    entry = server.jobs_created[0]["files"][0]
    assert entry["page_range"] == "1-5"
    assert server.jobs_created[0]["tier"] == "flash"


@pytest.mark.asyncio
async def test_missing_size_bytes_fails_before_any_request() -> None:
    """size_bytes 是 V1 create 的必填字段；缺失必须本地响亮报错，不打半截请求。"""
    server = LocalServer()
    client = make_client(server, FakeClock())

    with pytest.raises(MineruError) as ei:
        await client.create_upload_batch([spec("a.pdf", size=None)])

    assert ei.value.retryable is False
    assert "size_bytes" in str(ei.value)
    assert server.calls == []


@pytest.mark.asyncio
async def test_create_completed_state_is_protocol_violation() -> None:
    """我们不传 sha256sum，服务端不可能秒回 completed——出现即协议违例，响亮失败。"""
    server = LocalServer()
    server.create_status = "completed"
    client = make_client(server, FakeClock())

    with pytest.raises(MineruError) as ei:
        await client.create_upload_batch([spec("a.pdf")])

    assert "completed" in str(ei.value) or "pending" in str(ei.value)


@pytest.mark.asyncio
async def test_finalize_unknown_token_fails_loud() -> None:
    client = make_client(LocalServer(), FakeClock())
    with pytest.raises(MineruError) as ei:
        await client.finalize_submission("local-deadbeef")
    assert ei.value.retryable is False


# ── 鉴权与重试 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bad_api_key_is_auth_error_without_retry() -> None:
    server = LocalServer(api_key="right-key")
    clock = FakeClock()
    cfg = MineruConfig(token="wrong-key", base_url=server.base, mode=MODE_LOCAL)
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    client = MineruLocalClient(cfg, client=http, sleep=clock.sleep, clock=clock.now)

    with pytest.raises(MineruAuthError) as ei:
        await client.create_upload_batch([spec("a.pdf")])

    assert ei.value.retryable is False
    assert clock.slept == [], "鉴权失败不许烧重试"
    assert len([c for c in server.calls if c[0] == "POST"]) == 1


@pytest.mark.asyncio
async def test_anonymous_server_omits_bearer() -> None:
    """服务端没配 --api-key：客户端 key 为空时不许带 Authorization 头。"""
    server = LocalServer(api_key="")
    clock = FakeClock()
    cfg = MineruConfig(token="", base_url=BASE, mode=MODE_LOCAL)
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    client = MineruLocalClient(cfg, client=http, sleep=clock.sleep, clock=clock.now)

    await client.create_upload_batch([spec("a.pdf")])

    assert server.auth_seen == [None]


@pytest.mark.asyncio
async def test_transient_500_retries_with_backoff() -> None:
    server = LocalServer()
    server.fail_once["POST /v1/uploads"] = 1
    clock = FakeClock()
    client = make_client(server, clock)

    token, urls = await client.create_upload_batch([spec("a.pdf")])

    assert token.startswith("local-") and len(urls) == 1
    assert clock.slept == [1.0], "500 属暂态：退避一次后重试成功"


# ── 轮询映射 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_results_running_then_completed_mapping(tmp_path: Path) -> None:
    server = LocalServer()
    clock = FakeClock()
    client = make_client(server, clock)
    _, job_id, _ = await submit_files(client, server, [(spec("a.pdf"), b"A")])

    items = await client.get_batch_results(job_id)
    assert [(i.file_name, i.state) for i in items] == [("a.pdf", "running")]
    assert items[0].data_id is None, "V1 没有 data_id——编排层按名字回钉"

    server.set_job(
        job_id,
        status="completed",
        files=[
            {
                "file_id": "file_upload_1",
                "name": "a.pdf",
                "page_range": "1-7",
                "status": "completed",
                "output_files": {"zip": {"file_id": "file_out_zip", "bytes": 99}},
            }
        ],
    )
    items = await client.get_batch_results(job_id)
    item = items[0]
    assert item.state == "done"
    assert item.full_zip_url == f"{BASE}/v1/files/file_out_zip/content"
    assert item.total_pages == 7


@pytest.mark.asyncio
async def test_results_failed_file_carries_error_text() -> None:
    server = LocalServer()
    client = make_client(server, FakeClock())
    _, job_id, _ = await submit_files(client, server, [(spec("a.pdf"), b"A")])

    server.set_job(
        job_id,
        status="partial",
        files=[
            {
                "file_id": "file_upload_1",
                "name": "a.pdf",
                "page_range": "",
                "status": "failed",
                "error": {"code": "unsupported_format", "message": "cannot parse"},
            }
        ],
    )
    item = (await client.get_batch_results(job_id))[0]
    assert item.state == "failed"
    assert "cannot parse" in item.err_msg
    assert "unsupported_format" in item.err_msg


@pytest.mark.asyncio
async def test_results_terminal_job_never_leaves_files_running() -> None:
    """终态 job 下 queued 的部件必须映射为 failed——否则编排层永远轮询挂死。"""
    server = LocalServer()
    client = make_client(server, FakeClock())
    _, job_id, _ = await submit_files(client, server, [(spec("a.pdf"), b"A")])

    server.set_job(job_id, status="canceled")
    item = (await client.get_batch_results(job_id))[0]
    assert item.state == "failed"
    assert "取消" in item.err_msg

    server.set_job(job_id, status="failed")
    item = (await client.get_batch_results(job_id))[0]
    assert item.state == "failed"


@pytest.mark.asyncio
async def test_results_unknown_job_status_fails_loud() -> None:
    server = LocalServer()
    client = make_client(server, FakeClock())
    _, job_id, _ = await submit_files(client, server, [(spec("a.pdf"), b"A")])

    server.set_job(job_id, status="weird_state")
    with pytest.raises(MineruError) as ei:
        await client.get_batch_results(job_id)
    assert ei.value.retryable is False, "未知状态不许当「在跑」静默挂死"


@pytest.mark.asyncio
async def test_results_job_404_fails_loud() -> None:
    """服务重启丢 job 索引（V1 无持久化）：404 必须定格失败，不许轮询到超时。"""
    client = make_client(LocalServer(), FakeClock())
    with pytest.raises(MineruError) as ei:
        await client.get_batch_results("job_gone")
    assert ei.value.retryable is False
    assert "404" in str(ei.value)


def test_page_range_counting() -> None:
    from backend.services.engine.alpha_agent.mineru_local import count_pages_in_range

    assert count_pages_in_range("1-7") == 7
    assert count_pages_in_range("1-5,8") == 6
    assert count_pages_in_range("3") == 1
    assert count_pages_in_range("") is None
    assert count_pages_in_range("all") is None
    assert count_pages_in_range("r3-r1") is None, "不认识的形态一律 None，不猜"


# ── 产物下载 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_zip_keeps_auth_same_origin(tmp_path: Path) -> None:
    server = LocalServer()
    client = make_client(server, FakeClock())
    server.downloads["file_out_zip"] = b"PK\x03\x04zipbytes"

    dest = tmp_path / "out.zip"
    out = await client.download_zip(f"{BASE}/v1/files/file_out_zip/content", dest)

    assert out == dest and dest.read_bytes() == b"PK\x03\x04zipbytes"
    assert server.auth_seen[-1] == f"Bearer {API_KEY}"


@pytest.mark.asyncio
async def test_download_zip_302_cross_origin_drops_auth(tmp_path: Path) -> None:
    """302 到别的源：绝不重发 Bearer（官方 --location 都不带 --location-trusted）。"""
    server = LocalServer()
    client = make_client(server, FakeClock())
    cdn = "http://cdn.lan:9100/blobs/z.zip"
    server.downloads["file_out_zip"] = ("redirect", cdn)

    seen_auth: list[str | None] = []

    def cdn_handler(req: httpx.Request) -> httpx.Response:
        seen_auth.append(req.headers.get("authorization"))
        return httpx.Response(200, content=b"PK-remote")

    real_handler = server.handler

    def routing(req: httpx.Request) -> httpx.Response:
        if req.url.host == "cdn.lan":
            return cdn_handler(req)
        return real_handler(req)

    cfg = MineruConfig(token=API_KEY, base_url=BASE, mode=MODE_LOCAL)
    http = httpx.AsyncClient(transport=httpx.MockTransport(routing))
    client = MineruLocalClient(
        cfg, client=http, sleep=FakeClock().sleep, clock=FakeClock().now
    )

    dest = tmp_path / "out.zip"
    await client.download_zip(f"{BASE}/v1/files/file_out_zip/content", dest)

    assert dest.read_bytes() == b"PK-remote"
    assert seen_auth == [None], "跨源下载不许携带 Bearer"
    assert server.auth_seen[-1] == f"Bearer {API_KEY}", "首跳同源仍然是带 key 的"


@pytest.mark.asyncio
async def test_download_zip_size_cap_removes_partial(tmp_path: Path) -> None:
    server = LocalServer()
    client = make_client(server, FakeClock())
    server.downloads["file_out_zip"] = b"x" * 4096

    dest = tmp_path / "out.zip"
    with pytest.raises(MineruError):
        await client.download_zip(
            f"{BASE}/v1/files/file_out_zip/content", dest, max_bytes=1024
        )
    assert not dest.exists(), "超限残包必须删除——下游绝不能拿到半截 zip"


@pytest.mark.asyncio
async def test_download_zip_resolves_relative_redirect(tmp_path: Path) -> None:
    server = LocalServer()
    client = make_client(server, FakeClock())
    server.downloads["file_out_zip"] = ("redirect", "/v1/files/file_real/content")
    server.downloads["file_real"] = b"PK-real"

    dest = tmp_path / "out.zip"
    await client.download_zip(f"{BASE}/v1/files/file_out_zip/content", dest)

    assert dest.read_bytes() == b"PK-real"


@pytest.mark.asyncio
async def test_download_zip_follows_zip_layout_with_alias(tmp_path: Path) -> None:
    """本地 zip 的 md 叫 markdown.md —— 端到端顺一遍解包别名。"""
    import io

    from backend.services.engine.alpha_agent.mineru_client import extract_zip_whitelist

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("markdown.md", "# local paper")
        zf.writestr("images/fig1.png", b"png")
    server = LocalServer()
    client = make_client(server, FakeClock())
    server.downloads["file_out_zip"] = buf.getvalue()

    zip_path = tmp_path / "out.zip"
    await client.download_zip(f"{BASE}/v1/files/file_out_zip/content", zip_path)
    result = extract_zip_whitelist(zip_path, tmp_path / "unpacked")

    assert result.md_path.name == "full.md"
    assert result.md_path.read_text() == "# local paper"
    assert result.image_count == 1
