from fastapi import HTTPException

from backend.services.api.training_explain import normalize_explain
from backend.services.engine.training.local_docker_orchestrator import LocalDockerOrchestrator


def test_normalize_explain_sets_defaults() -> None:
    normalized = normalize_explain(None)
    assert normalized == {
        "enable_shap": True,
        "shap_split": "valid",
        "shap_sample_rows": 30000,
    }


def test_normalize_explain_rejects_invalid_shap_split() -> None:
    try:
        normalize_explain({"enable_shap": True, "shap_split": "oops", "shap_sample_rows": 30000})
        assert False, "expected HTTPException for invalid shap_split"
    except HTTPException as exc:
        assert exc.status_code == 422
        assert "explain.shap_split" in str(exc.detail)


def test_normalize_explain_rejects_out_of_range_shap_sample_rows() -> None:
    try:
        normalize_explain({"enable_shap": True, "shap_split": "valid", "shap_sample_rows": 999})
        assert False, "expected HTTPException for out-of-range shap_sample_rows"
    except HTTPException as exc:
        assert exc.status_code == 422
        assert "explain.shap_sample_rows" in str(exc.detail)


def test_build_config_yaml_passthroughs_explain_values() -> None:
    orchestrator = LocalDockerOrchestrator.__new__(LocalDockerOrchestrator)
    orchestrator.api_base = "http://quantmind-api:8000"
    orchestrator.internal_secret = "secret"
    cfg = orchestrator._build_config_yaml(
        "run_1",
        {
            "job_name": "run_1",
            "features": ["alpha_x"],
            "explain": {
                "enable_shap": False,
                "shap_split": "test",
                "shap_sample_rows": 12345,
            },
        },
    )
    assert cfg["explain"] == {
        "enable_shap": False,
        "shap_split": "test",
        "shap_sample_rows": 12345,
    }


def test_build_config_yaml_uses_explain_defaults_when_missing() -> None:
    orchestrator = LocalDockerOrchestrator.__new__(LocalDockerOrchestrator)
    orchestrator.api_base = "http://quantmind-api:8000"
    orchestrator.internal_secret = "secret"
    cfg = orchestrator._build_config_yaml(
        "run_2",
        {
            "job_name": "run_2",
            "features": ["alpha_x"],
        },
    )
    assert cfg["explain"] == {
        "enable_shap": True,
        "shap_split": "valid",
        "shap_sample_rows": 30000,
    }
