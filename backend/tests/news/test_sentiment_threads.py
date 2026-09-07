"""FinBERT CPU 线程限制 + 运行时开关的单元测试。

不加载真实 torch/transformers——sentiment.py 只在 _try_load 内惰性导入，
测试用 sys.modules 假模块替换即可覆盖线程设置逻辑。
"""

from __future__ import annotations

import sys

import pytest

from backend.services.api.news import sentiment as sentiment_mod

# pipeline 假实现：记录 kwargs，返回单条 positive 结果
def _fake_pipeline(*args, **kwargs):
    _fake_pipeline.kwargs = kwargs
    _fake_pipeline.calls = getattr(_fake_pipeline, "calls", 0) + 1
    return lambda text: [{"label": "positive", "score": 0.9}]


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """每个用例前重置单例状态，避免用例间互相污染。"""
    sentiment_mod._model_ready = False
    sentiment_mod._model_failed = False
    sentiment_mod._pipeline = None
    sentiment_mod._MODEL_LOAD_THREAD = None
    sentiment_mod._RUNTIME_OVERRIDE = None
    sentiment_mod._RUNTIME_MTIME = 0.0
    sentiment_mod._AUTO_REWRITE_THREAD = None
    _fake_pipeline.kwargs = None
    _fake_pipeline.calls = 0
    yield


@pytest.fixture
def fake_torch_and_transformers(monkeypatch):
    """用假 torch/transformers 替换 sys.modules，捕获 set_num_threads 调用。"""
    import types

    fake_torch = types.SimpleNamespace()
    fake_torch.set_num_threads = lambda n: setattr(fake_torch, "threads", n)

    fake_transformers = types.ModuleType("transformers")
    fake_transformers.pipeline = _fake_pipeline
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    return fake_torch


@pytest.mark.unit
def test_cpu_load_caps_threads_to_env_value(monkeypatch, fake_torch_and_transformers):
    """CPU 环境（DEVICE=-1）加载模型时按 FINBERT_CPU_THREADS 限制线程数。"""
    # Arrange
    monkeypatch.setattr(sentiment_mod, "DEVICE", -1)
    monkeypatch.setenv("FINBERT_CPU_THREADS", "3")

    # Act
    sentiment_mod._try_load()

    # Assert
    assert fake_torch_and_transformers.threads == 3
    assert sentiment_mod._model_ready


@pytest.mark.unit
def test_cpu_load_defaults_to_two_threads(monkeypatch, fake_torch_and_transformers):
    """未设置 FINBERT_CPU_THREADS 时默认限制 2 线程。"""
    # Arrange
    monkeypatch.setattr(sentiment_mod, "DEVICE", -1)
    monkeypatch.delenv("FINBERT_CPU_THREADS", raising=False)

    # Act
    sentiment_mod._try_load()

    # Assert
    assert fake_torch_and_transformers.threads == 2


@pytest.mark.unit
def test_cpu_load_rejects_invalid_env_value(monkeypatch, fake_torch_and_transformers):
    """FINBERT_CPU_THREADS 非法时回退默认 2，不抛错。"""
    # Arrange
    monkeypatch.setattr(sentiment_mod, "DEVICE", -1)
    monkeypatch.setenv("FINBERT_CPU_THREADS", "abc")

    # Act
    sentiment_mod._try_load()

    # Assert
    assert fake_torch_and_transformers.threads == 2


@pytest.mark.unit
def test_gpu_load_does_not_cap_threads(monkeypatch, fake_torch_and_transformers):
    """GPU 环境（DEVICE>=0）不设置 CPU 线程限制。"""
    # Arrange
    monkeypatch.setattr(sentiment_mod, "DEVICE", 0)

    # Act
    sentiment_mod._try_load()

    # Assert
    assert not hasattr(fake_torch_and_transformers, "threads")
    assert _fake_pipeline.kwargs.get("device") == 0


@pytest.mark.unit
def test_runtime_toggle_file_off_disables(monkeypatch, tmp_path):
    """运行时开关文件内容为 false 时，即使已安装模型也视为关闭。"""
    # Arrange：假模型目录（is_model_installed 需要 config.json + 权重）
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "model.safetensors").write_text("fake", encoding="utf-8")
    monkeypatch.setattr(sentiment_mod, "DEFAULT_MODEL", str(model_dir))
    toggle_file = tmp_path / "enabled"
    toggle_file.write_text("false", encoding="utf-8")
    monkeypatch.setattr(sentiment_mod, "_RUNTIME_TOGGLE_PATH", str(toggle_file))

    # Act & Assert
    assert sentiment_mod.is_finbert_enabled() is False


@pytest.mark.unit
def test_runtime_toggle_file_on_overrides_cpu_default(monkeypatch, tmp_path):
    """运行时开关文件为 true 时覆盖 CPU 环境默认关闭（管理员显式开启语义）。"""
    # Arrange
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "model.safetensors").write_text("fake", encoding="utf-8")
    monkeypatch.setattr(sentiment_mod, "DEFAULT_MODEL", str(model_dir))
    toggle_file = tmp_path / "enabled"
    toggle_file.write_text("true", encoding="utf-8")
    monkeypatch.setattr(sentiment_mod, "_RUNTIME_TOGGLE_PATH", str(toggle_file))
    monkeypatch.setattr(sentiment_mod, "USE_FINBERT", False)

    # Act & Assert
    assert sentiment_mod.is_finbert_enabled() is True
