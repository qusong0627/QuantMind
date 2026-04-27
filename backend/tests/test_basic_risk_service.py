import asyncio
import os
import sys

import pytest

project_root = os.path.join(os.path.dirname(__file__), "../../")
sys.path.append(project_root)


def _create_service():
    from backend.services.engine.qlib_app.services.basic_risk_service import (
        BasicRiskService,
    )

    return BasicRiskService()


def test_get_backtest_returns_drops_initial_pct_change_nan(monkeypatch):
    service = _create_service()

    async def fake_get_result(backtest_id, tenant_id=None, include_fields=None):
        return type(
            "Result",
            (),
            {
                "equity_curve": [
                    {"date": "2024-01-02", "value": 100.0},
                    {"date": "2024-01-03", "value": 110.0},
                    {"date": "2024-01-04", "value": 99.0},
                ]
            },
        )()

    monkeypatch.setattr(service._persistence, "get_result", fake_get_result)

    returns = asyncio.run(
        service._get_backtest_returns(
            backtest_id="bt", user_id="u1", tenant_id="default"
        )
    )

    assert list(returns.index.strftime("%Y-%m-%d")) == [
        "2024-01-03",
        "2024-01-04",
    ]
    assert returns.tolist() == pytest.approx([0.10, -0.10])


def test_generate_histogram_uses_true_return_sample_count():
    service = _create_service()
    returns = [0.10, -0.10, 0.0, 0.05]

    histogram = service._generate_histogram(returns, bins=4)

    assert sum(histogram.counts) == len(returns)