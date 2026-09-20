"""评估长序列侧车（`shared/eval_series.py`，设计 §1.6）。

侧车存在的理由：模型逐日 IC、因子逐日 IC/分位线是上千点的长序列，塞进
`eval_scores.dimensions` 会让 `GET /eval/scores` 列表整体变重。列表只留标量，
长序列按需取（`GET /api/v1/eval/series`）。

盯三件事：
- **object_id 来自 URL**：必须挡路径穿越（`../../etc/passwd` 不许落到盘上）；
- **读不到就说清是哪一种读不到**（缺失 / 损坏 / 版本不符），绝不当成空序列返回
  ——空序列在前端会画成一条平线，等于伪造证据；
- 写入原子（tmp + replace），半截 JSON 不许留在盘上。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.shared.eval_series import (
    SERIES_VERSION,
    UnsafeObjectId,
    is_series_id,
    load_series,
    resolve_series_dir,
    safe_object_id,
    save_series,
    series_path,
)


# ── 路径安全 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_series_path_rejects_traversal_object_id(tmp_path: Path):
    """object_id 来自 URL：`..` 一律拒绝，不允许把文件写到目录外。"""
    for bad in ("../../etc/passwd", "..", "a/../../b", "a/b", "", " ", ".hidden"):
        with pytest.raises(UnsafeObjectId):
            series_path("model", bad, root=tmp_path)


@pytest.mark.unit
def test_series_path_rejects_unknown_object_type(tmp_path: Path):
    """object_type 也进路径段：只认白名单里的六类。"""
    with pytest.raises(UnsafeObjectId):
        series_path("../model", "mdl_ok", root=tmp_path)


@pytest.mark.unit
def test_series_path_accepts_real_object_ids(tmp_path: Path):
    """真实 id 形态必须都能落盘（模型带时间戳、因子带点号、日期带横线）。"""
    for ot, oid in (
        ("model", "mdl_cn_train_20260906064130_ab12cd"),
        ("factor", "a158_KMID"),
        ("daily_selection", "2026-09-18"),
        ("account", "10000001"),
        ("strategy", "bt_16e67c"),
    ):
        path = series_path(ot, oid, root=tmp_path)

        assert path.parent == tmp_path / ot
        assert path.name == f"{oid}.json"


@pytest.mark.unit
def test_safe_object_id_normalizes_without_raising():
    """非抛错场景的调用方（写侧）用 safe_object_id 拿布尔，不靠异常控流程。"""
    assert safe_object_id("mdl_a_b") is True
    assert safe_object_id("../../x") is False
    assert safe_object_id(None) is False


# ── 目录解析 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_resolve_series_dir_prefers_explicit_root_and_env(tmp_path: Path, monkeypatch):
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("QM_EVAL_SERIES_DIR", str(tmp_path / "fromenv"))

    assert resolve_series_dir(explicit) == explicit
    assert resolve_series_dir() == tmp_path / "fromenv"


# ── 写入 / 读取 ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_save_then_load_round_trips_payload(tmp_path: Path):
    payload = {"series": {"daily_ic": [{"date": "2026-09-18", "value": 0.03}]}}

    written = save_series(
        "model", "mdl_a", payload, root=tmp_path, generated_at="2026-09-19T00:00:00Z"
    )
    loaded = load_series("model", "mdl_a", root=tmp_path)

    assert written["written"] is True
    assert loaded["available"] is True
    assert loaded["data"]["series"]["daily_ic"][0]["value"] == pytest.approx(0.03)
    assert loaded["generated_at"] == "2026-09-19T00:00:00Z"
    assert loaded["version"] == SERIES_VERSION


@pytest.mark.unit
def test_save_series_stamps_version_and_generated_at(tmp_path: Path):
    """版本戳是读取侧判「这份侧车过期口径」的唯一依据，必须写进去。"""
    save_series("model", "mdl_a", {"series": {}}, root=tmp_path)

    raw = json.loads((tmp_path / "model" / "mdl_a.json").read_text(encoding="utf-8"))

    assert raw["version"] == SERIES_VERSION
    assert raw["object_type"] == "model" and raw["object_id"] == "mdl_a"
    assert raw["generated_at"]  # 不传则自动盖当前时间


@pytest.mark.unit
def test_save_series_leaves_no_tmp_file_behind(tmp_path: Path):
    save_series("model", "mdl_a", {"series": {}}, root=tmp_path)

    assert [p.name for p in (tmp_path / "model").iterdir()] == ["mdl_a.json"]


@pytest.mark.unit
def test_load_missing_series_reports_missing_not_empty(tmp_path: Path):
    """没有侧车 ≠ 空序列：前端必须能区分「没算」与「算出来是空的」。"""
    loaded = load_series("model", "mdl_nope", root=tmp_path)

    assert loaded["available"] is False
    assert loaded["reason"] == "missing"
    assert loaded["data"] is None
    assert "侧车不存在" in loaded["note"]


@pytest.mark.unit
def test_load_corrupt_series_reports_corrupt(tmp_path: Path):
    path = tmp_path / "model" / "mdl_bad.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"series": {"daily_ic": [1, 2', encoding="utf-8")

    loaded = load_series("model", "mdl_bad", root=tmp_path)

    assert loaded["available"] is False
    assert loaded["reason"] == "corrupt"


@pytest.mark.unit
def test_load_version_mismatch_is_reported_not_served(tmp_path: Path):
    """口径变了的旧侧车不许直接端给前端（字段含义可能已经不同）。"""
    path = tmp_path / "model" / "mdl_old.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": SERIES_VERSION - 1, "series": {"daily_ic": []}}),
        encoding="utf-8",
    )

    loaded = load_series("model", "mdl_old", root=tmp_path)

    assert loaded["available"] is False
    assert loaded["reason"] == "version_mismatch"
    assert str(SERIES_VERSION) in loaded["note"]


@pytest.mark.unit
def test_load_unsafe_object_id_reports_instead_of_raising(tmp_path: Path):
    """读侧（HTTP 路径）不许抛 500：非法 id 如实报 unsafe 让路由回 400。"""
    loaded = load_series("model", "../../etc/passwd", root=tmp_path)

    assert loaded["available"] is False
    assert loaded["reason"] == "unsafe_object_id"


# ── 含冒号的账户 id（object_id 编码）────────────────────────────────


@pytest.mark.unit
def test_series_filename_encodes_account_id_with_colon(tmp_path: Path):
    """账户 object_id 是 `用户:市场`，冒号在 Windows 文件名里非法 —— 编码后落盘。"""
    # Act
    path = series_path("account", "10000001:CN", root=tmp_path)

    # Assert
    assert path.parent == tmp_path / "account"
    assert path.name == "10000001%3ACN.json"


@pytest.mark.unit
def test_series_filename_round_trips_account_id(tmp_path: Path):
    """写与读走同一个映射：save 之后 load 必须能读回同一个对象。"""
    # Arrange
    written = save_series("account", "10000001:CN", {"series": {}}, root=tmp_path)

    # Act
    loaded = load_series("account", "10000001:CN", root=tmp_path)

    # Assert
    assert written["written"] is True
    assert loaded["available"] is True
    assert loaded["data"]["object_id"] == "10000001:CN"


@pytest.mark.unit
def test_series_filename_keeps_plain_ids_unchanged(tmp_path: Path):
    """安全 id 原样用（既有因子/模型侧车文件名不许变，否则线上文件全部读空）。"""
    for ot, oid in (
        ("model", "mdl_cn_train_20260906064130_ab12cd"),
        ("factor", "a158_KMID"),
        ("strategy", "8fe477cae8ae4673885ed00bf1917bdb"),
        ("daily_selection", "2026-09-18"),
    ):
        assert series_path(ot, oid, root=tmp_path).name == f"{oid}.json"


@pytest.mark.unit
def test_series_filename_still_blocks_traversal_after_encoding(tmp_path: Path):
    """编码不得成为穿越的后门：路径分隔符 / 前导点 / 已编码形态一律拒绝。"""
    for bad in (
        "../../etc/passwd",
        "..",
        "a/../../b",
        "a/b",
        "%2e%2e/x",
        ".hidden",
        "",
        " ",
    ):
        with pytest.raises(UnsafeObjectId):
            series_path("account", bad, root=tmp_path)


@pytest.mark.unit
def test_series_filename_rejects_path_separators_instead_of_encoding():
    """分隔符不做编码而是直接拒：含分隔符的输入是**路径**，不是对象 id。"""
    for bad in ("a/b", "a\\b", "a/../../b"):
        with pytest.raises(UnsafeObjectId):
            series_path("account", bad, root="/tmp")


@pytest.mark.unit
def test_series_filename_never_collides_between_plain_and_encoded():
    """原样 id 永不含 `%`，编码结果必含 `%` —— 两段不撞名（否则两个对象共用一个文件）。"""
    # Arrange
    plain = "10000001_CN"  # 看起来像编码结果，但它是原样 id
    encoded_source = "10000001:CN"

    # Act
    plain_name = series_path("account", plain, root="/tmp").name
    encoded_name = series_path("account", encoded_source, root="/tmp").name

    # Assert
    assert plain_name == "10000001_CN.json"
    assert encoded_name == "10000001%3ACN.json"
    assert plain_name != encoded_name


@pytest.mark.unit
def test_is_series_id_accepts_encodable_and_rejects_traversal():
    """路由的入参闸门（不抛异常）：可编码的 id 放行，穿越尝试拒绝。"""
    assert is_series_id("10000001:CN") is True
    assert is_series_id("2026-09-18") is True
    assert is_series_id("../../etc/passwd") is False
    assert is_series_id("") is False
    assert is_series_id(None) is False
