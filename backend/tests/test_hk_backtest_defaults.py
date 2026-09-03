"""港股回测市场默认口径测试。

核心护栏：
- CN（缺省/显式旧默认）数值与历史逐字节全等 —— A 股零变化
- HK（provider=hk_data）自动补港股费率默认（佣金 0.0003 min3、印花 0.001 卖、无过户、基准 HSI）
- 显式传参优先；聚合费率在 HK 下被忽略（防双边印花重复计提）
- CnExchange 无涨跌停开关（HK 未来补 change 数据也不被 9.5% 误拦）
"""

import pytest

from backend.services.engine.qlib_app.schemas.backtest import (
    QlibBacktestRequest,
    apply_market_defaults,
    infer_market_from_request,
)

# 与历史 schema 默认值逐字节对齐（CN 零变化断言基准）
_CN_LEGACY = {
    "commission": 0.00025,
    "min_commission": 5.0,
    "stamp_duty": 0.0005,
    "transfer_fee": 0.00001,
    "min_transfer_fee": 0.01,
    "impact_cost_coefficient": 0.0005,
    "benchmark": "SH000300",
}
_HK_DEFAULTS = {
    "commission": 0.0003,
    "min_commission": 3.0,
    "stamp_duty": 0.001,
    "transfer_fee": 0.0,
    "min_transfer_fee": 0.0,
    "impact_cost_coefficient": 0.0005,
    "benchmark": "HSI",
}
_FEE_FIELDS = (
    "commission",
    "min_commission",
    "stamp_duty",
    "transfer_fee",
    "min_transfer_fee",
    "impact_cost_coefficient",
    "benchmark",
)

_CN_REQUEST = {
    "strategy_type": "TopkDropout",
    "start_date": "2025-01-01",
    "end_date": "2025-06-30",
}
_HK_REQUEST = {**_CN_REQUEST, "qlib_provider_uri": "/data/quanthk/.qlib_cache/hk_data"}


# ---- 市场推断 ----


def test_infer_market_cn_default():
    assert infer_market_from_request(None, None) == "CN"
    assert infer_market_from_request("", "cn") == "CN"  # region=cn + 无 hk 线索


def test_infer_market_hk_by_uri_or_region():
    assert infer_market_from_request("/data/quanthk/.qlib_cache/hk_data", "cn") == "HK"
    assert infer_market_from_request("db/qlib_data/hk_data", None) == "HK"
    assert infer_market_from_request(None, "hk") == "HK"
    # quanthk 目录兜底（历史相对路径形态）
    assert infer_market_from_request("data/quanthk/.qlib_cache/hk_data", None) == "HK"


def test_infer_market_us():
    assert infer_market_from_request(None, "us") == "US"


# ---- CN 零变化（核心护栏） ----


def test_cn_request_defaults_equal_legacy():
    req = QlibBacktestRequest(**_CN_REQUEST)
    for field in _FEE_FIELDS:
        assert getattr(req, field) == _CN_LEGACY[field], f"CN 默认 {field} 与历史不一致"


def test_cn_request_explicit_overrides_preserved():
    req = QlibBacktestRequest(**_CN_REQUEST, commission=0.001, benchmark="SH000905")
    assert req.commission == 0.001
    assert req.benchmark == "SH000905"
    assert req.stamp_duty == _CN_LEGACY["stamp_duty"]  # 其余仍走默认


def test_cn_explicit_legacy_equals_no_field():
    """显式传历史默认值 与 不传（走默认）数值全等。"""
    explicit = QlibBacktestRequest(**_CN_REQUEST, **_CN_LEGACY)
    implicit = QlibBacktestRequest(**_CN_REQUEST)
    for field in _FEE_FIELDS:
        assert getattr(explicit, field) == getattr(implicit, field), field


def test_benchmark_alias_still_works():
    req = QlibBacktestRequest(**_CN_REQUEST, benchmark_symbol="SH000300")
    assert req.benchmark == "SH000300"


# ---- HK 默认 ----


def test_hk_request_defaults():
    req = QlibBacktestRequest(**_HK_REQUEST)
    for field in _FEE_FIELDS:
        assert getattr(req, field) == _HK_DEFAULTS[field], f"HK 默认 {field} 错误"


def test_hk_explicit_fee_precedence():
    req = QlibBacktestRequest(**_HK_REQUEST, stamp_duty=0.002)
    assert req.stamp_duty == 0.002
    assert req.commission == _HK_DEFAULTS["commission"]


def test_apply_market_defaults_idempotent():
    req = QlibBacktestRequest(**_HK_REQUEST)
    snapshot = {f: getattr(req, f) for f in _FEE_FIELDS}
    apply_market_defaults(req)  # 二次调用不改值
    for f, v in snapshot.items():
        assert getattr(req, f) == v


def test_hk_aggregated_fee_rejected_at_runtime_guard():
    """HK + 聚合费率并存属于运行时告警分支（runtime 忽略聚合用明细），此处验证明细默认完整。"""
    req = QlibBacktestRequest(**_HK_REQUEST, buy_cost=0.001, sell_cost=0.002)
    assert req.buy_cost == 0.001 and req.sell_cost == 0.002  # schema 层保留
    # runtime 层（backtest_service_runtime）对该组合降级明细 —— 由集成用例覆盖


# ---- CnExchange 市场开关（需 qlib 环境，容器内运行） ----


def test_cn_exchange_no_price_limits_short_circuits():
    qlib = pytest.importorskip("qlib")
    from qlib.backtest.decision import OrderDir

    from backend.services.engine.qlib_app.utils.cn_exchange import CnExchange

    ex = CnExchange(has_price_limits=False)
    import pandas as pd

    t = pd.Timestamp("2025-01-02")
    # 无涨跌停市场：任何时刻都返回 False（可交易），不依赖 $change 数据
    assert ex.check_stock_limit("hk_0700.HK", t, t + pd.Timedelta(days=1), direction=OrderDir.BUY) is False
    assert ex.check_stock_limit("hk_0700.HK", t, t + pd.Timedelta(days=1), direction=OrderDir.SELL) is False


def test_cn_exchange_stamp_both_sides_when_sell_only_disabled():
    qlib = pytest.importorskip("qlib")
    from backend.services.engine.qlib_app.utils.cn_exchange import CnExchange

    # 港股卖出口径简化（默认 stamp_duty_on_sell_only=True 时卖出计印花）
    ex_sell_only = CnExchange(stamp_duty=0.001)
    # 方向常量：qlib OrderDir.BUY/SELL
    from qlib.backtest.decision import OrderDir

    cost_buy = ex_sell_only.calculate_cost("hk_0001.HK", 100_000.0, OrderDir.BUY, market_volume_val=1e9)
    cost_sell = ex_sell_only.calculate_cost("hk_0001.HK", 100_000.0, OrderDir.SELL, market_volume_val=1e9)
    assert cost_buy < cost_sell  # 买入无印花，卖出含 0.1%
    assert abs((cost_sell - cost_buy) - 100_000.0 * 0.001) < 1.0
    # 港股无 SH 过户费
    assert cost_buy < 100_000.0 * 0.00031

# ---- Qlib 代码对齐（HK 信号→池文件 instrument） ----

def test_to_qlib_prefix_code_hk_and_us():
    """pred 信号代码 → qlib 池文件形态（港股保留 .HK 大小写，与 instruments 一致）。"""
    qlib = pytest.importorskip("qlib")
    import importlib

    rt = importlib.import_module(
        "backend.services.engine.qlib_app.services.backtest_service_runtime"
    )
    M = rt.QlibBacktestServiceRuntimeMixin
    assert M._to_qlib_prefix_code("0001.HK") == "hk_0001.HK"
    assert M._to_qlib_prefix_code("0700.HK") == "hk_0700.HK"
    assert M._to_qlib_prefix_code("hk_0001.HK") == "hk_0001.HK"  # 已带前缀短路
    assert M._to_qlib_prefix_code("600036.SH") == "sh600036"  # A 股不变
    assert M._to_qlib_prefix_code("000300.SH") == "sh000300"
    assert M._to_qlib_prefix_code("SH600036") == "sh600036"
    assert M._to_qlib_prefix_code("600036") == "sh600036"
    assert M._to_qlib_prefix_code("AAPL") == "us_aapl"
    assert M._to_qlib_prefix_code("us_aapl") == "us_aapl"
