import asyncio
import os
import sys
import types
import json
from pathlib import Path

import pytest

project_root = os.path.join(os.path.dirname(__file__), "../../")
sys.path.append(project_root)

from backend.services.engine.qlib_app.services.backtest_service import QlibBacktestService
from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer
from backend.services.engine.qlib_app.services import risk_analyzer as risk_analyzer_module


def test_normalize_signal_config_rejects_module_path_only_dict():
    service = QlibBacktestService()
    signal = {"module_path": "backend.services.engine.qlib_app.utils.simple_signal"}
    assert service._normalize_signal_config(signal) == "$close"


def test_build_signal_data_rejects_module_path_only_signal_dict():
    service = QlibBacktestService()
    request = types.SimpleNamespace(
        strategy_params=types.SimpleNamespace(
            signal={"module_path": "backend.services.engine.qlib_app.utils.simple_signal"}
        ),
        universe="all",
        start_date="2025-01-01",
        end_date="2025-01-02",
    )
    signal_data, signal_meta = asyncio.run(service._build_signal_data(request))
    assert signal_data is not None
    assert signal_meta.get("source") in {"feature_field", "close_fallback"}


def test_enforce_signal_quality_blocks_implicit_close_fallback():
    service = QlibBacktestService()
    request = types.SimpleNamespace(allow_feature_signal_fallback=False)

    with pytest.raises(ValueError, match="信号质量预检失败"):
        service._enforce_signal_quality(
            {"source": "close_fallback", "fallback_reason": "pred_path_not_found"},
            request=request,
        )


def test_lag_signal_frame_moves_signal_to_next_trade_date():
    import pandas as pd

    idx = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2025-01-02"), "SH600000"),
            (pd.Timestamp("2025-01-03"), "SH600000"),
            (pd.Timestamp("2025-01-06"), "SH600000"),
        ],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame({"score": [1.0, 2.0, 3.0]}, index=idx)

    lagged = QlibBacktestService._lag_signal_frame(df, 1)

    assert (pd.Timestamp("2025-01-02"), "SH600000") not in lagged.index
    assert lagged.loc[(pd.Timestamp("2025-01-03"), "SH600000"), "score"] == 1.0
    assert lagged.loc[(pd.Timestamp("2025-01-06"), "SH600000"), "score"] == 2.0


def test_build_pred_signal_meta_uses_lagged_effective_dates():
    import pandas as pd

    service = QlibBacktestService()
    idx = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2025-01-02"), "SH600000"),
            (pd.Timestamp("2025-01-03"), "SH600000"),
            (pd.Timestamp("2025-01-06"), "SH600000"),
        ],
        names=["datetime", "instrument"],
    )
    pred = pd.DataFrame({"score": [0.1, 0.2, 0.3]}, index=idx)
    request = types.SimpleNamespace(
        start_date="2025-01-03",
        end_date="2025-01-06",
        signal_lag_days=1,
    )

    meta = service._build_pred_signal_meta(pred, "/tmp/pred.pkl", request)

    assert meta["max_signal_date"] == "2025-01-06"
    assert meta["rows_in_range"] == 2
    assert meta["date_count"] == 2


def test_build_signal_data_supports_explicit_parquet_path(tmp_path: Path):
    service = QlibBacktestService()
    parquet_path = tmp_path / "pred.parquet"
    parquet_path.write_text("", encoding="utf-8")
    request = types.SimpleNamespace(
        strategy_params=types.SimpleNamespace(signal=str(parquet_path)),
        universe="all",
        start_date="2025-01-01",
        end_date="2025-01-31",
    )

    captured = {}

    def fake_load_pred(path, req):
        captured["path"] = path
        captured["request"] = req
        return "parquet-signal", {"source": "pred_pkl"}

    service._load_pred_pkl = fake_load_pred  # type: ignore[method-assign]

    signal_data, signal_meta = asyncio.run(service._build_signal_data(request))

    assert signal_data == "parquet-signal"
    assert signal_meta["source"] == "pred_pkl"
    assert captured["path"] == str(parquet_path)
    assert captured["request"] is request


def test_normalize_trades_for_display_backfills_factor_and_price(monkeypatch):
    monkeypatch.setattr(
        RiskAnalyzer,
        "_load_factor_map",
        classmethod(lambda cls, pairs: {("SZ002822", "2025-01-02"): 0.14105364680290222}),
    )
    trades = [
        {
            "date": "2025-01-02",
            "symbol": "SZ002822",
            "price": 0.5444670915603638,
            "quantity": 34738.55593997433,
            "totalAmount": 18914.00051764482,
            "adj_price": None,
            "adj_quantity": None,
            "factor": None,
        }
    ]
    normalized = RiskAnalyzer.normalize_trades_for_display(trades)
    row = normalized[0]
    assert row["factor"] == pytest.approx(0.14105364680290222)
    assert row["price"] == pytest.approx(3.86, rel=1e-3)
    assert row["quantity"] == pytest.approx(4900.0, rel=1e-6)


def test_normalize_trades_for_display_snaps_cn_board_lot_under_factor_drift(monkeypatch):
    monkeypatch.setattr(
        RiskAnalyzer,
        "_load_factor_map",
        classmethod(lambda cls, pairs: {("SH600018", "2025-01-10"): 0.4312969446182251}),
    )
    trades = [
        {
            "date": "2025-01-10",
            "symbol": "SH600018",
            "price": 2.4670183658599854,
            "quantity": 6264.226624905601,
            "totalAmount": 15453.962131551227,
            "adj_price": None,
            "adj_quantity": None,
            "factor": None,
        }
    ]
    normalized = RiskAnalyzer.normalize_trades_for_display(trades)
    row = normalized[0]
    assert row["price"] == pytest.approx(5.72, rel=1e-3)
    assert row["quantity"] == pytest.approx(2700.0, rel=1e-6)


def test_recording_strategy_drops_pool_file_local_before_super(monkeypatch):
    qlib = pytest.importorskip("qlib")
    assert qlib is not None

    from backend.services.engine.qlib_app.utils import recording_strategy as rs

    captured = {}

    def fake_init_redis(self, kwargs):
        return None

    def fake_init_dynamic_risk(self, kwargs):
        return None

    def fake_super_init(self, *args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(rs.RedisRecordingStrategy, "init_redis", fake_init_redis)
    monkeypatch.setattr(rs.RedisRecordingStrategy, "init_dynamic_risk", fake_init_dynamic_risk)
    monkeypatch.setattr(rs.TopkDropoutStrategy, "__init__", fake_super_init)

    rs.RedisRecordingStrategy(
        signal="$close",
        topk=10,
        n_drop=2,
        pool_file_local="/tmp/custom_pool.txt",
        rebalance_days=1,
    )

    assert "pool_file_local" not in captured


def test_recording_strategy_applies_f_prefix_fundamental_filter(monkeypatch):
    qlib = pytest.importorskip("qlib")
    assert qlib is not None

    import pandas as pd
    from backend.services.engine.qlib_app.utils import recording_strategy as rs

    def fake_init_redis(self, kwargs):
        return None

    def fake_init_dynamic_risk(self, kwargs):
        return None

    def fake_super_init(self, *args, **kwargs):
        return None

    monkeypatch.setattr(rs.RedisRecordingStrategy, "init_redis", fake_init_redis)
    monkeypatch.setattr(rs.RedisRecordingStrategy, "init_dynamic_risk", fake_init_dynamic_risk)
    monkeypatch.setattr(rs.TopkDropoutStrategy, "__init__", fake_super_init)
    monkeypatch.setattr(
        rs.fundamental_aligner,
        "filter_instruments",
        lambda *_args, **_kwargs: ["SH600001"],
    )

    strategy = rs.RedisRecordingStrategy(
        signal="$close",
        topk=10,
        n_drop=2,
        rebalance_days=1,
        f_pe_ttm_max=25,
    )

    idx = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2026-04-01"), "SH600001"),
            (pd.Timestamp("2026-04-01"), "SH600002"),
        ],
        names=["datetime", "instrument"],
    )
    score = pd.Series([0.8, 0.3], index=idx)

    result = strategy.apply_fundamental_filter(score, pd.Timestamp("2026-04-01"))
    instruments = list(result.index.get_level_values("instrument"))
    assert instruments == ["SH600001"]


def test_fundamental_filter_handles_flat_instrument_index(monkeypatch):
    """回归：SimpleSignal.get_signal() 返回的是只按 instrument 索引的 Series，
    旧实现用 pd.IndexSlice[:, ...] 取数会抛 IndexingError('Too many indexers')。"""
    qlib = pytest.importorskip("qlib")
    assert qlib is not None

    import pandas as pd
    from backend.services.engine.qlib_app.utils import recording_strategy as rs

    monkeypatch.setattr(rs.RedisRecordingStrategy, "init_redis", lambda self, kwargs: None)
    monkeypatch.setattr(rs.RedisRecordingStrategy, "init_dynamic_risk", lambda self, kwargs: None)
    monkeypatch.setattr(rs.TopkDropoutStrategy, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(
        rs.fundamental_aligner, "filter_instruments", lambda *_a, **_k: ["sh600001"]
    )

    strategy = rs.RedisRecordingStrategy(signal="$close", topk=10, n_drop=2, f_pe_ttm_max=25)
    score = pd.Series([0.8, 0.3], index=pd.Index(["sh600001", "sh600002"], name="instrument"))

    result = strategy.apply_fundamental_filter(score, pd.Timestamp("2026-04-01"))
    assert list(result.index) == ["sh600001"]


def test_topk_dropout_filters_signal_inside_trade_decision(monkeypatch):
    """回归：TopkDropoutStrategy.generate_trade_decision 直接读 self.signal 选股，
    从不调用 generate_target_weight_position，因此 f_* 必须通过
    _fundamental_filtered_signal 在 generate_trade_decision 里生效。
    同时校验过滤用的是「上一交易日」快照（避免前视偏差）。"""
    qlib = pytest.importorskip("qlib")
    assert qlib is not None

    import pandas as pd
    from backend.services.engine.qlib_app.utils import recording_strategy as rs

    monkeypatch.setattr(rs.RedisRecordingStrategy, "init_redis", lambda self, kwargs: None)
    monkeypatch.setattr(rs.RedisRecordingStrategy, "init_dynamic_risk", lambda self, kwargs: None)
    monkeypatch.setattr(rs.TopkDropoutStrategy, "__init__", lambda self, *a, **k: None)

    filter_calls = []

    def fake_filter(_date, instruments, constraints=None):
        filter_calls.append(_date)
        return ["SH600001"]

    monkeypatch.setattr(rs.fundamental_aligner, "filter_instruments", fake_filter)

    strategy = rs.RedisRecordingStrategy(signal="$close", topk=10, n_drop=2, f_pe_ttm_max=25)

    idx = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2026-04-01"), "SH600001"),
            (pd.Timestamp("2026-04-01"), "SH600002"),
        ],
        names=["datetime", "instrument"],
    )

    class _FakeSignal:
        def get_signal(self, *args, **kwargs):
            return pd.Series([0.8, 0.3], index=idx)

    fake_signal = _FakeSignal()
    strategy.signal = fake_signal
    # trade_calendar 是 qlib BaseStrategy 的只读 property，只能从类上打补丁。
    # shift=1 模拟 qlib 语义：返回上一个 bar（2026-03-31）。
    monkeypatch.setattr(
        rs.RedisRecordingStrategy,
        "trade_calendar",
        property(
            lambda self: types.SimpleNamespace(
                get_step_time=lambda step, shift=0: (
                    pd.Timestamp("2026-04-01") - pd.Timedelta(days=shift),
                    pd.Timestamp("2026-04-02") - pd.Timedelta(days=shift),
                )
            )
        ),
    )

    with strategy._fundamental_filtered_signal(0):
        filtered = strategy.signal.get_signal(start_time=None, end_time=None)

    assert list(filtered.index.get_level_values("instrument")) == ["SH600001"]
    assert filter_calls == [pd.Timestamp("2026-03-31")]
    # 退出上下文后必须恢复原 signal，避免污染后续交易日
    assert strategy.signal is fake_signal


def test_custom_adjust_signal_hook_is_called_with_prev_trade_date(monkeypatch):
    """回归：自定义策略类只覆写 generate_target_weight_position 是无效的
    （qlib 的 TopkDropoutStrategy 从不调用它）。``_adjust_signal`` 才是基类
    generate_trade_decision 真正调用的钩子，且 ref_date 必须是上一交易日（T-1），
    否则等于用 T 日信息选股。"""
    qlib = pytest.importorskip("qlib")
    assert qlib is not None

    import pandas as pd
    from backend.services.engine.qlib_app.utils import recording_strategy as rs

    monkeypatch.setattr(rs.RedisRecordingStrategy, "init_redis", lambda self, kwargs: None)
    monkeypatch.setattr(rs.RedisRecordingStrategy, "init_dynamic_risk", lambda self, kwargs: None)
    monkeypatch.setattr(rs.TopkDropoutStrategy, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(
        rs.RedisRecordingStrategy,
        "trade_calendar",
        property(
            lambda self: types.SimpleNamespace(
                get_step_time=lambda step, shift=0: (
                    pd.Timestamp("2026-04-01") - pd.Timedelta(days=shift),
                    pd.Timestamp("2026-04-02") - pd.Timedelta(days=shift),
                )
            )
        ),
    )

    calls = []

    class _HookStrategy(rs.RedisRecordingStrategy):
        def _adjust_signal(self, score, ref_date):
            calls.append(ref_date)
            return score[score > 0.5]

    strategy = _HookStrategy(signal="$close", topk=10, n_drop=2)
    original_signal = types.SimpleNamespace(
        get_signal=lambda *a, **k: pd.Series(
            [0.8, 0.3], index=pd.Index(["SH600001", "SH600002"], name="instrument")
        )
    )
    strategy.signal = original_signal

    with strategy._custom_signal_hook(0):
        result = strategy.signal.get_signal(start_time=None, end_time=None)

    assert calls == [pd.Timestamp("2026-03-31")]
    assert list(result.index) == ["SH600001"]
    # 退出上下文后必须恢复原 signal，避免污染后续交易日
    assert strategy.signal is original_signal


def test_dynamic_risk_degree_hook_applies_then_restores(monkeypatch):
    """回归：TopkDropoutStrategy 直接读 self.risk_degree 下单，
    ``_dynamic_risk_degree`` 必须在调用基类前改写它、调用后还原，
    否则动态仓位（波动率目标 / 回撤阶梯）会被静默忽略。"""
    qlib = pytest.importorskip("qlib")
    assert qlib is not None

    import pandas as pd
    from backend.services.engine.qlib_app.utils import recording_strategy as rs

    monkeypatch.setattr(rs.RedisRecordingStrategy, "init_redis", lambda self, kwargs: None)
    monkeypatch.setattr(rs.RedisRecordingStrategy, "init_dynamic_risk", lambda self, kwargs: None)
    monkeypatch.setattr(rs.TopkDropoutStrategy, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(rs.RedisRecordingStrategy, "check_account_stop_loss", lambda self: False)
    monkeypatch.setattr(
        rs.RedisRecordingStrategy,
        "trade_calendar",
        property(
            lambda self: types.SimpleNamespace(
                get_step_time=lambda step, shift=0: (
                    pd.Timestamp("2026-04-01") - pd.Timedelta(days=shift),
                    pd.Timestamp("2026-04-02") - pd.Timedelta(days=shift),
                )
            )
        ),
    )

    seen = {}

    def fake_super_decision(self, execute_result=None):
        seen["risk_degree"] = self.risk_degree
        return "DECISION"

    monkeypatch.setattr(rs.TopkDropoutStrategy, "generate_trade_decision", fake_super_decision)

    class _HookStrategy(rs.RedisRecordingStrategy):
        def _dynamic_risk_degree(self, base, ref_date):
            seen["ref_date"] = ref_date
            return base * 0.5

    strategy = _HookStrategy(signal="$close", topk=10, n_drop=2, rebalance_days=1)
    strategy.risk_degree = 0.8
    strategy.signal = types.SimpleNamespace(get_signal=lambda *a, **k: None)

    assert strategy.generate_trade_decision() == "DECISION"
    assert seen["ref_date"] == pd.Timestamp("2026-03-31")
    assert seen["risk_degree"] == pytest.approx(0.4)
    assert strategy.risk_degree == pytest.approx(0.8)


def test_risk_degree_kwarg_reaches_topk_dropout_attribute(monkeypatch):
    """回归：risk_degree 被 init_dynamic_risk pop 成 default_risk_degree 后，
    BaseSignalStrategy 又把它设回默认 0.95，模板里的 risk_degree 因此静默失效
    （实测 as40「固定 60% 仓位」一直按 95% 下单）。修复后必须回写到 self.risk_degree。"""
    qlib = pytest.importorskip("qlib")
    assert qlib is not None

    from backend.services.engine.qlib_app.utils import recording_strategy as rs

    monkeypatch.setattr(rs.RedisRecordingStrategy, "init_redis", lambda self, kwargs: None)
    monkeypatch.setattr(rs.TopkDropoutStrategy, "__init__", lambda self, *a, **k: None)

    strategy = rs.RedisRecordingStrategy(signal="$close", topk=10, n_drop=2, risk_degree=0.6)
    assert strategy.risk_degree == pytest.approx(0.6)

    # 超过 max_leverage(1.0) 的取值必须被夹住，不能放杠杆
    leveraged = rs.RedisRecordingStrategy(signal="$close", topk=10, n_drop=2, risk_degree=1.5)
    assert leveraged.risk_degree == pytest.approx(1.0)


def test_market_state_series_scales_risk_degree_for_topk(monkeypatch):
    """回归：UI 开启「动态仓位」时平台注入 market_state_series，
    但 TopkDropout 链路不会调用 get_risk_degree，动态降仓必须走 _dynamic_risk_degree。"""
    qlib = pytest.importorskip("qlib")
    assert qlib is not None

    import pandas as pd
    from backend.services.engine.qlib_app.utils import recording_strategy as rs

    monkeypatch.setattr(rs.RedisRecordingStrategy, "init_redis", lambda self, kwargs: None)
    monkeypatch.setattr(rs.TopkDropoutStrategy, "__init__", lambda self, *a, **k: None)

    strategy = rs.RedisRecordingStrategy(signal="$close", topk=10, n_drop=2)
    strategy.market_state_series = {"2026-03-31": "down"}
    strategy.position_by_state = {"up": 1.0, "neutral": 0.8, "down": 0.5}
    strategy.strategy_total_position = 0.9
    strategy.max_leverage = 1.0
    strategy.default_risk_degree = None

    # 下行市：0.5（状态仓位）× 0.9（总仓位）= 0.45
    assert strategy._dynamic_risk_degree(0.95, pd.Timestamp("2026-03-31")) == pytest.approx(0.45)
    # 序列里没有的日期：保持基础仓位
    assert strategy._dynamic_risk_degree(0.95, pd.Timestamp("2026-04-01")) == pytest.approx(0.95)


def test_inverse_vol_template_weights_by_inverse_vol(monkeypatch):
    """回归：as37 的逆波动加权必须走 WeightStrategyBase 链路（会真正调用
    generate_target_weight_position），权重 ∝ 1/σ 且受单票上限约束。"""
    qlib = pytest.importorskip("qlib")
    assert qlib is not None

    import importlib.util

    import pandas as pd
    from backend.services.engine.qlib_app.utils import recording_strategy as rs

    path = Path(__file__).resolve().parents[2] / "strategy_templates" / "as37_inverse_vol.py"
    spec = importlib.util.spec_from_file_location("qm_tpl_as37_inverse_vol", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = module.InverseVolWeightStrategy

    assert issubclass(cls, rs.WeightStrategyBase)

    strategy = cls.__new__(cls)
    strategy.vol_window = 20
    strategy.weight_cap = 0.6
    strategy.use_fundamental_filter = False
    strategy.topk = None
    strategy.min_score = 0.0
    strategy.max_weight = 1.0
    strategy.account_stop_loss = 0.0
    strategy._is_account_stopped = False
    strategy.rebalance_days = 5
    monkeypatch.setattr(cls, "_prev_trade_date", lambda self: pd.Timestamp("2026-03-31"))
    monkeypatch.setattr(
        cls,
        "_vol_map",
        lambda self, stocks, ref_date: pd.Series({"SH600001": 0.01, "SH600002": 0.04}),
    )

    weights = strategy.generate_target_weight_position(
        pd.Series({"SH600001": 0.9, "SH600002": 0.8}),
        current=None,
        trade_exchange=None,
        trade_start_time=pd.Timestamp("2026-04-01"),
        trade_end_time=pd.Timestamp("2026-04-01"),
    )

    # 等权应被 1/σ 改写（低波动那只权重更大），再按 60% 单票上限归一化
    assert weights["SH600001"] == pytest.approx(0.75)
    assert weights["SH600002"] == pytest.approx(0.25)


def test_price_frame_prefetches_once_and_slices(monkeypatch):
    """回归：D.features 对全市场每调一次要 10~17 秒且不跨调用复用，自定义类若在
    每个调仓步各调一次，一年回测会被拖到几十分钟（实测 as11 单次回测 60 分钟没结束）。
    ``_price_frame`` 必须首次就把回测区间（含回看 buffer）取满，之后只做切片；
    中途新出现的标的只增量补取它自己。"""
    qlib = pytest.importorskip("qlib")
    assert qlib is not None

    import pandas as pd
    from backend.services.engine.qlib_app.utils import recording_strategy as rs

    calls = []

    def fake_fetch(symbols, fields, start, end):
        calls.append((tuple(symbols), tuple(fields), pd.Timestamp(start), pd.Timestamp(end)))
        dates = pd.date_range("2024-01-01", "2024-12-31", freq="D")
        # 平台上的 D.features 返回 instrument 在前的多级索引（见 _normalize_price_frame）
        index = pd.MultiIndex.from_product(
            [list(symbols), dates], names=["instrument", "datetime"]
        )
        return pd.DataFrame(
            {"$close": [float(i) for i in range(len(index))]}, index=index
        )

    monkeypatch.setattr(rs.PriceFrameMixin, "_fetch_price_frame", staticmethod(fake_fetch))

    strategy = rs.RedisRecordingStrategy.__new__(rs.RedisRecordingStrategy)
    monkeypatch.setattr(
        rs.RedisRecordingStrategy,
        "trade_calendar",
        property(
            lambda self: types.SimpleNamespace(
                get_step_time=lambda step, shift=0: (
                    pd.Timestamp("2024-01-02")
                    if step == 0
                    else pd.Timestamp("2024-12-31"),
                    pd.Timestamp("2024-12-31"),
                ),
                get_trade_len=lambda: 242,
            )
        ),
    )

    first = strategy._price_frame(
        ["SH600000", "SH600001"], ["$close"], "2024-06-01", "2024-06-30"
    )
    assert first is not None
    assert len(calls) == 1
    assert first.index.names[0] == "datetime"  # 必须归一化成日期在前，否则按日期切片会 TypeError
    _, _, call_start, call_end = calls[0]
    # 首次必须取满「回测首日往前 buffer → 回测末日」，而不是只取请求的那一个月
    assert call_start == pd.Timestamp("2024-01-02") - pd.Timedelta(days=400)
    assert call_end == pd.Timestamp("2024-12-31")
    assert set(first.index.get_level_values("instrument")) == {"SH600000", "SH600001"}

    # 后续调仓步只切片，不再取数
    second = strategy._close_matrix(["SH600000"], "2024-07-01", "2024-07-15")
    assert len(calls) == 1
    assert list(second.columns) == ["SH600000"]

    # 中途新出现的标的：只增量补取它自己，不重取全市场
    third = strategy._price_frame(["SH600002"], ["$close"], "2024-07-01", "2024-07-15")
    assert len(calls) == 2
    assert calls[1][0] == ("SH600002",)
    assert set(third.index.get_level_values("instrument")) == {"SH600002"}


def test_custom_templates_do_not_call_dfeatures_directly():
    """回归：自定义模板类取行情必须走基类缓存，禁止直接 D.features（见上一条测试）。"""
    template_dir = Path(__file__).resolve().parents[2] / "strategy_templates"
    offenders = []
    for path in sorted(template_dir.glob("as*.py")):
        code = path.read_text(encoding="utf-8")
        if "class " not in code:
            continue
        for line in code.splitlines():
            if line.strip().startswith("#"):
                continue
            if "D.features(" in line:
                offenders.append(f"{path.name}: {line.strip()}")
    assert offenders == []


def test_position_get_stock_list_is_sorted_and_deterministic():
    """回归：qlib 的 Position.get_stock_list() 原本是 list(set(keys) - {...})，
    顺序随 PYTHONHASHSEED 变化；TopkDropoutStrategy 用它构造卖出单列表，
    现金不足的调仓日先卖哪只会改变实际成交 → 同一份信号在不同进程跑出不同年化
    （实测 standard_topk 2024 全年在 54.98%~61.31% 之间漂移，最大回撤恒为 -18.42%）。
    补丁后必须按代码排序，与插入顺序和哈希种子无关。"""
    qlib = pytest.importorskip("qlib")
    assert qlib is not None

    from backend.services.engine.qlib_app.utils import qlib_utils  # noqa: F401 触发补丁

    from qlib.backtest.position import Position

    pos = Position.__new__(Position)
    pos.position = {}
    for code in ["SZ000001", "SH600000", "SH600519", "cash", "now_account_value"]:
        pos.position[code] = {"amount": 0.0, "price": 0.0}

    assert pos.get_stock_list() == ["SH600000", "SH600519", "SZ000001"]


def test_crash_buy_dip_enters_after_crash_without_lookahead(monkeypatch):
    """回归：暴跌抄底的决策时序。

    - 决策在 T 日开盘前做，只能读 T-1 日（prev）为止的数据：暴跌日本身不能触发买入；
    - 买入确认用的是 prev 的 K 线，不是 T 日自己的（否则构成未来信息）；
    - 目标持仓与当前持仓一致时返回 None（返回 {} 会被 qlib 当成清仓）；
    - 持有到期后返回 {} 触发清仓。
    旧实现读不存在的 self.trade_step → 永远 None → 全年 0 笔成交。
    """
    qlib = pytest.importorskip("qlib")
    assert qlib is not None

    import pandas as pd
    from backend.services.engine.qlib_app.utils import extended_strategies as es

    day_strs = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
    days = [pd.Timestamp(d) for d in day_strs]
    close = {d: c for d, c in zip(day_strs, [10.0, 10.2, 9.6, 9.65, 9.8])}
    change = {
        "2024-01-02": 0.01,
        "2024-01-03": 0.02,
        "2024-01-04": -0.0588,  # 暴跌日
        "2024-01-05": 0.005,  # 企稳（prev 不跌 → 可入场）
        "2024-01-08": 0.015,
    }
    idx = pd.MultiIndex.from_product([["SH600000"], days], names=["instrument", "datetime"])
    factor_df = pd.DataFrame(
        {
            "$close": [close[d] for d in day_strs],
            "$change": [change[d] for d in day_strs],
            "ROC20": [0.1] * len(days),
            "MA5": [10.0] * len(days),
            "MA20": [9.0] * len(days),
            "VOL_RATIO": [1.2] * len(days),
            "KMID": [-0.01] * len(days),
            "LOWER_SHADOW": [0.005] * len(days),
        },
        index=idx,
    )

    monkeypatch.setattr(es.WeightStrategyBase, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(es.RedisCrashBuyDipStrategy, "init_redis", lambda self, kwargs: None)
    monkeypatch.setattr(
        es.RedisCrashBuyDipStrategy, "init_dynamic_risk", lambda self, kwargs: None
    )
    monkeypatch.setattr(
        es.RedisCrashBuyDipStrategy, "check_account_stop_loss", lambda self: False
    )

    strategy = es.RedisCrashBuyDipStrategy(signal="$close", top_k=5, rebalance_days=1)
    strategy._initialized = True
    strategy._all_dates = days
    strategy._date_index = {d: i for i, d in enumerate(days)}
    strategy._factor_df = factor_df
    strategy._crash_dates = {days[2]}
    strategy._crash_info = {days[2]: {"pct_change": -0.02}}

    empty_pos = types.SimpleNamespace(get_stock_list=lambda: [])
    held_pos = types.SimpleNamespace(get_stock_list=lambda: ["SH600000"])

    # 暴跌日本身不买（决策时只能看到 01-03 的数据）
    assert strategy.generate_target_weight_position(current=empty_pos, trade_start_time=days[2]) is None
    # 暴跌次日：候选股还没企稳（prev=01-04 仍在下跌）→ 不买
    assert strategy.generate_target_weight_position(current=empty_pos, trade_start_time=days[3]) is None
    # 再次日：prev=01-05 企稳 → 买入，价格用 prev 收盘价
    target = strategy.generate_target_weight_position(current=empty_pos, trade_start_time=days[4])
    assert target == {"SH600000": 1.0}
    assert strategy._positions["SH600000"]["buy_price"] == pytest.approx(9.65)
    assert strategy._positions["SH600000"]["crash_date"] == days[2]
    # 已持有 → 无委托（None 而不是 {}，{} 会被当成清仓）
    assert strategy.generate_target_weight_position(current=held_pos, trade_start_time=days[4]) is None
    # 持有到期 → 清仓
    strategy.hold_days = 2
    strategy._positions["SH600000"]["buy_date"] = days[2]
    assert strategy.generate_target_weight_position(current=held_pos, trade_start_time=days[4]) == {}


def test_crash_buy_dip_initializes_instruments_and_crash_dates(monkeypatch):
    """回归：旧实现用 list(D.instruments('csi300')) 拿到的是 dict 的键
    ['market','filter_pipe']，D.features 必然失败且被静默吞掉 → 全年 0 笔交易；
    另外指数 $change 在 qlib cn_data 里是 NaN，暴跌判断要用收盘价自算涨跌幅兜底。"""
    qlib = pytest.importorskip("qlib")
    assert qlib is not None

    import pandas as pd
    from backend.services.engine.qlib_app.utils import extended_strategies as es

    days = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    index_close = [3400.0, 3390.0, 3100.0]  # 最后一天跌 290 点

    class _FakeD:
        @staticmethod
        def instruments(market):
            return {"market": market, "filter_pipe": []}

        @staticmethod
        def list_instruments(cfg, start_time=None, end_time=None, as_list=False):
            return ["SH600000", "SZ000001"]

        @staticmethod
        def features(instruments, fields, start_time=None, end_time=None):
            idx = pd.MultiIndex.from_product(
                [list(instruments), days], names=["instrument", "datetime"]
            )
            if list(instruments) == ["SH000300"]:
                return pd.DataFrame(
                    {
                        "$close": index_close,
                        # 指数 $change 全为 NaN（真实 qlib cn_data 就是这样）
                        "$change": [float("nan")] * len(days),
                    },
                    index=idx,
                )
            data = {
                field: [10.0 + i for i in range(len(idx))]
                for field in fields
            }
            return pd.DataFrame(data, index=idx)

    monkeypatch.setattr(es, "D", _FakeD)
    monkeypatch.setattr(es.WeightStrategyBase, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(es.RedisCrashBuyDipStrategy, "init_redis", lambda self, kwargs: None)
    monkeypatch.setattr(
        es.RedisCrashBuyDipStrategy, "init_dynamic_risk", lambda self, kwargs: None
    )

    strategy = es.RedisCrashBuyDipStrategy(
        signal="$close", top_k=5, rebalance_days=1, benchmark="SH000300"
    )
    # trade_exchange 同样是 qlib BaseStrategy 的只读 property
    monkeypatch.setattr(
        es.RedisCrashBuyDipStrategy,
        "trade_exchange",
        property(lambda self: types.SimpleNamespace(_trade_calendar=days)),
    )

    strategy._ensure_initialized()

    assert strategy._instruments == ["SH600000", "SZ000001"]
    assert strategy._crash_dates == {pd.Timestamp("2024-01-04")}
    assert strategy._factor_df is not None
    # 暴跌日的涨跌幅要能算出来（$change 是 NaN，必须用收盘价兜底）
    assert strategy._crash_info[pd.Timestamp("2024-01-04")]["pct_change"] == pytest.approx(
        3100.0 / 3390.0 - 1.0
    )


def test_trade_stats_win_rate_ignores_flat_days():
    """回归：无 pnl 的成交流水回退到日收益口径时，空仓日（收益恰为 0）不能算进分母。

    否则事件型策略（as41 大部分时间空仓）的胜率会被 200 多个空仓日摊成 3%，
    而年化是 +21%。"""
    import pandas as pd

    trades = [
        {"date": "2025-01-02", "symbol": "SH600000", "action": "buy", "price": 10.0, "quantity": 100}
    ]
    returns = pd.Series(
        [0.0, 0.0, 0.01, -0.02, 0.03, 0.0],
        index=pd.bdate_range("2025-01-02", periods=6),
    )

    stats = RiskAnalyzer._calculate_trade_stats(trades, daily_returns=returns)

    assert stats["total_trades"] == 1
    assert stats["win_rate"] == pytest.approx(2 / 3)


def test_trade_stats_win_rate_uses_closed_trades_only():
    """回归：有 pnl 的流水里，买入单没有盈亏，不能算进胜率分母。"""
    trades = [
        {"action": "buy", "pnl": None},
        {"action": "sell", "pnl": 100.0},
        {"action": "sell", "pnl": -50.0},
    ]

    stats = RiskAnalyzer._calculate_trade_stats(trades, daily_returns=None)

    assert stats["win_rate"] == pytest.approx(0.5)


def test_normalize_trades_for_display_deduplicates_mixed_writers(monkeypatch):
    monkeypatch.setattr(RiskAnalyzer, "_load_factor_map", classmethod(lambda cls, pairs: {}))
    trades = [
        {
            "date": "2025-01-07",
            "symbol": "SH600018",
            "action": "buy",
            "price": 5.84,
            "quantity": 2700,
            "amount": 15768.0,
            "commission": 5.79,
            "timestamp": "2026-04-22T10:00:00",
        },
        {
            "date": "2025-01-07",
            "symbol": "SH600018",
            "action": "buy",
            "price": 5.84,
            "quantity": 2700.0,
            "totalAmount": 15768.0,
            "commission": 5.79,
            "adj_price": 2.5197833776474,
            "adj_quantity": 6264.226624905601,
            "factor": 0.4312969446182251,
            "equity_after": 999686.33,
        },
    ]

    normalized = RiskAnalyzer.normalize_trades_for_display(trades)
    assert len(normalized) == 1
    row = normalized[0]
    assert row["quantity"] == pytest.approx(2700.0, rel=1e-6)
    assert row["totalAmount"] == pytest.approx(15768.0, rel=1e-6)
    assert row["adj_price"] == pytest.approx(2.5197833776474, rel=1e-9)


def test_build_trades_list_deduplicates_redis_dupes(monkeypatch):
    monkeypatch.setattr(RiskAnalyzer, "_load_factor_map", classmethod(lambda cls, pairs: {}))

    class _FakeRedis:
        def lrange(self, key, start, end):
            assert key == "qlib:backtest:trades:bt-dup"
            exchange_trade = {
                "date": "2025-01-07",
                "symbol": "SH600018",
                "action": "buy",
                "price": 5.84,
                "quantity": 2700,
                "amount": 15768.0,
                "commission": 5.79,
            }
            strategy_trade = {
                "date": "2025-01-07",
                "symbol": "SH600018",
                "action": "buy",
                "price": 5.84,
                "quantity": 2700.0,
                "totalAmount": 15768.0,
                "commission": 5.79,
                "adj_price": 2.5197833776474,
                "adj_quantity": 6264.226624905601,
                "factor": 0.4312969446182251,
            }
            return [json.dumps(exchange_trade), json.dumps(strategy_trade)]

    monkeypatch.setattr(risk_analyzer_module, "get_redis_sentinel_client", lambda: _FakeRedis())
    trades = RiskAnalyzer._build_trades_list({}, backtest_id="bt-dup")
    assert len(trades) == 1
    assert trades[0]["totalAmount"] == pytest.approx(15768.0, rel=1e-6)


def test_advanced_trade_stats_handles_missing_pnl_columns():
    trades = [
        {"date": "2025-01-02", "symbol": "SH600000", "action": "buy", "price": 10.0, "quantity": 100},
        {"date": "2025-01-03", "symbol": "SH600001", "action": "sell", "price": 11.0, "quantity": 100},
    ]

    stats = RiskAnalyzer._calculate_advanced_trade_stats(trades)

    assert stats["pnl_distribution"]["counts"]
    assert stats["trade_frequency_series"]["values"] == [2.0]


def test_risk_metrics_uses_geometric_benchmark_annualization(monkeypatch):
    import pandas as pd

    dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08"])
    daily_returns = pd.Series([0.01, 0.02, -0.01, 0.0, 0.03], index=dates)
    prices = pd.Series([100.0, 101.0, 103.02, 101.9898, 101.9898, 105.049494], index=pd.to_datetime([
        "2025-01-01",
        "2025-01-02",
        "2025-01-03",
        "2025-01-06",
        "2025-01-07",
        "2025-01-08",
    ]))
    idx = pd.MultiIndex.from_product([["SH000300"], prices.index], names=["instrument", "datetime"])
    bm_df = pd.DataFrame({"$close": prices.to_numpy()}, index=idx)

    monkeypatch.setattr(risk_analyzer_module.D, "features", lambda *args, **kwargs: bm_df, raising=False)

    metrics = RiskAnalyzer._compute_risk_metrics(
        daily_returns=daily_returns,
        benchmark="SH000300",
        start_date="2025-01-01",
        end_date="2025-01-08",
        annual_return=9.99,
        risk_free_rate=0.0,
    )

    bm_returns = prices.pct_change().dropna()
    aligned = pd.concat([daily_returns, bm_returns], axis=1, join="inner")
    aligned.columns = ["portfolio", "benchmark"]
    beta = aligned["portfolio"].cov(aligned["benchmark"]) / aligned["benchmark"].var()
    expected_port_annual = (1.0 + aligned["portfolio"]).prod() ** (252 / len(aligned)) - 1
    expected_bm_annual = (1.0 + aligned["benchmark"]).prod() ** (252 / len(aligned)) - 1

    assert metrics["beta"] == pytest.approx(beta)
    assert metrics["alpha"] == pytest.approx(expected_port_annual - beta * expected_bm_annual)
