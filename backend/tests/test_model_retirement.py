"""§7 退役保留策略（P4）单元：``plan_retirement`` 纯函数 + ``purge_model_artifacts`` 安全闸。

口径钉死（设计 §7「退役」行）：保留 N=3 版本（按 ``(tenant,user,market)`` 分组）+ 30 天
冷却（含边界）+ 缺时间戳/越界/符号链接/被活跃 rollout 引用一律不清退。无 IO。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.shared.model_retirement import (
    COOLDOWN_DAYS,
    KEEP_N,
    plan_retirement,
    purge_model_artifacts,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)


def test_spec_pins_keep_n_and_cooldown():
    """设计 §7：保留 N=3、冷却 30 天——改常量就是改验收口径，单独钉住。"""
    assert KEEP_N == 3
    assert COOLDOWN_DAYS == 30


def _row(mid: str, *, days: float, market: str = "CN", tenant: str = "t1",
         user: str = "u1", **extra) -> dict:
    return {
        "model_id": mid,
        "tenant_id": tenant,
        "user_id": user,
        "market": market,
        "archived_at": (_NOW - timedelta(days=days)).isoformat(),
        **extra,
    }


# ── plan_retirement：保留面 ────────────────────────────────────────────


def test_keep_n_keeps_newest_and_purges_older():
    rows = [_row("m40", days=40), _row("m35", days=35), _row("m30", days=30), _row("m5", days=5)]
    plan = plan_retirement(rows, keep_n=3, cooldown_days=30, now=_NOW)
    assert [e["model_id"] for e in plan["kept"]] == ["m5", "m30", "m35"]
    assert [e["model_id"] for e in plan["purge"]] == ["m40"]
    assert all(e["reason"] == "retention_excess" for e in plan["purge"])


def test_keep_n_zero_purges_all_eligible():
    rows = [_row("m60", days=60), _row("m50", days=50), _row("m45", days=45)]
    plan = plan_retirement(rows, keep_n=0, cooldown_days=30, now=_NOW)
    assert plan["kept"] == []
    # 最老先清
    assert [e["model_id"] for e in plan["purge"]] == ["m60", "m50", "m45"]


def test_cooldown_boundary_exactly_30d_purges_29d_skips():
    rows = [_row("m_exact30", days=30), _row("m29", days=29)]
    plan = plan_retirement(rows, keep_n=0, cooldown_days=30, now=_NOW)
    assert [e["model_id"] for e in plan["purge"]] == ["m_exact30"]
    skipped = {e["model_id"]: e for e in plan["skipped"]}
    assert skipped["m29"]["reason"] == "cooldown"
    assert skipped["m29"]["eligible_at"] == (_NOW + timedelta(days=1)).isoformat()


def test_groups_isolated_per_market():
    """keep_n 按 (tenant,user,market) 分组：CN 的多余归档不许挤掉 HK 自己的保留名额。"""
    rows = [
        _row("cn40", days=40, market="CN"), _row("cn35", days=35, market="CN"),
        _row("hk40", days=40, market="HK"), _row("hk35", days=35, market="HK"),
    ]
    plan = plan_retirement(rows, keep_n=1, cooldown_days=30, now=_NOW)
    assert sorted(e["model_id"] for e in plan["kept"]) == ["cn35", "hk35"]
    assert sorted(e["model_id"] for e in plan["purge"]) == ["cn40", "hk40"]


def test_same_timestamp_tiebreak_deterministic_input_order_independent():
    rows = [_row("ma", days=40), _row("mb", days=40), _row("mc", days=40)]
    plan1 = plan_retirement(rows, keep_n=1, cooldown_days=30, now=_NOW)
    plan2 = plan_retirement(list(reversed(rows)), keep_n=1, cooldown_days=30, now=_NOW)
    assert [e["model_id"] for e in plan1["kept"]] == ["mc"]  # 平手取 model_id 最大
    assert [e["model_id"] for e in plan1["purge"]] == ["ma", "mb"]
    assert plan1 == plan2  # 输入顺序不影响结果


def test_missing_or_unparseable_timestamp_never_purged():
    rows = [
        _row("m_bad", days=40, archived_at="not-a-date"),
        _row("m_none", days=40, archived_at=None),
        _row("m_ok", days=40),
    ]
    plan = plan_retirement(rows, keep_n=0, cooldown_days=30, now=_NOW)
    assert [e["model_id"] for e in plan["purge"]] == ["m_ok"]
    bad = {e["model_id"]: e["reason"] for e in plan["skipped"]}
    assert bad["m_bad"] == "missing_archived_at"
    assert bad["m_none"] == "missing_archived_at"


def test_blocked_beyond_keep_skipped_with_reason():
    rows = [
        _row("m_blocked", days=40, blocked=True, blocked_reason="active_rollout_reference"),
        _row("m_ok", days=40),
    ]
    plan = plan_retirement(rows, keep_n=0, cooldown_days=30, now=_NOW)
    assert [e["model_id"] for e in plan["purge"]] == ["m_ok"]
    assert plan["skipped"][0]["model_id"] == "m_blocked"
    assert plan["skipped"][0]["reason"] == "active_rollout_reference"


def test_naive_datetime_and_z_suffix_accepted():
    rows = [
        _row("m_naive", days=40, archived_at=(_NOW - timedelta(days=40)).replace(tzinfo=None)),
        _row("m_z", days=40, archived_at=(_NOW - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"),
    ]
    plan = plan_retirement(rows, keep_n=0, cooldown_days=30, now=_NOW)
    assert sorted(e["model_id"] for e in plan["purge"]) == ["m_naive", "m_z"]


def test_blank_model_id_skipped_not_crash():
    """生产库有 ``model_id=''`` 的归档哨兵行（{readonly, system_default} 实测存在）：
    如实跳过并记原因，不许整轮巡检崩掉。"""
    rows = [_row("m_ok", days=40), _row("", days=40), _row("   ", days=40)]
    plan = plan_retirement(rows, keep_n=0, cooldown_days=30, now=_NOW)
    assert [e["model_id"] for e in plan["purge"]] == ["m_ok"]
    blanks = [e for e in plan["skipped"] if e["reason"] == "missing_model_id"]
    assert len(blanks) == 2


def test_invalid_params_raise():
    with pytest.raises(ValueError):
        plan_retirement([], keep_n=-1, now=_NOW)
    with pytest.raises(ValueError):
        plan_retirement([], cooldown_days=-1, now=_NOW)


def test_empty_rows_gives_empty_plan():
    plan = plan_retirement([], now=_NOW)
    assert plan == {"purge": [], "kept": [], "skipped": []}


# ── purge_model_artifacts：安全闸 ─────────────────────────────────────


def _seed_dir(root, mid: str, files: int = 2, nested: bool = True):
    d = root / mid
    d.mkdir(parents=True)
    for i in range(files):
        (d / f"f{i}.bin").write_bytes(b"x" * (i + 1))
    if nested:
        (d / "sub").mkdir()
        (d / "sub" / "deep.bin").write_bytes(b"y" * 10)
    return d


def test_purge_removes_directory_and_counts(tmp_path):
    root = tmp_path / "root"
    d = _seed_dir(root, "m1")
    result = purge_model_artifacts(root, "m1", str(d))
    assert result["removed"] is True and result["existed"] is True
    assert result["files_removed"] == 3  # 2 个顶层 + 1 个嵌套
    assert result["bytes_freed"] == 1 + 2 + 10
    assert not d.exists()


def test_purge_without_storage_path_uses_root_mid(tmp_path):
    root = tmp_path / "root"
    d = _seed_dir(root, "m1")
    result = purge_model_artifacts(root, "m1")
    assert result["removed"] is True
    assert not d.exists()


def test_purge_refuses_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.bin").write_bytes(b"z")
    result = purge_model_artifacts(root, "m1", str(outside))
    assert result["removed"] is False and result["reason"] == "outside_models_root"
    assert (outside / "keep.bin").exists()


def test_purge_refuses_root_itself(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    result = purge_model_artifacts(root, "m1", str(root))
    assert result["removed"] is False and result["reason"] == "outside_models_root"
    assert root.exists()


def test_purge_refuses_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.bin").write_bytes(b"z")
    link = root / "m1"
    link.symlink_to(target)
    result = purge_model_artifacts(root, "m1", str(link))
    assert result["removed"] is False and result["reason"] == "symlink_refused"
    assert (target / "keep.bin").exists()


def test_purge_missing_dir_reports_not_found(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    result = purge_model_artifacts(root, "nope")
    assert result["removed"] is False and result["existed"] is False
    assert result["reason"] == "not_found"


def test_purge_bad_model_id_raises(tmp_path):
    root = tmp_path / "root"
    for bad in ("", "..", "a/b"):
        with pytest.raises(ValueError):
            purge_model_artifacts(root, bad)


def test_purge_counts_real_files_but_not_symlinks_inside(tmp_path):
    root = tmp_path / "root"
    d = _seed_dir(root, "m1", files=1, nested=False)
    (d / "link").symlink_to(d / "f0.bin")
    result = purge_model_artifacts(root, "m1")
    assert result["removed"] is True
    assert result["files_removed"] == 1  # 链接不计数、不 deref
    assert not d.exists()
