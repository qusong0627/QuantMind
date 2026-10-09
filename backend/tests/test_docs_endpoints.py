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
from backend.services.engine.alpha_agent.mineru_client import (  # noqa: E402
    MODE_LOCAL,
    MineruConfig,
    MineruError,
)
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
        # 行上 original_paths 与真 store 同口径：JSON 文本（created 记录的是
        # router 传参原样，行是持久化形态——两者断言面不同）
        from backend.services.engine.alpha_agent.doc_store import (
            encode_original_paths,
        )

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
                files_count=kw.get("files_count", 1),
                original_paths=encode_original_paths(kw.get("original_paths")),
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
        self.submitted: list[dict] = []
        self.submit_error: Exception | None = None
        self.reuse_result = False
        self.reuse_calls: list[dict] = []
        self.cancelled: list[str] = []
        self.scheduled: list[str] = []

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

    def schedule_quota_reconcile(self, doc, batch_id: str | None = None) -> None:
        self.scheduled.append(str(doc["doc_id"]))


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
        self.committed: list[str] = []
        self.accounting_calls: list[tuple[str, bool | None]] = []
        self.rate_checks: list[tuple[str, str]] = []
        self.locks: list[str] = []
        self.unlocks: list[tuple[str, str | None]] = []
        self.exc = exc
        self.rate_exc = rate_exc
        self.lock_granted = lock_granted
        self.status_obj = status_obj
        self.status_user = None
        self.events = events if events is not None else []
        self._lock_seq = 0

    def reserve(self, user_id, pages, *, doc_id, accounting=None):
        self.events.append("guard")
        self.guards.append((user_id, int(pages)))
        self.reserve_doc_ids.append(str(doc_id))
        self.accounting_calls.append(("reserve", accounting))
        if self.exc is not None:
            raise self.exc

    def release(self, doc_id, user_id, *, accounting=None):
        self.released.append((str(doc_id), str(user_id)))
        self.accounting_calls.append(("release", accounting))

    def commit_reservation(self, doc_id, *, accounting=None):
        self.committed.append(str(doc_id))
        self.accounting_calls.append(("commit", accounting))

    def check_rate(self, user_id, action):
        self.rate_checks.append((user_id, action))
        if self.rate_exc is not None:
            raise self.rate_exc

    def try_lock(self, name, *, ttl_s):
        self.locks.append(name)
        if not self.lock_granted:
            return None
        self._lock_seq += 1
        return f"tok-{self._lock_seq}"

    def unlock(self, name, token=None):
        self.unlocks.append((name, token))

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
    return _upload_many([(data, filename)], declared_length=declared_length)


def _upload_many(
    files: list[tuple[bytes, str]], *, declared_length: int | None = None
) -> FakeRequest:
    """多文件上传替身：同一字段 file 重复出现（顺序=上传顺序；T-FM-19a）。"""
    from starlette.datastructures import FormData

    total = sum(len(data) for data, _ in files)
    declared = total if declared_length is None else declared_length
    return FakeRequest(
        headers={"content-length": str(declared)},
        form_data=FormData(
            [("file", _upload_file(data, name)) for data, name in files]
        ),
    )


PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
DOCX_BYTES = b"PK\x03\x04" + b"\x00" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 16
#: 未识别内容（防改名可执行文件的正样本）：任何嗅探表都不命中
ELF_BYTES = b"\x7fELF\x02\x01\x01" + b"\x00" * 16
#: HEIC 头（ftypheic）—— 可识别但不可直接解析
HEIC_BYTES = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00" + b"\x00" * 16


def _pillow_bytes(fmt: str, *, size: tuple[int, int] = (4, 4)) -> bytes:
    """用 Pillow 生成真实可解码的图片字节（转码测试必须真图，magic 桩不够）。"""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover —— 环境缺 Pillow 时跳过该格式
        pytest.skip("Pillow 不在本环境")
    buf = io.BytesIO()
    try:
        Image.new("RGB", size, (200, 30, 30)).save(buf, fmt)
    except Exception as exc:  # pragma: no cover —— Pillow 构建不支持该格式
        pytest.skip(f"Pillow 不支持 {fmt}: {exc}")
    return buf.getvalue()


def _blank_pdf_bytes(pages: int = 3) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class FakeConfigResolver:
    """有效 MinerU 配置解析替身（上传预检 / quota / stats / 设置页共用）。

    ``spec = (mode, value)``：cloud → ``MineruConfig(token=value)``；
    local → ``MineruConfig(base_url=value, mode=local)``；``None`` → 未配置
    ``(None, src)``（src 恒为字符串，"none"）。
    """

    def __init__(self, spec=("cloud", "test-tok"), src="env") -> None:
        self.spec = spec
        self.src = src
        self.calls: list[tuple] = []

    async def __call__(self, user_id, tenant_id, *, strict=False):
        self.calls.append((user_id, tenant_id, strict))
        if self.spec is None:
            return None, self.src
        mode, value = self.spec
        if mode == MODE_LOCAL:
            return MineruConfig(token="", base_url=value, mode=MODE_LOCAL), self.src
        return MineruConfig(token=value), self.src


class FakeTaskStore:
    """挖掘任务表替身（T-FM-19b）：count_tasks_by_docs / list_by_doc。

    契约与真 store 对齐：count 只回非零项（缺键=确认 0 条）；fail_* 开关
    模拟查询炸掉——两者在 API 上语义不同（未知 vs 0/空），必须能被区分。
    """

    def __init__(
        self,
        counts: dict[str, dict[str, int]] | None = None,
        tasks: dict[str, list[dict]] | None = None,
        *,
        fail_counts: bool = False,
        fail_tasks: bool = False,
    ) -> None:
        self.counts = counts or {}  # {user_id: {doc_id: n}}
        self.tasks = tasks or {}  # {doc_id: [行, ...]}
        self.fail_counts = fail_counts
        self.fail_tasks = fail_tasks
        self.count_calls: list[tuple[str, list[str]]] = []
        self.list_calls: list[tuple[str, str]] = []

    async def count_tasks_by_docs(self, *, user_id, doc_ids):
        self.count_calls.append((user_id, list(doc_ids)))
        if self.fail_counts:
            raise RuntimeError("db down: task counts")
        by_doc = self.counts.get(user_id, {})
        return {d: by_doc[d] for d in doc_ids if d in by_doc}

    async def list_by_doc(self, *, user_id, doc_id, limit=20):
        self.list_calls.append((user_id, doc_id))
        if self.fail_tasks:
            raise RuntimeError("db down: task list")
        return [dict(r) for r in self.tasks.get(doc_id, [])][:limit]


def _wire(
    monkeypatch,
    store,
    svc,
    quota,
    user_id="u-1",
    *,
    cfg=("cloud", "test-tok"),
    cfg_src="env",
    tasks_store=None,
):
    monkeypatch.setattr(docs_mod, "get_doc_store", lambda: store)
    monkeypatch.setattr(docs_mod, "get_doc_parse_service", lambda: svc)
    monkeypatch.setattr(docs_mod, "get_doc_quota", lambda: quota)
    fake_tasks = tasks_store if tasks_store is not None else FakeTaskStore()
    monkeypatch.setattr(docs_mod, "get_mining_task_store", lambda: fake_tasks)
    resolver = FakeConfigResolver(cfg, src=cfg_src)
    monkeypatch.setattr(docs_mod, "resolve_effective_mineru_config", resolver)
    _auth_as(monkeypatch, user_id)
    return resolver


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
    # 行上带 tenant：重启续轮询时凭 (user_id, tenant_id) 重读用户 Token
    assert store.created[0]["tenant_id"] == "t-1"
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
    """C1：Content-Length 超「合计上限+multipart 余量」→ 解析前 413。"""
    monkeypatch.setenv(docs_mod.MAX_UPLOAD_ENV, "1")
    monkeypatch.setenv(docs_mod.MAX_TOTAL_UPLOAD_ENV, "1")
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
async def test_upload_declared_over_single_cap_but_under_total_passes_coarse(
    monkeypatch, tmp_path: Path
) -> None:
    """粗闸按**合计**上限：声明值超单文件上限不等于拒——多文件合计合法超它。"""
    monkeypatch.setenv(docs_mod.MAX_UPLOAD_ENV, "1")
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    _wire(monkeypatch, store, svc, FakeQuota())
    req = _upload(
        PDF_BYTES,
        "p.pdf",
        declared_length=3 * 1024 * 1024,  # > 1MB 单件上限，< 默认 500MB 合计
    )

    out = await docs_mod.upload_doc(request=req)

    assert out["code"] == 200
    assert req.form_called == 1, "合计上限内必须放行进解析"
    assert store.created != []


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
    resolver = _wire(monkeypatch, store, svc, quota)
    req = _upload(PDF_BYTES, "p.pdf")

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=req)
    assert ei.value.status_code == 429
    assert quota.rate_checks == [("u-1", "upload")]
    assert req.form_called == 0 and store.created == []
    assert resolver.calls == [], "被限流的请求连 Token 预检都不做"


@pytest.mark.asyncio
async def test_upload_rejects_unknown_content_and_cleans_dir(
    monkeypatch, tmp_path: Path
) -> None:
    """防改名可执行文件：内容任何嗅探都不命中 → 400，不留垃圾目录。"""
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    _wire(monkeypatch, store, svc, FakeQuota())

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=_upload(ELF_BYTES, "fake.pdf"))
    assert ei.value.status_code == 400
    assert "无法识别" in str(ei.value.detail)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"%PDF-1.4\n", ".pdf"),
        (b"%pdf-1.4\n", ".pdf"),  # 大小写不敏感
        (b"\x89PNG\r\n\x1a\n\x00\x00", ".png"),
        (b"\xff\xd8\xff\xe0\x00\x10", ".jpeg"),
        (b"GIF87a\x00", ".gif"),
        (b"GIF89a\x00", ".gif"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", ".webp"),
        (b"BM\x36\x00\x00\x00", ".bmp"),
        (b"II*\x00\x10\x00\x00\x00", ".tiff"),
        (b"MM\x00*\x00\x00\x00\x10", ".tiff"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00", ".heic"),
        (b"\x00\x00\x00\x18ftypmif1\x00\x00", ".heic"),
        (b"\x00\x00\x00\x18ftypavif\x00\x00", ".avif"),
        (b"PK\x03\x04\x14\x00", ".zip"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", ".ole"),
        (b"RIFF\x24\x00\x00\x00WAVEfmt ", None),  # RIFF 但不是 WEBP
        (ELF_BYTES, None),
        (b"", None),
    ],
)
def test_sniff_content_type(head: bytes, expected: str | None) -> None:
    assert docs_mod.sniff_content_type(head) == expected


@pytest.mark.asyncio
async def test_upload_renamed_png_as_jpg_corrects_ext_and_parses(
    monkeypatch, tmp_path: Path
) -> None:
    """内容即真相：PNG 内容声明 .jpg —— 以实际内容落盘（original.png），照常解析。"""
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    quota = FakeQuota()
    _wire(monkeypatch, store, svc, quota)

    out = await docs_mod.upload_doc(request=_upload(PNG_BYTES, "照片.jpg"))

    doc = out["data"]["doc"]
    assert out["code"] == 200 and out["data"]["reused"] is False
    assert doc["ext"] == ".png"
    assert store.created[0]["ext"] == ".png"
    doc_dir = tmp_path / doc["doc_id"]
    assert (doc_dir / "original.png").read_bytes() == PNG_BYTES
    assert not (doc_dir / "original.jpg").exists(), "声明尾缀不留残影"
    assert len(svc.submitted) == 1
    assert quota.guards == [("u-1", 1)], "转正后按图片 1 页预估"


@pytest.mark.asyncio
async def test_upload_renamed_jpeg_as_png_corrects_ext(
    monkeypatch, tmp_path: Path
) -> None:
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    _wire(monkeypatch, store, svc, FakeQuota())

    out = await docs_mod.upload_doc(request=_upload(JPEG_BYTES, "scan.png"))
    doc = out["data"]["doc"]
    assert doc["ext"] == ".jpeg"
    assert (tmp_path / doc["doc_id"] / "original.jpeg").read_bytes() == JPEG_BYTES


@pytest.mark.parametrize(
    ("fmt", "filename"), [("WEBP", "图.webp"), ("GIF", "动图.gif")]
)
@pytest.mark.asyncio
async def test_upload_native_transcode_formats_to_png(
    monkeypatch, tmp_path: Path, fmt: str, filename: str
) -> None:
    """WebP/GIF 等 MinerU 直吃不下的格式：服务端解首帧转 PNG 再提交。

    文件行 sha256 必须是**转码后**字节的哈希——复用键跟着真正入解析链的
    那份内容走，否则同图重传既不复用也无法对账。
    """
    data = _pillow_bytes(fmt)
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    quota = FakeQuota()
    _wire(monkeypatch, store, svc, quota)

    out = await docs_mod.upload_doc(request=_upload(data, filename))

    doc = out["data"]["doc"]
    assert out["code"] == 200
    assert doc["ext"] == ".png" and store.created[0]["ext"] == ".png"
    doc_dir = tmp_path / doc["doc_id"]
    png_path = doc_dir / "original.png"
    png_bytes = png_path.read_bytes()
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert png_path.with_suffix(f".{fmt.lower()}").exists() is False
    from PIL import Image

    with Image.open(io.BytesIO(png_bytes)) as im:
        assert im.size == (4, 4)
    assert store.created[0]["sha256"] == hashlib.sha256(png_bytes).hexdigest()
    assert quota.guards == [("u-1", 1)]


@pytest.mark.asyncio
async def test_upload_renamed_webp_as_jpg_transcoded_to_png(
    monkeypatch, tmp_path: Path
) -> None:
    """用户实测形状：WebP 内容顶着 .jpg 名字 —— 转码 + 纠尾缀，一条链走通。"""
    data = _pillow_bytes("WEBP")
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    _wire(monkeypatch, store, svc, FakeQuota())

    out = await docs_mod.upload_doc(request=_upload(data, "截屏 2026.jpg"))
    doc = out["data"]["doc"]
    assert out["code"] == 200
    assert doc["ext"] == ".png"
    assert (
        (tmp_path / doc["doc_id"] / "original.png")
        .read_bytes()
        .startswith(b"\x89PNG\r\n\x1a\n")
    )


@pytest.mark.parametrize("filename", ["IMG_0001.jpg", "IMG_0001.heic"])
@pytest.mark.asyncio
async def test_upload_heic_rejected_with_actionable_message(
    monkeypatch, tmp_path: Path, filename: str
) -> None:
    """HEIC 可识别但不可直接解析：给出「导出为 PNG/JPG」的可操作报错，不留垃圾。"""
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    _wire(monkeypatch, store, svc, FakeQuota())

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=_upload(HEIC_BYTES, filename))
    detail = str(ei.value.detail)
    assert ei.value.status_code == 400
    assert "HEIC" in detail and "PNG" in detail
    assert store.created == [] and list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_upload_office_zip_renamed_pdf_rejected(
    monkeypatch, tmp_path: Path
) -> None:
    """zip 内容顶着 .pdf：容器族只能靠声明定形，声明不符一律拒绝（防换头）。"""
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    _wire(monkeypatch, store, svc, FakeQuota())

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=_upload(DOCX_BYTES, "paper.pdf"))
    assert ei.value.status_code == 400
    assert "不符" in str(ei.value.detail)
    assert store.created == [] and list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_upload_multi_renamed_parts_correct_ext_and_hash_stored_bytes(
    monkeypatch, tmp_path: Path
) -> None:
    """多文件逐件纠尾缀/转码；复用键按转码后字节算（逐件对齐）。"""
    webp = _pillow_bytes("WEBP")
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    _wire(monkeypatch, store, svc, FakeQuota())

    out = await docs_mod.upload_doc(
        request=_upload_many([(PNG_BYTES, "正文.jpg"), (webp, "附录.png")])
    )
    doc = out["data"]["doc"]
    assert out["code"] == 200 and doc["ext"] == ".png"
    doc_dir = tmp_path / doc["doc_id"]
    p1 = doc_dir / "originals" / "p1.png"
    p2 = doc_dir / "originals" / "p2.png"
    assert p1.read_bytes() == PNG_BYTES
    assert p2.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    manifest = store.created[0]["original_paths"]
    assert [p["ext"] for p in manifest] == [".png", ".png"]
    assert [p["name"] for p in manifest] == ["正文.jpg", "附录.png"]
    expected = hashlib.sha256()
    for name, path in (("正文.jpg", p1), ("附录.png", p2)):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        expected.update(f"{name}\0{digest}\0".encode())
    assert store.created[0]["sha256"] == expected.hexdigest()


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
    """任何来源（因子挖掘设置 / env）都没配通道：落盘之前 503，并说清去哪配。"""
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    resolver = _wire(monkeypatch, store, svc, FakeQuota(), cfg=None, cfg_src="none")

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=_upload(PDF_BYTES, "p.pdf"))
    assert ei.value.status_code == 503
    assert "MINERU_API_TOKEN" in str(ei.value.detail)
    assert "因子挖掘" in str(ei.value.detail), "必须给出用户自助配置入口"
    assert "个人中心" not in str(ei.value.detail), "配置入口已迁出用户中心"
    assert resolver.calls == [("u-1", "t-1", False)], "预检按有效配置口径"
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


# ── 上传：多文件（T-FM-19a） ────────────────────────────────────────


def test_composite_sha256_order_and_name_sensitive() -> None:
    """复用键顺序/文件名敏感：换序或改名 = 另一份文档（不复用）。"""
    a = ("正文.pdf", "a" * 64)
    b = ("附录.pdf", "b" * 64)
    assert docs_mod._composite_sha256([a, b]) == docs_mod._composite_sha256([a, b])
    assert docs_mod._composite_sha256([a, b]) != docs_mod._composite_sha256([b, a])
    renamed = ("正文(1).pdf", "a" * 64)
    assert docs_mod._composite_sha256([a, b]) != docs_mod._composite_sha256(
        [renamed, b]
    )


@pytest.mark.asyncio
async def test_upload_multi_files_records_manifest_and_reserves_summed_pages(
    monkeypatch, tmp_path: Path
) -> None:
    """多文件：originals/p{i}{ext} 落盘、manifest/files_count 落行、页数求和预留。"""
    events: list[str] = []
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store, events=events)
    quota = FakeQuota(events=events)
    _wire(monkeypatch, store, svc, quota)
    body = _blank_pdf_bytes(3)
    annex = _blank_pdf_bytes(2)

    out = await docs_mod.upload_doc(
        request=_upload_many([(body, "正文.pdf"), (annex, "附录.pdf")])
    )

    doc_id = store.created[0]["doc_id"]
    doc = out["data"]["doc"]
    assert out["code"] == 200 and out["data"]["reused"] is False
    assert doc["filename"] == "正文.pdf（共2个文件）"
    assert doc["files_count"] == 2
    assert doc["size_bytes"] == len(body) + len(annex)
    # manifest：顺序=上传顺序，路径 originals/p{i}{ext}（与 MinerU 部件序号对齐）
    assert store.created[0]["original_paths"] == [
        {
            "path": str(tmp_path / doc_id / "originals" / "p1.pdf"),
            "name": "正文.pdf",
            "ext": ".pdf",
        },
        {
            "path": str(tmp_path / doc_id / "originals" / "p2.pdf"),
            "name": "附录.pdf",
            "ext": ".pdf",
        },
    ]
    assert store.created[0]["original_path"] == str(
        tmp_path / doc_id / "originals" / "p1.pdf"
    )
    assert store.created[0]["sha256"] == docs_mod._composite_sha256(
        [
            ("正文.pdf", hashlib.sha256(body).hexdigest()),
            ("附录.pdf", hashlib.sha256(annex).hexdigest()),
        ]
    )
    # 落盘
    assert (tmp_path / doc_id / "originals" / "p1.pdf").read_bytes() == body
    assert (tmp_path / doc_id / "originals" / "p2.pdf").read_bytes() == annex
    # 配额先于提交；页数 = 各件之和（3+2）
    assert events == ["guard", "submit"]
    assert quota.guards == [("u-1", 5)]
    # 提交链拿到的是真 store 形态（JSON 文本 manifest）且能解码出两件
    from backend.services.engine.alpha_agent.doc_store import decode_original_paths

    assert [p["name"] for p in decode_original_paths(svc.submitted[0])] == [
        "正文.pdf",
        "附录.pdf",
    ]
    # API 输出不吐磁盘布局
    assert "original_paths" not in doc and "original_path" not in doc


@pytest.mark.asyncio
async def test_upload_multi_rejects_over_max_files_400_before_disk(
    monkeypatch, tmp_path: Path
) -> None:
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    _wire(monkeypatch, store, svc, FakeQuota())
    files = [(PDF_BYTES, f"p{i}.pdf") for i in range(docs_mod.MAX_DOC_FILES + 1)]

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=_upload_many(files))
    assert ei.value.status_code == 400
    assert str(docs_mod.MAX_DOC_FILES) in str(ei.value.detail)
    assert store.created == [] and list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_upload_multi_total_size_over_cap_413_cleans_dir(
    monkeypatch, tmp_path: Path
) -> None:
    """合计精验：各件都在单件上限内，累计超合计 → 413 且连根清。"""
    monkeypatch.setenv(docs_mod.MAX_UPLOAD_ENV, "1")
    monkeypatch.setenv(docs_mod.MAX_TOTAL_UPLOAD_ENV, "1")
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    _wire(monkeypatch, store, svc, FakeQuota())
    part = b"\x89PNG\r\n\x1a\n" + b"0" * (700 * 1024)

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(
            request=_upload_many(
                [(part, "a.png"), (part, "b.png")],
                declared_length=1_400_000,  # 粗闸（1MB+余量）内，靠逐件累计精验拦
            )
        )
    assert ei.value.status_code == 413
    assert "合计" in str(ei.value.detail)
    assert store.created == [] and list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_upload_multi_magic_failure_midway_cleans_everything(
    monkeypatch, tmp_path: Path
) -> None:
    """第二件内容不可识别：第一件已落盘也要连根清（不留半批孤儿）。"""
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    _wire(monkeypatch, store, svc, FakeQuota())

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(
            request=_upload_many([(PDF_BYTES, "ok.pdf"), (ELF_BYTES, "fake.pdf")])
        )
    assert ei.value.status_code == 400 and "无法识别" in str(ei.value.detail)
    assert store.created == [] and list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_upload_multi_over_page_cap_names_offending_part(
    monkeypatch, tmp_path: Path
) -> None:
    """多文件页数上限逐件判：超限的是哪件要在消息里点名（不然无从拆）。"""
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    quota = FakeQuota()
    _wire(monkeypatch, store, svc, quota)
    over = _blank_pdf_bytes(docs_mod.MAX_PAGES_PER_FILE + 1)

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(
            request=_upload_many(
                [(_blank_pdf_bytes(2), "正文.pdf"), (over, "附录.pdf")]
            )
        )
    assert ei.value.status_code == 400
    assert "附录.pdf" in str(ei.value.detail)
    assert str(docs_mod.MAX_PAGES_PER_FILE) in str(ei.value.detail)
    assert quota.guards == [] and svc.submitted == []
    assert len(store.hard_deleted) == 1 and store.rows == {}
    assert list(tmp_path.iterdir()) == []


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


# ── 一文档多方向（T-FM-19b）：task_count / tasks ─────────────────────


@pytest.mark.asyncio
async def test_list_docs_carries_task_count_and_true_zero(monkeypatch) -> None:
    """已挖 2 个方向的带 2；没挖过的带 **0**（确认零，不是缺键）。"""
    store = FakeStore(
        [mk_row(doc_id="d1", user_id="u-1"), mk_row(doc_id="d2", user_id="u-1")]
    )
    tasks_store = FakeTaskStore(counts={"u-1": {"d1": 2}})
    _wire(
        monkeypatch,
        store,
        FakeParseService(Path("/nonexistent")),
        FakeQuota(),
        tasks_store=tasks_store,
    )

    out = await docs_mod.list_docs(
        request=FakeRequest(), status=None, limit=10, offset=0
    )

    by_id = {d["doc_id"]: d for d in out["data"]["items"]}
    assert by_id["d1"]["task_count"] == 2
    assert by_id["d2"]["task_count"] == 0
    # 一次批量查询覆盖整页（不是每行一发）
    assert tasks_store.count_calls == [("u-1", ["d2", "d1"])]


@pytest.mark.asyncio
async def test_list_docs_task_count_failure_omits_key_not_zero(monkeypatch) -> None:
    """计数查询失败 → 键整体缺省：宁可不显示，绝不冒充 0（0=确认没挖过）。"""
    store = FakeStore([mk_row(doc_id="d1", user_id="u-1")])
    _wire(
        monkeypatch,
        store,
        FakeParseService(Path("/nonexistent")),
        FakeQuota(),
        tasks_store=FakeTaskStore(fail_counts=True),
    )

    out = await docs_mod.list_docs(
        request=FakeRequest(), status=None, limit=10, offset=0
    )

    assert out["data"]["total"] == 1, "任务数坏掉不拦文档列表"
    assert "task_count" not in out["data"]["items"][0]


@pytest.mark.asyncio
async def test_get_doc_detail_includes_tasks_and_true_count(monkeypatch) -> None:
    """详情带明细（最近优先由 store 保证）与**真实总数**——明细截断时靠它报数。"""
    store = FakeStore([mk_row(doc_id="d1", user_id="u-1", status="organized")])
    tasks_store = FakeTaskStore(
        counts={"u-1": {"d1": 3}},
        tasks={
            "d1": [
                {
                    "task_id": "t-2",
                    "status": "running",
                    "direction": "动量 × 波动率",
                    "created_at": NOW,
                },
                {
                    "task_id": "t-1",
                    "status": "completed",
                    "direction": "复现论文《X》",
                    "created_at": NOW,
                },
            ]
        },
    )
    _wire(
        monkeypatch,
        store,
        FakeParseService(Path("/nonexistent")),
        FakeQuota(),
        tasks_store=tasks_store,
    )

    out = await docs_mod.get_doc_detail(request=FakeRequest(), doc_id="d1")

    assert out["data"]["doc"]["task_count"] == 3, "总数是 COUNT，不是明细长度"
    tasks = out["data"]["tasks"]
    assert [t["task_id"] for t in tasks] == ["t-2", "t-1"]
    assert tasks[0]["status"] == "running" and tasks[1]["direction"] == "复现论文《X》"
    assert tasks_store.list_calls == [("u-1", "d1")]


@pytest.mark.asyncio
async def test_get_doc_detail_task_queries_failure_omits_keys(monkeypatch) -> None:
    """计数/明细各自独立降级：doc 照常返回，坏掉的那部分键缺省。"""
    store = FakeStore([mk_row(doc_id="d1", user_id="u-1")])
    _wire(
        monkeypatch,
        store,
        FakeParseService(Path("/nonexistent")),
        FakeQuota(),
        tasks_store=FakeTaskStore(fail_counts=True, fail_tasks=True),
    )

    out = await docs_mod.get_doc_detail(request=FakeRequest(), doc_id="d1")

    assert out["data"]["doc"]["doc_id"] == "d1"
    assert "task_count" not in out["data"]["doc"]
    assert "tasks" not in out["data"]


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
    _wire(
        monkeypatch,
        FakeStore(),
        FakeParseService(Path("/n")),
        quota,
        cfg=None,
        cfg_src="none",
    )

    out = await docs_mod.doc_quota_status(request=FakeRequest())

    data = out["data"]
    assert data["day"] == "20261009" and data["user_used"] == 12
    assert data["user_remaining"] == 188 and data["platform_remaining"] == 960
    assert data["exhausted"] is False and data["warning"] is False
    assert data["token_configured"] is False
    assert quota.status_user == "u-1"


@pytest.mark.asyncio
async def test_quota_token_configured_uses_effective_token(monkeypatch) -> None:
    """token_configured 是「有效 Token」口径（用户自带也算已配置），按身份解析。"""
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
    resolver = _wire(
        monkeypatch,
        FakeStore(),
        FakeParseService(Path("/n")),
        FakeQuota(status_obj=st),
        cfg=("cloud", "user-tok"),
        cfg_src="user",
    )

    data = (await docs_mod.doc_quota_status(request=FakeRequest()))["data"]

    assert data["token_configured"] is True
    assert data["mineru_mode"] == "cloud"
    assert resolver.calls == [("u-1", "t-1", False)]


@pytest.mark.asyncio
async def test_quota_endpoint_reports_cloud_mode_by_default(monkeypatch) -> None:
    monkeypatch.delenv("MINERU_MODE", raising=False)
    _wire(
        monkeypatch,
        FakeStore(),
        FakeParseService(Path("/n")),
        FakeQuota(
            status_obj=QuotaStatus(
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
        ),
    )

    data = (await docs_mod.doc_quota_status(request=FakeRequest()))["data"]
    assert data["mineru_mode"] == "cloud"


@pytest.mark.asyncio
async def test_quota_endpoint_reports_local_channel_ready_and_mode(monkeypatch) -> None:
    """本地通道就绪：token_configured=True 且 mineru_mode=local（前端据此把
    页数配额显示为「不限」——本地解析不烧平台云配额）。"""
    monkeypatch.setenv("MINERU_MODE", "local")
    monkeypatch.setenv("MINERU_LOCAL_URL", "http://192.168.31.9:8000")

    def _quota_stub() -> FakeQuota:
        return FakeQuota(
            status_obj=QuotaStatus(
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
        )

    resolver = _wire(
        monkeypatch,
        FakeStore(),
        FakeParseService(Path("/n")),
        _quota_stub(),
        cfg=("local", "http://192.168.31.9:8000"),
        cfg_src="env",
    )

    data = (await docs_mod.doc_quota_status(request=FakeRequest()))["data"]
    assert data["mineru_mode"] == "local"
    assert data["token_configured"] is True
    assert resolver.calls == [("u-1", "t-1", False)], "按身份解析（本地/云端同口径）"


@pytest.mark.asyncio
async def test_upload_503_channel_unconfigured_points_at_local_env(
    monkeypatch, tmp_path: Path
) -> None:
    """本地模式没配 MINERU_LOCAL_URL（= 未配置任何通道）：落盘之前 503，
    文案同时给出设置页入口与本地配置键。"""
    monkeypatch.setenv("MINERU_MODE", "local")
    monkeypatch.delenv("MINERU_LOCAL_URL", raising=False)
    store = FakeStore()
    svc = FakeParseService(tmp_path, store=store)
    resolver = _wire(monkeypatch, store, svc, FakeQuota(), cfg=None, cfg_src="none")

    with pytest.raises(HTTPException) as ei:
        await docs_mod.upload_doc(request=_upload(PDF_BYTES, "p.pdf"))
    assert ei.value.status_code == 503
    assert "MINERU_LOCAL_URL" in str(ei.value.detail)
    assert "因子挖掘" in str(ei.value.detail), "设置页入口（本地/云端都在这配）"
    assert resolver.calls == [("u-1", "t-1", False)]
    assert store.created == [] and list(tmp_path.iterdir()) == []


# ── MinerU 解析设置（因子挖掘内，2026-10-09 自用户中心迁入） ────────


class FakeSettingsStore:
    """设置存储替身：读（可注入故障）/写（可注入校验错）/清。"""

    def __init__(self, settings=None, *, read_error=None, save_error=None) -> None:
        self.settings = settings
        self.read_error = read_error
        self.save_error = save_error
        self.saved: list[dict] = []
        self.cleared = 0

    def get(self, user_id, tenant_id, *, strict=False):
        if self.read_error is not None:
            raise self.read_error
        return self.settings

    def save(self, user_id, tenant_id, payload):
        if self.save_error is not None:
            raise self.save_error
        self.saved.append({"user_id": user_id, "tenant_id": tenant_id, **payload})

    def clear(self, user_id, tenant_id):
        self.cleared += 1
        return True


def _wire_settings(
    monkeypatch, settings_store, *, env_cfg=None, cfg=None, cfg_src="none"
):
    monkeypatch.setattr(
        docs_mod, "get_doc_mining_settings_store", lambda: settings_store
    )
    monkeypatch.setattr(docs_mod, "resolve_mineru_config", lambda: env_cfg)
    resolver = FakeConfigResolver(cfg, src=cfg_src)
    monkeypatch.setattr(docs_mod, "resolve_effective_mineru_config", resolver)
    _auth_as(monkeypatch)
    return resolver


@pytest.mark.asyncio
async def test_get_mineru_settings_masks_secrets_and_reports_sources(
    monkeypatch,
) -> None:
    from backend.services.engine.alpha_agent.doc_mining_settings import (
        MineruUserSettings,
    )

    store = FakeSettingsStore(
        MineruUserSettings(
            mode="cloud",
            api_token="user-token-abcdef123456",
            local_url="http://10.0.0.5:8000",
            local_api_key="localkey-1234567890",
            local_tier="standard",
        )
    )
    _wire_settings(
        monkeypatch,
        store,
        env_cfg=MineruConfig(token="env-tok"),  # env 概览仅展示，不参与来源
        cfg=("cloud", "user-token-abcdef123456"),
        cfg_src="user",
    )

    data = (await docs_mod.get_mineru_settings(request=FakeRequest()))["data"]

    assert data["readable"] is True
    assert data["source"] == "user"
    assert data["effective_mode"] == "cloud"
    assert data["env_configured"] is True and data["env_mode"] == "cloud"
    s = data["settings"]
    assert s["mode"] == "cloud"
    assert s["api_token_set"] is True
    assert s["api_token_masked"] == "use****3456", "只回掩码，绝不回明文"
    assert s["local_api_key_masked"] == "loc****7890"
    assert s["local_url"] == "http://10.0.0.5:8000"
    assert s["local_tier"] == "standard"
    assert "api_token" not in s and "local_api_key" not in s, "公开视图不许带明文字段名"


@pytest.mark.asyncio
async def test_get_mineru_settings_unreadable_is_not_empty_settings(
    monkeypatch,
) -> None:
    """存储读故障 ≠ 没配：readable=False 必须与「空设置」可区分——把故障显示成
    空设置会诱导用户覆盖掉自己已存的密钥（旧 Profile 网关同款纪律）。"""
    from backend.services.engine.alpha_agent.doc_mining_settings import (
        DocMiningSettingsError,
    )

    store = FakeSettingsStore(read_error=DocMiningSettingsError("redis down"))
    _wire_settings(monkeypatch, store)

    data = (await docs_mod.get_mineru_settings(request=FakeRequest()))["data"]

    assert data["readable"] is False
    assert data["settings"] is None
    assert data["source"] == "none" and data["effective_mode"] is None


@pytest.mark.asyncio
async def test_put_mineru_settings_saves_scoped_and_returns_view(monkeypatch) -> None:
    store = FakeSettingsStore()
    _wire_settings(monkeypatch, store, cfg=("cloud", "new-tok"), cfg_src="user")

    out = await docs_mod.save_mineru_settings(
        request=FakeRequest(),
        payload=docs_mod.MineruSettingsPayload(mode="cloud", api_token="new-tok"),
    )

    assert out["code"] == 200
    assert store.saved == [
        {
            "user_id": "u-1",
            "tenant_id": "t-1",
            "mode": "cloud",
            "api_token": "new-tok",
            "local_url": None,
            "local_api_key": None,
            "local_tier": None,
        }
    ], "写侧按 (user, tenant) 收口，payload 原样透传（校验在 store）"
    assert out["data"]["source"] == "user"


@pytest.mark.asyncio
async def test_put_mineru_settings_validation_error_maps_400(monkeypatch) -> None:
    store = FakeSettingsStore(save_error=ValueError("mode 必须是 cloud 或 local"))
    _wire_settings(monkeypatch, store)

    with pytest.raises(HTTPException) as ei:
        await docs_mod.save_mineru_settings(
            request=FakeRequest(),
            payload=docs_mod.MineruSettingsPayload(mode="bogus"),
        )
    assert ei.value.status_code == 400
    assert "mode" in str(ei.value.detail), "校验失败原因要透出（可操作）"


@pytest.mark.asyncio
async def test_put_mineru_settings_store_failure_maps_500_fixed_text(
    monkeypatch,
) -> None:
    store = FakeSettingsStore(save_error=RuntimeError("redis conn refused secret"))
    _wire_settings(monkeypatch, store)

    with pytest.raises(HTTPException) as ei:
        await docs_mod.save_mineru_settings(
            request=FakeRequest(),
            payload=docs_mod.MineruSettingsPayload(mode="cloud", api_token="t"),
        )
    assert ei.value.status_code == 500
    assert ei.value.detail == "保存失败，请稍后重试"
    assert "secret" not in str(ei.value.detail), "内部错误不回显"


@pytest.mark.asyncio
async def test_delete_mineru_settings_clears_and_returns_view(monkeypatch) -> None:
    store = FakeSettingsStore()
    _wire_settings(monkeypatch, store)

    out = await docs_mod.clear_mineru_settings(request=FakeRequest())

    assert out["code"] == 200
    assert store.cleared == 1
    assert out["data"]["settings"] is None


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
    _wire(
        monkeypatch,
        store,
        FakeParseService(Path("/n")),
        FakeQuota(status_obj=st),
        cfg=None,
        cfg_src="none",
    )

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
    _wire(
        monkeypatch,
        store,
        FakeParseService(Path("/n")),
        FakeQuota(status_obj=st),
        cfg=None,
        cfg_src="none",
    )

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
    assert quota.unlocks == [("organize:d1", "tok-1")], "无论成败锁都要释放（带令牌）"
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
    assert svc.scheduled == ["d1"], (
        "批次已建的删除：轮询停了但账不能没人收——补挂只记账收尾盯批次到终态"
    )
    assert quota.released == [], (
        "批次已建（MinerU 已拿到文件、照常计费）→ 不许退预留："
        "上传→秒删循环能在账面为零的情况下烧穿平台额度；账由收尾结到实际页数"
    )


@pytest.mark.asyncio
async def test_delete_uploaded_without_batch_releases_reservation(
    monkeypatch, tmp_path: Path
) -> None:
    """MinerU 从未拿到文件（批次号未落库）→ 预留全额退回（费用没发生）。"""
    doc_dir = tmp_path / "d1"
    doc_dir.mkdir()
    store = FakeStore(
        [mk_row(doc_id="d1", user_id="u-1", status="uploaded", mineru_batch_id=None)]
    )
    svc = FakeParseService(tmp_path)
    quota = FakeQuota()
    _wire(monkeypatch, store, svc, quota)

    out = await docs_mod.delete_doc(request=FakeRequest(), doc_id="d1")

    assert out["data"] == {"doc_id": "d1", "deleted": True}
    assert svc.cancelled == ["d1"]
    assert svc.scheduled == [], "没有批次号就没有可盯的账，不补挂收尾"
    assert quota.released == [("d1", "u-1")], "从未提交 MinerU：预留要退"


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
