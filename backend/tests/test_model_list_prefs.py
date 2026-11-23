"""模型列表偏好清洗（置顶/收藏/排序/文件夹，2026-11-22 跨重启保存）。"""

from __future__ import annotations

from backend.services.api.routers.model_training import (
    _merge_absent_folder_prefs,
    _normalize_model_list_prefs,
)


def _empty_prefs(**overrides):
    base = {
        "pinned": [],
        "favorites": [],
        "sort_key": "default",
        "folders": [],
        "model_folders": {},
    }
    base.update(overrides)
    return base


def test_normalize_dedupes_trims_and_keeps_valid_sort_key():
    out = _normalize_model_list_prefs(
        {
            "pinned": [" a ", "a", 3, "", "b"],
            "favorites": ["x"],
            "sort_key": "created_desc",
        }
    )
    assert out == _empty_prefs(pinned=["a", "b"], favorites=["x"], sort_key="created_desc")


def test_normalize_rejects_bad_shapes_and_sort_key():
    out = _normalize_model_list_prefs(
        {"pinned": "nope", "favorites": None, "sort_key": "hack"}
    )
    assert out == _empty_prefs()
    assert _normalize_model_list_prefs(None) == _empty_prefs()


def test_normalize_caps_items_and_drops_overlong_ids():
    out = _normalize_model_list_prefs(
        {
            "favorites": [f"m{i}" for i in range(600)],
            "pinned": ["ok", "x" * 129],
        }
    )
    assert len(out["favorites"]) == 500
    assert out["pinned"] == ["ok"]


def test_normalize_folders_trims_dedupes_and_drops_bad_entries():
    out = _normalize_model_list_prefs(
        {
            "folders": [
                {"id": " f1 ", "name": " T+5 "},
                {"id": "f1", "name": "重复 ID 丢弃"},
                {"id": "", "name": "空 ID"},
                {"id": "f2", "name": "   "},
                {"id": "f3"},
                "not-a-dict",
                {"id": "f" * 65, "name": "ID 过长"},
                {"id": "f4", "name": "名" * 41},
            ]
        }
    )
    assert out["folders"] == [{"id": "f1", "name": "T+5"}]


def test_normalize_folders_caps_count():
    out = _normalize_model_list_prefs(
        {"folders": [{"id": f"f{i}", "name": f"文件夹{i}"} for i in range(300)]}
    )
    assert len(out["folders"]) == 200


def test_normalize_model_folders_only_keeps_existing_folders():
    out = _normalize_model_list_prefs(
        {
            "folders": [{"id": "f1", "name": "A"}, {"id": "f2", "name": "B"}],
            "model_folders": {
                " mdl_1 ": " f1 ",
                "mdl_2": "f2",
                "mdl_3": "gone",  # 悬空分配：文件夹不存在
                "": "f1",  # 空模型 ID
                "mdl_4": 3,  # 非字符串
                "x" * 129: "f1",  # 模型 ID 过长
            },
        }
    )
    assert out["model_folders"] == {"mdl_1": "f1", "mdl_2": "f2"}


def test_normalize_model_folders_rejects_bad_shapes_and_caps():
    assert _normalize_model_list_prefs(
        {"folders": [{"id": "f1", "name": "A"}], "model_folders": ["nope"]}
    )["model_folders"] == {}
    out = _normalize_model_list_prefs(
        {
            "folders": [{"id": "f1", "name": "A"}],
            "model_folders": {f"m{i}": "f1" for i in range(2500)},
        }
    )
    assert len(out["model_folders"]) == 2000
    assert set(out["model_folders"].values()) == {"f1"}


def test_legacy_put_without_folder_fields_preserves_stored_folders():
    """升级前的页签不会发送 folders/model_folders：保留服务端已有分组。"""
    incoming = _normalize_model_list_prefs({"pinned": ["mdl_a"]})
    existing = {
        "folders": [{"id": "f1", "name": "T+5"}],
        "model_folders": {"mdl_a": "f1"},
    }
    merged = _merge_absent_folder_prefs(
        incoming,
        {"market", "pinned", "favorites", "sort_key"},
        existing,
    )
    assert merged["folders"] == [{"id": "f1", "name": "T+5"}]
    assert merged["model_folders"] == {"mdl_a": "f1"}
    assert merged["pinned"] == ["mdl_a"]


def test_new_put_with_explicit_empty_folders_clears_them():
    """新前端显式发送空文件夹 = 用户确实删光了，必须清空而不是被保护逻辑救回来。"""
    incoming = _normalize_model_list_prefs({"folders": [], "model_folders": {}})
    existing = {"folders": [{"id": "f1", "name": "T+5"}]}
    merged = _merge_absent_folder_prefs(
        incoming, {"market", "folders", "model_folders"}, existing
    )
    assert merged["folders"] == []
    assert merged["model_folders"] == {}
