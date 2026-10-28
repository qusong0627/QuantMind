"""资金流概率产物只读层测试：缺失降级 / 日期不覆盖 / 正常读取。"""

from __future__ import annotations

import json

from backend.services.api.market_analysis import flow_probability as fp


def test_missing_artifact_degrades(tmp_path, monkeypatch):
    monkeypatch.setenv("QM_FLOW_PROBABILITY_DIR", str(tmp_path))
    fp._CACHE.clear()
    out = fp.get_probability()
    assert out["available"] is False and out["reason"] == "not_built"


def test_reads_artifact_and_flags_date_mismatch(tmp_path, monkeypatch):
    (tmp_path / fp.ARTIFACT_NAME).write_text(
        json.dumps({"trade_date": "20261027", "latest": {"stocks": {}, "sectors": []}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("QM_FLOW_PROBABILITY_DIR", str(tmp_path))
    fp._CACHE.clear()
    latest = fp.get_probability()
    assert latest["available"] is True and latest["as_of_latest"] is True
    old = fp.get_probability("2026-09-30")
    assert old["available"] is True and old["as_of_latest"] is False
    assert old["requested_date"] == "20260930"
