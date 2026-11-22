"""模型列表偏好清洗（置顶/收藏/排序，2026-11-22 跨重启保存）。"""

from __future__ import annotations

from backend.services.api.routers.model_training import _normalize_model_list_prefs


def test_normalize_dedupes_trims_and_keeps_valid_sort_key():
    out = _normalize_model_list_prefs(
        {
            "pinned": [" a ", "a", 3, "", "b"],
            "favorites": ["x"],
            "sort_key": "created_desc",
        }
    )
    assert out == {
        "pinned": ["a", "b"],
        "favorites": ["x"],
        "sort_key": "created_desc",
    }


def test_normalize_rejects_bad_shapes_and_sort_key():
    out = _normalize_model_list_prefs(
        {"pinned": "nope", "favorites": None, "sort_key": "hack"}
    )
    assert out == {"pinned": [], "favorites": [], "sort_key": "default"}
    assert _normalize_model_list_prefs(None) == {
        "pinned": [],
        "favorites": [],
        "sort_key": "default",
    }


def test_normalize_caps_items_and_drops_overlong_ids():
    out = _normalize_model_list_prefs(
        {
            "favorites": [f"m{i}" for i in range(600)],
            "pinned": ["ok", "x" * 129],
        }
    )
    assert len(out["favorites"]) == 500
    assert out["pinned"] == ["ok"]
