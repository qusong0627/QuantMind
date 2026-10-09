"""融合推理模板的数据面分派契约 —— QuantDB 直读优先 / parquet 兜底。

背景（2026-10-09）：融合模板 `load_day_data` 只认 model_features_{year}.parquet
布局，而融合 metadata 被硬编码 data_source=parquet —— 成员读 quantdb_factors
时融合模型读的是停更快照。修复后模板按自身 metadata 分派数据面（口径与成员
模板 inference_parquet.load_date_data 同源），本测试锁定：

1. data_source=quantdb_factors → 用 pin 目录的 reader 读，按名过滤不可交易行
   （close<=0 / volume<=0 / is_st==1 全剔除），read_day 收到 features/source/
   feature_sources 元数据原样。
2. reader 初始化失败 / 读取异常 → 返回 None（exit 2 语义），绝不静默换面。
3. 未声明 data_source → parquet 布局（向后兼容旧融合模型）。
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pandas as pd
import pytest

_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "engine"
    / "inference"
    / "templates"
    / "inference_ensemble_src.py"
)
_TRADE_DATE = "2026-10-09"


@pytest.fixture(scope="module")
def tpl():
    spec = importlib.util.spec_from_file_location(
        "inference_ensemble_src_daydata_under_test", _TEMPLATE
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeReader:
    """记录调用参数 + 返回含不可交易行的当日截面。"""

    instances: list[_FakeReader] = []
    fail_on_read = False

    def __init__(self, path: str):
        self.path = path
        self.calls: list[tuple] = []
        _FakeReader.instances.append(self)

    def assert_ready(self, source, *, start=None, end=None):
        self.calls.append(("assert_ready", source, start, end))
        return types.SimpleNamespace(ready=True, schema_hash="abc123")

    def read_day(self, source, *, features, trade_date, feature_sources=None):
        if _FakeReader.fail_on_read:
            raise RuntimeError("parquet 损坏")
        self.calls.append(("read_day", source, tuple(features), trade_date, feature_sources))
        return pd.DataFrame(
            {
                "symbol": ["SH600036", "SZ000001", "SH600000"],
                "trade_date": [trade_date] * 3,
                "close": [10.0, 0.0, 5.0],   # 第 2 行价格异常
                "volume": [100, 200, 0],     # 第 3 行零成交
                "is_st": [0, 0, 1],          # 第 3 行 ST
                "f1": [1.0, 2.0, 3.0],
            }
        )


@pytest.fixture()
def patched(monkeypatch):
    from backend.services.engine.data_platform import quantdb_factor_reader as qfr
    from backend.shared import quantdb_paths

    _FakeReader.instances = []
    _FakeReader.fail_on_read = False
    monkeypatch.setattr(qfr, "QuantDBFactorReader", _FakeReader)
    monkeypatch.setattr(
        quantdb_paths, "resolve_pinned_data_dir", lambda p: Path(p) if p else None
    )
    return _FakeReader


_META = {
    "data_source": "quantdb_factors",
    "quantdb_dir": "/data/quantcustom",
    "factor_source": "l1_factors",
    "factor_schema_hash": "abc123",
    "feature_columns": ["f1"],
    "factor_field_sources": {"f1": "lib_a:c1"},
}


class TestQuantdbPlane:
    def test_reads_pinned_reader_and_filters_untradable(self, tpl, patched):
        out = tpl.load_day_data(_TRADE_DATE, Path("/fallback"), meta=_META)
        assert out is not None
        assert out["symbol"].tolist() == ["SH600036"]  # 仅全部合规行存活
        reader = patched.instances[-1]
        assert reader.path == "/data/quantcustom"  # pin 目录，不是 data_dir 兜底
        read_day = [c for c in reader.calls if c[0] == "read_day"][0]
        assert read_day[1] == "l1_factors"
        assert read_day[2] == ("f1",)
        assert read_day[3] == _TRADE_DATE
        assert read_day[4] == {"f1": "lib_a:c1"}

    def test_read_failure_returns_none_not_silent_parquet(self, tpl, patched):
        patched.fail_on_read = True
        assert tpl.load_day_data(_TRADE_DATE, Path("/fallback"), meta=_META) is None

    def test_reader_init_failure_does_not_crash(self, tpl, monkeypatch):
        from backend.services.engine.data_platform import quantdb_factor_reader as qfr

        def boom(path):
            raise RuntimeError("reader 初始化炸了")

        monkeypatch.setattr(qfr, "QuantDBFactorReader", boom)
        # reader 初始化失败 → 回退 parquet 布局；目录里没有 parquet → None
        assert (
            tpl.load_day_data(_TRADE_DATE, Path("/nonexistent-dir"), meta=_META) is None
        )


class TestParquetFallback:
    def test_undeclared_data_source_uses_parquet_layout(self, tpl, tmp_path):
        df = pd.DataFrame(
            {
                "symbol": ["SH600036", "SZ000001"],
                "trade_date": [_TRADE_DATE, _TRADE_DATE],
                "close": [10.0, 0.0],
                "volume": [100, 100],
                "is_st": [0, 0],
                "f1": [1.0, 2.0],
            }
        )
        df.to_parquet(tmp_path / "model_features_2026.parquet", index=False)
        out = tpl.load_day_data(_TRADE_DATE, tmp_path, meta={})
        assert out is not None
        assert out["symbol"].tolist() == ["SH600036"]  # close<=0 被剔除
