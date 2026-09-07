"""enricher 版本升级/重跑判定 + sentiment 状态字段的单元测试。

纯函数测试，不触库。核心语义：
- FinBERT 就绪 → 目标版本带 +finbert 后缀，纯词典法行落后 → 重跑升级
- FinBERT 停用 → 目标版本无后缀，+finbert 行不降级重跑（历史融合结果保留）
"""

from __future__ import annotations

import pytest

from backend.services.api.news import sentiment as sentiment_mod
from backend.services.api.news.enricher import is_row_outdated

LEX_ONLY = "ac-v5+lex-v4+ent+cn"
FUSED = "ac-v5+lex-v4+ent+cn+finbert"
LEGACY = "ac-v1+lex-v1"


@pytest.mark.unit
def test_missing_row_is_outdated():
    """未 enrich / 出错行（空版本）必须重跑。"""
    assert is_row_outdated(None, LEX_ONLY) is True
    assert is_row_outdated("", LEX_ONLY) is True


@pytest.mark.unit
def test_same_version_skips():
    """版本与目标一致时跳过（幂等断点语义）。"""
    assert is_row_outdated(LEX_ONLY, LEX_ONLY) is False
    assert is_row_outdated(FUSED, FUSED) is False


@pytest.mark.unit
def test_lex_row_upgrades_when_finbert_on():
    """FinBERT 开启后，纯词典法行落后于融合目标版本 → 重跑。"""
    assert is_row_outdated(LEX_ONLY, FUSED) is True


@pytest.mark.unit
def test_fused_row_not_downgraded_when_finbert_off():
    """FinBERT 停用后，融合行不得降级回纯词典法 → 跳过。"""
    assert is_row_outdated(FUSED, LEX_ONLY) is False


@pytest.mark.unit
def test_legacy_lex_row_never_rerun():
    """远古 ac-v1+lex-v1 行保留不动（与历史行为一致）。"""
    assert is_row_outdated(LEGACY, LEX_ONLY) is False
    assert is_row_outdated(LEGACY, FUSED) is False


@pytest.mark.unit
def test_current_target_version_tracks_model_state(monkeypatch):
    """current_target_version 跟随模型就绪状态切换后缀。"""
    # Arrange
    from backend.services.api.news.enricher import current_target_version

    monkeypatch.setattr(sentiment_mod, "_model_ready", False)
    monkeypatch.setattr(sentiment_mod, "_model_failed", True)
    # Act & Assert：模型不可用 → 无 +finbert
    assert current_target_version() == LEX_ONLY

    monkeypatch.setattr(sentiment_mod, "_model_ready", True)
    monkeypatch.setattr(sentiment_mod, "_model_failed", False)
    # Act & Assert：模型就绪 → 带 +finbert
    assert current_target_version() == FUSED


@pytest.mark.unit
def test_status_exposes_cpu_threads(monkeypatch):
    """get_finbert_status 在 CPU 环境暴露限制后的线程数。"""
    monkeypatch.setattr(sentiment_mod, "DEVICE", -1)
    monkeypatch.setenv("FINBERT_CPU_THREADS", "3")
    assert sentiment_mod.cpu_inference_threads() == 3
    st = sentiment_mod.get_finbert_status()
    assert st["cpu_threads"] == 3

    monkeypatch.setattr(sentiment_mod, "DEVICE", 0)
    st_gpu = sentiment_mod.get_finbert_status()
    assert st_gpu["cpu_threads"] is None
