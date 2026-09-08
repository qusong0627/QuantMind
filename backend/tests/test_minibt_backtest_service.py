# -*- coding: utf-8 -*-
"""回测中心 minibt 派发链路单测。

覆盖三块纯逻辑（不依赖 docker）：
1. minibt 识别口径（与 AI-IDE / 前端一致）
2. 脚本 START/END 日期覆盖（回测中心日期区间 → minibt 模板常量）
3. minibt result.json → QlibBacktestResult 映射
"""
import os
import sys

import pytest

project_root = os.path.join(os.path.dirname(__file__), "../../")
sys.path.append(project_root)

from backend.shared.minibt_detect import detect_minibt


class TestDetectMinibt:
    def test_detects_real_minibt_import(self):
        assert detect_minibt("import minibt\n") is True
        assert detect_minibt("from minibt import Bt, Strategy\n") is True
        assert detect_minibt("def f():\n    from minibt import Bt\n") is True

    def test_ignores_comments_strings_and_prefixed_modules(self):
        assert detect_minibt("# import minibt 只是注释\n") is False
        assert detect_minibt("NOTE = 'from minibt import Bt'\n") is False
        assert detect_minibt("from backend.shared.minibt_qdb import load_daily\n") is False
        assert detect_minibt("import minibt_qdb\n") is False

    def test_handles_empty(self):
        assert detect_minibt("") is False
        assert detect_minibt(None) is False


class TestApplyDateOverrides:
    def test_replaces_start_and_end_with_compact_dates(self):
        from backend.services.engine.qlib_app.services.minibt_backtest_service import (
            apply_minibt_date_overrides,
        )

        code = "SYMBOL = '600036'\nSTART = '20220101'\nEND = '20250630'\nDF = load_daily(SYMBOL, START, END)\n"
        new_code, applied = apply_minibt_date_overrides(code, "2024-01-02", "2024-12-31")

        assert applied == {"start": True, "end": True}
        assert "START = '20240102'" in new_code
        assert "END = '20241231'" in new_code
        assert "SYMBOL = '600036'" in new_code

    def test_replaces_only_start_when_end_absent(self):
        from backend.services.engine.qlib_app.services.minibt_backtest_service import (
            apply_minibt_date_overrides,
        )

        code = 'START = "20220101"\n'
        new_code, applied = apply_minibt_date_overrides(code, "2024-01-02", "2024-12-31")

        assert applied == {"start": True, "end": False}
        assert 'START = "20240102"' in new_code

    def test_leaves_indented_or_unrelated_assignments_alone(self):
        from backend.services.engine.qlib_app.services.minibt_backtest_service import (
            apply_minibt_date_overrides,
        )

        code = (
            "class S:\n"
            "    START = '20220101'\n"
            "START_DATE = '20220101'\n"
            "END = '20250630'\n"
        )
        new_code, applied = apply_minibt_date_overrides(code, "2024-01-02", "2024-12-31")

        assert applied == {"start": False, "end": True}
        assert "    START = '20220101'" in new_code  # 缩进的不动
        assert "START_DATE = '20220101'" in new_code  # 同名前缀不动
        assert "END = '20241231'" in new_code

    def test_skips_invalid_dates(self):
        from backend.services.engine.qlib_app.services.minibt_backtest_service import (
            apply_minibt_date_overrides,
        )

        code = "START = '20220101'\nEND = '20250630'\n"
        new_code, applied = apply_minibt_date_overrides(code, "", "2024/12/31")

        assert applied == {"start": False, "end": False}
        assert new_code == code

    def test_accepts_compact_dates_as_input(self):
        from backend.services.engine.qlib_app.services.minibt_backtest_service import (
            apply_minibt_date_overrides,
        )

        code = "START = '20220101'\n"
        new_code, applied = apply_minibt_date_overrides(code, "20240102", "20241231")

        assert applied["start"] is True
        assert "START = '20240102'" in new_code


class TestMapMinibtResult:
    def _payload(self):
        return {
            "run_id": "run-1",
            "status": "success",
            "metrics": {
                "cum_return": 0.0069,
                "annual_return": 0.0312,
                "sharpe": 0.41,
                "max_drawdown": 0.0523,
                "win_rate": 0.55,
                "n_trades": 18,
                "avg_position": 0.42,
            },
            "equity": [
                {"date": "2022-01-04T00:00:00", "value": 1000000.0, "benchmark": None},
                {"date": "2022-01-05T00:00:00", "value": 1010000.0, "benchmark": None},
            ],
            "trades": [
                {
                    "date": "2022-01-05",
                    "symbol": "600036",
                    "direction": "BUY",
                    "price": 38.5,
                    "qty": 1000.0,
                    "detail": {"fee": 9.6, "value": 38500.0},
                },
                {
                    "date": "2022-01-06",
                    "symbol": "600036",
                    "direction": "SELL",
                    "price": 39.1,
                    "qty": 1000.0,
                    "detail": {"fee": 19.5, "value": 39100.0},
                },
            ],
            "warnings": ["minibt 撮合口径: 信号当根K线收盘价成交"],
            "config": {"engine": "minibt", "market": "CN"},
            "elapsed_sec": 12.5,
            "extra": {"profit_factor": 1.4, "total_fee": 250.0, "final_equity": 1006900.0},
        }

    def test_maps_metrics_and_equity(self):
        from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
        from backend.services.engine.qlib_app.services.minibt_backtest_service import (
            map_minibt_payload_to_result,
        )

        request = QlibBacktestRequest(
            strategy_type="CustomStrategy",
            start_date="2022-01-01",
            end_date="2025-06-30",
            user_id="00000001",
            tenant_id="default",
            initial_capital=1_000_000,
        )
        result = map_minibt_payload_to_result(
            self._payload(), request=request, backtest_id="bt-1"
        )

        assert result.status == "completed"
        assert result.backtest_id == "bt-1"
        assert result.annual_return == pytest.approx(0.0312)
        assert result.sharpe_ratio == pytest.approx(0.41)
        # qlib 口径 max_drawdown 为负值（minibt 给的是正值）
        assert result.max_drawdown == pytest.approx(-0.0523)
        assert result.total_return == pytest.approx(0.0069)
        assert result.total_trades == 18
        assert result.win_rate == pytest.approx(0.55)
        assert result.profit_factor == pytest.approx(1.4)
        assert result.equity_curve and len(result.equity_curve) == 2
        assert result.equity_curve[0] == {"date": "2022-01-04", "value": 1000000.0}
        assert result.drawdown_curve is not None
        # 回撤曲线两种键名都给：qlib 用 drawdown，旧图表读 value
        assert set(result.drawdown_curve[0]) == {"date", "drawdown", "value"}
        assert result.config and result.config.get("engine") == "minibt"
        assert result.config.get("initial_capital") == 1_000_000
        # 前端 execution_time 直接 .toFixed(2)，必须是数字
        assert isinstance(result.execution_time, (int, float))
        assert result.portfolio_metrics and result.portfolio_metrics.final_value == pytest.approx(
            1006900.0
        )

    def test_normalizes_trades_to_qlib_shape(self):
        from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
        from backend.services.engine.qlib_app.services.minibt_backtest_service import (
            map_minibt_payload_to_result,
        )

        request = QlibBacktestRequest(
            strategy_type="CustomStrategy",
            start_date="2022-01-01",
            end_date="2025-06-30",
            user_id="00000001",
        )
        result = map_minibt_payload_to_result(
            self._payload(), request=request, backtest_id="bt-3"
        )

        assert result.trades and len(result.trades) == 2
        buy = result.trades[0]
        # 前端按 action === 'buy' 判方向，必须是两态小写
        assert buy["action"] == "buy"
        assert buy["quantity"] == 1000
        assert buy["totalAmount"] == pytest.approx(38500.0)
        assert buy["commission"] == pytest.approx(9.6)
        assert buy["factor"] == 1.0
        assert result.trades[1]["action"] == "sell"

    def test_marks_failure_when_payload_missing(self):
        from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
        from backend.services.engine.qlib_app.services.minibt_backtest_service import (
            map_minibt_payload_to_result,
        )

        request = QlibBacktestRequest(
            strategy_type="CustomStrategy",
            start_date="2022-01-01",
            end_date="2025-06-30",
            user_id="00000001",
        )
        result = map_minibt_payload_to_result(
            {}, request=request, backtest_id="bt-2", error_message="runner 无结果输出"
        )

        assert result.status == "failed"
        assert "runner 无结果输出" in (result.error_message or "")
        assert isinstance(result.execution_time, (int, float))


class TestIsMinibtRequest:
    def _request(self, **kwargs):
        from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest

        base = {
            "strategy_type": "CustomStrategy",
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
        }
        base.update(kwargs)
        return QlibBacktestRequest(**base)

    def test_detects_minibt_code_and_template_type(self):
        from backend.services.engine.qlib_app.services.minibt_backtest_service import (
            is_minibt_request,
        )

        assert is_minibt_request(
            self._request(strategy_content="from minibt import Bt\n")
        )
        assert is_minibt_request(self._request(strategy_type="minibt_dual_ma"))
        assert is_minibt_request(self._request(strategy_type="MINIBT_RSI_REVERSAL"))

    def test_ignores_plain_qlib_strategy(self):
        from backend.services.engine.qlib_app.services.minibt_backtest_service import (
            is_minibt_request,
        )

        assert not is_minibt_request(
            self._request(
                strategy_content="from qlib.contrib.strategy import TopkDropoutStrategy\n"
            )
        )
        assert not is_minibt_request(
            self._request(strategy_content="# 提到 minibt 只是注释\n")
        )
