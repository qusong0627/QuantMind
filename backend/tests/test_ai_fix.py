import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 端点已从 api/backtest.py 拆到 api/ai_fix.py（backtest 只 include_router）——
# 老路径 import 直接失败，会让**整个** unit 套件在收集期中断（零项参与即失败）。
from backend.services.engine.qlib_app.api.ai_fix import ai_fix_strategy
from backend.services.engine.qlib_app.schemas.backtest import QlibAIFixRequest


@pytest.mark.asyncio
async def test_ai_fix_strategy_logic():
    # Mock request
    request = QlibAIFixRequest(backtest_id="test_id", error_message="SyntaxError", full_error="Traceback...")

    # Mock dependencies
    mock_run_data = MagicMock()
    mock_run_data.config = {
        "strategy_content": "print('hello')",
        "strategy_id": "999",
        "name": "Test Strategy",
        "user_id": "1",
        "tenant_id": "test_tenant",
    }

    with (
        patch("backend.services.engine.qlib_app.services.backtest_persistence.BacktestPersistence") as MockPersistence,
        patch("backend.services.engine.ai_strategy.services.strategy_service.StrategyService") as MockAI,
        patch("backend.services.engine.qlib_app.services.user_strategy_loader.UserStrategyLoader") as MockLoader,
    ):

        # Setup mocks
        persistence_instance = MockPersistence.return_value
        persistence_instance.get_result = AsyncMock(return_value=mock_run_data)

        ai_instance = MockAI.return_value
        ai_instance.generate_strategy_direct = AsyncMock(return_value="import qlib\nprint('fixed')")

        loader_instance = MockLoader.return_value
        loader_instance.save_strategy = MagicMock(return_value="new_id_123")

        # Call the endpoint function
        response = await ai_fix_strategy(None, request)

        # Verify results
        assert response.success is True
        assert "fixed" in response.repaired_code
        assert response.strategy_id == "new_id_123"

        # Verify loader was called with correct strategy_id for overwrite
        loader_instance.save_strategy.assert_called_once()
        args, kwargs = loader_instance.save_strategy.call_args
        assert kwargs["strategy_id"] == "999"
        assert "import qlib" in kwargs["code"]


@pytest.mark.asyncio
async def test_ai_fix_strategy_no_code():
    request = QlibAIFixRequest(backtest_id="no_code_id")

    mock_run_data = MagicMock()
    mock_run_data.config = {}  # No code

    with patch("backend.services.engine.qlib_app.services.backtest_persistence.BacktestPersistence") as MockPersistence:
        persistence_instance = MockPersistence.return_value
        persistence_instance.get_result = AsyncMock(return_value=mock_run_data)

        response = await ai_fix_strategy(None, request)
        assert response.success is False
        assert "不包含自定义策略代码" in response.message
