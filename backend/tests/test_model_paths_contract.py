"""模型生产目录收敛（BUG-08）的回归契约。

`os.getenv("MODELS_PRODUCTION", "/app/models/production")` 原先散落在 7 处，默认值
分散导致改路径要逐个搜改；且兜底目录的默认值口径不一致：

- `model_registry` / `router_service`：`MODELS_FALLBACK_PRODUCTION` 未配置 → 空串（= 无兜底）
- `script_runner`：未配置 → 回落生产目录

现在统一走 `backend.shared.model_paths`，并把上面这个隐式差异变成显式参数
`default_to_production=True`。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]

from backend.shared.model_paths import (  # noqa: E402
    DEFAULT_MODELS_PRODUCTION,
    models_fallback_production_dir,
    models_production_dir,
)

# 生产代码里不允许再出现硬编码默认值（唯一例外是 model_paths 自身的说明）
_ALLOWED = {_BACKEND / "shared" / "model_paths.py"}
_HARDCODED = re.compile(
    r'os\.getenv\(\s*"MODELS_(?:FALLBACK_)?PRODUCTION"\s*,\s*"/app/models/production"\s*\)'
)


def test_default_matches_container_mount() -> None:
    assert DEFAULT_MODELS_PRODUCTION == "/app/models/production"


def test_production_dir_follows_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MODELS_PRODUCTION", str(tmp_path / "prod"))
    assert models_production_dir() == str(tmp_path / "prod")


def test_production_dir_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODELS_PRODUCTION", raising=False)
    assert models_production_dir() == DEFAULT_MODELS_PRODUCTION


def test_fallback_defaults_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置 = 无兜底，保持 model_registry / router_service 的既有语义。"""
    monkeypatch.delenv("MODELS_FALLBACK_PRODUCTION", raising=False)
    assert models_fallback_production_dir() == ""


def test_fallback_can_opt_into_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """script_runner 的「未配置则回落生产目录」语义必须显式声明才能得到。"""
    monkeypatch.delenv("MODELS_FALLBACK_PRODUCTION", raising=False)
    monkeypatch.delenv("MODELS_PRODUCTION", raising=False)
    assert models_fallback_production_dir(default_to_production=True) == DEFAULT_MODELS_PRODUCTION


def test_fallback_env_wins_over_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MODELS_FALLBACK_PRODUCTION", str(tmp_path / "fb"))
    assert models_fallback_production_dir() == str(tmp_path / "fb")
    assert models_fallback_production_dir(default_to_production=True) == str(tmp_path / "fb")


def test_no_hardcoded_default_left_in_production_code() -> None:
    """生产代码不得再各自硬编码默认值，否则改路径仍要逐处搜改。"""
    offenders = []
    for path in (_BACKEND / "shared", _BACKEND / "services", _BACKEND / "scripts"):
        for py in path.rglob("*.py"):
            if py in _ALLOWED or "tests" in py.parts:
                continue
            text = py.read_text(encoding="utf-8", errors="ignore")
            if _HARDCODED.search(text):
                offenders.append(str(py.relative_to(_BACKEND)))
    assert not offenders, f"仍在硬编码生产目录默认值: {offenders}"


def test_known_call_sites_use_shared_helper() -> None:
    """原先 6 处散落点必须都改走共享入口。"""
    expected = {
        "shared/model_registry.py",
        "services/engine/inference/router_service.py",
        "services/engine/inference/script_runner.py",
        "services/trade/services/trading_precheck_service.py",
        "services/simulation/replay/signal_generator.py",
        "services/simulation/replay/router.py",
    }
    missing = []
    for rel in expected:
        text = (_BACKEND / rel).read_text(encoding="utf-8")
        if "model_paths" not in text:
            missing.append(rel)
    assert not missing, f"以下文件未改走 backend.shared.model_paths: {sorted(missing)}"
