"""特征选择台账（registry）与自动经验/坑生成器的单元测试。

设计约束（防过拟合）：
- 经验只从验证窗（valid）生成；测试窗（test）只做最终核验，不参与学习。
- 台账只记录与诊断，不自动改门槛/配额；改协议必须升 protocol_version。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from backend.scripts.stock_classifier import selection_registry as reg


def _selection_payload() -> dict:
    features = [
        {
            "code": "alpha_good",
            "reason": "test",
            "group": "量价/挖掘",
            "library": "lib_a",
            "source": "lib_a:alpha_good",
        },
        {
            "code": "alpha_flip",
            "reason": "test",
            "group": "量价/挖掘",
            "library": "lib_a",
            "source": "lib_a:alpha_flip",
        },
        {
            "code": "fund_good",
            "reason": "test",
            "group": "基本面",
            "library": "lib_b",
            "source": "lib_b:fund_good",
        },
    ]
    metrics = {
        "alpha_good": {
            "group": "量价/挖掘",
            "library": "lib_a",
            "ic5": 0.05,
            "ic3": 0.04,
            "ic1": 0.02,
            "ic1_cc": 0.05,
            "icir5": 0.60,
            "pos5": 0.70,
            "coverage": 1.0,
            "cap_ic": [0.06, 0.05, 0.04],
            "lgb_gain": 10.0,
            "gain_ratio": 5.0,
            "gain_rank": 1,
        },
        "alpha_flip": {
            "group": "量价/挖掘",
            "library": "lib_a",
            "ic5": -0.04,
            "ic3": -0.03,
            "ic1": -0.02,
            "ic1_cc": -0.04,
            "icir5": -0.50,
            "pos5": 0.30,
            "coverage": 0.95,
            "cap_ic": [-0.05, -0.04, -0.03],
            "lgb_gain": 8.0,
            "gain_ratio": 4.0,
            "gain_rank": 2,
        },
        "fund_good": {
            "group": "基本面",
            "library": "lib_b",
            "ic5": 0.03,
            "ic3": 0.02,
            "ic1": 0.01,
            "ic1_cc": 0.03,
            "icir5": 0.30,
            "pos5": 0.62,
            "coverage": 0.98,
            "cap_ic": [0.03, 0.03, 0.02],
            "lgb_gain": 5.0,
            "gain_ratio": 2.0,
            "gain_rank": 3,
        },
    }
    return {
        "generated_at": "2026-11-10T16:52:21",
        "protocol": {
            "models": ["T+3", "T+5"],
            "selection_label": "T+5",
            "tag_cutoff": "2024-12-31",
            "train": ["2016-07-01", "2024-12-31"],
            "valid": ["2025-01-01", "2025-12-31"],
            "test": ["2026-01-01", "2026-11-05"],
            "dedup": 0.9,
            "sample_step": 10,
            "coarse_n": 240,
            "n_sample_days": 207,
            "n_sample_rows": 705579,
            "n_lgb_rows": 248400,
        },
        "selected": {"100": features, "200": features},
        "metrics": metrics,
        "rejected": [{"code": "bad", "group": "其他", "reason": "|IC5|<0.010"}],
    }


def _protocol_snapshot() -> dict:
    return {
        "protocol_version": "v1",
        "gates": {"min_abs_icir5": 0.15},
        "quotas": {"量价/挖掘": 0.35},
        "notes": ["test"],
    }


def _oos_payload() -> dict:
    return {
        "valid": {
            "alpha_good": {"icir5": 0.55, "ic5": 0.045, "n": 60},
            "alpha_flip": {"icir5": 0.40, "ic5": 0.030, "n": 60},
            "fund_good": {"icir5": 0.20, "ic5": 0.020, "n": 60},
        },
        "test": {
            "alpha_good": {"icir5": 0.50, "ic5": 0.040, "n": 40},
            "alpha_flip": {"icir5": 0.10, "ic5": 0.010, "n": 40},
            "fund_good": {"icir5": 0.15, "ic5": 0.015, "n": 40},
        },
        "meta": {"start": "2025-01-01", "end": "2026-11-05", "step": 5},
    }


def _record_with_oos() -> dict:
    record = reg.build_run_record(
        _selection_payload(), top=100, protocol_snapshot=_protocol_snapshot()
    )
    return reg.merge_oos(record, _oos_payload())


def _record_perfect() -> dict:
    record = reg.build_run_record(
        _selection_payload(), top=100, protocol_snapshot=_protocol_snapshot()
    )
    oos = {
        "valid": {
            f["code"]: {"icir5": f["selection"]["icir5"] * 0.9}
            for f in record["features"]
        },
        "test": {},
        "meta": {},
    }
    return reg.merge_oos(record, oos)


@pytest.mark.unit
def test_make_run_id_deterministic_and_collision_safe():
    a = reg.make_run_id("2026-11-10T16:52:21", 100)
    b = reg.make_run_id("2026-11-10T16:52:21", 100)
    c = reg.make_run_id("2026-11-10T16:52:21", 100, existing={a})
    assert a == b == "sel_20261110_165221_top100"
    assert c == "sel_20261110_165221_top100_2"


@pytest.mark.unit
def test_build_run_record_extracts_features_and_protocol_hash():
    record = reg.build_run_record(
        _selection_payload(), top=100, protocol_snapshot=_protocol_snapshot()
    )
    assert record["schema_version"] == reg.SCHEMA_VERSION
    assert record["run_id"] == "sel_20261110_165221_top100"
    assert record["protocol_version"] == "v1"
    assert record["protocol_hash"] == reg.protocol_hash(_protocol_snapshot())
    assert [f["code"] for f in record["features"]] == [
        "alpha_good",
        "alpha_flip",
        "fund_good",
    ]
    assert record["features"][0]["selection"]["icir5"] == 0.60
    assert record["features"][0]["lifecycle"] == "selected"
    assert record["rejected_count"] == 1
    assert record["model_jobs"] == {}


@pytest.mark.unit
def test_build_run_record_does_not_mutate_input():
    payload = _selection_payload()
    before = json.dumps(payload, sort_keys=True)
    record = reg.build_run_record(payload, top=100)
    record["features"][0]["lifecycle"] = "retired"
    assert json.dumps(payload, sort_keys=True) == before


@pytest.mark.unit
def test_merge_model_jobs_keeps_original():
    record = reg.build_run_record(_selection_payload(), top=100)
    jobs = {
        "jobs": {
            "t3": {"run_id": "train_1", "status": "running", "result": {"logs": "x"}},
            "t5": {"run_id": "train_2", "status": "pending"},
        }
    }
    merged = reg.merge_model_jobs(record, jobs)
    assert record["model_jobs"] == {}
    assert merged["model_jobs"]["t3"]["run_id"] == "train_1"
    assert merged["model_jobs"]["t3"]["status"] == "running"
    assert "logs" not in merged["model_jobs"]["t3"]["result"]
    assert merged["model_jobs"]["t5"]["run_id"] == "train_2"


@pytest.mark.unit
def test_merge_oos_attaches_only_known_codes():
    record = reg.build_run_record(_selection_payload(), top=100)
    oos = {
        "valid": {"alpha_good": {"icir5": 0.5}, "unknown": {"icir5": 0.9}},
        "test": {},
        "meta": {"step": 5},
    }
    merged = reg.merge_oos(record, oos)
    by_code = {f["code"]: f for f in merged["features"]}
    assert by_code["alpha_good"]["oos"]["valid"]["icir5"] == 0.5
    assert "unknown" not in by_code
    assert merged["oos_meta"] == {"step": 5}


@pytest.mark.unit
def test_audit_window_perfect_positive_correlation():
    audit = reg.audit_window(_record_perfect(), "valid")
    assert audit["n"] == 3
    assert audit["spearman_signed"] == pytest.approx(1.0)
    assert audit["sign_hit_rate"] == pytest.approx(1.0)
    assert audit["metric_effectiveness"]["selection_icir5"][
        "spearman_signed"
    ] == pytest.approx(1.0)


@pytest.mark.unit
def test_audit_window_anti_correlation():
    record = _record_perfect()
    for f in record["features"]:
        f["oos"]["valid"]["icir5"] = -f["selection"]["icir5"]
    audit = reg.audit_window(record, "valid")
    assert audit["spearman_signed"] == pytest.approx(-1.0)
    assert audit["sign_hit_rate"] == pytest.approx(0.0)


@pytest.mark.unit
def test_audit_ignores_missing_oos():
    record = _record_with_oos()
    record["features"][0]["oos"]["valid"] = {}
    audit = reg.audit_window(record, "valid")
    assert audit["n"] == 2
    assert audit["missing"] == 1


@pytest.mark.unit
def test_build_lessons_sign_flip_is_pitfall():
    record = _record_with_oos()
    lessons = reg.build_lessons(record, window="valid")
    flip = [x for x in lessons if x["key"] == "alpha_flip"]
    assert flip and flip[0]["kind"] == "pitfall"
    assert flip[0]["scope"] == "feature"
    assert flip[0]["do_not_apply_to"] == ["test"]


@pytest.mark.unit
def test_build_lessons_persisted_is_lesson():
    record = _record_with_oos()
    lessons = reg.build_lessons(record, window="valid")
    good = [x for x in lessons if x["key"] == "alpha_good"]
    assert good and good[0]["kind"] == "lesson"
    assert good[0]["scope"] == "feature"


@pytest.mark.unit
def test_build_lessons_never_uses_test_window():
    record = _record_with_oos()
    record["features"][0]["oos"]["valid"] = {}
    record["features"][1]["oos"]["valid"] = {}
    record["features"][2]["oos"]["valid"] = {}
    lessons = reg.build_lessons(record, window="valid")
    assert lessons == []


@pytest.mark.unit
def test_merge_lessons_confirms_after_two_runs():
    record = _record_with_oos()
    first = reg.build_lessons(record, window="valid")
    merged = reg.merge_lessons([], first)
    assert all(x["status"] == "open" for x in merged)
    second = [dict(x, run_id="sel_other") for x in first]
    merged2 = reg.merge_lessons(merged, second)
    assert all(x["status"] == "confirmed" for x in merged2)
    assert all(x["times_seen"] == 2 for x in merged2)


@pytest.mark.unit
def test_registry_register_and_attach_grows_feature_history(tmp_path: Path):
    store = reg.SelectionRegistry(tmp_path)
    record = store.register(
        _selection_payload(), top=100, protocol_snapshot=_protocol_snapshot()
    )
    assert (tmp_path / "runs" / f"{record['run_id']}.json").exists()
    store.attach_oos(record["run_id"], {"valid": {"alpha_good": {"icir5": 0.5}}})
    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    features = json.loads((tmp_path / "features.json").read_text(encoding="utf-8"))
    assert index["runs"][0]["run_id"] == record["run_id"]
    assert features["features"]["alpha_good"]["runs"] == [record["run_id"]]
    assert features["features"]["alpha_good"]["latest"]["oos_valid_icir5"] == 0.5
    assert len(features["features"]["alpha_good"]["history"]) == 1


@pytest.mark.unit
def test_registry_upsert_is_idempotent(tmp_path: Path):
    store = reg.SelectionRegistry(tmp_path)
    first = store.upsert(_selection_payload(), top=100)
    second = store.upsert(
        _selection_payload(),
        top=100,
        model_jobs={"jobs": {"t3": {"run_id": "train_1", "status": "running"}}},
    )
    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert first["run_id"] == second["run_id"]
    assert len(index["runs"]) == 1
    assert second["model_jobs"]["t3"]["run_id"] == "train_1"


@pytest.mark.unit
def test_registry_upsert_merges_data_issues(tmp_path: Path):
    store = reg.SelectionRegistry(tmp_path)
    first = store.upsert(
        _selection_payload(),
        top=100,
        data_issues=[{"key": "a", "statement": "first"}],
    )
    second = store.upsert(
        _selection_payload(),
        top=100,
        data_issues=[
            {"key": "a", "statement": "updated"},
            {"key": "b", "statement": "second"},
        ],
    )
    assert first["run_id"] == second["run_id"]
    assert {x["key"]: x["statement"] for x in second["data_issues"]} == {
        "a": "updated",
        "b": "second",
    }


@pytest.mark.unit
def test_registry_lessons_are_appended_and_rendered(tmp_path: Path):
    store = reg.SelectionRegistry(tmp_path)
    record = store.register(_selection_payload(), top=100)
    store.attach_oos(record["run_id"], _oos_payload())
    lessons = reg.build_lessons(store.get(record["run_id"]), window="valid")
    store.add_lessons(record["run_id"], lessons)
    assert (tmp_path / "lessons.json").exists()
    assert (tmp_path / "lessons.jsonl").exists()
    assert (tmp_path / "lessons.md").exists()
    ledger = json.loads((tmp_path / "lessons.json").read_text(encoding="utf-8"))
    assert ledger["lessons"]


@pytest.mark.unit
def test_daily_growth_orchestrates_registry(tmp_path: Path, monkeypatch):
    from backend.scripts.stock_classifier import daily_growth

    selection_path = tmp_path / "selection.json"
    selection_path.write_text(
        json.dumps(_selection_payload(), ensure_ascii=False), encoding="utf-8"
    )
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_text(json.dumps(_protocol_snapshot()), encoding="utf-8")
    oos_path = tmp_path / "oos.json"
    oos_path.write_text(json.dumps(_oos_payload()), encoding="utf-8")
    out = tmp_path / "audit.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "daily_growth.py",
            "--selection",
            str(selection_path),
            "--registry",
            str(tmp_path / "registry"),
            "--protocol",
            str(protocol_path),
            "--oos",
            str(oos_path),
            "--out-md",
            str(out),
        ],
    )
    assert daily_growth.main() == 0
    assert (tmp_path / "registry" / "runs" / "sel_20261110_165221_top100.json").exists()
    assert (tmp_path / "registry" / "lessons.json").exists()
    assert out.exists()
    assert "防过拟合" in out.read_text(encoding="utf-8")


@pytest.mark.unit
def test_registry_flags_protocol_drift(tmp_path: Path):
    store = reg.SelectionRegistry(tmp_path)
    store.register(
        _selection_payload(), top=100, protocol_snapshot=_protocol_snapshot()
    )
    changed = {**_protocol_snapshot(), "gates": {"min_abs_icir5": 0.20}}
    second = store.register(_selection_payload(), top=100, protocol_snapshot=changed)
    assert second["protocol_drift"]["expected_hash"] == reg.protocol_hash(
        _protocol_snapshot()
    )
    assert second["protocol_drift"]["actual_hash"] == reg.protocol_hash(changed)


@pytest.mark.unit
def test_load_model_eval_reads_split_and_shap(tmp_path: Path):
    job_dir = tmp_path / "train_1"
    job_dir.mkdir()
    (job_dir / "eval_report.json").write_text(
        json.dumps(
            {
                "by_split": {"test": {"mean": 0.08, "icir": 0.4, "win_rate": 0.62}},
                "rank_ic": {"mean": 0.1},
                "long_short": {"sharpe": 4.6, "max_drawdown": -0.32, "curve": []},
                "groups": {"monotonicity": 1.0},
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "shap_summary.csv").write_text(
        "feature,mean_abs_shap,mean_shap,positive_ratio\n"
        "a,0.005,0.001,0.6\nb,0.004,0.002,0.5\n",
        encoding="utf-8",
    )
    evaluation = reg.load_model_eval("train_1", root=tmp_path)
    assert evaluation["by_split"]["test"]["icir"] == 0.4
    assert evaluation["long_short"] == {"sharpe": 4.6, "max_drawdown": -0.32}
    assert evaluation["shap_top"][0] == {"feature": "a", "mean_abs_shap": 0.005}


@pytest.mark.unit
def test_merge_lessons_updates_kind_on_same_object():
    first = [
        {
            "kind": "lesson",
            "scope": "metric",
            "key": "selection_pos5",
            "statement": "old",
            "seen_in_runs": ["r1"],
            "run_id": "r1",
        }
    ]
    second = [
        {
            "kind": "pitfall",
            "scope": "metric",
            "key": "selection_pos5",
            "statement": "new",
            "seen_in_runs": ["r2"],
            "run_id": "r2",
        }
    ]
    merged = reg.merge_lessons(first, second)
    assert len(merged) == 1
    assert merged[0]["kind"] == "pitfall"
    assert merged[0]["statement"] == "new"
    assert merged[0]["times_seen"] == 2
    assert merged[0]["status"] == "confirmed"
