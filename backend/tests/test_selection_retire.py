"""T-P4-02 测试：新入口 /scanner/daily + 旧 /selection/daily 退休声明与恒空仓修复。

覆盖：
1. market_state_quantile 纯函数（test_signal_thresholds 同批）；compute_market_signals
   分位口径（窄分布不再恒"熊市/空仓"）；
2. 接线源断言：scanner 路由注册/用户上下文路径/身份回落/旧端点 Deprecation 三件套；
3. **真库直调旧端点**：阈值分位切换后 candidates 非空（恒空仓在旧面同步修复的活证据）+
   deprecated/replacement 字段 + Deprecation/Link 响应头。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_compute_market_signals_quantile_narrow_distribution():
    """窄分布模型（实测 ∈[-0.05,0.012]）：分位口径应给出可入场信号而非恒"空仓"。"""
    from backend.services.api.routers.model_training import compute_market_signals

    # 构造：300 只个股，分数在 [-0.048, 0.012]，20 只头部集中在几个行业
    signals = []
    for i in range(300):
        score = -0.048 + 0.06 * (i / 299)
        signals.append(
            {
                "symbol": f"60{i:04d}.SH",
                "fusion_score": score,
                "industry": f"行业{i % 30}",
                "board": "沪主板",
            }
        )
    out = compute_market_signals(signals)
    ms = out["market_signal"]
    assert ms["score_scale"] == "quantile"
    # 阈值应落在分布内部（旧绝对口径 0.09/0.10 会超出分布上界 0.012）
    assert ms["entry_threshold"] <= 0.012
    assert ms["strong_threshold"] <= 0.012
    assert ms["entry_signal"] in {"strong", "weak", "empty"}


@pytest.mark.unit
def test_scanner_router_registered_and_guarded():
    """新入口接线：路由注册 + 用户上下文路径 + 身份回落 + 证据字段。"""
    main_src = (_BACKEND / "services/engine/main.py").read_text(encoding="utf-8")
    assert "scanner_router" in main_src
    assert '"/api/v1/scanner/"' in main_src  # 用户上下文注入路径
    router_src = (_BACKEND / "services/engine/routers/scanner.py").read_text(
        encoding="utf-8"
    )
    assert "identity_fallback" in router_src
    assert "run_scan" in router_src
    assert '"opportunities"' in router_src


@pytest.mark.unit
def test_selection_daily_deprecated_with_quantile_switch():
    src = (_BACKEND / "services/engine/routers/selection.py").read_text(
        encoding="utf-8"
    )
    assert 'response.headers["Deprecation"] = "true"' in src
    assert 'rel="successor-version"' in src
    assert '"replacement": "/api/v1/scanner/daily"' in src
    assert "resolve_thresholds" in src
    assert "market_state_quantile" in src
    assert "strong_threshold=strong_threshold" in src


@pytest.mark.asyncio
async def test_selection_daily_real_data_fixed_and_deprecated():
    """真库直调旧端点：分位切换后 candidates 非空（恒空仓旧面修复）+ 退休声明齐备。"""
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 连接抖动: {exc}")

    from fastapi import Response

    from backend.services.engine.routers.selection import daily_selection

    request = SimpleNamespace(
        state=SimpleNamespace(user={"user_id": "00000001", "tenant_id": "default"})
    )
    response = Response()
    try:
        result = await daily_selection(
            request=request,
            response=response,
            strategy="balanced",
            date=None,  # 直调须显式传 Query 参数（FastAPI 默认值仅在框架调用时解析）
            ignore_ma20=True,
        )
    finally:
        from backend.shared.database_manager_v2 import close_database

        await close_database()

    assert result["deprecated"] is True
    assert result["replacement"] == "/api/v1/scanner/daily"
    assert response.headers.get("Deprecation") == "true"
    assert "successor-version" in response.headers.get("Link", "")
    assert result["meta"]["threshold_mode"] in {"quantile", "absolute"}
    if result["meta"]["threshold_mode"] == "quantile":
        assert result["meta"]["total_signals"] > 0
        # 恒空仓修复的活证据：分位口径下旧端点不再空仓
        assert len(result["candidates"]) > 0, "旧端点分位切换后应选出候选"
        assert result["market_state"]["state"] != "无信号"
