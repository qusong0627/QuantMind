"""文档中心 store —— ``rd_agent_docs``（机构级 P1 / T-FM-07）。

这张表是文档解析链的**权威状态**：进程重启后靠它续轮询、靠它算幂等复用、
靠它做留存 GC。所以用例盯的是「行本身不能骗人」：

- 时间列一律 TIMESTAMPTZ → ISO-8601 UTC（带 Z）；未整理就是 None；
- ``update_doc`` 是**增量语义**：没传的字段绝不动（全量覆盖会把解析进度
  写回空）；清 error 必须显式（None = 清，不传 = 不动）；
- 状态白名单在进 SQL 前拦：未知状态显式 ValueError（静默写坏状态 =
  前端永远转圈、GC 永远扫不到）；
- 幂等复用只认 ``parsed/organized`` 且产物目录**还在盘上**的行——
  行在产物没了（被 GC）假装复用 = 用户拿到一个打不开的文档；
- 重启对账只碰 ``uploaded``（还没提交给 MinerU 的），parsing 行归续轮询管。

真库用例租户前缀 `t-`，用完必删、按用例 `close_database()`，DB 不可用整体 skip。
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent.doc_store import (  # noqa: E402
    DOC_STATUSES,
    resolve_list_filters,
    row_to_dict,
)


# ── 纯函数 ───────────────────────────────────────────────────────────


def test_row_to_dict_serializes_timestamps_as_utc_z() -> None:
    row = {
        "doc_id": "t-doc-1",
        "created_at": datetime.fromisoformat("2026-10-09T00:00:00+00:00"),
        "updated_at": datetime.fromisoformat("2026-10-09T01:02:03+00:00"),
        "organized_at": None,
    }
    out = row_to_dict(row)
    assert out["created_at"] == "2026-10-09T00:00:00Z"
    assert out["updated_at"] == "2026-10-09T01:02:03Z"
    assert out["organized_at"] is None


def test_row_to_dict_passes_through_unknown_columns() -> None:
    out = row_to_dict({"doc_id": "x", "future_column": 7})
    assert out["future_column"] == 7


def test_resolve_list_filters_clamps_and_validates() -> None:
    out = resolve_list_filters(status=None, limit=50, offset=0)
    assert out == {"status": None, "limit": 50, "offset": 0}

    out = resolve_list_filters(status=" parsing ", limit=9999, offset=-3)
    assert out == {"status": "parsing", "limit": 200, "offset": 0}

    with pytest.raises(ValueError):
        resolve_list_filters(status="nonexistent", limit=10, offset=0)


def test_doc_status_whitelist_content() -> None:
    assert DOC_STATUSES == (
        "uploaded",
        "parsing",
        "parsed",
        "parse_failed",
        "organized",
        "expired",
        "deleted",
    )


# ── 真库 ─────────────────────────────────────────────────────────────


def _scope() -> str:
    return f"t-docm-{uuid.uuid4().hex[:10]}"


async def _ready():
    from sqlalchemy import text

    from backend.services.engine.alpha_agent.doc_store import get_doc_store
    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")
    store = get_doc_store()
    await store.ensure_tables()
    return store


async def _cleanup(user: str) -> None:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        await session.execute(
            text("DELETE FROM rd_agent_docs WHERE user_id = :u"), {"u": user}
        )


async def _close() -> None:
    from backend.shared.database_manager_v2 import close_database

    await close_database()


async def _mk_doc(store, doc_id: str, user: str, **overrides):
    kwargs = {
        "filename": "paper.pdf",
        "ext": ".pdf",
        "size_bytes": 12345,
        "sha256": uuid.uuid4().hex * 2,
        "original_path": f"/tmp/{doc_id}/original.pdf",
    }
    kwargs.update(overrides)
    await store.create_doc(doc_id=doc_id, user_id=user, **kwargs)
    return await store.get_doc(doc_id, user_id=user)


@pytest.mark.asyncio
async def test_real_db_create_get_roundtrip_is_user_scoped() -> None:
    store = await _ready()
    user, other = _scope(), _scope()
    doc_id = f"t-doc-{uuid.uuid4().hex[:12]}"
    try:
        row = await _mk_doc(store, doc_id, user)
        assert row is not None
        assert row["status"] == "uploaded"
        assert row["filename"] == "paper.pdf"
        assert row["created_at"].endswith("Z")
        assert row["error"] is None and row["md_path"] is None

        assert await store.get_doc(doc_id, user_id=other) is None, (
            "归属收口：别人的 doc_id 查空"
        )
        assert (await store.get_doc(doc_id, user_id=user))["doc_id"] == doc_id
    finally:
        await _cleanup(user)
        await _cleanup(other)
        await _close()


@pytest.mark.asyncio
async def test_real_db_create_is_idempotent_on_doc_id() -> None:
    store = await _ready()
    user = _scope()
    doc_id = f"t-doc-{uuid.uuid4().hex[:12]}"
    try:
        await _mk_doc(store, doc_id, user, filename="first.pdf")
        await _mk_doc(store, doc_id, user, filename="second.pdf")
        row = await store.get_doc(doc_id, user_id=user)
        assert row["filename"] == "first.pdf", "重放不能覆盖首次写入"
    finally:
        await _cleanup(user)
        await _close()


@pytest.mark.asyncio
async def test_real_db_update_doc_is_incremental() -> None:
    store = await _ready()
    user = _scope()
    doc_id = f"t-doc-{uuid.uuid4().hex[:12]}"
    try:
        await _mk_doc(store, doc_id, user)

        await store.update_doc(
            doc_id, status="parsing", mineru_batch_id="b-1", parse_state="pending"
        )
        row = await store.get_doc(doc_id, user_id=user)
        assert row["status"] == "parsing"
        assert row["mineru_batch_id"] == "b-1"
        assert row["filename"] == "paper.pdf", "没传的字段不许被动"

        await store.update_doc(
            doc_id,
            status="parsed",
            parse_state="done",
            page_count=12,
            md_path="/data/rd_agent_docs/x/parsed/full.md",
            content_list_path="/data/rd_agent_docs/x/parsed/cl.json",
        )
        row = await store.get_doc(doc_id, user_id=user)
        assert row["status"] == "parsed"
        assert row["page_count"] == 12
        assert row["mineru_batch_id"] == "b-1", "解析完成后批次号还在（追溯用）"

        # error 语义：不传 = 不动；传 None = 清
        await store.update_doc(doc_id, error="boom")
        assert (await store.get_doc(doc_id, user_id=user))["error"] == "boom"
        await store.update_doc(doc_id, status="parsing")
        assert (await store.get_doc(doc_id, user_id=user))["error"] == "boom"
        await store.update_doc(doc_id, error=None)
        assert (await store.get_doc(doc_id, user_id=user))["error"] is None
    finally:
        await _cleanup(user)
        await _close()


@pytest.mark.asyncio
async def test_real_db_update_doc_rejects_deleted_row_with_false() -> None:
    """H2：软删行拒绝一切写且返回 False——在途解析/整理不许把行写回 parsed。"""
    store = await _ready()
    user = _scope()
    doc_id = f"t-doc-{uuid.uuid4().hex[:12]}"
    try:
        await _mk_doc(store, doc_id, user, status="uploaded")
        assert await store.update_doc(doc_id, status="parsing") is True, (
            "未删行的写返回 True（调用方按它判断是否落定）"
        )
        assert await store.soft_delete(doc_id, user_id=user) is True

        assert (
            await store.update_doc(doc_id, status="parsed", md_path="/x/full.md")
            is False
        ), "已删行写 status 必须 False"
        assert await store.update_doc(doc_id, error="late write") is False
        row = await store.get_doc(doc_id, user_id=user)
        assert row["status"] == "deleted", "写完还是 deleted（没复活）"
        assert row["md_path"] is None and row["error"] is None, "半成品字段没落库"
    finally:
        await _cleanup(user)
        await _close()


@pytest.mark.asyncio
async def test_real_db_update_doc_missing_row_returns_false() -> None:
    store = await _ready()
    user = _scope()
    try:
        assert (
            await store.update_doc(f"t-doc-{uuid.uuid4().hex[:12]}", status="parsing")
            is False
        ), "行不存在返回 False（不是静默 True）"
    finally:
        await _cleanup(user)
        await _close()


@pytest.mark.asyncio
async def test_real_db_hard_delete_only_touches_uploaded() -> None:
    """L2：hard_delete 只允许清理未进解析链的 uploaded 行；parsing/deleted 删不动。"""
    store = await _ready()
    user = _scope()
    up, par = f"t-doc-{uuid.uuid4().hex[:8]}-up", f"t-doc-{uuid.uuid4().hex[:8]}-par"
    try:
        await _mk_doc(store, up, user, status="uploaded")
        await _mk_doc(store, par, user, status="uploaded")
        assert await store.update_doc(par, status="parsing") is True

        assert await store.hard_delete(par, user_id=user) is False, (
            "已进解析链的行有产物与审计价值，不许连根删"
        )
        assert (await store.get_doc(par, user_id=user))["status"] == "parsing"

        assert await store.hard_delete(up, user_id=user) is True
        assert await store.get_doc(up, user_id=user) is None
    finally:
        await _cleanup(user)
        await _close()


@pytest.mark.asyncio
async def test_real_db_update_rejects_unknown_status() -> None:
    store = await _ready()
    user = _scope()
    doc_id = f"t-doc-{uuid.uuid4().hex[:12]}"
    try:
        await _mk_doc(store, doc_id, user)
        with pytest.raises(ValueError):
            await store.update_doc(doc_id, status="finished")  # 因子域的词的混入
    finally:
        await _cleanup(user)
        await _close()


@pytest.mark.asyncio
async def test_real_db_list_and_count_respect_scope_status_paging() -> None:
    store = await _ready()
    user, other = _scope(), _scope()
    base = f"t-doc-{uuid.uuid4().hex[:8]}"
    try:
        for i in range(3):
            await _mk_doc(store, f"{base}-{i}", user)
        await _mk_doc(store, f"{base}-x", other)
        await store.update_doc(f"{base}-0", status="parsed", md_path="/x/full.md")
        await store.update_doc(f"{base}-1", status="parse_failed", error="bad")

        rows = await store.list_docs(user_id=user, limit=50, offset=0)
        assert {r["doc_id"] for r in rows} == {f"{base}-0", f"{base}-1", f"{base}-2"}
        assert await store.count_docs(user_id=user) == 3

        parsed = await store.list_docs(
            user_id=user, status="parsed", limit=50, offset=0
        )
        assert [r["doc_id"] for r in parsed] == [f"{base}-0"]

        page = await store.list_docs(user_id=user, limit=2, offset=0)
        assert len(page) == 2

        # deleted 默认隐藏
        await store.soft_delete(f"{base}-2", user_id=user)
        assert await store.count_docs(user_id=user) == 2
        assert await store.count_docs(user_id=user, include_deleted=True) == 3
    finally:
        await _cleanup(user)
        await _cleanup(other)
        await _close()


@pytest.mark.asyncio
async def test_real_db_soft_delete_is_user_scoped() -> None:
    store = await _ready()
    user, other = _scope(), _scope()
    doc_id = f"t-doc-{uuid.uuid4().hex[:12]}"
    try:
        await _mk_doc(store, doc_id, user)
        assert await store.soft_delete(doc_id, user_id=other) is False
        assert (await store.get_doc(doc_id, user_id=user))["status"] == "uploaded"
        assert await store.soft_delete(doc_id, user_id=user) is True
        assert (await store.get_doc(doc_id, user_id=user))["status"] == "deleted"
    finally:
        await _cleanup(user)
        await _cleanup(other)
        await _close()


@pytest.mark.asyncio
async def test_real_db_find_reusable_parsed_only_parsed_and_same_user() -> None:
    store = await _ready()
    user, other = _scope(), _scope()
    base = f"t-doc-{uuid.uuid4().hex[:8]}"
    sha = uuid.uuid4().hex * 2
    try:
        await _mk_doc(store, f"{base}-a", user, sha256=sha, filename="v1.pdf")
        # 还没解析：不可复用
        assert await store.find_reusable_parsed(user, sha) is None

        await store.update_doc(
            f"{base}-a", status="parsed", md_path="/x/full.md", page_count=5
        )
        donor = await store.find_reusable_parsed(user, sha)
        assert donor is not None and donor["doc_id"] == f"{base}-a"

        # 别的用户不可复用（跨租户内容泄露的另一面：白拿别人的解析结果）
        assert await store.find_reusable_parsed(other, sha) is None

        # parse_failed 不可复用
        await _mk_doc(store, f"{base}-b", other, sha256=sha)
        await store.update_doc(f"{base}-b", status="parse_failed", error="x")
        assert await store.find_reusable_parsed(other, sha) is None
    finally:
        await _cleanup(user)
        await _cleanup(other)
        await _close()


@pytest.mark.asyncio
async def test_real_db_list_parsing_for_resume() -> None:
    store = await _ready()
    user = _scope()
    base = f"t-doc-{uuid.uuid4().hex[:8]}"
    try:
        await _mk_doc(store, f"{base}-p", user)
        await store.update_doc(f"{base}-p", status="parsing", mineru_batch_id="b-9")
        await _mk_doc(store, f"{base}-u", user)  # uploaded：不在续轮询名单

        rows = await store.list_parsing()
        mine = [r for r in rows if r["doc_id"] == f"{base}-p"]
        assert len(mine) == 1
        assert mine[0]["mineru_batch_id"] == "b-9"
        assert all(r["doc_id"] != f"{base}-u" for r in rows)
    finally:
        await _cleanup(user)
        await _close()


@pytest.mark.asyncio
async def test_real_db_fail_stale_uploaded_only_touches_old_uploaded() -> None:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    store = await _ready()
    user = _scope()
    base = f"t-doc-{uuid.uuid4().hex[:8]}"
    try:
        await _mk_doc(store, f"{base}-old", user)
        await _mk_doc(store, f"{base}-fresh", user)
        await _mk_doc(store, f"{base}-parsing", user)
        await store.update_doc(f"{base}-parsing", status="parsing", mineru_batch_id="b")
        # 把 old 的 updated_at 拨回 2 小时前
        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE rd_agent_docs SET updated_at = now() - interval '2 hours' "
                    "WHERE doc_id = :d"
                ),
                {"d": f"{base}-old"},
            )

        n = await store.fail_stale_uploaded(older_than_minutes=30)
        assert n == 1
        old = await store.get_doc(f"{base}-old", user_id=user)
        assert old["status"] == "parse_failed"
        assert "重新" in (old["error"] or "")
        assert (await store.get_doc(f"{base}-fresh", user_id=user))[
            "status"
        ] == "uploaded"
        assert (await store.get_doc(f"{base}-parsing", user_id=user))[
            "status"
        ] == "parsing", "parsing 行归续轮询管，对账不许碰"
    finally:
        await _cleanup(user)
        await _close()


@pytest.mark.asyncio
async def test_real_db_expired_candidates_and_mark() -> None:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    store = await _ready()
    user = _scope()
    base = f"t-doc-{uuid.uuid4().hex[:8]}"
    try:
        await _mk_doc(store, f"{base}-old", user)
        await store.update_doc(f"{base}-old", status="parsed", md_path="/x/full.md")
        await _mk_doc(store, f"{base}-new", user)
        await store.update_doc(f"{base}-new", status="parsed", md_path="/y/full.md")
        await _mk_doc(store, f"{base}-act", user)
        await store.update_doc(f"{base}-act", status="parsing", mineru_batch_id="b")
        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE rd_agent_docs SET updated_at = now() - interval '100 days' "
                    "WHERE doc_id = :d"
                ),
                {"d": f"{base}-old"},
            )

        candidates = await store.list_expired_candidates(retention_days=90)
        ids = {r["doc_id"] for r in candidates}
        assert f"{base}-old" in ids
        assert f"{base}-new" not in ids
        assert f"{base}-act" not in ids, "parsing 中的不许 GC（正在写产物）"

        await store.mark_expired(f"{base}-old")
        old = await store.get_doc(f"{base}-old", user_id=user)
        assert old["status"] == "expired"
    finally:
        await _cleanup(user)
        await _close()


@pytest.mark.asyncio
async def test_real_db_multi_file_parts_roundtrip() -> None:
    """多文件文档：件数 + 部件清单（路径/原名/扩展名）落库并按 JSON 还原。"""
    from backend.services.engine.alpha_agent.doc_store import decode_original_paths

    store = await _ready()
    user = _scope()
    doc_id = f"t-doc-{uuid.uuid4().hex[:12]}"
    parts = [
        {
            "path": f"/data/rd_agent_docs/{doc_id}/originals/p1.pdf",
            "name": "正文.pdf",
            "ext": ".pdf",
        },
        {
            "path": f"/data/rd_agent_docs/{doc_id}/originals/p2.pdf",
            "name": "附录.pdf",
            "ext": ".pdf",
        },
    ]
    try:
        await _mk_doc(
            store,
            doc_id,
            user,
            filename="正文.pdf（共2个文件）",
            original_path=parts[0]["path"],
            files_count=2,
            original_paths=parts,
        )
        row = await store.get_doc(doc_id, user_id=user)
        assert row["files_count"] == 2
        assert decode_original_paths(row) == parts
    finally:
        await _cleanup(user)
        await _close()


def test_single_file_row_synthesizes_one_part() -> None:
    """单文件（无论新旧行）没有 original_paths → 用 original_path 合成一件。"""
    from backend.services.engine.alpha_agent.doc_store import decode_original_paths

    row = {
        "filename": "paper.pdf",
        "ext": ".pdf",
        "original_path": "/x/original.pdf",
    }
    assert decode_original_paths(row) == [
        {"path": "/x/original.pdf", "name": "paper.pdf", "ext": ".pdf"}
    ]
    assert decode_original_paths({"filename": "x"}) == []
    assert decode_original_paths(
        {"original_paths": "not json", "original_path": "/x/a.pdf", "filename": "a.pdf"}
    ) == [{"path": "/x/a.pdf", "name": "a.pdf", "ext": ""}]


@pytest.mark.asyncio
async def test_real_db_single_file_defaults_to_one() -> None:
    store = await _ready()
    user = _scope()
    doc_id = f"t-doc-{uuid.uuid4().hex[:12]}"
    try:
        row = await _mk_doc(store, doc_id, user)
        assert row["files_count"] == 1
        assert row["original_paths"] is None
    finally:
        await _cleanup(user)
        await _close()


@pytest.mark.asyncio
async def test_real_db_update_doc_accepts_files_count() -> None:
    """复用（maybe_reuse）要能把 donor 的件数抄到新行上。"""
    store = await _ready()
    user = _scope()
    doc_id = f"t-doc-{uuid.uuid4().hex[:12]}"
    try:
        await _mk_doc(store, doc_id, user)
        assert await store.update_doc(doc_id, files_count=3) is True
        row = await store.get_doc(doc_id, user_id=user)
        assert row["files_count"] == 3
    finally:
        await _cleanup(user)
        await _close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
