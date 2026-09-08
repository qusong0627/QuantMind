# -*- coding: utf-8 -*-
"""AI-IDE minibt 策略运行时集成测试。

覆盖:
- executor 的 minibt 代码检测
- skill_engine 意图路由(minibt 优先且互斥)
- QuantDB 适配器 load_daily(真实 parquet)
- minibt_result 报告器(需 minibt 运行环境,缺失时跳过)
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from backend.services.engine.routers.ai_ide.executor import _detect_minibt
from backend.services.engine.routers.ai_ide.skill_engine import SkillEngine

QUANTDB_ROOT = Path(
    os.getenv("QM_QUANTDB_DATA_DIR", os.getenv("QM_QUANTDB_DATA_DIR_LOCAL", "data/quantdb"))
)


class TestDetectMinibt:
    def test_detects_import(self):
        assert _detect_minibt("from minibt import Bt, Strategy\nx = 1") is True

    def test_detects_module_import(self):
        assert _detect_minibt("import minibt\n") is True

    def test_ignores_comment_mentions(self):
        assert _detect_minibt("# minibt is great\nimport pandas as pd") is False

    def test_ignores_substring(self):
        assert _detect_minibt("import minibt_utils") is False

    def test_empty_code(self):
        assert _detect_minibt("") is False


class TestSkillRouting:
    def setup_method(self):
        self.engine = SkillEngine()

    def test_minibt_keyword_routes_exclusively(self):
        # "回测/均线" 同时是 traditional 关键词,minibt 必须独占
        templates = self.engine.detect_intent("用 minibt 写一个双均线回测策略", {})
        assert templates == ["minibt_strategy"]

    def test_traditional_unaffected(self):
        templates = self.engine.detect_intent("MACD 金叉回测", {})
        assert templates == ["traditional_indicator_backtest"]

    def test_model_unaffected(self):
        templates = self.engine.detect_intent("用模型预测选股", {})
        assert templates == ["qlib_model_strategy_config", "fundamental_factor_reference"]

    def test_plain_chat_no_template(self):
        assert self.engine.detect_intent("你好", {}) == []

    def test_error_guardrail_still_applies(self):
        templates = self.engine.detect_intent("minibt 回测报错了", {"error_msg": "NameError"})
        assert templates == ["minibt_strategy", "debug_guardrail"]


@pytest.mark.skipif(
    not (QUANTDB_ROOT / "1_kline_data" / "daily_forward").exists(),
    reason="本地无 QuantDB daily_forward 数据",
)
class TestMinibtQdbAdapter:
    def test_load_daily_suffix_code(self):
        from backend.shared.minibt_qdb import load_daily

        df = load_daily("600036.SH", "20240101", "20241231")
        assert list(df.columns) == [
            "datetime", "open", "high", "low", "close", "volume", "amount"
        ]
        assert len(df) > 200
        assert df["datetime"].is_monotonic_increasing
        assert str(df["datetime"].iloc[0])[:4] == "2024"

    def test_load_daily_prefix_code_normalized(self):
        from backend.shared.minibt_qdb import load_daily

        df_a = load_daily("SH600036", "20240101", "20240301")
        df_b = load_daily("600036", "20240101", "20240301")
        assert len(df_a) == len(df_b) > 0
        assert df_a["close"].equals(df_b["close"])

    def test_load_daily_date_filter(self):
        from backend.shared.minibt_qdb import load_daily

        df = load_daily("600036", "20240301", "20240331")
        assert len(df) > 0
        assert df["datetime"].min() >= pd.Timestamp("2024-03-01")
        assert df["datetime"].max() <= pd.Timestamp("2024-03-31")

    def test_unknown_symbol_returns_empty(self):
        from backend.shared.minibt_qdb import load_daily

        df = load_daily("999999.ZZ")
        assert len(df) == 0
        assert list(df.columns)[0] == "datetime"

    def test_missing_data_dir_raises(self):
        from backend.shared.minibt_qdb import load_daily

        with pytest.raises(FileNotFoundError):
            load_daily("600036", data_dir="/nonexistent_quantdb_root")


class TestMinibtResultReporter:
    def test_report_with_pilot_minibt(self, tmp_path):
        minibt = pytest.importorskip("minibt", reason="本环境未安装 minibt 运行时")
        from backend.shared.minibt_result import run_and_report

        qdb = pytest.importorskip("backend.shared.minibt_qdb")
        df = qdb.load_daily("600036", "20240101", "20241231")
        if df.empty:
            pytest.skip("本地无 QuantDB 数据")
        df = df[df["datetime"] >= "2024-04-01"].reset_index(drop=True)  # 预热后截取

        captured = []

        class MACross(minibt.Strategy):
            params = dict(l1=10, l2=20)

            def __init__(self):
                self.kline = self.get_kline(df, duration_seconds=86400)
                self.percent_commission = 0.00025
                self.ma1 = self.kline.close.sma(self.params.l1)
                self.ma2 = self.kline.close.sma(self.params.l2)
                self.long_signal = self.ma1.cross_up(self.ma2)
                self.short_signal = self.ma1.cross_down(self.ma2)

            def next(self):
                if not self.kline.position:
                    if self.long_signal.new:
                        self.kline.buy(size=1000)
                    elif self.short_signal.new:
                        self.kline.sell(size=1000)
                elif self.kline.position > 0 and self.short_signal.new:
                    self.kline.sell(size=1000)
                elif self.kline.position < 0 and self.long_signal.new:
                    self.kline.buy(size=1000)

        import minibt as _m

        bt = _m.Bt(auto=False)
        bt.addstrategy(MACross)
        result = run_and_report(bt, df, result_dir=str(tmp_path))

        assert (tmp_path / "result.json").exists()
        assert result["status"] == "success"
        assert len(result["equity"]) == len(df)
        metrics = result["metrics"]
        for key in ("cum_return", "annual_return", "sharpe", "max_drawdown", "win_rate", "n_trades", "avg_position"):
            assert key in metrics
        # 手续费已配置 → 必须为正
        assert (result["extra"]["total_fee"] or 0) > 0
