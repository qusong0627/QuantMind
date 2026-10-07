"""推理**执行侧**的 QuantDB schema 漂移判据（与预检同策）。

判据边界（2026-09-20 b3e3a61b 定的政策，本文件把它锁到执行侧）：

- **缺列 = 硬失败**——模型要用的列按名取不到，这是真错误；
- **整库列名漂移 = 放行 + 告警**——列都在，只是因子库里多了别的列。

为什么必须锁执行侧：`script_runner._query_quantdb_readiness`（预检）当天改成了
「漂移只提示」，但真正读数的两处 —— `templates/inference_parquet.py`（渲染进每个
模型目录的脚本）与 `data_loader.py`（回测/批量共用加载器）—— 当时没跟着改，仍是
`raise`。后果正是那次改动要消灭的症状：**预检绿灯、跑起来 exit 2「该日期无数据」**。

实测受影响：`mdl_cust_train_20260914130341_887a7a0d_c2e90650`（锚库
`/data/quantcustom/l1_factors` 273→282 列，注册表状态 `ready`）。

漂移在两侧的作用不同，别把这里的放行理解成「不检查」：执行侧的硬失败由
`QuantDBFactorReader.read_range` 的按名缺列检查给出（锚库/副库分别指名道姓），
比「整库列名集合哈希不等」精确得多。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from backend.services.engine.data_platform.quantdb_factor_reader import (
    QuantDBFactorError,
)
from backend.services.engine.inference import data_loader
from backend.services.engine.inference.templates import inference_parquet

_RECORDED_HASH = "recordedhash"
_LIVE_HASH = "livehash"


class _Status:
    def __init__(self, schema_hash: str) -> None:
        self.schema_hash = schema_hash
        self.columns = ["symbol", "date", "close", "volume", "vol_20"]
        self.ready = True
        self.reason = None
        self.missing_required: list[str] = []
        self.min_date = "2016-01-04"
        self.max_date = "2026-09-18"


class _Reader:
    """伪读取器：只实现 load_date_data 用到的那两个方法。"""

    def __init__(
        self, *, schema_hash: str, read_error: Exception | None = None
    ) -> None:
        self._schema_hash = schema_hash
        self._read_error = read_error
        self.calls: list[str] = []

    def assert_ready(
        self, source: str, *, start: Any = None, end: Any = None
    ) -> _Status:
        self.calls.append(f"assert_ready:{source}")
        return _Status(self._schema_hash)

    def read_day(
        self,
        source: str,
        *,
        features: list[str],
        trade_date: Any,
        feature_sources: dict[str, str] | None = None,
    ) -> pd.DataFrame:
        self.calls.append(f"read_day:{source}")
        if self._read_error is not None:
            # read_range 的缺列口径：锚库/副库都按名报错
            raise self._read_error
        return pd.DataFrame(
            {
                "symbol": ["600519.SH", "000001.SZ"],
                "trade_date": [trade_date, trade_date],
                "close": [1700.0, 12.0],
                "volume": [1000.0, 2000.0],
                "vol_20": [0.1, 0.2],
            }
        )


_META = {
    "data_source": "quantdb_factors",
    "factor_source": "l1_factors",
    "factor_field_sources": {"vol_20": "vol_20"},
    "feature_columns": ["vol_20"],
}

# 两条执行链：(名字, 模块, 调用适配器)。适配器只吃 (meta)，reader 由 fixture 注入。
_CHAINS = [
    (
        "data_loader",
        data_loader,
        lambda meta: data_loader.load_date_data(
            "2026-09-18", data_dir=Path("/tmp"), meta=meta
        ),
    ),
    (
        "inference_parquet",
        inference_parquet,
        lambda meta: inference_parquet.load_date_data("2026-09-18", Path("/tmp"), meta),
    ),
]


@pytest.fixture(params=_CHAINS, ids=[c[0] for c in _CHAINS])
def chain(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """(名字, 注入 reader 后可直接调用的 load_date_data)。"""
    name, module, call = request.param

    def _armed(reader: _Reader):
        monkeypatch.setattr(module, "_quantdb_reader", lambda _meta, _dir: reader)
        return call

    return name, _armed


def test_schema_drift_with_columns_present_is_not_fatal(chain) -> None:
    """漂移只该告警：列都在，推理必须跑得下去（旧行为 return None → exit 2）。"""
    name, armed = chain
    reader = _Reader(schema_hash=_LIVE_HASH)

    day_df = armed(reader)({"factor_schema_hash": _RECORDED_HASH, **_META})

    assert day_df is not None, f"{name}: 列齐全的漂移不得中断推理"
    assert len(day_df) == 2, f"{name}: 数据行必须原样返回"
    assert "read_day:l1_factors" in reader.calls, f"{name}: 必须真的去读数"


def test_missing_mapped_column_still_fails_hard(chain) -> None:
    """缺列仍是硬失败：read_range 报缺列 → None → exit 2（这条不许被放松）。"""
    name, armed = chain
    reader = _Reader(
        schema_hash=_LIVE_HASH,
        read_error=QuantDBFactorError("l1_factors is missing mapped fields: vol_20"),
    )

    day_df = armed(reader)({"factor_schema_hash": _LIVE_HASH, **_META})

    assert day_df is None, f"{name}: 缺列必须硬失败"


def test_schema_drift_is_visible_in_the_log(chain, caplog) -> None:
    """放行 ≠ 无事发生：漂移必须在日志里留下 WARNING。

    没有这条，"漂移只提示"里的**提示**可以被无声删掉（或降成 debug）而测试全绿——
    那时推理就在没有信号的情况下按新库面跑，运维无从察觉。
    """
    name, armed = chain
    reader = _Reader(schema_hash=_LIVE_HASH)

    with caplog.at_level(logging.WARNING):
        day_df = armed(reader)({"factor_schema_hash": _RECORDED_HASH, **_META})

    assert day_df is not None, name
    drift = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "schema drift" in r.getMessage()
    ]
    assert drift, f"{name}: 漂移没有留下 WARNING"
    msg = drift[0].getMessage()
    assert _RECORDED_HASH[:16] in msg and _LIVE_HASH[:16] in msg, f"{name}: 告警要能对上两边哈希"


def test_hash_match_is_unaffected(chain) -> None:
    """哈希一致（绝大多数模型）走原路，不得被本次改动波及。"""
    name, armed = chain
    reader = _Reader(schema_hash=_LIVE_HASH)

    day_df = armed(reader)({"factor_schema_hash": _LIVE_HASH, **_META})

    assert day_df is not None and len(day_df) == 2, name


def test_absent_recorded_hash_skips_comparison(chain) -> None:
    """没记哈希的老模型：不做比较（与就绪检查同判据）。"""
    name, armed = chain
    reader = _Reader(schema_hash=_LIVE_HASH)

    day_df = armed(reader)({**_META})

    assert day_df is not None and len(day_df) == 2, name
