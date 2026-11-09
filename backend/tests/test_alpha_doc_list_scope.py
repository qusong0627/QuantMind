from contextlib import asynccontextmanager

import pytest

from backend.services.engine.alpha_agent import doc_store


@pytest.mark.asyncio
async def test_document_list_excludes_deleted_before_pagination_and_is_owner_scoped(monkeypatch):
    calls = []

    class Result:
        def mappings(self):
            return self

        def all(self):
            return []

    class Session:
        async def execute(self, statement, params):
            calls.append((str(statement), params))
            return Result()

    @asynccontextmanager
    async def get_session(*, read_only=False):
        assert read_only
        yield Session()

    monkeypatch.setattr(doc_store, 'get_session', get_session)
    await doc_store.DocStore().list_docs(tenant_id='tenant-a', user_id='user-a', limit=10, offset=20)
    sql, params = calls[0]
    assert "status <> 'deleted'" in sql
    assert 'tenant_id=:tenant_id' in sql and 'user_id=:user_id' in sql
    assert sql.index("status <> 'deleted'") < sql.index('LIMIT :limit OFFSET :offset')
    assert (params['tenant_id'], params['user_id'], params['limit'], params['offset']) == ('tenant-a', 'user-a', 11, 20)
