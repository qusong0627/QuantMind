"""全局自动推理任务的生产模型前置检查。"""

from __future__ import annotations

from backend.services.engine.tasks import celery_tasks


def test_production_inference_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("MODELS_PRODUCTION", str(tmp_path))
    assert celery_tasks._production_inference_available() is False


def test_production_inference_present(monkeypatch, tmp_path):
    (tmp_path / "inference.py").write_text("# model\n", encoding="utf-8")
    monkeypatch.setenv("MODELS_PRODUCTION", str(tmp_path))
    assert celery_tasks._production_inference_available() is True
