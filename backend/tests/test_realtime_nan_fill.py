"""实时装配的缺失值判据必须认 **float32**（QuantDB 直读的唯一数值类型）。

缺陷（2026-10-08 实测确认）：`compute_cycle` 的缺失判据是
``isinstance(val, float) and np.isnan(val)``，但 QuantDB 因子列在 DuckDB 侧被
显式 CAST 成 ``FLOAT``（float32，见 `quantdb_factor_reader.py` 的 CAST 段注释：
float64 会让 429 因子全历史长表超训练容器内存），而 ``np.float32`` **不是**
Python ``float`` 的子类（``np.float64`` 是）——判据恒为 False。

后果：QuantDB 直读模型的每个 NaN 特征都**静默绕过** `fill_values`，以 NaN 直接
进 ONNX 输入矩阵，且 ``missing`` 计数不涨（运维看不到）。实测受害面：模型
``mdl_cust_train_20260914130341_887a7a0d_c2e90650`` 在 2026-09-11 的基线上，
600519 有 3 个、000001 有 2 个 float32 NaN，旧判据识别到 0 个。

同源第二处：`incremental_features.features_with_fallback` 的 ``base_finite``
——float32 NaN 基线会被判成「有限」，provenance 记成 ``t1``（声称沿用了基线
值），实际 row 里仍是 NaN。修好本文件的两个判据后，NaN 才真正被兜住。

遗留快照模型（``model_features_{year}.parquet``，float64）不受影响，故两条路径
都要有守卫：float32 必须被认出来，float64 与 None 的既有语义不许变。
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.services.engine.inference import realtime_core as rc
from backend.services.engine.inference.incremental_features import (
    IncrementalFeatureEngine,
)

_COL = "vol_20"
_FILL = {_COL: -7.0}


class _Session:
    """伪 ONNX 会话：原样收下输入矩阵，返回每行求和当分数。"""

    def __init__(self) -> None:
        self.seen: np.ndarray | None = None

    def run(self, _outputs: list[str], feeds: dict[str, np.ndarray]):
        x = feeds["features"]
        self.seen = x
        return [x.sum(axis=1)]


def _cycle(value):
    """把单个标的的单个特征值喂进 compute_cycle，返回 (结果, 会话)。"""
    session = _Session()
    # 基线键=后缀身份（identity；引擎侧身份空间，000001.SH≠000001.SZ——审计 M2）
    baseline = {rc.identity("600519"): {_COL: value}}
    result = rc.compute_cycle(
        session=session,
        input_name="features",
        cols=[_COL],
        fill=dict(_FILL),
        model_version="test",
        hot=["600519"],
        snapshots={},
        baseline=baseline,
        histories={},
        override=set(),
        engine=None,
        bootstrapped=set(),
    )
    return result, session


def test_float32_nan_is_filled() -> None:
    """QuantDB 直读的 float32 NaN 必须走 fill_values（旧行为：NaN 进 ONNX）。"""
    result, session = _cycle(np.float32("nan"))

    assert session.seen is not None
    assert not np.isnan(session.seen[0, 0]), "float32 NaN 漏进了 ONNX 输入矩阵"
    assert session.seen[0, 0] == pytest.approx(_FILL[_COL])


def test_float32_nan_counts_as_missing() -> None:
    """漏填的连带后果：missing 计数不涨，运维看不见缺口。"""
    result, _ = _cycle(np.float32("nan"))

    assert result.missing == 1


def test_float32_finite_value_is_used_as_is() -> None:
    """反向守卫：正常的 float32 值不许被当成缺失填掉。"""
    result, session = _cycle(np.float32(3.5))

    assert session.seen is not None
    assert session.seen[0, 0] == pytest.approx(3.5)
    assert result.missing == 0


def test_float64_nan_still_filled() -> None:
    """遗留快照路径（float64）语义不变。"""
    result, session = _cycle(np.float64("nan"))

    assert session.seen is not None
    assert session.seen[0, 0] == pytest.approx(_FILL[_COL])
    assert result.missing == 1


def test_none_still_filled() -> None:
    """None（列缺失）语义不变。"""
    result, session = _cycle(None)

    assert session.seen is not None
    assert session.seen[0, 0] == pytest.approx(_FILL[_COL])
    assert result.missing == 1


def test_python_float_nan_still_filled() -> None:
    """纯 Python float NaN 语义不变。"""
    result, session = _cycle(float("nan"))

    assert session.seen is not None
    assert session.seen[0, 0] == pytest.approx(_FILL[_COL])
    assert result.missing == 1


def test_provenance_marks_float32_nan_baseline_as_cold() -> None:
    """float32 NaN 基线不是「可用基线」：provenance 必须是 cold，不是 t1。"""
    engine = IncrementalFeatureEngine()
    engine.compute = lambda _symbol: {_COL: np.float32("nan")}  # type: ignore[method-assign]

    row, prov = engine.features_with_fallback("600519", {_COL: np.float32("nan")})

    assert prov[_COL] == "cold", "float32 NaN 基线被当成了可用的 t1 值"
    assert np.isnan(row[_COL])


def test_provenance_keeps_finite_float32_baseline_as_t1() -> None:
    """反向守卫：有限的 float32 基线仍应记 t1（live 缺失时确实沿用了基线）。"""
    engine = IncrementalFeatureEngine()
    engine.compute = lambda _symbol: {_COL: np.float32("nan")}  # type: ignore[method-assign]

    row, prov = engine.features_with_fallback("600519", {_COL: np.float32(1.25)})

    assert prov[_COL] == "t1"
    assert row[_COL] == pytest.approx(1.25)
