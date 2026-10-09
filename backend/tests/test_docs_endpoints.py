"""文档端点（T-FM-08）：上传/列表/详情/整理/预览/删除/配额。

七类静默故障点，逐条钉住：

1. **上传安全**：路径穿越（含 Windows 反斜杠）、非白名单扩展名、magic 伪装、
   超大文件、控制字符文件名——拒绝后**不留垃圾**（目录连根清、行不落库）。
2. **配额先于提交**：reserve（原子预留；安全审查 H1）必须在 submit_parse
   **之前**（先提交再判额 = 平台已经被扣了才拦）；sha256 复用命中则**不走
   配额也不提交**；被配额拦下的行连根清（不留「永远解析不了」的滞留）。
3. **收口到本人**：他人 doc_id 一律 404（不多说一个字）；列表只回自己。
4. **不吐内部路径**：API 输出剥掉 original_path/md_path/content_list_path/
   sha256/mineru_batch_id——展示面不需要，泄漏磁盘布局。
5. **预览白名单**：只允许解析产物目录下的 .md/图片，路径先规范化再比绝对
   前缀（`..`/绝对路径/反斜杠全拒）。
6. **删除防复活**：先 cancel 在途轮询再 rmtree，软删后退未结算预留；
   上传/整理窗口内被删除 → 404 且不烧配额、不复活。
7. **C1 上传闸门与频控**：Content-Length 缺失 411 / 超上限 413，都发生在
   multipart 解析**之前**；按用户频控 429（M1）；整理在途锁 409。

单元用例全用内存替身直接调 handler（P0 同款手法）；末尾一个真库往返用例。
"""

from __future__ import annotations

import hashlib
import io
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent.doc_quota import (  # noqa: E402
    QuotaExceeded,
    QuotaStatus,
    RateLimited,
)
from backend.services.engine.alpha_agent.doc_organize import OrganizeError  # noqa: E402
from backend.services.engine.alpha_agent.mineru_client import MineruError  # noqa: E402
from backend.services.engine.routers import alpha_agent_docs as docs_mod  # noqa: E402

NOW = "2026-10-09T00:00:00Z"


# ── 替身 ────────────────────────────────────────────────────────────


def mk_row(doc_id: str = "d1", user_id: str = "u-1", status: str = "parsed", **over):
    row = {
        "doc_id": doc_id,
        "user_id": user_id,
        "filename": "paper.pdf",
        "ext": ".pdf",
        "size_bytes": 123,
        "sha256": "a" * 64,
        "original_path": "/x/original.pdf",
        "mineru_batch_id": "b-1",
        "parse_state": "done",
        "page_count": 7,
        "md_path": None,
        "content_list_path": None,
        "status": status,
        "organized_text": None,
        "organize_kind": None,
        "organize_prompt_version": None,
        "organized_at": None,
        "task_id": None,
        "error": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(over)
    return row


class FakeStore:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = {r["doc_id"]: dict(r) for r in (rows or [])}
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []
        self.hard_deleted: list[tuple[str, str]] = []
        self.soft_deleted: list[tuple[str, str]] = []
        self.list_calls: list[dict] = []
        self.count_calls: list[dict] = []

    async def create_doc(self, **kw):
        self.created.append(kw)
        self.rows.setdefault(
            kw["doc_id"],
            mk_row(
                doc_id=kw["doc_id"],
                user_id=kw["user_id"],
                status=kw.get("status", "uploaded"),
                filename=kw.get("filename"),
                ext=kw.get("ext"),
                sha256=kw.get("sha256"),
                size_bytes=kw.get("size_bytes"),
                original_path=kw.get("original_path"),
            ),
        )

    async def get_doc(self, doc_id, *, user_id=None):
        row = self.rows.get(doc_id)
        if row is None or (user_id is not None and row["user_id"] != user_id):
            return None
        return dict(row)

    async def update_doc(self, doc_id, **fields):
        self.updated.append((doc_id, fields))
        row = self.rows.get(doc_id)
        if row is not None:
            row.update(fields)

    async def hard_delete(self, doc_id, *, user_id):
        self.hard_deleted.append((doc_id, user_id))
        row = self.rows.get(doc_id)
        if row is not None and row["user_id"] == user_id:
            del self.rows[doc_id]
            return True
        return False

    async def soft_delete(self, doc_id, *, user_id):
        self.soft_deleted.append((doc_id, user_id))
        row = self.rows.get(doc_id)
        if row is not None and row["user_id"] == user_id and row["status"] != "deleted":
            row["status"] = "deleted"
            return True
        return False

    async def list_docs(self, **kw):
        self.list_calls.append(kw)
        rows = [
            dict(r)
            for r in self.rows.values()
            if r["user_id"] == kw["user_id"]
            and (kw.get("status") is None or r["status"] == kw["status"])
        ]
        # 镜像真实 store 的 ORDER BY created_at DESC, doc_id DESC
        rows.sort(key=lambda r: (r["created_at"], r["doc_id"]), reverse=True)
        return rows

    async def count_docs(self, **kw):
        # 镜像真实 store：显式 status 优先；status=None 时排除 deleted
        self.count_calls.append(kw)
        st = kw.get("status")
        return len(
            [
                r
                for r in self.rows.values()
                if r["user_id"] == kw["user_id"]
                and (r["status"] == st if st is not None else r["status"] != "deleted")
            ]
        )


class FakeParseService:
    def __init__(
        self, root: Path, *, store: FakeStore | None = None, events=None
    ) -> None:
        self._root = Path(root)
        self._store = store
        self.events = events if events is not None else []
        self.ensure_ready_calls = 0
        self.ready_error: Exception | None = None
        self.submitted: list[dict] = []
        self.submit_error: Exception | None = None
        self.reuse_result = False
        self.reuse_calls: list[dict] = []
        self.cancelled: list[str] = []

    def ensure_ready(self):
        self.ensure_ready_calls += 1
        if self.ready_error is not None:
            raise self.ready_error

    def doc_dir(self, doc_id: str) -> Path:
        return self._root / doc_id

    async def maybe_reuse(self, doc) -> bool:
        self.reuse_calls.append(dict(doc))
        return self.reuse_result

    async def submit_parse(self, doc) -> None:
        self.events.append("submit")
        self.submitted.append(dict(doc))
        if self.submit_error is not None:
            raise self.submit_error
        if self._store is not None:
            await self._store.update_doc(
                str(doc["doc_id"]), status="parsing", parse_state="pending"
            )

    async def cancel(self, doc_id: str) -> bool:
        self.cancelled.append(doc_id)
        return True


class FakeQuota:
    """镜像 DocQuota 公开面：reserve（原子预留）/release/check_rate/try_lock。"""

    def __init__(
        self,
        exc=None,
        status_obj=None,
        events=None,
        *,
        rate_exc: RateLimited | None = None,
        lock_granted: bool = True,
    ) -> None:
        self.guards: list[tuple[str, int]] = []
        self.reserve_doc_ids: list[str] = []
        self.released: list[tuple[str, str]] = []
        self.rate_checks: list[tuple[str, str]] = []
        self.locks: list[str] = []
        self.unlocks: list[str] = []
        self.exc = exc
        self.rate_exc = rate_exc
        self.lock_granted = lock_granted
        self.status_obj = status_obj
        self.status_user = None
        self.events = events if events is not None else []

    def reserve(self, user_id, pages, *, doc_id):
        self.events.append("guard")
        self.guards.append((user_id, int(pages)))
        self.reserve_doc_ids.append(str(doc_id))
        if self.exc is not None:
            raise self.exc

    def release(self, doc_id, user_id):
        self.released.append((str(doc_id), str(user_id)))

    def check_rate(self, user_id, action):
        self.rate_checks.append((user_id, action))
        if self.rate_exc is not None:
            raise self.rate_exc

    def try_lock(self, name, *, ttl_s):
        self.locks.append(name)
        return self.lock_granted

    def unlock(self, name):
        self.unlocks.append(name)

    def status(self, user_id):
        self.status_user = user_id
        return self.status_obj


class FakeRequest:
    """最小 Request 替身：headers + async form()（上传 handler 只用到这些）。"""

    def __init__(self, *, headers=None, form_data=None) -> None:
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._form = form_data
        self.form_called = 0

    async def form(self):
        self.form_called += 1
        return self._form or {}


def _auth_as(monkeypatch, user_id: str = "u-1", tenant: str = "t-1") -> None:
    monkeypatch.setattr(
        docs_mod, "get_authenticated_identity", lambda req: (user_id, tenant)
    )


def _upload_file(data: bytes, filename: str):
    from starlette.datastructures import UploadFile

    return UploadFile(io.BytesIO(data), filename=filename)


def _upload(
    data: bytes, filename: str, *, declared_length: int | None = None
) -> FakeRequest:
    """上传请求替身：multipart 字段 file + Content-Length 头（C1 粗闸用它）。"""
    declared = len(data) if declared_length is None else declared_length
    return FakeRequest(
        headers={"content-length": str(declared)},
        form_data={"file": _upload_file(data, filename)},
    )


PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
DOCX_BYTES = b"PK\x03\x04" + b"\x00" * 32


def _blank_pdf_bytes(pages: int = 3) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _wire(monkeypatch, store, svc, quota, user_id="u-1"):
    monkeypatch.setattr(docs_mod, "get_doc_store", lambda: store)
    monkeypatch.setattr(docs_mod, "get_doc_parse_service", lambda: svc)
    monkeypatch.setattr(docs_mod, "get_doc_quota", lambda: quota)
    _auth_as(monkeypatch, user_id)


# ── 上传 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_pdf_happy_path_orders_guard_before_submit(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[str] = []
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store, events=events)
    quota = FakeQuota(events=events)
    _wire(monkeypatch, store, svc, quota)

    out = await docs_mod.upload_doc(request=_upload(PDF_BYTES, "研报 2026.pdf"))

    doc = out["data"]["doc"]
    assert out["code"] == 200 and out["data"]["reused"] is False
    assert store.created[0]["user_id"] == "u-1"
    assert store.created[0]["filename"] == "研报 2026.pdf"
    assert store.created[0]["sha256"] == hashlib.sha256(PDF_BYTES).hexdigest()
    # 配额预留先于提交（先提交再判额 = 平台已经扣了才拦）
    assert events == ["guard", "submit"]
    # 页数数不动（测试件不是合法 PDF）→ 保守按单文件上限预留，结算多退少补
    assert quota.guards == [("u-1", 200)]
    assert quota.reserve_doc_ids == [store.created[0]["doc_id"]]
    assert len(svc.submitted) == 1
    assert svc.ensure_ready_calls == 1
    # 原件落盘 + 返回解析中状态 + 不吐内部路径
    original = tmp_path / doc["doc_id"] / "original.pdf"
    assert original.read_bytes() == PDF_BYTES
    assert doc["status"] == "parsing"
    for hidden in (
        "original_path",
        "md_path",
        "content_list_path",
        "sha256",
        "mineru_batch_id",
        "user_id",
    ):
        assert hidden not in doc, hidden


@pytest.mark.asyncio
async def test_upload_reserve_receives_pdf_page_count(
    monkeypatch, tmp_path: Path
) -> None:
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    quota = FakeQuota()
    _wire(monkeypatch, store, svc, quota)

    await docs_mod.upload_doc(request=_upload(_blank_pdf_bytes(3), "p.pdf"))
    assert quota.guards == [("u-1", 3)], "数得动的 PDF 按实际页数预留"


@pytest.mark.asyncio
async def test_upload_docx_reserves_full_page_cap(monkeypatch, tmp_path: Path) -> None:
    """office 族不在上传路径数页：一律按单文件上限保守预留（结算再退）。"""
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    quota = FakeQuota()
    _wire(monkeypatch, store, svc, quota)

    await docs_mod.upload_doc(request=_upload(DOCX_BYTES, "论文.docx"))
    assert quota.guards == [("u-1", docs_mod.MAX_PAGES_PER_FILE)]
    assert store.created[0]["ext"] == ".docx"


@pytest.mark.asyncio
async def test_upload_over_page_cap_400_cleans_row_and_dir(
    monkeypatch, tmp_path: Path
) -> None:
    """页数超单文件上限：还没提交就拒——烧的是共享配额，不许先斩后奏。"""
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    quota = FakeQuota()
    _wire(monkeypatch, store, svc, quota)

    over = _blank_pdf_bytes(docs_mod.MAX_PAGES_PER_FILE + 1)
    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=_upload(over, "huge.pdf"))
    assert ei.value.status_code == 400
    assert str(docs_mod.MAX_PAGES_PER_FILE) in str(ei.value.detail)
    assert quota.guards == [], "没预留"
    assert svc.submitted == []
    assert len(store.hard_deleted) == 1 and store.rows == {}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_upload_reuses_parsed_artifact_without_quota_or_submit(
    monkeypatch, tmp_path: Path
) -> None:
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    svc.reuse_result = True
    quota = FakeQuota()
    _wire(monkeypatch, store, svc, quota)

    out = await docs_mod.upload_doc(request=_upload(PDF_BYTES, "p.pdf"))

    assert out["data"]["reused"] is True
    assert quota.guards == [] and svc.submitted == []
    assert len(svc.reuse_calls) == 1


@pytest.mark.asyncio
async def test_upload_deleted_during_window_404_without_quota(
    monkeypatch, tmp_path: Path
) -> None:
    """H2：复用判定窗口内被删除 → 404 收口，不预留配额、不提交。"""
    store = FakeStore()

    class DeletingSvc(FakeParseService):
        async def maybe_reuse(self, doc) -> bool:
            self.reuse_calls.append(dict(doc))
            self._store.rows[doc["doc_id"]]["status"] = "deleted"
            return False

    svc = DeletingSvc(tmp_path, store=store)
    quota = FakeQuota()
    _wire(monkeypatch, store, svc, quota)

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=_upload(PDF_BYTES, "p.pdf"))
    assert ei.value.status_code == 404
    assert quota.guards == [], "已删的行不许再预烧共享配额"
    assert svc.submitted == []
    assert list(tmp_path.iterdir()) == [], "原件目录必须清掉"
    assert store.rows[store.created[0]["doc_id"]]["status"] == "deleted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename",
    [
        "../evil.pdf",
        "a\\b.pdf",
        "/abs/evil.pdf",
        "..",
        ".hidden",
        "",
        "x.exe",
        "noext",
        "a\nb.pdf",  # L3：Cc 控制字符（伪造日志行）
        "evil‮gpj.pdf",  # L3：Cf 格式字符（bidi 伪装）
    ],
)
async def test_upload_rejects_bad_filenames(
    monkeypatch, tmp_path: Path, filename
) -> None:
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    quota = FakeQuota()
    _wire(monkeypatch, store, svc, quota)

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=_upload(PDF_BYTES, filename))
    assert ei.value.status_code == 400, filename
    assert store.created == []
    assert list(tmp_path.iterdir()) == [], "拒绝后不许留垃圾目录"


@pytest.mark.asyncio
async def test_upload_missing_content_length_411_before_form_parse(
    monkeypatch, tmp_path: Path
) -> None:
    """C1：缺 Content-Length（chunked）在读 multipart 之前就拒。"""
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    quota = FakeQuota()
    _wire(monkeypatch, store, svc, quota)
    req = FakeRequest(
        form_data={"file": _upload_file(PDF_BYTES, "p.pdf")}
    )  # 无 headers

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=req)
    assert ei.value.status_code == 411
    assert req.form_called == 0, "闸门必须在 multipart 解析之前"
    assert store.created == []


@pytest.mark.asyncio
async def test_upload_declared_length_over_hard_cap_413_before_form_parse(
    monkeypatch, tmp_path: Path
) -> None:
    """C1：Content-Length 超「文件上限+multipart 余量」→ 解析前 413。"""
    monkeypatch.setenv(docs_mod.MAX_UPLOAD_ENV, "1")
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    _wire(monkeypatch, store, svc, FakeQuota())
    req = _upload(
        PDF_BYTES,
        "p.pdf",
        declared_length=1024 * 1024 + docs_mod.MULTIPART_OVERHEAD_BYTES + 1,
    )

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=req)
    assert ei.value.status_code == 413
    assert req.form_called == 0, "粗闸不过就别解析 multipart"
    assert store.created == []


@pytest.mark.asyncio
async def test_upload_invalid_content_length_400(monkeypatch, tmp_path: Path) -> None:
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    _wire(monkeypatch, store, svc, FakeQuota())
    req = FakeRequest(
        headers={"content-length": "banana"},
        form_data={"file": _upload_file(PDF_BYTES, "p.pdf")},
    )
    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=req)
    assert ei.value.status_code == 400 and req.form_called == 0


@pytest.mark.asyncio
async def test_upload_rate_limited_429_before_any_io(
    monkeypatch, tmp_path: Path
) -> None:
    """M1：按用户频控在上传端点最前——被限流的请求一分磁盘都不落。"""
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    quota = FakeQuota(
        rate_exc=RateLimited("太频繁", scope="upload", limit=30, window_s=3600)
    )
    _wire(monkeypatch, store, svc, quota)
    req = _upload(PDF_BYTES, "p.pdf")

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=req)
    assert ei.value.status_code == 429
    assert quota.rate_checks == [("u-1", "upload")]
    assert req.form_called == 0 and store.created == []
    assert svc.ensure_ready_calls == 0


@pytest.mark.asyncio
async def test_upload_rejects_magic_mismatch_and_cleans_dir(
    monkeypatch, tmp_path: Path
) -> None:
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    _wire(monkeypatch, store, svc, FakeQuota())

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=_upload(PNG_BYTES, "fake.pdf"))
    assert ei.value.status_code == 400
    assert "magic" in str(ei.value.detail)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_upload_rejects_oversize_and_cleans_dir(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(docs_mod.MAX_UPLOAD_ENV, "1")
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    _wire(monkeypatch, store, svc, FakeQuota())

    payload = b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024)
    # 声明值可信范围（粗闸放过），真实字节数由分块计数精验拦下
    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=_upload(payload, "big.png"))
    assert ei.value.status_code == 413
    assert store.created == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_upload_503_when_token_missing(monkeypatch, tmp_path: Path) -> None:
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    svc.ready_error = MineruError("MINERU_API_TOKEN 未配置", retryable=False)
    _wire(monkeypatch, store, svc, FakeQuota())

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=_upload(PDF_BYTES, "p.pdf"))
    assert ei.value.status_code == 503
    assert "MINERU_API_TOKEN" in str(ei.value.detail)
    assert store.created == [] and list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_upload_quota_exceeded_429_cleans_row_and_dir(
    monkeypatch, tmp_path: Path
) -> None:
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    st = QuotaStatus(
        day="20261009",
        user_id="u-1",
        user_used=200,
        user_limit=200,
        platform_used=10,
        platform_budget=1000,
        user_remaining=0,
        platform_remaining=990,
        exhausted=True,
        warning=False,
    )
    quota = FakeQuota(
        exc=QuotaExceeded("您今日文档解析页数已用尽…", scope="user", status=st)
    )
    _wire(monkeypatch, store, svc, quota)

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=_upload(PDF_BYTES, "p.pdf"))
    assert ei.value.status_code == 429
    assert svc.submitted == []
    assert len(store.hard_deleted) == 1, "被配额拦下的上传不许滞留 uploaded 行"
    assert store.rows == {}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_upload_submit_error_502_keeps_row(monkeypatch, tmp_path: Path) -> None:
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    svc.submit_error = MineruError("MinerU 上传失败", retryable=True)
    _wire(monkeypatch, store, svc, FakeQuota())

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=_upload(PDF_BYTES, "p.pdf"))
    assert ei.value.status_code == 502
    assert store.hard_deleted == [], "提交失败的行要留着（可见的失败，不是消失）"


@pytest.mark.asyncio
async def test_upload_png_estimates_one_page(monkeypatch, tmp_path: Path) -> None:
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    quota = FakeQuota()
    _wire(monkeypatch, store, svc, quota)

    await docs_mod.upload_doc(request=_upload(PNG_BYTES, "chart.png"))
    assert quota.guards == [("u-1", 1)], "图片按 MinerU 单页处理，恒预留 1"
    assert store.created[0]["ext"] == ".png"


# ── 列表 / 详情 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_docs_scopes_and_redacts(monkeypatch) -> None:
    store = FakeStore(
        [
            mk_row(doc_id="d1", user_id="u-1"),
            mk_row(doc_id="d2", user_id="u-1", status="parsing", organized_text="草稿"),
            mk_row(doc_id="d9", user_id="other"),
        ]
    )
    _wire(monkeypatch, store, FakeParseService(Path("/nonexistent")), FakeQuota())

    out = await docs_mod.list_docs(
        request=FakeRequest(), status=None, limit=10, offset=5
    )

    data = out["data"]
    # 新人在前（镜像真库 ORDER BY created_at DESC, doc_id DESC）
    assert [d["doc_id"] for d in data["items"]] == ["d2", "d1"]
    # 他人行（d9）不入 items 也不入 total——跨用户不可见
    assert data["total"] == 2
    assert data["limit"] == 10 and data["offset"] == 5
    for item in data["items"]:
        for hidden in (
            "original_path",
            "md_path",
            "content_list_path",
            "sha256",
            "mineru_batch_id",
            "user_id",
        ):
            assert hidden not in item, hidden
        assert "organized_text" not in item, "列表不带正文（体积）"
    assert store.list_calls[0]["limit"] == 10 and store.list_calls[0]["offset"] == 5


@pytest.mark.asyncio
async def test_list_docs_unknown_status_400_without_db(monkeypatch) -> None:
    store = FakeStore()
    _wire(monkeypatch, store, FakeParseService(Path("/nonexistent")), FakeQuota())

    with pytest.raises(HTTPException) as ei:
        await docs_mod.list_docs(
            request=FakeRequest(), status="weird", limit=50, offset=0
        )
    assert ei.value.status_code == 400
    assert store.list_calls == []


@pytest.mark.asyncio
async def test_get_doc_detail_owner_and_404(monkeypatch) -> None:
    store = FakeStore(
        [
            mk_row(
                doc_id="d1", user_id="u-1", status="organized", organized_text="# 简报"
            )
        ]
    )
    _wire(monkeypatch, store, FakeParseService(Path("/nonexistent")), FakeQuota())

    out = await docs_mod.get_doc_detail(request=FakeRequest(), doc_id="d1")
    assert out["data"]["doc"]["organized_text"] == "# 简报"
    assert "md_path" not in out["data"]["doc"]

    with pytest.raises(HTTPException) as ei:
        await docs_mod.get_doc_detail(request=FakeRequest(), doc_id="d-other")
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_get_doc_detail_deleted_404(monkeypatch) -> None:
    store = FakeStore([mk_row(doc_id="d1", user_id="u-1", status="deleted")])
    _wire(monkeypatch, store, FakeParseService(Path("/nonexistent")), FakeQuota())
    with pytest.raises(HTTPException) as ei:
        await docs_mod.get_doc_detail(request=FakeRequest(), doc_id="d1")
    assert ei.value.status_code == 404


# ── 配额 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_quota_endpoint_serializes_status(monkeypatch) -> None:
    st = QuotaStatus(
        day="20261009",
        user_id="u-1",
        user_used=12,
        user_limit=200,
        platform_used=40,
        platform_budget=1000,
        user_remaining=188,
        platform_remaining=960,
        exhausted=False,
        warning=False,
    )
    quota = FakeQuota(status_obj=st)
    _wire(monkeypatch, FakeStore(), FakeParseService(Path("/n")), quota)
    monkeypatch.setattr(docs_mod, "resolve_mineru_config", lambda: None)

    out = await docs_mod.doc_quota_status(request=FakeRequest())

    data = out["data"]
    assert data["day"] == "20261009" and data["user_used"] == 12
    assert data["user_remaining"] == 188 and data["platform_remaining"] == 960
    assert data["exhausted"] is False and data["warning"] is False
    assert data["token_configured"] is False
    assert quota.status_user == "u-1"


# ── 统计 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_counts_failure_rate_and_quota(monkeypatch) -> None:
    store = FakeStore(
        [
            mk_row(doc_id="d1", user_id="u-1", status="parsed"),
            mk_row(doc_id="d2", user_id="u-1", status="parsed"),
            mk_row(doc_id="d3", user_id="u-1", status="organized"),
            mk_row(doc_id="d4", user_id="u-1", status="parse_failed"),
            mk_row(doc_id="d5", user_id="u-1", status="parsing"),
            mk_row(doc_id="d6", user_id="u-1", status="deleted"),
            mk_row(doc_id="d9", user_id="other", status="parse_failed"),
        ]
    )
    st = QuotaStatus(
        day="20261009",
        user_id="u-1",
        user_used=1,
        user_limit=200,
        platform_used=2,
        platform_budget=1000,
        user_remaining=199,
        platform_remaining=998,
        exhausted=False,
        warning=False,
    )
    _wire(monkeypatch, store, FakeParseService(Path("/n")), FakeQuota(status_obj=st))
    monkeypatch.setattr(docs_mod, "resolve_mineru_config", lambda: None)

    out = await docs_mod.docs_stats(request=FakeRequest())

    data = out["data"]
    assert data["total"] == 5, "deleted 与他人行都不入总数"
    assert data["counts"] == {
        "uploaded": 0,
        "parsing": 1,
        "parsed": 2,
        "parse_failed": 1,
        "organized": 1,
        "expired": 0,
    }
    assert data["attempted"] == 4, "2 parsed + 1 organized + 1 failed；parsing 在途不计"
    assert data["parse_failed"] == 1
    assert data["failure_rate"] == 0.25
    assert data["quota"]["platform_budget"] == 1000
    assert data["quota"]["token_configured"] is False


@pytest.mark.asyncio
async def test_stats_failure_rate_zero_when_nothing_attempted(monkeypatch) -> None:
    """全在途（uploaded/parsing）：attempted=0 → 失败率 0.0，不能除零。"""
    store = FakeStore([mk_row(doc_id="d1", user_id="u-1", status="uploaded")])
    st = QuotaStatus(
        day="20261009",
        user_id="u-1",
        user_used=0,
        user_limit=200,
        platform_used=0,
        platform_budget=1000,
        user_remaining=200,
        platform_remaining=1000,
        exhausted=False,
        warning=False,
    )
    _wire(monkeypatch, store, FakeParseService(Path("/n")), FakeQuota(status_obj=st))
    monkeypatch.setattr(docs_mod, "resolve_mineru_config", lambda: None)

    data = (await docs_mod.docs_stats(request=FakeRequest()))["data"]

    assert data["attempted"] == 0 and data["failure_rate"] == 0.0


# ── 整理 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_organize_happy_returns_payload_and_persists(monkeypatch) -> None:
    store = FakeStore([mk_row(doc_id="d1", user_id="u-1", status="parsed")])
    quota = FakeQuota()
    _wire(monkeypatch, store, FakeParseService(Path("/n")), quota)
    calls: dict = {}

    class FakeConfig:
        pass

    async def fake_resolve(user_id, tenant_id):
        calls["llm_user"] = user_id
        return FakeConfig(), "user_profile", None

    async def fake_organize(
        store_arg, doc_arg, *, kind, extra=None, config=None, chat_fn=None
    ):
        calls.update(kind=kind, extra=extra, config=config, doc_id=doc_arg["doc_id"])
        return {
            "kind": kind,
            "prompt_version": "v1",
            "payload": {"title": "t"},
            "markdown": "# t\n",
            "truncated": False,
            "chunks_used": 1,
        }

    monkeypatch.setattr(docs_mod, "_resolve_effective_llm_config", fake_resolve)
    monkeypatch.setattr(docs_mod, "organize_and_store", fake_organize)

    out = await docs_mod.organize_doc(
        request=FakeRequest(),
        doc_id="d1",
        payload=docs_mod.OrganizeRequest(kind="free", extra="只看日频"),
    )

    assert calls["kind"] == "free" and calls["extra"] == "只看日频"
    assert calls["llm_user"] == "u-1" and calls["doc_id"] == "d1"
    assert isinstance(calls["config"], FakeConfig)
    assert quota.rate_checks == [("u-1", "organize")]
    assert quota.locks == ["organize:d1"], "在途锁防同一文档并发重复烧 LLM"
    assert quota.unlocks == ["organize:d1"], "无论成败锁都要释放"
    data = out["data"]
    assert data["prompt_version"] == "v1" and data["markdown"] == "# t\n"
    assert data["doc"]["doc_id"] == "d1" and "original_path" not in data["doc"]


@pytest.mark.asyncio
async def test_organize_requires_llm_config_412(monkeypatch) -> None:
    store = FakeStore([mk_row(doc_id="d1", user_id="u-1")])
    _wire(monkeypatch, store, FakeParseService(Path("/n")), FakeQuota())
    called = []

    async def fake_resolve(user_id, tenant_id):
        return None, "none", None

    async def fake_organize(*a, **kw):  # pragma: no cover - 不应被调到
        called.append(1)

    monkeypatch.setattr(docs_mod, "_resolve_effective_llm_config", fake_resolve)
    monkeypatch.setattr(docs_mod, "organize_and_store", fake_organize)

    with pytest.raises(HTTPException) as ei:
        await docs_mod.organize_doc(
            request=FakeRequest(),
            doc_id="d1",
            payload=docs_mod.OrganizeRequest(kind="paper"),
        )
    assert ei.value.status_code == 412
    assert "API Key" in str(ei.value.detail)
    assert called == []


@pytest.mark.asyncio
async def test_organize_unknown_kind_400_before_llm(monkeypatch) -> None:
    store = FakeStore([mk_row(doc_id="d1", user_id="u-1")])
    _wire(monkeypatch, store, FakeParseService(Path("/n")), FakeQuota())
    called = []

    async def fake_resolve(user_id, tenant_id):  # pragma: no cover - 不应被调到
        called.append("llm")
        return None, "none", None

    monkeypatch.setattr(docs_mod, "_resolve_effective_llm_config", fake_resolve)
    with pytest.raises(HTTPException) as ei:
        await docs_mod.organize_doc(
            request=FakeRequest(),
            doc_id="d1",
            payload=docs_mod.OrganizeRequest(kind="magic"),
        )
    assert ei.value.status_code == 400
    assert called == []


@pytest.mark.asyncio
async def test_organize_status_conflict_maps_400(monkeypatch) -> None:
    store = FakeStore([mk_row(doc_id="d1", user_id="u-1", status="parsing")])
    _wire(monkeypatch, store, FakeParseService(Path("/n")), FakeQuota())

    async def fake_resolve(user_id, tenant_id):
        return object(), "user_profile", None

    async def fake_organize(*a, **kw):
        raise OrganizeError("文档尚未解析完成（当前状态 parsing），无法整理")

    monkeypatch.setattr(docs_mod, "_resolve_effective_llm_config", fake_resolve)
    monkeypatch.setattr(docs_mod, "organize_and_store", fake_organize)

    with pytest.raises(HTTPException) as ei:
        await docs_mod.organize_doc(
            request=FakeRequest(),
            doc_id="d1",
            payload=docs_mod.OrganizeRequest(kind="free"),
        )
    assert ei.value.status_code == 400
    assert "尚未解析完成" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_organize_not_owner_404(monkeypatch) -> None:
    store = FakeStore([mk_row(doc_id="d1", user_id="other")])
    _wire(monkeypatch, store, FakeParseService(Path("/n")), FakeQuota())
    with pytest.raises(HTTPException) as ei:
        await docs_mod.organize_doc(
            request=FakeRequest(),
            doc_id="d1",
            payload=docs_mod.OrganizeRequest(kind="free"),
        )
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_organize_rate_limited_429_before_lock_and_llm(monkeypatch) -> None:
    """M1：一次整理最多 9 次 LLM 调用，按用户频控拦在锁与 LLM 之前。"""
    store = FakeStore([mk_row(doc_id="d1", user_id="u-1", status="parsed")])
    quota = FakeQuota(
        rate_exc=RateLimited("太频繁", scope="organize", limit=60, window_s=3600)
    )
    _wire(monkeypatch, store, FakeParseService(Path("/n")), quota)
    llm_called = []

    async def fake_resolve(*a, **kw):  # pragma: no cover - 不应被调到
        llm_called.append(1)
        return None, "none", None

    monkeypatch.setattr(docs_mod, "_resolve_effective_llm_config", fake_resolve)

    with pytest.raises(HTTPException) as ei:
        await docs_mod.organize_doc(
            request=FakeRequest(),
            doc_id="d1",
            payload=docs_mod.OrganizeRequest(kind="free"),
        )
    assert ei.value.status_code == 429
    assert quota.locks == [] and llm_called == []


@pytest.mark.asyncio
async def test_organize_concurrent_same_doc_409(monkeypatch) -> None:
    """M1：同一文档并发重复整理被在途锁挡下（409），且不去解占用者的锁。"""
    store = FakeStore([mk_row(doc_id="d1", user_id="u-1", status="parsed")])
    quota = FakeQuota(lock_granted=False)
    _wire(monkeypatch, store, FakeParseService(Path("/n")), quota)
    llm_called = []

    async def fake_resolve(*a, **kw):  # pragma: no cover - 不应被调到
        llm_called.append(1)
        return None, "none", None

    monkeypatch.setattr(docs_mod, "_resolve_effective_llm_config", fake_resolve)

    with pytest.raises(HTTPException) as ei:
        await docs_mod.organize_doc(
            request=FakeRequest(),
            doc_id="d1",
            payload=docs_mod.OrganizeRequest(kind="free"),
        )
    assert ei.value.status_code == 409
    assert "正在整理" in str(ei.value.detail)
    assert quota.unlocks == [], "锁不是自己拿到的，不许替别人解"
    assert llm_called == []


@pytest.mark.asyncio
async def test_organize_deleted_during_llm_404(monkeypatch) -> None:
    """H2：LLM 期间被删除——落库被守卫拦下（organized_text 不复活），如实 404。"""
    store = FakeStore([mk_row(doc_id="d1", user_id="u-1", status="parsed")])
    _wire(monkeypatch, store, FakeParseService(Path("/n")), FakeQuota())

    async def fake_resolve(user_id, tenant_id):
        return object(), "user_profile", None

    async def fake_organize(
        store_arg, doc_arg, *, kind, extra=None, config=None, chat_fn=None
    ):
        store_arg.rows["d1"]["status"] = "deleted"  # LLM 跑着的时候用户点了删除
        return {
            "kind": kind,
            "prompt_version": "v1",
            "payload": {},
            "markdown": "# t\n",
            "truncated": False,
            "chunks_used": 1,
        }

    monkeypatch.setattr(docs_mod, "_resolve_effective_llm_config", fake_resolve)
    monkeypatch.setattr(docs_mod, "organize_and_store", fake_organize)

    with pytest.raises(HTTPException) as ei:
        await docs_mod.organize_doc(
            request=FakeRequest(),
            doc_id="d1",
            payload=docs_mod.OrganizeRequest(kind="free"),
        )
    assert ei.value.status_code == 404
    assert store.rows["d1"]["status"] == "deleted", "已删行不许被写回"


# ── 预览 ────────────────────────────────────────────────────────────


def _parsed_doc(tmp_path: Path, **over) -> tuple[FakeStore, Path]:
    parsed = tmp_path / "d1" / "parsed"
    (parsed / "images").mkdir(parents=True)
    (parsed / "full.md").write_text("# 标题", encoding="utf-8")
    (parsed / "images" / "f0.png").write_bytes(PNG_BYTES)
    store = FakeStore(
        [
            mk_row(
                doc_id="d1",
                user_id="u-1",
                status="parsed",
                md_path=str(parsed / "full.md"),
                content_list_path=str(parsed / "content_list.json"),
                **over,
            )
        ]
    )
    return store, parsed


@pytest.mark.asyncio
async def test_preview_markdown_and_image(monkeypatch, tmp_path: Path) -> None:
    store, _ = _parsed_doc(tmp_path)
    _wire(monkeypatch, store, FakeParseService(tmp_path), FakeQuota())

    resp = await docs_mod.preview_doc_file(
        request=FakeRequest(), doc_id="d1", path="full.md"
    )
    assert resp.status_code == 200
    assert resp.media_type.startswith("text/markdown")
    assert Path(resp.path) == tmp_path / "d1" / "parsed" / "full.md"
    assert "filename" not in (resp.headers.get("content-disposition") or "")

    img = await docs_mod.preview_doc_file(
        request=FakeRequest(), doc_id="d1", path="images/f0.png"
    )
    assert img.media_type == "image/png"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_path",
    [
        "../secret.md",
        "/etc/passwd",
        "images/../../x.png",
        "a\\b.md",
        "images/../x.png",
        "content_list.json",
        "x.exe",
        "",
    ],
)
async def test_preview_rejects_bad_paths(
    monkeypatch, tmp_path: Path, bad_path: str
) -> None:
    store, _ = _parsed_doc(tmp_path)
    _wire(monkeypatch, store, FakeParseService(tmp_path), FakeQuota())
    with pytest.raises(HTTPException) as ei:
        await docs_mod.preview_doc_file(
            request=FakeRequest(), doc_id="d1", path=bad_path
        )
    assert ei.value.status_code == 400, bad_path


@pytest.mark.asyncio
async def test_preview_missing_file_404(monkeypatch, tmp_path: Path) -> None:
    store, _ = _parsed_doc(tmp_path)
    _wire(monkeypatch, store, FakeParseService(tmp_path), FakeQuota())
    with pytest.raises(HTTPException) as ei:
        await docs_mod.preview_doc_file(
            request=FakeRequest(), doc_id="d1", path="images/nope.png"
        )
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_preview_before_parse_409(monkeypatch, tmp_path: Path) -> None:
    store = FakeStore(
        [mk_row(doc_id="d1", user_id="u-1", status="parsing", md_path=None)]
    )
    _wire(monkeypatch, store, FakeParseService(tmp_path), FakeQuota())
    with pytest.raises(HTTPException) as ei:
        await docs_mod.preview_doc_file(
            request=FakeRequest(), doc_id="d1", path="full.md"
        )
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_preview_not_owner_404(monkeypatch, tmp_path: Path) -> None:
    store, _ = _parsed_doc(tmp_path)
    _wire(monkeypatch, store, FakeParseService(tmp_path), FakeQuota(), user_id="u-2")
    with pytest.raises(HTTPException) as ei:
        await docs_mod.preview_doc_file(
            request=FakeRequest(), doc_id="d1", path="full.md"
        )
    assert ei.value.status_code == 404


# ── 删除 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_cancels_poll_then_removes_dir_and_soft_deletes(
    monkeypatch, tmp_path: Path
) -> None:
    doc_dir = tmp_path / "d1"
    doc_dir.mkdir()
    (doc_dir / "original.pdf").write_bytes(PDF_BYTES)
    store = FakeStore([mk_row(doc_id="d1", user_id="u-1", status="parsing")])
    svc = FakeParseService(tmp_path)
    quota = FakeQuota()
    _wire(monkeypatch, store, svc, quota)

    out = await docs_mod.delete_doc(request=FakeRequest(), doc_id="d1")

    assert out["data"] == {"doc_id": "d1", "deleted": True}
    assert svc.cancelled == ["d1"], "必须先取消在途轮询（否则删完又被写回 parsed）"
    assert not doc_dir.exists()
    assert store.soft_deleted == [("d1", "u-1")]
    assert quota.released == [("d1", "u-1")], "删除退回未结算的预留"


@pytest.mark.asyncio
async def test_delete_not_owner_404_no_side_effects(
    monkeypatch, tmp_path: Path
) -> None:
    doc_dir = tmp_path / "d1"
    doc_dir.mkdir()
    store = FakeStore([mk_row(doc_id="d1", user_id="other")])
    svc = FakeParseService(tmp_path)
    quota = FakeQuota()
    _wire(monkeypatch, store, svc, quota)

    with pytest.raises(HTTPException) as ei:
        await docs_mod.delete_doc(request=FakeRequest(), doc_id="d1")
    assert ei.value.status_code == 404
    assert svc.cancelled == [] and doc_dir.exists() and store.soft_deleted == []
    assert quota.released == [], "没删成就不许动配额"


@pytest.mark.asyncio
async def test_delete_already_deleted_404(monkeypatch, tmp_path: Path) -> None:
    store = FakeStore([mk_row(doc_id="d1", user_id="u-1", status="deleted")])
    _wire(monkeypatch, store, FakeParseService(tmp_path), FakeQuota())
    with pytest.raises(HTTPException) as ei:
        await docs_mod.delete_doc(request=FakeRequest(), doc_id="d1")
    assert ei.value.status_code == 404


# ── 真库往返 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_docs_real_db_roundtrip(monkeypatch, tmp_path: Path) -> None:
    from sqlalchemy import text

    from backend.services.engine.alpha_agent.doc_store import get_doc_store
    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")

    user = f"t-docs-api-{uuid.uuid4().hex[:10]}"
    store = get_doc_store()
    await store.ensure_tables()
    svc = FakeParseService(tmp_path, store=store)
    quota = FakeQuota()
    _wire(monkeypatch, store, svc, quota, user_id=user)

    try:
        out = await docs_mod.upload_doc(request=_upload(PDF_BYTES, "paper.pdf"))
        doc_id = out["data"]["doc"]["doc_id"]
        row = await store.get_doc(doc_id, user_id=user)
        assert row is not None and row["status"] == "parsing"
        assert row["created_at"].endswith("Z")

        listed = await docs_mod.list_docs(
            request=FakeRequest(), status=None, limit=50, offset=0
        )
        assert [d["doc_id"] for d in listed["data"]["items"]] == [doc_id]
        assert listed["data"]["total"] == 1
        assert "original_path" not in listed["data"]["items"][0]

        detail = await docs_mod.get_doc_detail(request=FakeRequest(), doc_id=doc_id)
        assert detail["data"]["doc"]["doc_id"] == doc_id

        _auth_as(monkeypatch, user_id=f"{user}-other")
        with pytest.raises(HTTPException) as ei2:
            await docs_mod.get_doc_detail(request=FakeRequest(), doc_id=doc_id)
        assert ei2.value.status_code == 404

        _auth_as(monkeypatch, user)
        deleted = await docs_mod.delete_doc(request=FakeRequest(), doc_id=doc_id)
        assert deleted["data"]["deleted"] is True
        assert (await store.get_doc(doc_id, user_id=user))["status"] == "deleted"
        assert not (tmp_path / doc_id).exists()
    finally:
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM rd_agent_docs WHERE user_id = :u"), {"u": user}
            )
            await session.execute(
                text("DELETE FROM rd_agent_docs WHERE user_id = :u"),
                {"u": f"{user}-other"},
            )
        await close_database()
