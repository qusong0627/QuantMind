from contextlib import asynccontextmanager
import asyncio
from datetime import datetime, timezone
import hashlib
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.services.engine.alpha_agent import doc_parser, paper_vault_api


class Result:
    def __init__(self, row=None, rowcount=1):
        self.row = row
        self.rowcount = rowcount

    def mappings(self):
        return self

    def first(self):
        return self.row


class Session:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def execute(self, statement, params):
        sql = str(statement)
        self.calls.append((sql, dict(params)))
        if "SELECT filename" in sql:
            return Result(
                self.row
                if params["tenant_id"] == "tenant-a" and params["user_id"] == "user-a"
                else None
            )
        if "SELECT doc_id,status" in sql:
            return Result(None)
        return Result()


@pytest.fixture
def setup_migration(monkeypatch):
    row = {
        "filename": "paper.pdf",
        "size_bytes": 8,
        "sha256": "a" * 64,
        "storage_key": "alpha_research/papers/p1/original.pdf",
        "created_at": datetime.now(timezone.utc),
    }
    session = Session(row)

    @asynccontextmanager
    async def get_session():
        yield session

    monkeypatch.setattr(paper_vault_api, "get_session", get_session)
    monkeypatch.setattr(
        paper_vault_api, "get_authenticated_identity", lambda _: ("user-a", "tenant-a")
    )
    monkeypatch.setattr(doc_parser, "_safe_source_parts", lambda *args, **kwargs: [])
    return session


@pytest.mark.asyncio
async def test_migrate_uses_same_owner_and_file_in_one_transaction(setup_migration):
    session = setup_migration
    result = await paper_vault_api.migrate_paper("p1", SimpleNamespace())
    assert result["data"]["doc_id"].startswith("paper-")
    assert len(session.calls) == 3
    assert "FOR UPDATE" in session.calls[0][0]
    assert "INSERT INTO rd_agent_docs" in session.calls[1][0]
    assert session.calls[1][1]["storage_key"] == session.row["storage_key"]
    assert "DELETE FROM user_papers" in session.calls[2][0]
    assert all(
        call[1]["tenant_id"] == "tenant-a" and call[1]["user_id"] == "user-a"
        for call in session.calls
    )


@pytest.mark.asyncio
async def test_migrate_rejects_other_owner_without_write(setup_migration, monkeypatch):
    monkeypatch.setattr(
        paper_vault_api, "get_authenticated_identity", lambda _: ("user-a", "tenant-b")
    )
    with pytest.raises(HTTPException) as exc:
        await paper_vault_api.migrate_paper("p1", SimpleNamespace())
    assert exc.value.status_code == 404
    assert all(
        "INSERT" not in sql and "DELETE" not in sql for sql, _ in setup_migration.calls
    )


@pytest.mark.asyncio
async def test_migrate_does_not_move_missing_or_changed_file(
    setup_migration, monkeypatch
):
    def invalid(*args, **kwargs):
        raise ValueError("invalid file")

    monkeypatch.setattr(doc_parser, "_safe_source_parts", invalid)
    with pytest.raises(HTTPException) as exc:
        await paper_vault_api.migrate_paper("p1", SimpleNamespace())
    assert exc.value.status_code == 409
    assert len(setup_migration.calls) == 1


@pytest.mark.asyncio
async def test_postgres_concurrent_migration_keeps_one_file_and_owner_scope(
    tmp_path, monkeypatch
):
    url = os.getenv("PAPER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("PAPER_TEST_DATABASE_URL not configured")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.services.engine.alpha_agent.doc_api import _artifact_path

    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE user_papers (tenant_id text, user_id text, paper_id text, filename text, size_bytes bigint, sha256 text, storage_key text, created_at timestamptz, PRIMARY KEY (tenant_id,user_id,paper_id))"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE rd_agent_docs (tenant_id text, user_id text, doc_id text, filename text, ext text, size_bytes bigint, sha256 text, storage_key text, files_count integer, original_parts jsonb, status text, created_at timestamptz, updated_at timestamptz, PRIMARY KEY (tenant_id,user_id,doc_id))"
            )
        )
    monkeypatch.setenv("PIPELINE_ARTIFACTS_DIR", str(tmp_path))
    data = b"%PDF-1.7 test"
    key = "alpha_research/papers/p1/original.pdf"
    for tenant in ("a", "b"):
        path = _artifact_path(tenant, "u", key)
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO user_papers VALUES (:t,'u','p1','p.pdf',:size,:sha,:key,now())"
                ),
                {
                    "t": tenant,
                    "size": len(data),
                    "sha": hashlib.sha256(data).hexdigest(),
                    "key": key,
                },
            )

    @asynccontextmanager
    async def get_session():
        async with sessions() as session:
            async with session.begin():
                yield session

    monkeypatch.setattr(paper_vault_api, "get_session", get_session)
    monkeypatch.setattr(
        paper_vault_api, "get_authenticated_identity", lambda _: ("u", "a")
    )
    try:
        results = await asyncio.gather(
            *(paper_vault_api.migrate_paper("p1", SimpleNamespace()) for _ in range(2))
        )
        assert results[0] == results[1]
        async with engine.connect() as conn:
            docs = (
                await conn.execute(
                    text("SELECT tenant_id,storage_key FROM rd_agent_docs")
                )
            ).all()
            papers = (
                await conn.execute(text("SELECT tenant_id FROM user_papers"))
            ).all()
        assert docs == [("a", key)]
        assert papers == [("b",)]
        assert _artifact_path("a", "u", key).read_bytes() == data
        assert _artifact_path("b", "u", key).read_bytes() == data

        from fastapi import Response

        key2 = "alpha_research/papers/p2/original.pdf"
        file2 = _artifact_path("a", "u", key2)
        file2.parent.mkdir(parents=True)
        file2.write_bytes(data)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO user_papers VALUES ('a','u','p2','p.pdf',:size,:sha,:key,now())"
                ),
                {
                    "size": len(data),
                    "sha": hashlib.sha256(data).hexdigest(),
                    "key": key2,
                },
            )

        async def remove_file(tenant, user, storage_key):
            _artifact_path(tenant, user, storage_key).unlink()

        monkeypatch.setattr(paper_vault_api, "_delete_file_and_usage", remove_file)
        results = await asyncio.gather(
            paper_vault_api.migrate_paper("p2", SimpleNamespace()),
            paper_vault_api.delete_paper("p2", SimpleNamespace(), Response()),
            return_exceptions=True,
        )
        assert (
            sum(
                isinstance(result, HTTPException) and result.status_code == 404
                for result in results
            )
            == 1
        )
        async with engine.connect() as conn:
            found = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM rd_agent_docs WHERE tenant_id='a' AND storage_key=:key"
                    ),
                    {"key": key2},
                )
            ).scalar_one()
        assert (found == 1) == file2.exists()
    finally:
        await engine.dispose()
