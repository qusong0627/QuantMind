from types import SimpleNamespace

import pytest

from backend.shared.strategy_template_sync import (
    existing_template_markers,
    sync_builtin_templates,
)


def test_existing_template_markers_reads_strategy_type_and_template_tag():
    types, names = existing_template_markers(
        [
            {
                "name": "默认 Top-K 选股策略",
                "parameters": {"strategy_type": "standard_topk"},
                "tags": ["SystemSync"],
            },
            {
                "name": "动量策略",
                "parameters": {},
                "tags": ["demo", "template:momentum"],
            },
        ]
    )
    assert "standard_topk" in types
    assert "momentum" in types
    assert "默认 Top-K 选股策略" in names
    assert "动量策略" in names


def test_existing_template_markers_ignores_blank_and_non_template_tags():
    types, names = existing_template_markers(
        [
            {"name": "  ", "parameters": {"strategy_type": ""}, "tags": ["basic"]},
            {"name": "手工策略", "parameters": None, "tags": None},
        ]
    )
    assert types == set()
    assert names == {"手工策略"}


@pytest.mark.anyio
async def test_perform_sync_updates_system_copies_and_adds_missing(monkeypatch):
    templates = [
        SimpleNamespace(
            id="standard_topk",
            name="默认 Top-K 选股策略",
            description="新描述",
            category="basic",
            difficulty="beginner",
            code="print('v2')",
        ),
        SimpleNamespace(
            id="momentum",
            name="动量",
            description="d",
            category="basic",
            difficulty="beginner",
            code="print(2)",
        ),
        SimpleNamespace(
            id="new_one",
            name="新策略",
            description="d",
            category="basic",
            difficulty="beginner",
            code="print(3)",
        ),
    ]
    saved: list[dict] = []

    class _Svc:
        def list(self, user_id=None):
            return [
                {
                    "id": "11",
                    "name": "默认 Top-K 选股策略",
                    "parameters": {"strategy_type": "standard_topk"},
                    "tags": ["basic", "SystemSync", "template:standard_topk"],
                },
                {
                    "id": "12",
                    "name": "动量",
                    "parameters": {},
                    "tags": ["template:momentum"],
                },
            ]

        async def save(self, **kwargs):
            saved.append(kwargs)

        async def delete(self, strategy_id, user_id):
            raise AssertionError("should not delete")

    async def _no_refs(strategy_id):
        return False

    monkeypatch.setattr(
        "backend.shared.strategy_template_sync._load_templates",
        lambda: templates,
    )
    monkeypatch.setattr(
        "backend.shared.strategy_template_sync._storage_service",
        lambda: _Svc(),
    )
    monkeypatch.setattr(
        "backend.shared.strategy_template_sync._has_references", _no_refs
    )

    count = await sync_builtin_templates("00000001")

    assert count == 2
    by_name = {item["name"]: item for item in saved}
    assert set(by_name) == {"默认 Top-K 选股策略", "新策略"}
    assert by_name["默认 Top-K 选股策略"]["strategy_id"] == "11"
    assert by_name["默认 Top-K 选股策略"]["code"] == "print('v2')"
    assert by_name["新策略"].get("strategy_id") is None
    assert by_name["新策略"]["metadata"]["parameters"]["strategy_type"] == "new_one"


@pytest.mark.anyio
async def test_perform_sync_removes_stale_system_copy(monkeypatch):
    deleted: list[tuple[str, str]] = []

    class _Svc:
        def list(self, user_id=None):
            return [
                {
                    "id": "19",
                    "name": "动态风控TOPK策略 v9",
                    "parameters": {"strategy_type": "dynamic_risk_topk_v9"},
                    "tags": ["advanced", "SystemSync", "template:dynamic_risk_topk_v9"],
                }
            ]

        async def save(self, **kwargs):
            raise AssertionError("should not save")

        async def delete(self, strategy_id, user_id):
            deleted.append((strategy_id, user_id))
            return True

    async def _no_refs(strategy_id):
        return False

    monkeypatch.setattr(
        "backend.shared.strategy_template_sync._load_templates",
        lambda: [],
    )
    monkeypatch.setattr(
        "backend.shared.strategy_template_sync._storage_service",
        lambda: _Svc(),
    )
    monkeypatch.setattr(
        "backend.shared.strategy_template_sync._has_references", _no_refs
    )

    count = await sync_builtin_templates("00000001")

    assert count == 1
    assert deleted == [("19", "00000001")]


@pytest.mark.anyio
async def test_perform_sync_keeps_referenced_stale_copy(monkeypatch):
    class _Svc:
        def list(self, user_id=None):
            return [
                {
                    "id": "19",
                    "name": "旧模板",
                    "parameters": {"strategy_type": "gone"},
                    "tags": ["SystemSync", "template:gone"],
                }
            ]

        async def save(self, **kwargs):
            raise AssertionError("should not save")

        async def delete(self, strategy_id, user_id):
            raise AssertionError("should not delete referenced copy")

    async def _refs(strategy_id):
        return True

    monkeypatch.setattr(
        "backend.shared.strategy_template_sync._load_templates",
        lambda: [],
    )
    monkeypatch.setattr(
        "backend.shared.strategy_template_sync._storage_service",
        lambda: _Svc(),
    )
    monkeypatch.setattr("backend.shared.strategy_template_sync._has_references", _refs)

    count = await sync_builtin_templates("00000001")

    assert count == 0
