"""因子库代次指针（D2）测试。"""

from __future__ import annotations

from pathlib import Path

from backend.shared import factor_generation as g


def test_record_and_read_active(tmp_path: Path):
    payload = g.record_generation(
        tmp_path,
        generation_id="g1",
        source_fingerprint="20261104|2631",
        partitions=2631,
        schema_hash="abc",
    )

    assert payload["generation_id"] == "g1"
    assert (tmp_path / "generations" / "g1.json").is_file()
    assert (tmp_path / "generation.json").is_file()
    active = g.read_active(tmp_path)
    assert active is not None
    assert active["generation_id"] == "g1"
    assert active["partitions"] == 2631


def test_pinned_old_generation_is_readable_but_not_active(tmp_path: Path):
    g.record_generation(tmp_path, generation_id="g1", source_fingerprint="old")
    g.record_generation(tmp_path, generation_id="g2", source_fingerprint="new")

    pinned = g.validate_generation(tmp_path, "g1")
    assert pinned["ok"] is True
    assert pinned["requested"]["generation_id"] == "g1"

    strict = g.validate_generation(tmp_path, "g1", require_active=True)
    assert strict["ok"] is False
    assert any("generation_not_active" in r for r in strict["reasons"])

    current = g.validate_generation(tmp_path, "g2", require_active=True)
    assert current["ok"] is True


def test_missing_generation_and_no_active(tmp_path: Path):
    assert g.validate_generation(tmp_path)["ok"] is False
    assert g.read_active(tmp_path) is None

    g.record_generation(tmp_path, generation_id="g1")
    missing = g.validate_generation(tmp_path, "nope")
    assert missing["ok"] is False
    assert "generation_not_found" in missing["reasons"]
