"""`_sample_rows` 与它所替换的 pandas 原写法**逐位等价**。

背景：`compute_psi_drift` 原来用
`df[mask][features].dropna(how="all").sample(n, random_state=42)` 取 5 万行样本，
但那一行要付**三份整帧级临时量**（掩码选帧 / 283 列切片 / dropna 结果）——train 窗
覆盖全区间时每份 ≈ 一整个全帧，投影峰值 ≈ 48.9G。2026-09-23 实测 VmHWM 48.61G、
宿主可用一度只剩 3.71G，就是它。

换成 `_sample_rows`（逐列判 NaN + 同长度薄帧复现抽样流 + 只 take 那 5 万行）后
只剩 5 万行的拷贝。**等价性是这次替换的全部理由**，因此这里把原写法当判据锁死：
值 / dtype / 列序 / 索引标签 / 行顺序，全部逐位相同。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


def _load_drift_module():
    """按**文件路径**加载 drift.py，不走 `diagnostics` 包。

    `diagnostics/__init__.py` 会拉起 explain → lightgbm，而本测试只用到
    `_sample_rows`（drift.py 自身只依赖 numpy/pandas），没必要为它背整个包的重依赖。
    """
    path = Path(__file__).resolve().parents[2] / "docker" / "training" / "diagnostics" / "drift.py"
    spec = importlib.util.spec_from_file_location("qm_test_drift", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


_sample_rows = _load_drift_module()._sample_rows


def _make_frame(n_days: int = 40, n_syms: int = 25, nan_rows: int = 0) -> pd.DataFrame:
    """交易日 × 标的的帧；`nan_rows` 造若干「全部特征为 NaN」的行（dropna(how='all') 要剔的）。"""
    days = pd.bdate_range("2023-01-02", periods=n_days)
    n = n_days * n_syms
    rng = np.random.default_rng(7)
    df = pd.DataFrame(
        {
            "symbol": pd.Series(
                np.tile([f"SH6000{i:02d}" for i in range(n_syms)], n_days), dtype=object
            ),
            "trade_date": pd.Series(np.repeat(days.to_numpy(), n_syms)),
            "f1": rng.standard_normal(n).astype(np.float32),
            "f2": rng.standard_normal(n).astype(np.float32) * 3.0,
            "f3": rng.standard_normal(n).astype(np.float32),
        }
    )
    if nan_rows:
        idx = rng.choice(n, size=nan_rows, replace=False)
        df.loc[idx, ["f1", "f2", "f3"]] = np.float32("nan")
    return df


def _legacy(df: pd.DataFrame, mask, columns: list[str], n: int) -> pd.DataFrame:
    """被替换掉的原写法——等价性判据。"""
    return df[mask][columns].dropna(how="all").sample(n, random_state=42)


# ── 等价性 ──────────────────────────────────────────────────────────────────

def test_matches_legacy_selection_bit_for_bit() -> None:
    df = _make_frame()
    mask = df["trade_date"] <= pd.Timestamp("2023-02-10")
    columns = ["f1", "f2", "f3"]

    expected = _legacy(df, mask, columns, 200)
    got = _sample_rows(df, mask, columns, 200)

    pd.testing.assert_frame_equal(got, expected, check_exact=True)
    # 行顺序也必须一致（下游按位算 rank，顺序变了虽然不影响分布统计，但没必要冒险）
    assert list(got.index) == list(expected.index)


def test_matches_legacy_when_some_rows_are_all_nan() -> None:
    """有「全 NaN 行」时，抽样总体必须是 dropna 之后的那一批。"""
    df = _make_frame(nan_rows=37)
    mask = df["trade_date"] <= pd.Timestamp("2023-02-10")
    columns = ["f1", "f2", "f3"]

    expected = _legacy(df, mask, columns, 150)
    got = _sample_rows(df, mask, columns, 150)

    pd.testing.assert_frame_equal(got, expected, check_exact=True)


def test_matches_legacy_when_n_equals_population() -> None:
    """n == 总体时不许被裁（否则会静默少给样本）。"""
    df = _make_frame(n_days=2, n_syms=5)  # 10 行
    mask = df["trade_date"] <= pd.Timestamp("2023-01-03")
    columns = ["f1", "f2", "f3"]

    expected = _legacy(df, mask, columns, int(mask.sum()))
    got = _sample_rows(df, mask, columns, int(mask.sum()))

    pd.testing.assert_frame_equal(got, expected, check_exact=True)
    assert len(got) == int(mask.sum())


def test_sampling_is_independent_of_column_values() -> None:
    """抽样只取决于 (行数, n, 种子)：换一批数值，选中的**行**不变（薄帧方案的立足点）。"""
    df = _make_frame()
    mask = df["trade_date"] <= pd.Timestamp("2023-02-10")
    columns = ["f1", "f2", "f3"]

    a = _sample_rows(df, mask, columns, 120)
    shifted = df.copy()
    shifted[columns] = shifted[columns] + np.float32(100.0)  # 值全变，行集合应不变
    b = _sample_rows(shifted, mask, columns, 120)

    assert list(a.index) == list(b.index)


# ── 边界与回归 ───────────────────────────────────────────────────────────────

def test_raises_when_population_smaller_than_n() -> None:
    """总体不足时与 pandas 原写法同样抛 ValueError（不静默少给样本）。"""
    df = _make_frame(n_days=2, n_syms=5)
    mask = df["trade_date"] <= pd.Timestamp("2023-01-03")
    columns = ["f1", "f2", "f3"]

    with pytest.raises(ValueError, match="larger sample than population"):
        _sample_rows(df, mask, columns, int(mask.sum()) + 1)


def test_object_dtype_column_does_not_raise() -> None:
    """特征表里可能有非浮点列（如 ind_code_l1/l2）；不得因强制转 float 而抛错。

    原写法走 pandas 自己的 dropna，任何 dtype 都能过；替换实现必须守住这条。
    """
    df = _make_frame()
    df["ind_code_l1"] = pd.Series(
        np.tile([f"SW{i % 7:02d}" for i in range(len(df))], 1), dtype=object
    )
    df.loc[df.index[:3], "ind_code_l1"] = None  # 对象列的缺失值也要能被识别
    mask = df["trade_date"] <= pd.Timestamp("2023-02-10")
    columns = ["f1", "ind_code_l1"]

    expected = _legacy(df, mask, columns, 90)
    got = _sample_rows(df, mask, columns, 90)

    pd.testing.assert_frame_equal(got, expected, check_exact=True)


def test_returns_only_requested_columns_in_order() -> None:
    df = _make_frame()
    mask = df["trade_date"] <= pd.Timestamp("2023-02-10")

    got = _sample_rows(df, mask, ["f3", "f1"], 40)

    assert list(got.columns) == ["f3", "f1"]


def test_does_not_mutate_source_frame() -> None:
    df = _make_frame(nan_rows=5)
    before = df.copy(deep=True)
    mask = df["trade_date"] <= pd.Timestamp("2023-02-10")

    got = _sample_rows(df, mask, ["f1", "f2"], 30)
    got.iloc[0, 0] = np.float32(0.0)  # 写样本：不得回灌源帧

    pd.testing.assert_frame_equal(df, before, check_exact=True)
