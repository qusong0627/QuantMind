"""编排模式开关（QM_ORCHESTRATION_MODE）测试。

背景：实盘策略执行走**本机进程内沙箱**（`/start` → sandbox_manager.submit_strategy），
`create_deployment`/`delete_deployment` 在整个代码库里没有被任何路径调用；Docker
只服务 AI-IDE 代码执行 / minibt 回测 / 模型训练。缺省「按 Docker 引擎判就绪」会把
一台只用实盘的机器（Windows 免容器节点包）永久拦在 `/start` 之外 —— `/start` 复用
同一份准备度检测，任一项不过即 409。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.services.live_trading.services.k8s_manager import (
    ORCHESTRATION_MODE_ENV,
    orchestration_disabled,
    orchestration_mode,
)

_BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_orchestration_mode_defaults_to_docker(monkeypatch):
    monkeypatch.delenv(ORCHESTRATION_MODE_ENV, raising=False)
    assert orchestration_mode() == "docker"
    assert orchestration_disabled() is False


@pytest.mark.unit
def test_orchestration_mode_none_disables(monkeypatch):
    for raw in ("none", "NONE", "  None  "):
        monkeypatch.setenv(ORCHESTRATION_MODE_ENV, raw)
        assert orchestration_mode() == "none"
        assert orchestration_disabled() is True


@pytest.mark.unit
def test_orchestration_mode_unknown_value_falls_back_to_docker(monkeypatch):
    """取值写错不能静默变成 none —— 那等于悄悄拆掉 Docker 机器的就绪门禁。"""
    monkeypatch.setenv(ORCHESTRATION_MODE_ENV, "non")  # 手滑少一个字母
    assert orchestration_mode() == "docker"
    assert orchestration_disabled() is False


@pytest.mark.unit
def test_orchestration_consumers_read_the_switch():
    """两个门禁消费点必须走同一开关，不许再有硬编码的 docker 判定。"""
    for rel in (
        "services/trade/services/trading_precheck_service.py",
        "services/live_trading/routers/real_trading_preflight.py",
    ):
        source = (_BACKEND / rel).read_text(encoding="utf-8")
        assert "orchestration_disabled()" in source, rel
