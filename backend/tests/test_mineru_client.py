"""MinerU 在线 API v4 客户端（T-FM-06）—— 文档解析链的唯一出网口。

盯的四类问题，每一类都对应一条真实故障路径：

1. **三层信封**：HTTP 200 不等于成功。`code≠0`、`success:false/msgCode`、
   批次项 `state=failed` 是三个独立层，任何一层被漏掉，「解析失败」都会被
   编排层当成「还在跑」，任务挂死而没有任何告警。
2. **错误分类**：暂态码（-60007 等）必须退避重试；永久码必须立即失败并带
   可读文案。把永久码当暂态 → 配额空转；把暂态当永久 → 用户重传十次。
   配额码（-60018/-60019）与 token 码（A0202/A0211）单独成类：它们要求的是
   「告诉用户去做什么」，不是「再试一次」。
3. **zip 是不可信输入**：`../`、绝对路径、符号链接条目一律拒绝（Zip-Slip）；
   条目数与解压总量有上限，声明值超限先拒、流式解压再兜底（Zip-Bomb）。
   白名单只提取 `full.md` / `*_content_list.json` / `images/**`，其余静默跳过。
4. **限速与预检**：提交 ≤300/min、查询 ≤1000/min；PDF 页数在提交前预检
   （平台 200 页/文件上限），pypdf 读不动时返回 None 交平台兜底，不猜。

httpx 全部走 MockTransport，不碰网络；解压样本在内存里现造。
"""

from __future__ import annotations

import asyncio
import io
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent.mineru_client import (  # noqa: E402
    AUTH_CODES,
    PERMANENT_CODES,
    QUOTA_CODES,
    TERMINAL_STATES,
    TRANSIENT_CODES,
    MineruAuthError,
    MineruBatchItem,
    MineruClient,
    MineruConfig,
    MineruError,
    MineruFileSpec,
    MineruQuotaError,
    MineruZipError,
    MinIntervalLimiter,
    assert_safe_remote_url,
    classify_error_code,
    count_pdf_pages,
    extract_err_code,
    extract_zip_whitelist,
    resolve_mineru_config,
    run_pdf_job,
)

# ── 工具 ────────────────────────────────────────────────────────────


def make_zip(entries: dict[str, bytes], *, symlinks: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        for name in symlinks:
            zi = zipfile.ZipInfo(name)
            zi.external_attr = 0o120777 << 16  # S_IFLNK
            zf.writestr(zi, "target")
    return buf.getvalue()


def write_zip(tmp_path: Path, data: bytes) -> Path:
    p = tmp_path / "mineru.zip"
    p.write_bytes(data)
    return p


class FakeClock:
    """手动推进的单调钟；sleep 同时推进钟面。"""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def fake_clock() -> FakeClock:
    return FakeClock()


def make_client(handler, clock: FakeClock, **kwargs) -> MineruClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    cfg = MineruConfig(token="tok-123", base_url="https://mineru.test")
    return MineruClient(
        cfg,
        client=http,
        sleep=clock.sleep,
        clock=clock.now,
        **kwargs,
    )


def json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def batch_ok(batch_id: str = "b-1", urls: int = 1) -> dict:
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "batch_id": batch_id,
            "file_urls": [f"https://upload.test/f{i}" for i in range(urls)],
        },
    }


# ── 错误码分类（纯函数） ─────────────────────────────────────────────


def test_classify_documented_codes() -> None:
    for code in TRANSIENT_CODES:
        retryable, exc = classify_error_code(code)
        assert retryable is True, f"{code} 是暂态码"
        assert exc is MineruError

    for code in PERMANENT_CODES:
        retryable, exc = classify_error_code(code)
        assert retryable is False, f"{code} 是永久码"
        assert exc is MineruError

    for code in QUOTA_CODES:
        retryable, exc = classify_error_code(code)
        assert retryable is False, f"{code} 是配额码，重试无意义"
        assert exc is MineruQuotaError

    for code in AUTH_CODES:
        retryable, exc = classify_error_code(code)
        assert retryable is False, f"{code} 是 token 码，重试无意义"
        assert exc is MineruAuthError

    # 未知码按永久处理：宁可让用户看到原因，也不要拿未知码烧重试和配额
    retryable, exc = classify_error_code(-99999)
    assert retryable is False
    assert exc is MineruError

    # 字符串数字与浮点码都要认（信封里 code 类型不稳定）
    assert classify_error_code("-60007")[0] is True
    assert classify_error_code(-60018.0)[1] is MineruQuotaError


def test_extract_err_code_from_message_text() -> None:
    """批次项只有 err_msg 文本时也要能捞出错误码。"""
    assert extract_err_code("file over page limit (-60006)") == -60006
    assert extract_err_code("token 无效 (A0202)") == "A0202"
    assert extract_err_code("no code here") is None
    assert extract_err_code(None) is None


def test_terminal_states_are_done_and_failed() -> None:
    assert TERMINAL_STATES == frozenset({"done", "failed"})


# ── 三层信封 + 重试 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_batch_success_contract() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["method"] = req.method
        seen["auth"] = req.headers.get("authorization")
        import json

        seen["body"] = json.loads(req.content)
        return json_response(batch_ok(urls=2))

    clock = fake_clock()
    client = make_client(handler, clock)

    batch_id, urls = await client.create_upload_batch(
        [
            MineruFileSpec(name="paper.pdf", data_id="doc-1", is_ocr=False),
            MineruFileSpec(name="scan.pdf", data_id="doc-2", is_ocr=True),
        ]
    )

    assert batch_id == "b-1"
    assert urls == ["https://upload.test/f0", "https://upload.test/f1"]
    assert seen["method"] == "POST"
    assert seen["url"] == "https://mineru.test/api/v4/file-urls/batch"
    assert seen["auth"] == "Bearer tok-123"
    body = seen["body"]
    assert body["model_version"] == "vlm"
    assert body["enable_formula"] is True
    assert body["enable_table"] is True
    assert body["language"] == "ch"
    assert body["files"][0]["name"] == "paper.pdf"
    assert body["files"][0]["data_id"] == "doc-1"
    assert body["files"][1]["is_ocr"] is True


@pytest.mark.asyncio
async def test_transient_code_retried_then_success() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return json_response({"code": -60007, "msg": "server busy"})
        return json_response(batch_ok())

    clock = fake_clock()
    client = make_client(handler, clock, backoff_base_s=1.0)

    batch_id, _ = await client.create_upload_batch([MineruFileSpec(name="a.pdf")])

    assert batch_id == "b-1"
    assert calls["n"] == 3
    assert clock.slept == [1.0, 2.0], "指数退避：1s、2s"


@pytest.mark.asyncio
async def test_transient_exhausts_max_attempts() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return json_response({"code": -60001, "msg": "internal"})

    clock = fake_clock()
    client = make_client(handler, clock, max_attempts=3, backoff_base_s=1.0)

    with pytest.raises(MineruError) as ei:
        await client.create_upload_batch([MineruFileSpec(name="a.pdf")])

    assert calls["n"] == 3, "重试上限后必须停下来，不许无限循环"
    assert ei.value.retryable is True
    assert ei.value.code == -60001


@pytest.mark.asyncio
async def test_success_false_envelope_permanent_no_retry() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return json_response(
            {"success": False, "msgCode": -60002, "msg": "invalid params"}
        )

    clock = fake_clock()
    client = make_client(handler, clock)

    with pytest.raises(MineruError) as ei:
        await client.create_upload_batch([MineruFileSpec(name="a.pdf")])

    assert calls["n"] == 1, "永久码不许重试"
    assert ei.value.retryable is False
    assert ei.value.code == -60002


@pytest.mark.asyncio
async def test_auth_code_maps_to_auth_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return json_response({"code": "A0202", "msg": "token expired"})

    clock = fake_clock()
    client = make_client(handler, clock)

    with pytest.raises(MineruAuthError):
        await client.create_upload_batch([MineruFileSpec(name="a.pdf")])


@pytest.mark.asyncio
async def test_quota_code_maps_to_quota_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return json_response({"code": -60018, "msg": "daily limit reached"})

    clock = fake_clock()
    client = make_client(handler, clock)

    with pytest.raises(MineruQuotaError):
        await client.create_upload_batch([MineruFileSpec(name="a.pdf")])


@pytest.mark.asyncio
async def test_http_5xx_retried_4xx_permanent() -> None:
    calls = {"n": 0}

    def handler_500(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502, text="bad gateway")
        return json_response(batch_ok())

    clock = fake_clock()
    client = make_client(handler_500, clock, backoff_base_s=0.5)
    batch_id, _ = await client.create_upload_batch([MineruFileSpec(name="a.pdf")])
    assert batch_id == "b-1"
    assert calls["n"] == 2

    calls2 = {"n": 0}

    def handler_400(req: httpx.Request) -> httpx.Response:
        calls2["n"] += 1
        return httpx.Response(400, text="bad request")

    client2 = make_client(handler_400, clock)
    with pytest.raises(MineruError) as ei:
        await client2.create_upload_batch([MineruFileSpec(name="a.pdf")])
    assert calls2["n"] == 1
    assert ei.value.retryable is False


@pytest.mark.asyncio
async def test_http_429_retried() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="too many requests")
        return json_response(batch_ok())

    clock = fake_clock()
    client = make_client(handler, clock, backoff_base_s=0.5)
    batch_id, _ = await client.create_upload_batch([MineruFileSpec(name="a.pdf")])
    assert batch_id == "b-1"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_create_batch_rejects_more_than_50_files() -> None:
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("超限必须本地拒绝，不出网")

    clock = fake_clock()
    client = make_client(handler, clock)
    specs = [MineruFileSpec(name=f"{i}.pdf") for i in range(51)]
    with pytest.raises(ValueError):
        await client.create_upload_batch(specs)


# ── 上传（预签名 PUT） ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_file_no_auth_no_content_type() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
        seen["body"] = req.content
        return httpx.Response(200)

    clock = fake_clock()
    client = make_client(handler, clock)

    await client.upload_file("https://upload.test/f0", b"PDFDATA")

    assert seen["method"] == "PUT"
    assert seen["body"] == b"PDFDATA"
    assert "authorization" not in seen["headers"], "预签名 URL 不能带 Authorization"
    assert "content-type" not in seen["headers"], "预签名 PUT 不能带 Content-Type"


@pytest.mark.asyncio
async def test_upload_file_retries_transport_error() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=req)
        return httpx.Response(200)

    clock = fake_clock()
    client = make_client(handler, clock, backoff_base_s=0.5)
    await client.upload_file("https://upload.test/f0", b"x")
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_upload_file_from_disk_recreatable_on_retry(tmp_path: Path) -> None:
    """文件句柄只能读一次：重试时必须重新打开，不许发出空 body。"""
    src = tmp_path / "big.pdf"
    src.write_bytes(b"A" * (3 * 1024 * 1024))
    bodies: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(len(req.content))
        if len(bodies) == 1:
            return httpx.Response(500)
        return httpx.Response(200)

    clock = fake_clock()
    client = make_client(handler, clock, backoff_base_s=0.5)
    await client.upload_file("https://upload.test/f0", src)
    assert bodies == [3 * 1024 * 1024, 3 * 1024 * 1024]


# ── 轮询结果 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_batch_results_parses_items() -> None:
    payload = {
        "code": 0,
        "data": {
            "batch_id": "b-1",
            "extract_result": [
                {
                    "file_name": "paper.pdf",
                    "state": "done",
                    "full_zip_url": "https://cdn.test/a.zip",
                    "data_id": "doc-1",
                },
                {
                    "file_name": "scan.pdf",
                    "state": "running",
                    "extract_progress": {
                        "extracted_pages": 3,
                        "total_pages": 10,
                    },
                },
                {
                    "file_name": "bad.pdf",
                    "state": "failed",
                    "err_msg": "over page limit (-60006)",
                    "data_id": "doc-3",
                },
            ],
        },
    }
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        return json_response(payload)

    clock = fake_clock()
    client = make_client(handler, clock)
    items = await client.get_batch_results("b-1")

    assert seen["url"] == "https://mineru.test/api/v4/extract-results/batch/b-1"
    assert seen["auth"] == "Bearer tok-123"

    assert [type(i) for i in items] == [MineruBatchItem] * 3
    done, running, failed = items
    assert done.state == "done"
    assert done.full_zip_url == "https://cdn.test/a.zip"
    assert done.data_id == "doc-1"
    assert running.extracted_pages == 3 and running.total_pages == 10
    assert failed.err_msg == "over page limit (-60006)"
    assert failed.err_code == -60006
    assert failed.retryable is False


@pytest.mark.asyncio
async def test_get_batch_results_missing_extract_result_is_structural_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return json_response({"code": 0, "data": {"batch_id": "b-1"}})

    clock = fake_clock()
    client = make_client(handler, clock)
    with pytest.raises(MineruError) as ei:
        await client.get_batch_results("b-1")
    assert ei.value.retryable is False, "结构异常重试也变不出来"


# ── zip 下载 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_zip_streams_to_disk(tmp_path: Path) -> None:
    payload = make_zip({"full.md": b"# hi"})

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    clock = fake_clock()
    client = make_client(handler, clock)
    dest = tmp_path / "out.zip"
    await client.download_zip(
        "https://cdn.test/a.zip", dest, max_bytes=10 * 1024 * 1024
    )
    assert dest.read_bytes() == payload


@pytest.mark.asyncio
async def test_download_zip_over_cap_raises_and_cleans(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"Z" * 4096)

    clock = fake_clock()
    client = make_client(handler, clock)
    dest = tmp_path / "out.zip"
    with pytest.raises(MineruError):
        await client.download_zip("https://cdn.test/a.zip", dest, max_bytes=1024)
    assert not dest.exists(), "超限的残包必须删掉，不能让下游拿到半截 zip"


# ── zip 白名单解包（安全核心） ───────────────────────────────────────


def test_extract_whitelist_happy_path(tmp_path: Path) -> None:
    zpath = write_zip(
        tmp_path,
        make_zip(
            {
                "full.md": b"# paper",
                "paper_content_list.json": b"[]",
                "images/fig1.png": b"\x89PNG",
                "images/fig2.jpg": b"\xff\xd8",
                "layout.json": b"{}",  # 白名单外：静默跳过
                "some_dir/notes.txt": b"x",
            }
        ),
    )
    dest = tmp_path / "parsed"
    out = extract_zip_whitelist(zpath, dest)

    assert out.md_path == dest / "full.md"
    assert (dest / "full.md").read_bytes() == b"# paper"
    assert out.content_list_paths == [dest / "paper_content_list.json"]
    assert out.image_count == 2
    assert (dest / "images" / "fig1.png").read_bytes() == b"\x89PNG"
    assert not (dest / "layout.json").exists()
    assert not (dest / "some_dir" / "notes.txt").exists()


def test_extract_nested_root_dir_still_matches(tmp_path: Path) -> None:
    """有些包把产物裹在一层目录里；白名单按 basename 匹配，落盘拍平。"""
    zpath = write_zip(
        tmp_path,
        make_zip(
            {
                "out/full.md": b"# x",
                "out/out_content_list.json": b"[]",
                "out/images/a.png": b"p",
            }
        ),
    )
    dest = tmp_path / "parsed"
    out = extract_zip_whitelist(zpath, dest)
    assert (dest / "full.md").exists()
    assert out.image_count == 1


def test_extract_rejects_parent_traversal(tmp_path: Path) -> None:
    zpath = write_zip(
        tmp_path,
        make_zip({"full.md": b"# ok", "../evil.md": b"boom"}),
    )
    dest = tmp_path / "parsed"
    with pytest.raises(MineruZipError):
        extract_zip_whitelist(zpath, dest)
    assert not (tmp_path / "evil.md").exists(), "穿越路径绝不许落盘"


def test_extract_rejects_absolute_path(tmp_path: Path) -> None:
    zpath = write_zip(
        tmp_path,
        make_zip({"full.md": b"# ok", "/tmp/evil.md": b"boom"}),
    )
    with pytest.raises(MineruZipError):
        extract_zip_whitelist(zpath, tmp_path / "parsed")


def test_extract_rejects_symlink_entry(tmp_path: Path) -> None:
    zpath = write_zip(
        tmp_path,
        make_zip({"full.md": b"# ok"}, symlinks=("images/link.png",)),
    )
    with pytest.raises(MineruZipError):
        extract_zip_whitelist(zpath, tmp_path / "parsed")


def test_extract_rejects_too_many_entries(tmp_path: Path) -> None:
    entries = {"full.md": b"# ok"}
    for i in range(60):
        entries[f"images/f{i}.png"] = b"p"
    zpath = write_zip(tmp_path, make_zip(entries))
    with pytest.raises(MineruZipError):
        extract_zip_whitelist(zpath, tmp_path / "parsed", max_entries=50)


def test_extract_rejects_bomb_by_declared_size(tmp_path: Path) -> None:
    """声明解压总量超上限：解压前就拒，不做任何 IO 解压。"""
    zpath = write_zip(
        tmp_path,
        make_zip({"full.md": b"# ok", "images/big.png": b"\x00" * (2 * 1024 * 1024)}),
    )
    dest = tmp_path / "parsed"
    with pytest.raises(MineruZipError):
        extract_zip_whitelist(zpath, dest, max_total_bytes=1024 * 1024)
    assert not dest.exists() or not any(dest.rglob("*")), "拒掉后不留半成品"


def test_extract_cleanup_on_midway_failure(tmp_path: Path) -> None:
    """第二次解包被总字节闸拦下时，第一次解出的文件也要清掉（不留半成品）。"""
    zpath = write_zip(
        tmp_path,
        make_zip(
            {
                "full.md": b"# ok",
                "images/a.png": b"x" * 700_000,
                "images/b.png": b"y" * 700_000,
            }
        ),
    )
    dest = tmp_path / "parsed"
    with pytest.raises(MineruZipError):
        # 声明总量 ~1.4MB > 1MB 上限——声明检查就会拒；这里断言清理语义
        extract_zip_whitelist(zpath, dest, max_total_bytes=1024 * 1024)
    assert not dest.exists()


def test_extract_missing_full_md_raises(tmp_path: Path) -> None:
    zpath = write_zip(tmp_path, make_zip({"images/a.png": b"p"}))
    with pytest.raises(MineruZipError) as ei:
        extract_zip_whitelist(zpath, tmp_path / "parsed")
    assert "full.md" in str(ei.value)


def test_extract_non_zip_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"this is not a zip")
    with pytest.raises(MineruZipError):
        extract_zip_whitelist(bad, tmp_path / "parsed")


# ── 限速 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_limiter_enforces_min_interval() -> None:
    clock = fake_clock()
    limiter = MinIntervalLimiter(per_minute=300, clock=clock.now, sleep=clock.sleep)

    await limiter.acquire()
    assert clock.slept == [], "第一次调用不等待"

    await limiter.acquire()
    assert len(clock.slept) == 1
    assert abs(clock.slept[0] - 60 / 300) < 1e-6, "≤300/min → 最小间隔 0.2s"

    # 时间自然流逝超过间隔：不再等
    clock.t += 1.0
    before = len(clock.slept)
    await limiter.acquire()
    assert len(clock.slept) == before


# ── PDF 页数预检 ────────────────────────────────────────────────────


def _make_pdf(pages: int) -> bytes:
    from pypdf import PdfWriter

    buf = io.BytesIO()
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=72, height=72)
    w.write(buf)
    return buf.getvalue()


def test_count_pdf_pages_real_pdf() -> None:
    assert count_pdf_pages(_make_pdf(3)) == 3


def test_count_pdf_pages_undecodable_returns_none() -> None:
    """pypdf 读不动 ≠ 平台读不动：返回 None 交给 MinerU 兜底，不猜不拦。"""
    assert count_pdf_pages(b"%PDF-1.4 garbage") is None
    assert count_pdf_pages(b"") is None


# ── 有界 PDF 执行器（M2） ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_pdf_job_returns_result() -> None:
    assert await run_pdf_job(count_pdf_pages, _make_pdf(2)) == 2


@pytest.mark.asyncio
async def test_run_pdf_job_timeout_returns_none() -> None:
    """畸形 PDF 不许拖死解析链：超时按「数不出」处理，调用方保守兜底。"""
    import time as _time

    def slow(_arg):
        _time.sleep(0.4)  # pragma: no cover - 会被 wait_for 掐断
        return 999

    assert await run_pdf_job(slow, b"x", timeout_s=0.05) is None


@pytest.mark.asyncio
async def test_run_pdf_job_exception_returns_none() -> None:
    def boom(_arg):
        raise RuntimeError("malformed")

    assert await run_pdf_job(boom, b"x") is None


# ── 远端 URL 闸（L4，SSRF 纵深） ────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://mineru.test/x.zip",  # 非 https
        "https://localhost/x.zip",
        "https://foo.localhost/x.zip",
        "https://127.0.0.1/x.zip",
        "https://10.0.0.8/x.zip",
        "https://192.168.1.10/x.zip",
        "https://172.16.0.1/x.zip",
        "https://169.254.169.254/x.zip",  # 链路本地（云元数据端点）
        "https://0.0.0.0/x.zip",
        "https://[::1]/x.zip",
        "https://[fe80::1]/x.zip",
        "ftp://mineru.test/x.zip",
        "https:///x.zip",  # 无主机名
        "",
    ],
)
def test_assert_safe_remote_url_rejects(url: str) -> None:
    with pytest.raises(MineruError) as ei:
        assert_safe_remote_url(url, context="测试")
    assert ei.value.retryable is False


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.test/a.zip",
        "https://cdn.mineru.net/x/y.zip?token=abc",
        "https://8.8.8.8/x.zip",  # 公网直写 IP：域名解析不可控，不解析域名只放公网 IP
    ],
)
def test_assert_safe_remote_url_allows_public_https(url: str) -> None:
    assert_safe_remote_url(url, context="测试")


@pytest.mark.asyncio
async def test_download_zip_rejects_private_url_before_any_request(
    tmp_path: Path,
) -> None:
    """闸在出网之前：内网 URL 一个字节都不许发。"""

    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("内网 URL 绝不许出网")

    clock = fake_clock()
    client = make_client(handler, clock)
    with pytest.raises(MineruError):
        await client.download_zip("https://127.0.0.1/secret.zip", tmp_path / "x.zip")
    assert not (tmp_path / "x.zip").exists()


@pytest.mark.asyncio
async def test_upload_file_rejects_private_url_before_any_request() -> None:
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("内网 URL 绝不许出网")

    clock = fake_clock()
    client = make_client(handler, clock)
    with pytest.raises(MineruError):
        await client.upload_file("http://10.1.2.3/upload", b"data")


# ── 配置 ────────────────────────────────────────────────────────────


def test_resolve_mineru_config_reads_env_at_call_time(monkeypatch) -> None:
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    assert resolve_mineru_config() is None, "无 token = 文档链未配置（端点转 503）"

    monkeypatch.setenv("MINERU_API_TOKEN", "  tok-x  ")
    monkeypatch.setenv("MINERU_MODEL_VERSION", "vlm")
    cfg = resolve_mineru_config()
    assert cfg is not None
    assert cfg.token == "tok-x", "token 去掉首尾空白（CRLF 的 .env 是真实故障）"
    assert cfg.model_version == "vlm"
    assert cfg.base_url == "https://mineru.net"

    monkeypatch.setenv("MINERU_BASE_URL", "https://mineru.internal")
    cfg2 = resolve_mineru_config()
    assert cfg2 is not None and cfg2.base_url == "https://mineru.internal"


def test_mineru_config_rejects_empty_token_construction() -> None:
    with pytest.raises(ValueError):
        MineruConfig(token="", base_url="https://mineru.net", model_version="vlm")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
