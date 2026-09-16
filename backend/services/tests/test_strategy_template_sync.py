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
async def test_perform_sync_skips_existing_type_tag_and_name(monkeypatch):
    templates = [
        SimpleNamespace(
            id="standard_topk",
            name="默认 Top-K 选股策略",
            description="d",
            category="basic",
            difficulty="beginner",
            code="print(1)",
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
                    "name": "默认 Top-K 选股策略",
                    "parameters": {"strategy_type": "standard_topk"},
                    "tags": [],
                },
                {
                    "name": "动量",
                    "parameters": {},
                    "tags": ["template:momentum"],
                },
            ]

        async def save(self, **kwargs):
            saved.append(kwargs)

    monkeypatch.setattr(
        "backend.shared.strategy_template_sync._load_templates",
        lambda: templates,
    )
    monkeypatch.setattr(
        "backend.shared.strategy_template_sync._storage_service",
        lambda: _Svc(),
    )
    count = await sync_builtin_templates("00000001")
    assert count == 1
    assert saved[0]["name"] == "新策略"
    assert saved[0]["metadata"]["parameters"]["strategy_type"] == "new_one"
    assert saved[0]["metadata"]["parameters"]["sort"] == 100
