"""文档中心 store —— ``rd_agent_docs``（机构级 P1 / T-FM-07）。

这张表是文档解析链的**权威状态**（优于挖掘链的「重启即 failed」）：
doc_parse_service 重启后靠 ``list_parsing`` 续轮询；上传端点靠
``find_reusable_parsed`` 做 sha256 幂等复用（省 MinerU 共享配额）；留存
GC 靠 ``list_expired_candidates``/``mark_expired``。

两条与规划稿的**刻意差异**（实现期决定，回写于本 docstring）：

1. **复用=复制产物，不是共享目录**。规划稿写「仅新建文档条目引用同一解析
   目录」——共享目录会让删除任一文档把另一方的产物连根带走（删除端点
   rmtree 整目录）。这里改为把 donor 的 parsed/ 下的产物硬链接（跨设备
   回落复制）进新文档自己的目录：配额（真正稀缺的共享资源）照样省，
   生命周期各自独立。
2. **重启对账只碰 ``uploaded``**：那种行意味着「落库了但还没提交给
   MinerU」（提交前一刻崩溃）。parsing 行不是孤儿——它在 MinerU 队列里，
   由续轮询接管。

时间纪律与其它 store 一致：TIMESTAMPTZ + ``utc_now()`` 写入、``to_utc_iso``
输出（Z 后缀）；``update_doc`` 是增量语义——没传的字段绝不动，error 清空
必须显式传 None。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.utc_datetime import to_utc_iso, utc_now

logger = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200

DOC_STATUSES = (
    "uploaded",
    "parsing",
    "parsed",
    "parse_failed",
    "organized",
    "expired",
    "deleted",
)
#: GC 候选：产物已定型的行到期转 expired（parsing/uploaded 有人在写，不碰）
_GC_STATUSES = ("parsed", "organized", "parse_failed")
#: sha256 幂等复用可接受的状态
_REUSABLE_STATUSES = ("parsed", "organized")

_TS_FIELDS = ("created_at", "updated_at", "organized_at")

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS rd_agent_docs (
  doc_id        TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL,
  filename      TEXT NOT NULL,
  ext           TEXT,
  size_bytes    BIGINT,
  sha256        TEXT,
  original_path TEXT,
  mineru_batch_id TEXT,
  parse_state   TEXT,
  page_count    INTEGER,
  md_path       TEXT,
  content_list_path TEXT,
  status        TEXT NOT NULL,
  organized_text TEXT,
  organize_kind TEXT,
  organize_prompt_version TEXT,
  organized_at  TIMESTAMPTZ,
  task_id       TEXT,
  error         TEXT,
  mineru_token_src TEXT,
  tenant_id     TEXT,
  created_at    TIMESTAMPTZ NOT NULL,
  updated_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rd_docs_user ON rd_agent_docs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rd_docs_sha ON rd_agent_docs (user_id, sha256);
CREATE INDEX IF NOT EXISTS idx_rd_docs_status ON rd_agent_docs (status);
ALTER TABLE rd_agent_docs ADD COLUMN IF NOT EXISTS mineru_token_src TEXT;
ALTER TABLE rd_agent_docs ADD COLUMN IF NOT EXISTS tenant_id TEXT;
"""

_UNSET = object()


def resolve_list_filters(
    *, status: str | None, limit: int, offset: int
) -> dict[str, Any]:
    """列表过滤唯一解析点：空白=不过滤、收敛 limit/offset、状态白名单。

    未知状态抛 ValueError——静默查空会让「上传了但没显示」无从定位。
    """
    clean_status = (status or "").strip() or None
    if clean_status is not None and clean_status not in DOC_STATUSES:
        raise ValueError(
            f"unknown doc status: {clean_status!r}; expected one of {DOC_STATUSES}"
        )
    return {
        "status": clean_status,
        "limit": max(1, min(int(limit), MAX_LIST_LIMIT)),
        "offset": max(0, int(offset)),
    }


def row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """DB 行 → API 字典：时间列统一 ISO-8601 UTC（带 Z），未知列原样透传。"""
    out = dict(row)
    for field in _TS_FIELDS:
        if field in out:
            out[field] = to_utc_iso(out[field])
    return out


class DocStore:
    async def ensure_tables(self) -> None:
        async with get_session() as session:
            for stmt in [s.strip() for s in _CREATE_TABLE_SQL.split(";") if s.strip()]:
                await session.execute(text(stmt))
        logger.info("rd_agent_docs table ensured")

    async def create_doc(
        self,
        *,
        doc_id: str,
        user_id: str,
        filename: str,
        ext: str | None = None,
        size_bytes: int | None = None,
        sha256: str | None = None,
        original_path: str | None = None,
        status: str = "uploaded",
        tenant_id: str | None = None,
    ) -> None:
        if status not in DOC_STATUSES:
            raise ValueError(f"unknown doc status: {status!r}")
        now = utc_now()
        async with get_session() as session:
            await session.execute(
                text("""
                    INSERT INTO rd_agent_docs
                      (doc_id, user_id, filename, ext, size_bytes, sha256,
                       original_path, status, tenant_id, created_at, updated_at)
                    VALUES
                      (:doc_id, :user_id, :filename, :ext, :size_bytes, :sha256,
                       :original_path, :status, :tenant_id, :now, :now)
                    ON CONFLICT (doc_id) DO NOTHING
                    """),
                {
                    "doc_id": doc_id,
                    "user_id": user_id,
                    "filename": filename,
                    "ext": ext,
                    "size_bytes": int(size_bytes) if size_bytes is not None else None,
                    "sha256": sha256,
                    "original_path": original_path,
                    "status": status,
                    "tenant_id": tenant_id,
                    "now": now,
                },
            )

    async def get_doc(
        self, doc_id: str, *, user_id: str | None = None
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM rd_agent_docs WHERE doc_id = :doc_id"
        params: dict[str, Any] = {"doc_id": doc_id}
        if user_id is not None:
            query += " AND user_id = :user_id"
            params["user_id"] = user_id
        async with get_session(read_only=True) as session:
            row = (await session.execute(text(query), params)).mappings().first()
        return row_to_dict(row) if row else None

    async def update_doc(self, doc_id: str, **fields: Any) -> bool:
        """增量更新：只写显式传入的字段（哨兵 ``_UNSET`` = 不动）。返回是否命中。

        ``status=None``/未传 = 不改状态；``error=None`` = 清空。

        **已软删（status='deleted'）的行拒绝一切写**（安全审查 H2）：在途的
        解析/整理/复用在下载或 LLM 返回后才落库，若用户已删除，UPDATE 命中
        0 行变成 no-op——否则「删完又复活」。调用方以返回值判定「文档已不在」
        并清理产物（``_finish_done``/``submit_parse``/``maybe_reuse`` 均已收口）。
        """
        allowed = (
            "status",
            "error",
            "mineru_batch_id",
            "parse_state",
            "page_count",
            "md_path",
            "content_list_path",
            "organized_text",
            "organize_kind",
            "organize_prompt_version",
            "organized_at",
            "task_id",
            # 凭据来源（"user"/"env"）：提交时定格，重启续轮询按它重建同一 Token
            "mineru_token_src",
        )
        unknown = set(fields) - set(allowed)
        if unknown:
            raise ValueError(f"update_doc 不认字段: {sorted(unknown)}")

        sets: list[str] = ["updated_at = :now"]
        params: dict[str, Any] = {"doc_id": doc_id, "now": utc_now()}
        for name in allowed:
            if name not in fields:
                continue
            value = fields[name]
            if value is _UNSET:
                continue
            if name == "status" and value is None:
                continue
            if name == "status" and value not in DOC_STATUSES:
                raise ValueError(f"unknown doc status: {value!r}")
            sets.append(f"{name} = :{name}")
            params[name] = value

        async with get_session() as session:
            result = await session.execute(
                text(
                    f"UPDATE rd_agent_docs SET {', '.join(sets)} "
                    "WHERE doc_id = :doc_id AND status <> 'deleted'"
                ),
                params,
            )
            return bool(result.rowcount)

    # -- 查询面 --------------------------------------------------------

    def _list_where(
        self,
        user_id: str,
        filters: Mapping[str, Any],
        *,
        include_deleted: bool,
    ) -> tuple[list[str], dict[str, Any]]:
        where = ["user_id = :user_id"]
        params: dict[str, Any] = {"user_id": user_id}
        if filters["status"] is not None:
            where.append("status = :status")
            params["status"] = filters["status"]
        elif not include_deleted:
            where.append("status <> 'deleted'")
        return where, params

    async def list_docs(
        self,
        *,
        user_id: str,
        status: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        filters = resolve_list_filters(status=status, limit=limit, offset=offset)
        where, params = self._list_where(
            user_id, filters, include_deleted=include_deleted
        )
        params["limit"] = filters["limit"]
        params["offset"] = filters["offset"]
        query = (
            "SELECT * FROM rd_agent_docs "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, doc_id DESC "
            "LIMIT :limit OFFSET :offset"
        )
        async with get_session(read_only=True) as session:
            rows = (await session.execute(text(query), params)).mappings().all()
        return [row_to_dict(r) for r in rows]

    async def count_docs(
        self,
        *,
        user_id: str,
        status: str | None = None,
        include_deleted: bool = False,
    ) -> int:
        filters = resolve_list_filters(status=status, limit=1, offset=0)
        where, params = self._list_where(
            user_id, filters, include_deleted=include_deleted
        )
        query = f"SELECT count(*) FROM rd_agent_docs WHERE {' AND '.join(where)}"
        async with get_session(read_only=True) as session:
            return int((await session.execute(text(query), params)).scalar_one())

    async def find_reusable_parsed(
        self, user_id: str, sha256: str
    ) -> dict[str, Any] | None:
        """同用户同 sha256 且已解析（产物定型）的最早一行——幂等复用锚点。"""
        if not sha256:
            return None
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text("""
                        SELECT * FROM rd_agent_docs
                        WHERE user_id = :user_id AND sha256 = :sha256
                          AND status = ANY(:statuses)
                        ORDER BY created_at ASC
                        LIMIT 1
                        """),
                        {
                            "user_id": user_id,
                            "sha256": sha256,
                            "statuses": list(_REUSABLE_STATUSES),
                        },
                    )
                )
                .mappings()
                .first()
            )
        return row_to_dict(row) if row else None

    async def list_parsing(self) -> list[dict[str, Any]]:
        """所有 parsing 行（重启续轮询名单，跨用户）。"""
        async with get_session(read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM rd_agent_docs WHERE status = 'parsing' "
                            "ORDER BY created_at ASC"
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [row_to_dict(r) for r in rows]

    # -- 对账 / 留存 ----------------------------------------------------

    async def fail_stale_uploaded(
        self,
        *,
        older_than_minutes: int = 30,
        reason: str = "Server restarted before parsing was submitted，请重新上传",
    ) -> int:
        """重启对账：超时仍停在 ``uploaded`` 的行翻 parse_failed（提交前崩溃的孤儿）。"""
        async with get_session() as session:
            result = await session.execute(
                text("""
                    UPDATE rd_agent_docs
                    SET status = 'parse_failed', error = :reason, updated_at = :now
                    WHERE status = 'uploaded'
                      AND updated_at < CAST(:now AS timestamptz)
                          - (:mins * interval '1 minute')
                    """),
                {"reason": reason, "now": utc_now(), "mins": int(older_than_minutes)},
            )
            flipped = int(result.rowcount or 0)
        if flipped:
            logger.warning("fail_stale_uploaded flipped %d doc(s)", flipped)
        return flipped

    async def list_expired_candidates(
        self, *, retention_days: int
    ) -> list[dict[str, Any]]:
        """到期 GC 候选：定型状态（parsed/organized/parse_failed）且超留存期。"""
        async with get_session(read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        text("""
                        SELECT * FROM rd_agent_docs
                        WHERE status = ANY(:statuses)
                          AND updated_at < CAST(:now AS timestamptz)
                              - (:days * interval '1 day')
                        ORDER BY updated_at ASC
                        """),
                        {
                            "statuses": list(_GC_STATUSES),
                            "now": utc_now(),
                            "days": int(retention_days),
                        },
                    )
                )
                .mappings()
                .all()
            )
        return [row_to_dict(r) for r in rows]

    async def mark_expired(self, doc_id: str) -> None:
        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE rd_agent_docs SET status = 'expired', updated_at = :now "
                    "WHERE doc_id = :doc_id"
                ),
                {"now": utc_now(), "doc_id": doc_id},
            )

    async def soft_delete(self, doc_id: str, *, user_id: str) -> bool:
        """归属收口软删（行保留审计）。返回是否命中。"""
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE rd_agent_docs SET status = 'deleted', updated_at = :now "
                    "WHERE doc_id = :doc_id AND user_id = :user_id AND status <> 'deleted'"
                ),
                {"now": utc_now(), "doc_id": doc_id, "user_id": user_id},
            )
            return bool(result.rowcount)

    async def hard_delete(self, doc_id: str, *, user_id: str) -> bool:
        """连行删除。**仅限从未进入解析链的清理**（如配额拦下的上传）——
        那种行没有产物、没有审计价值，留着只会让「已上传但永不解析」滞留
        在列表里。已解析文档的删除一律走 :meth:`soft_delete`。返回是否命中。

        状态守卫 ``status='uploaded'``（安全审查 L2）：把「仅限」从 docstring
        钉进 SQL——任何后续调用都越不过「先 cancel/软删/留审计」的纪律。
        """
        async with get_session() as session:
            result = await session.execute(
                text(
                    "DELETE FROM rd_agent_docs "
                    "WHERE doc_id = :doc_id AND user_id = :user_id "
                    "AND status = 'uploaded'"
                ),
                {"doc_id": doc_id, "user_id": user_id},
            )
            return bool(result.rowcount)


_store: DocStore | None = None


def get_doc_store() -> DocStore:
    global _store
    if _store is None:
        _store = DocStore()
    return _store
