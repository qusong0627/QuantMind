"""解析编排 doc_parse_service（T-FM-07）—— PG 权威状态机 + 重启续轮询。

与挖掘链「重启即 failed」不同，本链的可靠性目标是**续跑**：parsing 行是
MinerU 队列里的活任务，engine 重启后必须重新接管轮询，直到 done 落盘。
用例盯的状态机边角：

- running → 更新页进度（进度是给人看的，页数一到手就落库）；
- done → **立即**下载落盘（结果链接无 TTL）→ 白名单解包 → parsed + 记账；
- failed → 保留 MinerU 原文 + 错误码提示（-60006「页数超限」要看得懂）；
- 瞬态异常在轮询层继续等（受总时限约束），永久异常立即定格 parse_failed；
- 总时限到点定格「解析超时」，不许无声挂死；
- sha256 命中已解析文档 → 复制产物复用（配额省了，产物独立生命周期）；
- 行在产物没了 → 不许假装 parsed（用户会拿到打不开的文档）；
- GC 只扫定型行、删目录、转 expired。

单元用例全用内存替身（FakeStore/FakeMineru），文件系统走 tmp_path；
末尾一个真库集成用例走假 MinerU 全生命周期。
"""

from __future__ import annotations

import asyncio
import io
import sys
import uuid
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent.doc_parse_service import (  # noqa: E402
    DocParseService,
    detect_scanned_pdf,
    parse_failure_message,
    user_facing_error,
)
from backend.services.engine.alpha_agent.mineru_client import (  # noqa: E402
    MineruBatchItem,
    MineruError,
    MineruFileSpec,
)

# ── 替身 ────────────────────────────────────────────────────────────


class FakeStore:
    def __init__(self, rows: dict[str, dict] | None = None) -> None:
        self.rows = rows or {}
        self.updates: list[tuple[str, dict]] = []
        self.rejected_writes: list[tuple[str, dict]] = []
        self.marked_expired: list[str] = []
        self.fail_stale_calls: list[int] = []

    async def get_doc(self, doc_id, *, user_id=None):
        row = self.rows.get(doc_id)
        if row is None:
            return None
        if user_id is not None and row.get("user_id") != user_id:
            return None
        return dict(row)

    async def update_doc(self, doc_id, **fields):
        """镜像真库契约（安全审查 H2）：返回是否写中；已软删行拒绝一切写。"""
        row = self.rows.get(doc_id)
        if row is None or row.get("status") == "deleted":
            self.rejected_writes.append((doc_id, dict(fields)))
            return False
        self.updates.append((doc_id, fields))
        row.update(fields)
        return True

    async def find_reusable_parsed(self, user_id, sha256):
        for row in self.rows.values():
            if (
                row.get("user_id") == user_id
                and row.get("sha256") == sha256
                and row.get("status") in ("parsed", "organized")
                and row.get("doc_id") != ""
            ):
                return dict(row)
        return None

    async def list_parsing(self):
        return [dict(r) for r in self.rows.values() if r.get("status") == "parsing"]

    async def fail_stale_uploaded(self, *, older_than_minutes=30, reason=""):
        self.fail_stale_calls.append(older_than_minutes)
        return 0

    async def list_expired_candidates(self, *, retention_days):
        return [dict(r) for r in self.rows.values() if r.get("gc_candidate")]

    async def mark_expired(self, doc_id):
        self.marked_expired.append(doc_id)
        self.rows.setdefault(doc_id, {"doc_id": doc_id})["status"] = "expired"


class FakeMineru:
    def __init__(self) -> None:
        self.batches: list[list[MineruFileSpec]] = []
        self.uploads: list[tuple[str, object]] = []
        self.results_queue: list[object] = []
        self.calls = 0
        self.zip_payload: bytes = b""
        self.download_calls: list[str] = []
        self.download_error: Exception | None = None

    async def create_upload_batch(self, files, **kwargs):
        self.batches.append(list(files))
        return "b-1", ["https://upload.test/0"]

    async def upload_file(self, url, content):
        self.uploads.append((url, content))

    async def get_batch_results(self, batch_id):
        self.calls += 1
        if not self.results_queue:
            return [MineruBatchItem(file_name="paper.pdf", state="running")]
        nxt = self.results_queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def download_zip(self, url, dest, **kwargs):
        self.download_calls.append(url)
        if self.download_error is not None:
            raise self.download_error
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.zip_payload)
        return dest


class FakeQuota:
    """镜像 DocQuota 的 settle/release 契约（H1：预留-结算取代直达记账）。

    ``settle_status``：真 settle 返回结算后的 QuotaStatus（告警钩子拿它判余量），
    默认 None 表示「不关心」——老用例不受影响。
    """

    def __init__(self) -> None:
        self.recorded: list[tuple[str, int]] = []
        self.released: list[tuple[str, str]] = []
        self.committed: list[str] = []
        self.settle_status = None

    def settle(self, doc_id, user_id, pages):
        self.recorded.append((user_id, int(pages)))
        return self.settle_status

    def commit_reservation(self, doc_id):
        self.committed.append(str(doc_id))

    def release(self, doc_id, user_id):
        self.released.append((doc_id, user_id))


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def make_zip(md: bytes = b"# paper", *, images: int = 1) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("full.md", md)
        zf.writestr("paper_content_list.json", b"[]")
        for i in range(images):
            zf.writestr(f"images/f{i}.png", b"\x89PNG")
    return buf.getvalue()


DOC_ID = "d0c1234567890abc"


def mk_doc(tmp_path: Path, **overrides) -> dict:
    original = tmp_path / "original.pdf"
    if not original.exists():
        original.write_bytes(b"%PDF-1.4 fake")
    row = {
        "doc_id": DOC_ID,
        "user_id": "u1",
        "filename": "paper.pdf",
        "ext": ".pdf",
        "sha256": "a" * 64,
        "original_path": str(original),
        "status": "parsing",
        "mineru_batch_id": "b-1",
        "page_count": None,
        "md_path": None,
        "content_list_path": None,
        "parse_state": "pending",
    }
    row.update(overrides)
    return row


def mk_service(
    tmp_path: Path,
    store: FakeStore,
    *,
    client: FakeMineru,
    quota: FakeQuota | None = None,
    clock: FakeClock | None = None,
    poll_interval_s: float = 5.0,
    parse_timeout_s: float = 7200.0,
):
    clock = clock or FakeClock()
    return DocParseService(
        store=store,
        quota=quota or FakeQuota(),
        client=client,
        docs_root=tmp_path,
        poll_interval_s=poll_interval_s,
        parse_timeout_s=parse_timeout_s,
        clock=clock.now,
        sleep=clock.sleep,
    ), clock


# ── 失败文案 ────────────────────────────────────────────────────────


def test_parse_failure_message_keeps_raw_and_adds_code_hint() -> None:
    item = MineruBatchItem(
        file_name="a.pdf", state="failed", err_msg="over page limit (-60006)"
    )
    msg = parse_failure_message(item)
    assert "over page limit" in msg, "MinerU 原文必须保留（排查锚点）"
    assert "200 页" in msg, "常见错误码要有人话提示"

    quota_item = MineruBatchItem(
        file_name="a.pdf", state="failed", err_msg="daily limit (-60018)"
    )
    assert "明日" in parse_failure_message(quota_item)

    plain = MineruBatchItem(file_name="a.pdf", state="failed", err_msg="weird thing")
    assert parse_failure_message(plain) == "weird thing"

    empty = MineruBatchItem(file_name="a.pdf", state="failed", err_msg=None)
    assert "失败" in parse_failure_message(empty)


def test_user_facing_error_keeps_mineru_text_but_strips_paths() -> None:
    """L1：error 字段会经 API 回吐——MinerU 文案保留，本地路径绝不出库。"""
    assert "token" in user_facing_error(
        MineruError("token 无效", code="A0202", retryable=False)
    )

    leaked = user_facing_error(
        FileNotFoundError("/data/rd_agent_docs/abc/original.pdf")
    )
    assert "/data/rd_agent_docs" not in leaked, "绝对路径不许出库/出 API"
    assert "original.pdf" not in leaked
    assert "FileNotFoundError" in leaked, "留类型名给日志锚点"

    oserr = user_facing_error(OSError(28, "No space left on device"))
    assert "ENOSPC" in oserr and "/" not in oserr, "errno 符号名可读且无路径"

    generic = user_facing_error(ValueError("secret-ish detail"))
    assert "secret-ish detail" not in generic
    assert "ValueError" in generic


# ── 扫描件检测 ──────────────────────────────────────────────────────


def _pdf_with_text() -> bytes:
    """手搓一页带文字的 PDF（对象偏移逐个算好，pypdf 严格可解）。"""
    stream = b"BT /F1 12 Tf 20 100 Td (Hello Quant) Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


def test_detect_scanned_pdf_both_ways() -> None:
    from pypdf import PdfWriter

    buf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=72, height=72)
    w.write(buf)
    assert detect_scanned_pdf(buf.getvalue()) is True, "无文本层 = 扫描件 → OCR"

    assert detect_scanned_pdf(_pdf_with_text()) is False, "有文本层 = 电子版 → 不 OCR"
    assert detect_scanned_pdf(b"not a pdf") is None, "读不动 = 不猜（None）"


# ── 轮询状态机 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_once_running_updates_progress(tmp_path: Path) -> None:
    store = FakeStore({DOC_ID: mk_doc(tmp_path)})
    client = FakeMineru()
    client.results_queue = [
        [
            MineruBatchItem(
                file_name="paper.pdf",
                state="running",
                data_id=DOC_ID,
                extracted_pages=3,
                total_pages=10,
            )
        ]
    ]
    svc, _ = mk_service(tmp_path, store, client=client)

    state = await svc._poll_once(DOC_ID)

    assert state == "active"
    doc_id, fields = store.updates[-1]
    assert fields["parse_state"] == "running"
    assert fields["page_count"] == 10
    assert "status" not in fields, "running 不许动 status"


@pytest.mark.asyncio
async def test_poll_once_done_downloads_extracts_and_records_quota(
    tmp_path: Path,
) -> None:
    store = FakeStore({DOC_ID: mk_doc(tmp_path)})
    client = FakeMineru()
    client.zip_payload = make_zip()
    client.results_queue = [
        [
            MineruBatchItem(
                file_name="paper.pdf",
                state="done",
                data_id=DOC_ID,
                full_zip_url="https://cdn.test/a.zip",
                total_pages=7,
            )
        ]
    ]
    quota = FakeQuota()
    svc, _ = mk_service(tmp_path, store, client=client, quota=quota)

    state = await svc._poll_once(DOC_ID)

    assert state == "parsed"
    _, fields = store.updates[-1]
    assert fields["status"] == "parsed"
    assert fields["error"] is None
    md = Path(fields["md_path"])
    assert md.is_file() and md.read_text() == "# paper"
    assert (tmp_path / DOC_ID / "parsed" / "images" / "f0.png").exists()
    assert (tmp_path / DOC_ID / "mineru.zip").exists(), "zip 也要留档（追溯用）"
    assert quota.recorded == [("u1", 7)]
    assert quota.released == [], "成功落盘只结算不释放"


@pytest.mark.asyncio
async def test_poll_once_failed_marks_error_with_hint(tmp_path: Path) -> None:
    store = FakeStore({DOC_ID: mk_doc(tmp_path)})
    client = FakeMineru()
    client.results_queue = [
        [
            MineruBatchItem(
                file_name="paper.pdf",
                state="failed",
                data_id=DOC_ID,
                err_msg="over page limit (-60006)",
                total_pages=250,
            )
        ]
    ]
    quota = FakeQuota()
    svc, _ = mk_service(tmp_path, store, client=client, quota=quota)

    state = await svc._poll_once(DOC_ID)

    assert state == "parse_failed"
    _, fields = store.updates[-1]
    assert fields["status"] == "parse_failed"
    assert "200 页" in fields["error"]
    assert fields["parse_state"] == "failed"
    assert quota.recorded == [], "失败没有产物，不许记页数"
    assert quota.released == [(DOC_ID, "u1")], "失败必须全额退预留"


@pytest.mark.asyncio
async def test_poll_once_done_without_url_marks_failed(tmp_path: Path) -> None:
    store = FakeStore({DOC_ID: mk_doc(tmp_path)})
    client = FakeMineru()
    client.results_queue = [
        [MineruBatchItem(file_name="paper.pdf", state="done", data_id=DOC_ID)]
    ]
    quota = FakeQuota()
    svc, _ = mk_service(tmp_path, store, client=client, quota=quota)

    assert await svc._poll_once(DOC_ID) == "parse_failed"
    _, fields = store.updates[-1]
    assert "full_zip_url" in fields["error"]
    assert quota.released == [(DOC_ID, "u1")], "拿不到产物就全退"


@pytest.mark.asyncio
async def test_poll_once_download_failure_settles_actual_without_release(
    tmp_path: Path,
) -> None:
    """done 了但下载/解包失败：MinerU 真扣了页 → 按实际结算，不再退预留给用户。"""
    store = FakeStore({DOC_ID: mk_doc(tmp_path)})
    client = FakeMineru()
    client.download_error = MineruError("解包白名单拒绝", retryable=False)
    client.results_queue = [
        [
            MineruBatchItem(
                file_name="paper.pdf",
                state="done",
                data_id=DOC_ID,
                full_zip_url="https://cdn.test/a.zip",
                total_pages=6,
            )
        ]
    ]
    quota = FakeQuota()
    svc, _ = mk_service(tmp_path, store, client=client, quota=quota)

    assert await svc._poll_once(DOC_ID) == "parse_failed"
    assert quota.recorded == [("u1", 6)], "页已扣：按实际结算"
    assert quota.released == [], "不许再退（会把真实消耗洗掉）"
    _, fields = store.updates[-1]
    assert "产物下载/解包失败" in fields["error"]


@pytest.mark.asyncio
async def test_settle_quota_hands_status_to_alert_hook(
    tmp_path: Path, monkeypatch
) -> None:
    """余量告警挂在结算点（T-FM-14）：settle 返回的状态原样递钩子（to_thread）；
    结算抛错则没有状态可递——不告警、不上抛。"""
    from backend.services.engine.alpha_agent import doc_parse_service as parse_mod

    alerts: list = []
    monkeypatch.setattr(
        parse_mod,
        "maybe_alert_quota_low",
        lambda st, *, quota: alerts.append((st, quota)),
    )
    doc = mk_doc(tmp_path)

    quota = FakeQuota()
    sentinel = object()
    quota.settle_status = sentinel
    svc, _ = mk_service(tmp_path, FakeStore(), client=FakeMineru(), quota=quota)

    await svc._alert_quota_low(svc._settle_quota(doc, 7))
    assert quota.recorded == [("u1", 7)]
    assert alerts == [(sentinel, quota)], "结算状态要带着同一个 quota 实例递钩子"

    class Boom(FakeQuota):
        def settle(self, doc_id, user_id, pages):
            raise RuntimeError("redis down")

    svc2, _ = mk_service(tmp_path, FakeStore(), client=FakeMineru(), quota=Boom())
    await svc2._alert_quota_low(svc2._settle_quota(doc, 7))  # 不抛
    assert len(alerts) == 1, "结算失败没有状态可递，不许告警"


@pytest.mark.asyncio
async def test_settle_or_commit_quota_commits_when_pages_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    """done 但上游没报页数：页数不可知，预留转已用留在账上，不许 settle(0) 全退。"""
    from backend.services.engine.alpha_agent import doc_parse_service as parse_mod

    alerts: list = []
    monkeypatch.setattr(
        parse_mod,
        "maybe_alert_quota_low",
        lambda st, *, quota: alerts.append(st),
    )
    quota = FakeQuota()
    svc, _ = mk_service(tmp_path, FakeStore(), client=FakeMineru(), quota=quota)
    doc = mk_doc(tmp_path)

    await svc._settle_or_commit_quota(doc, 0)

    assert quota.committed == [DOC_ID], "页数不可知 → 预留转已用（费用已发生）"
    assert quota.recorded == [], "不许 settle(0) 把已发生的费用洗成 0"
    assert alerts == [], "commit 没有状态可递，不判告警"


@pytest.mark.asyncio
async def test_finish_done_deleted_during_download_discards_artifacts(
    tmp_path: Path,
) -> None:
    """H2：下载窗口内被删除 → parsed 写被守卫拦下，产物再清一次，不结算不复活。"""
    store = FakeStore({DOC_ID: mk_doc(tmp_path)})
    quota = FakeQuota()

    class DeletingZipClient(FakeMineru):
        async def download_zip(self, url, dest, **kwargs):
            await super().download_zip(url, dest, **kwargs)
            store.rows[DOC_ID]["status"] = "deleted"  # 下载慢，用户此刻点了删除

    client = DeletingZipClient()
    client.zip_payload = make_zip()
    client.results_queue = [
        [
            MineruBatchItem(
                file_name="paper.pdf",
                state="done",
                data_id=DOC_ID,
                full_zip_url="https://cdn.test/a.zip",
                total_pages=7,
            )
        ]
    ]
    svc, _ = mk_service(tmp_path, store, client=client, quota=quota)

    assert await svc._poll_once(DOC_ID) == "gone"
    assert store.rows[DOC_ID]["status"] == "deleted", "已删行绝不写回 parsed"
    assert not (tmp_path / DOC_ID).exists(), "写被拦下后产物必须再清一次"
    assert quota.recorded == [], "行都没了不许结算（删除端点已退预留）"
    assert quota.released == [], "删除端点退过一次，这里不许退第二次"


@pytest.mark.asyncio
async def test_poll_once_missing_batch_id_marks_failed(tmp_path: Path) -> None:
    store = FakeStore({DOC_ID: mk_doc(tmp_path, mineru_batch_id=None)})
    client = FakeMineru()
    svc, _ = mk_service(tmp_path, store, client=client)

    assert await svc._poll_once(DOC_ID) == "parse_failed"
    _, fields = store.updates[-1]
    assert "批次" in fields["error"]
    assert client.calls == 0, "没有批次号就不该打 MinerU"


@pytest.mark.asyncio
async def test_poll_once_gone_when_row_not_parsing(tmp_path: Path) -> None:
    store = FakeStore({DOC_ID: mk_doc(tmp_path, status="deleted")})
    client = FakeMineru()
    svc, _ = mk_service(tmp_path, store, client=client)

    assert await svc._poll_once(DOC_ID) == "gone"
    assert client.calls == 0
    assert store.updates == []


@pytest.mark.asyncio
async def test_poll_once_item_matched_by_filename_when_data_id_missing(
    tmp_path: Path,
) -> None:
    store = FakeStore({DOC_ID: mk_doc(tmp_path)})
    client = FakeMineru()
    client.results_queue = [
        [MineruBatchItem(file_name="paper.pdf", state="running", data_id=None)]
    ]
    svc, _ = mk_service(tmp_path, store, client=client)
    assert await svc._poll_once(DOC_ID) == "active"


@pytest.mark.asyncio
async def test_poll_once_no_matching_item_keeps_waiting(tmp_path: Path) -> None:
    store = FakeStore({DOC_ID: mk_doc(tmp_path)})
    client = FakeMineru()
    client.results_queue = [[MineruBatchItem(file_name="other.pdf", state="running")]]
    svc, _ = mk_service(tmp_path, store, client=client)

    assert await svc._poll_once(DOC_ID) == "pending"
    assert store.updates == [], "认不出自己的条目：只等待不写状态"


# ── 轮询循环：重试 / 超时 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_loop_retries_transient_then_parses(tmp_path: Path) -> None:
    store = FakeStore({DOC_ID: mk_doc(tmp_path)})
    client = FakeMineru()
    client.zip_payload = make_zip()
    transient = MineruError("网络错误", retryable=True)
    client.results_queue = [
        transient,
        transient,
        [
            MineruBatchItem(
                file_name="paper.pdf",
                state="done",
                data_id=DOC_ID,
                full_zip_url="https://cdn.test/a.zip",
                total_pages=3,
            )
        ],
    ]
    svc, clock = mk_service(
        tmp_path, store, client=client, poll_interval_s=2.0, parse_timeout_s=100.0
    )

    await svc._poll_loop(DOC_ID)

    assert store.rows[DOC_ID]["status"] == "parsed"
    assert clock.slept == [2.0, 2.0], "瞬态异常按轮询节拍再来，不空转"


@pytest.mark.asyncio
async def test_poll_loop_permanent_error_stops_immediately(tmp_path: Path) -> None:
    store = FakeStore({DOC_ID: mk_doc(tmp_path)})
    client = FakeMineru()
    client.results_queue = [MineruError("结构异常", retryable=False)]
    svc, clock = mk_service(tmp_path, store, client=client)

    await svc._poll_loop(DOC_ID)

    assert store.rows[DOC_ID]["status"] == "parse_failed"
    assert "结构异常" in store.rows[DOC_ID]["error"]
    assert clock.slept == [], "永久错误不许再睡再试"


@pytest.mark.asyncio
async def test_poll_loop_timeout_marks_failed(tmp_path: Path) -> None:
    store = FakeStore({DOC_ID: mk_doc(tmp_path)})
    client = FakeMineru()  # 永远 running
    svc, clock = mk_service(
        tmp_path, store, client=client, poll_interval_s=1.0, parse_timeout_s=2.5
    )

    await svc._poll_loop(DOC_ID)

    assert store.rows[DOC_ID]["status"] == "parse_failed"
    assert "超时" in store.rows[DOC_ID]["error"]
    assert client.calls == 3, "t=0/1/2 三次轮询后到点定格"


# ── 提交 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_parse_creates_batch_uploads_and_spawns(tmp_path: Path) -> None:
    doc = mk_doc(tmp_path, status="uploaded", mineru_batch_id=None)
    store = FakeStore({DOC_ID: doc})
    client = FakeMineru()
    svc, _ = mk_service(tmp_path, store, client=client)

    await svc.submit_parse(doc)

    specs = client.batches[0]
    assert specs[0].name == "paper.pdf"
    assert specs[0].data_id == DOC_ID
    assert specs[0].is_ocr is True, (
        "读不动的 PDF（测试件就是垃圾字节）必须保守送 OCR："
        "漏 OCR 的扫描件会产出空壳文本，多 OCR 只是慢"
    )
    url, content = client.uploads[0]
    assert url == "https://upload.test/0"
    assert Path(str(content)).name == "original.pdf"

    _, fields = store.updates[-1]
    assert fields["status"] == "parsing"
    assert fields["mineru_batch_id"] == "b-1"
    assert DOC_ID in svc._tasks, "提交成功必须挂上续命轮询"

    await svc.shutdown()
    assert svc._tasks == {}


@pytest.mark.asyncio
async def test_submit_parse_failure_marks_row_and_raises(tmp_path: Path) -> None:
    doc = mk_doc(tmp_path, status="uploaded", mineru_batch_id=None)
    store = FakeStore({DOC_ID: doc})

    class BoomClient(FakeMineru):
        async def create_upload_batch(self, files, **kwargs):
            raise MineruError("token 无效", code="A0202", retryable=False)

    quota = FakeQuota()
    svc, _ = mk_service(tmp_path, store, client=BoomClient(), quota=quota)

    with pytest.raises(MineruError):
        await svc.submit_parse(doc)

    _, fields = store.updates[-1]
    assert fields["status"] == "parse_failed"
    assert "token" in fields["error"]
    assert svc._tasks == {}, "提交失败不该挂轮询"
    assert quota.released == [(DOC_ID, "u1")], "提交失败全额退预留"


@pytest.mark.asyncio
async def test_submit_parse_deleted_during_upload_aborts_and_commits(
    tmp_path: Path,
) -> None:
    """H2：上传窗口内被删除 → 写 parsing 被守卫拦下，清目录、预留转已用
    （MinerU 已拿到文件会照常计费）、不挂轮询。"""
    doc = mk_doc(tmp_path, status="uploaded", mineru_batch_id=None)
    store = FakeStore({DOC_ID: dict(doc)})
    quota = FakeQuota()
    doc_dir = tmp_path / DOC_ID
    doc_dir.mkdir(parents=True)
    (doc_dir / "original.pdf").write_bytes(b"%PDF-1.4 fake")

    class DeletingClient(FakeMineru):
        async def upload_file(self, url, content):
            await super().upload_file(url, content)
            store.rows[DOC_ID]["status"] = "deleted"  # 上传慢，用户此刻点了删除

    svc, _ = mk_service(tmp_path, store, client=DeletingClient(), quota=quota)

    await svc.submit_parse(doc)  # 不抛：放弃解析是正常收尾

    assert store.rows[DOC_ID]["status"] == "deleted", "已删行不许被写回 parsing"
    assert not doc_dir.exists(), "上传窗口放弃解析必须清目录"
    assert quota.committed == [DOC_ID], "MinerU 已拿到文件：预留转已用，不许全退"
    assert quota.released == []
    assert svc._tasks == {}, "放弃解析不许挂轮询（挂上就是删后复活）"
    assert any("parsing" == f.get("status") for _, f in store.rejected_writes)


# ── sha256 幂等复用 ─────────────────────────────────────────────────


def _mk_donor(
    tmp_path: Path, doc_id: str = "donor0000000000"
) -> tuple[FakeStore, dict]:
    parsed = tmp_path / doc_id / "parsed"
    (parsed / "images").mkdir(parents=True)
    (parsed / "full.md").write_text("# donor content")
    (parsed / "donor0000000000_content_list.json").write_text("[]")
    (parsed / "images" / "f0.png").write_bytes(b"\x89PNG")
    donor = {
        "doc_id": doc_id,
        "user_id": "u1",
        "sha256": "a" * 64,
        "status": "parsed",
        "page_count": 9,
        "md_path": str(parsed / "full.md"),
        "content_list_path": str(parsed / "donor0000000000_content_list.json"),
    }
    new_doc = mk_doc(tmp_path, status="uploaded", mineru_batch_id=None)
    store = FakeStore({doc_id: donor, DOC_ID: new_doc})
    return store, new_doc


@pytest.mark.asyncio
async def test_maybe_reuse_copies_artifacts_without_quota(tmp_path: Path) -> None:
    store, new_doc = _mk_donor(tmp_path)
    client = FakeMineru()
    quota = FakeQuota()
    svc, _ = mk_service(tmp_path, store, client=client, quota=quota)

    assert await svc.maybe_reuse(new_doc) is True

    new_md = tmp_path / DOC_ID / "parsed" / "full.md"
    assert new_md.is_file() and new_md.read_text() == "# donor content"
    assert (tmp_path / DOC_ID / "parsed" / "images" / "f0.png").exists()
    _, fields = store.updates[-1]
    assert fields["status"] == "parsed"
    assert fields["page_count"] == 9
    assert Path(fields["md_path"]) == new_md
    assert quota.recorded == [], "复用不烧 MinerU 配额，一分都不许记"
    assert quota.released == [], "复用成功路径不涉及退预留"
    assert client.calls == 0 and client.batches == []

    # donor 的目录不因复用被移动/删除（生命周期独立）
    assert (tmp_path / "donor0000000000" / "parsed" / "full.md").exists()


@pytest.mark.asyncio
async def test_maybe_reuse_returns_false_without_donor(tmp_path: Path) -> None:
    store = FakeStore({DOC_ID: mk_doc(tmp_path, sha256="b" * 64)})
    svc, _ = mk_service(tmp_path, store, client=FakeMineru())
    assert await svc.maybe_reuse(store.rows[DOC_ID]) is False
    assert store.updates == []


@pytest.mark.asyncio
async def test_maybe_reuse_falls_through_when_donor_files_gone(tmp_path: Path) -> None:
    """行在产物没了（被 GC 清盘）：必须回落真解析，不许假装 parsed。"""
    store, new_doc = _mk_donor(tmp_path)
    import shutil

    shutil.rmtree(tmp_path / "donor0000000000")
    svc, _ = mk_service(tmp_path, store, client=FakeMineru())

    assert await svc.maybe_reuse(new_doc) is False
    assert store.updates == []


@pytest.mark.asyncio
async def test_maybe_reuse_deleted_row_discards_copy(tmp_path: Path) -> None:
    """H2：复制产物期间行已被删除 → 清掉复制出来的目录，回落真解析。"""
    store, new_doc = _mk_donor(tmp_path)
    store.rows[DOC_ID]["status"] = "deleted"  # 行已删（守卫会拦下 parsed 写）
    svc, _ = mk_service(tmp_path, store, client=FakeMineru())

    assert await svc.maybe_reuse(new_doc) is False
    assert not (tmp_path / DOC_ID / "parsed").exists(), "已删行不许留下复制产物"
    assert store.rows[DOC_ID]["status"] == "deleted"
    assert (tmp_path / "donor0000000000" / "parsed" / "full.md").exists(), (
        "donor 的产物生命周期独立，不许被误删"
    )


# ── 续跑 / GC ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_pending_spawns_and_fails_stale_uploaded(tmp_path: Path) -> None:
    store = FakeStore(
        {
            "p1": mk_doc(tmp_path, doc_id="p1"),
            "p2": mk_doc(
                tmp_path, doc_id="p2", status="parsing", mineru_batch_id="b-2"
            ),
            "u1": mk_doc(tmp_path, doc_id="u1", status="uploaded"),
        }
    )
    # FakeStore.mk_doc 的 doc_id 覆盖不走 dict 键：手工修正 rows 键
    store.rows["p1"]["doc_id"] = "p1"
    store.rows["p2"]["doc_id"] = "p2"
    svc, _ = mk_service(tmp_path, store, client=FakeMineru())

    n = await svc.resume_pending()

    assert n == 2
    assert set(svc._tasks) == {"p1", "p2"}
    assert store.fail_stale_calls == [30]
    await svc.shutdown()


@pytest.mark.asyncio
async def test_gc_expired_removes_dirs_and_marks(tmp_path: Path) -> None:
    store = FakeStore(
        {
            "g1": {"doc_id": "g1", "gc_candidate": True},
            "g2": {"doc_id": "g2", "gc_candidate": True},
        }
    )
    for did in ("g1", "g2"):
        (tmp_path / did / "parsed").mkdir(parents=True)
        (tmp_path / did / "parsed" / "full.md").write_text("x")
    svc, _ = mk_service(tmp_path, store, client=FakeMineru())

    n = await svc.gc_expired()

    assert n == 2
    assert store.marked_expired == ["g1", "g2"]
    assert not (tmp_path / "g1").exists()
    assert not (tmp_path / "g2").exists()


# ── 真库全生命周期（假 MinerU） ─────────────────────────────────────


@pytest.mark.asyncio
async def test_real_db_lifecycle_running_then_done(tmp_path: Path) -> None:
    from backend.services.engine.alpha_agent.doc_store import get_doc_store
    from backend.shared.database_manager_v2 import close_database, get_session
    from sqlalchemy import text

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")

    store = get_doc_store()
    await store.ensure_tables()
    user = f"t-docm-{uuid.uuid4().hex[:10]}"
    doc_id = f"t-doc-{uuid.uuid4().hex[:12]}"
    original = tmp_path / "original.pdf"
    original.write_bytes(b"%PDF-1.4 fake")

    try:
        await store.create_doc(
            doc_id=doc_id,
            user_id=user,
            filename="paper.pdf",
            ext=".pdf",
            size_bytes=original.stat().st_size,
            sha256=uuid.uuid4().hex * 2,
            original_path=str(original),
        )
        await store.update_doc(doc_id, status="parsing", mineru_batch_id="b-1")

        client = FakeMineru()
        client.zip_payload = make_zip(b"# real db lifecycle")
        client.results_queue = [
            [
                MineruBatchItem(
                    file_name="paper.pdf",
                    state="running",
                    data_id=doc_id,
                    extracted_pages=1,
                    total_pages=4,
                )
            ],
            [
                MineruBatchItem(
                    file_name="paper.pdf",
                    state="done",
                    data_id=doc_id,
                    full_zip_url="https://cdn.test/real.zip",
                    total_pages=4,
                )
            ],
        ]
        quota = FakeQuota()
        svc, _ = mk_service(tmp_path, store, client=client, quota=quota)

        assert await svc._poll_once(doc_id) == "active"
        mid = await store.get_doc(doc_id, user_id=user)
        assert mid["status"] == "parsing" and mid["page_count"] == 4

        assert await svc._poll_once(doc_id) == "parsed"
        final = await store.get_doc(doc_id, user_id=user)
        assert final["status"] == "parsed"
        assert final["parse_state"] == "done"
        assert Path(final["md_path"]).read_text() == "# real db lifecycle"
        assert quota.recorded == [(user, 4)]

        # 终态之后幂等：再轮询一次不应重写
        assert await svc._poll_once(doc_id) == "gone"
        assert client.calls == 2
    finally:
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM rd_agent_docs WHERE user_id = :u"), {"u": user}
            )
        await close_database()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))


# ── 预检与取消（上传/删除端点用） ───────────────────────────────────


def test_ensure_ready_raises_without_token(monkeypatch, tmp_path: Path) -> None:
    """ensure_ready 必须在落盘前探通通道：缺 token 抛 MineruError（端点转 503）。"""
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    svc = DocParseService(store=FakeStore(), quota=FakeQuota(), docs_root=tmp_path)
    with pytest.raises(MineruError) as ei:
        svc.ensure_ready()
    assert "MINERU_API_TOKEN" in str(ei.value)
    assert ei.value.retryable is False


def test_ensure_ready_noop_with_injected_client(tmp_path: Path) -> None:
    svc = DocParseService(
        store=FakeStore(), quota=FakeQuota(), client=FakeMineru(), docs_root=tmp_path
    )
    svc.ensure_ready()  # 注入替身时无需 env token，不抛


@pytest.mark.asyncio
async def test_cancel_running_poll_task_stops_further_polling(tmp_path: Path) -> None:
    store = FakeStore({DOC_ID: mk_doc(tmp_path)})
    client = FakeMineru()
    svc = DocParseService(
        store=store,
        quota=FakeQuota(),
        client=client,
        docs_root=tmp_path,
        poll_interval_s=0.02,
    )
    svc.start_poll(DOC_ID)
    await asyncio.sleep(0.06)
    assert svc._tasks, "轮询任务应当已挂起"

    assert await svc.cancel(DOC_ID) is True
    assert DOC_ID not in svc._tasks
    calls_after = client.calls
    await asyncio.sleep(0.06)
    assert client.calls == calls_after, "取消后不许再轮询（删除端点防复活）"

    assert await svc.cancel(DOC_ID) is False, "无任务可取消返回 False"


@pytest.mark.asyncio
async def test_cancel_while_sleeping_does_not_raise(tmp_path: Path) -> None:
    """取消撞上 sleep/await 点时必须安静退出（CancelledError 不许漏给调用方）。"""
    store = FakeStore({DOC_ID: mk_doc(tmp_path)})
    svc = DocParseService(
        store=store,
        quota=FakeQuota(),
        client=FakeMineru(),
        docs_root=tmp_path,
        poll_interval_s=30.0,
    )
    svc.start_poll(DOC_ID)
    await asyncio.sleep(0.02)
    assert await svc.cancel(DOC_ID) is True
    assert DOC_ID not in svc._tasks
