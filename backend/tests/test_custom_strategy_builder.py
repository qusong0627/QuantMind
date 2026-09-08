from unittest.mock import MagicMock

import pytest

from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
from backend.services.engine.qlib_app.services.strategy_builder import (
    CustomStrategyBuilder,
    LongShortTopkBuilder,
    StrategyFactory,
)


class MockStrategy:
    def __init__(self, pool_file_key, pool_file_url, signal="<PRED>"):
        self.pool_file_key = pool_file_key
        self.pool_file_url = pool_file_url
        self.signal = signal


def test_custom_strategy_builder_proactive_repair():
    builder = CustomStrategyBuilder()

    # Mock request
    request = MagicMock(spec=QlibBacktestRequest)
    request.strategy_content = """
class MockStrategy(BaseStrategy):
    def __init__(self, pool_file_key, pool_file_url, signal="<PRED>"):
        pass

STRATEGY_CONFIG = {
    "class": "MockStrategy",
    "kwargs": {
        "signal": "test_signal"
    }
}
"""
    request.strategy_params = MagicMock()
    # Mock attributes that builder looks for in request.strategy_params
    for key in ["topk", "n_drop", "min_score", "max_weight", "stop_loss", "take_profit"]:
        setattr(request.strategy_params, key, None)

    market_state_kwargs = {"market": "cn"}
    signal_data = "some_df"
    backtest_id = "test_id"

    # We need to mock _build_strategy_from_content to return our dict and namespace
    builder._build_strategy_from_content = MagicMock(
        return_value=({"class": "MockStrategy", "kwargs": {"signal": "test_signal"}}, {"MockStrategy": MockStrategy})
    )

    # We also need to bypass _validate_strategy_content which might fail on magicmock
    builder._validate_strategy_content = MagicMock()

    result = builder.build(request, market_state_kwargs, signal_data, backtest_id)

    # result should be a dict if it hasn't been instantiated, or we can check the kwargs in the returned dict
    # In the current implementation, if module_path is empty, it attempts instantiation
    # Let's check the result
    assert isinstance(result, dict) or hasattr(result, "pool_file_key")

    if isinstance(result, dict):
        kwargs = result["kwargs"]
        assert kwargs["pool_file_key"] == ""
        assert kwargs["pool_file_url"] == ""
        assert kwargs["signal"] == "test_signal"  # preserved
    else:
        assert result.pool_file_key == ""
        assert result.pool_file_url == ""


def test_custom_strategy_builder_strict_filtering_with_kwargs():
    builder = CustomStrategyBuilder()

    # Mock class that accepts **kwargs but shouldn't receive 'topk' from system
    class KwargsStrategy:
        def __init__(self, mandatory_param, **kwargs):
            self.mandatory_param = mandatory_param
            self.extra = kwargs

    # Mock request with topk slider value
    request = MagicMock(spec=QlibBacktestRequest)
    request.strategy_content = "class KwargsStrategy(BaseStrategy): ..."
    request.strategy_params = MagicMock()
    request.strategy_params.topk = 100
    for key in ["n_drop", "min_score", "max_weight", "stop_loss", "take_profit"]:
        setattr(request.strategy_params, key, None)

    market_state_kwargs = {}
    signal_data = None
    backtest_id = "test_id"

    # Mock return values for support methods
    builder._build_strategy_from_content = MagicMock(
        return_value=(
            {"class": "KwargsStrategy", "kwargs": {"mandatory_param": "val"}},
            {"KwargsStrategy": KwargsStrategy},
        )
    )
    builder._validate_strategy_content = MagicMock()

    result = builder.build(request, market_state_kwargs, signal_data, backtest_id)

    # Check that 'topk' was NOT merged because it's not in signature or original kwargs
    if isinstance(result, dict):
        kwargs = result["kwargs"]
        assert "topk" not in kwargs
        assert kwargs["mandatory_param"] == "val"
    else:
        assert not hasattr(result, "topk")
        assert result.mandatory_param == "val"


@pytest.mark.parametrize("strategy_type", ["custom", "CustomStrategy", "custom_strategy"])
def test_strategy_factory_maps_custom_aliases(strategy_type):
    builder = StrategyFactory.get_builder(strategy_type)
    assert isinstance(builder, CustomStrategyBuilder)


def test_strategy_factory_maps_long_short_topk_template():
    builder = StrategyFactory.get_builder("long_short_topk")
    assert isinstance(builder, LongShortTopkBuilder)


def test_custom_strategy_builder_sets_dynamic_module_path_for_local_class():
    builder = CustomStrategyBuilder()

    request = MagicMock(spec=QlibBacktestRequest)
    request.strategy_content = "class MarginTopKLongShortStrategy(BaseStrategy): ..."
    request.strategy_params = MagicMock()
    for key in [
        "topk",
        "n_drop",
        "min_score",
        "max_weight",
        "stop_loss",
        "take_profit",
        "rebalance_days",
        "enable_short_selling",
        "margin_stock_pool",
        "financing_rate",
        "borrow_rate",
        "max_short_exposure",
        "max_leverage",
    ]:
        setattr(request.strategy_params, key, None)

    builder._build_strategy_from_content = MagicMock(
        return_value=(
            {
                "class": "MarginTopKLongShortStrategy",
                "module_path": "",
                "kwargs": {"signal": "<PRED>"},
            },
            {
                "__strategy_module_name__": "custom_strategy_test_module",
                "MarginTopKLongShortStrategy": type("MarginTopKLongShortStrategy", (), {}),
            },
        )
    )
    builder._validate_strategy_content = MagicMock()

    result = builder.build(request, {}, None, "bt_test")

    assert isinstance(result, dict)
    assert result["module_path"] == "custom_strategy_test_module"


def test_validate_rejects_minibt_import_with_actionable_message():
    """回归 2026-09-08 回测中心：minibt_* 策略走 qlib 回测 → exec 抛
    ModuleNotFoundError 裸栈。AST 校验阶段应拦下并给出可操作提示。"""
    # Arrange
    builder = CustomStrategyBuilder()
    code = (
        "import minibt\n"
        "from minibt.indicators import MA\n\n"
        "def get_strategy_config():\n"
        "    return {'class': 'X', 'kwargs': {}}\n"
    )

    # Act / Assert
    with pytest.raises(ValueError) as exc:
        builder._validate_strategy_content(code)
    assert "minibt" in str(exc.value)
    assert "AI-IDE" in str(exc.value)


def test_validate_still_accepts_plain_qlib_strategy():
    """护栏不得误伤正常 qlib 策略。"""
    builder = CustomStrategyBuilder()
    builder._validate_strategy_content(
        "from qlib.strategy.base import BaseStrategy\n"
        "class S(BaseStrategy):\n"
        "    pass\n"
    )


def test_validate_ignores_minibt_in_comments_and_strings():
    """仅拦真实 import：注释/字符串里提到 minibt 不误判。"""
    builder = CustomStrategyBuilder()
    builder._validate_strategy_content(
        "# 本策略不是 minibt 框架\n"
        "NOTE = 'minibt 运行时'\n"
        "class S:\n"
        "    pass\n"
    )
