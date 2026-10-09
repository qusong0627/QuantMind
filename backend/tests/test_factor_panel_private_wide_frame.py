"""build_factor_panel_private._wide_frame —— (日,因子,标的)→宽表轴序回归。

**回归背景（2026-10-09）**

宽表 `monthly_scores.parquet` 写盘时对 `scores_o`（轴序 (D=日, F=因子, S=标的)）
直接 `reshape(D*S, F)`，缺 `transpose(0, 2, 1)` —— 这是按内存序错位切分：宽表
行 (d, s) 列 j 拿到的是 `orig(d, (s*F+j)//S, (s*F+j)%S)`，数值仍落在 [-4,4]，
不抛任何异常。与同一构建产出的面板逐因子比对 150/150 不一致，「多因子合成」
tab 经 `store.scores_for()` 吃这份错位宽表。本用例用 d*100+f*10+s 的哨兵值
逐格钉死轴序，并自检旧写法在同格子上必然给出不同值（否则用例本身失效）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "build_factor_panel_private.py"
)
_spec = importlib.util.spec_from_file_location("_bfpp_wide", _SCRIPT)
assert _spec and _spec.loader, f"无法加载 {_SCRIPT}"
bfpp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bfpp)


def _sentinel_scores() -> tuple[np.ndarray, pd.DatetimeIndex, list[str], list[str]]:
    dates = pd.DatetimeIndex(["2026-01-31", "2026-02-28", "2026-03-31"])
    symbols = ["000001.SZ", "600000.SH", "300750.SZ", "601318.SH"]
    names = ["FA", "FB"]
    scores = np.zeros((len(dates), len(names), len(symbols)), dtype=np.float64)
    for d in range(len(dates)):
        for f in range(len(names)):
            for s in range(len(symbols)):
                scores[d, f, s] = d * 100 + f * 10 + s
    return scores, dates, symbols, names


def test_wide_frame_axis_order_matches_day_factor_symbol_scores():
    scores, dates, symbols, names = _sentinel_scores()
    idx = pd.MultiIndex.from_product([dates, symbols], names=["trade_date", "symbol"])

    df = bfpp._wide_frame(scores, idx, names)

    assert list(df.index) == list(idx)
    assert list(df.columns) == names
    assert df.dtypes.eq(np.float32).all()
    for d, dt in enumerate(dates):
        for s, sym in enumerate(symbols):
            for f, name in enumerate(names):
                assert df.loc[(dt, sym), name] == scores[d, f, s], (
                    f"({dt}, {sym}) 列 {name} 轴序错位"
                )


def test_wide_frame_nan_slots_stay_nan():
    """全 NaN 日（分区缺失）整行 NaN —— 消费方按 dropna 剔除，不许写 0。"""
    scores, dates, symbols, names = _sentinel_scores()
    scores[1, :, :] = np.nan  # 中间一天整日缺失
    idx = pd.MultiIndex.from_product([dates, symbols], names=["trade_date", "symbol"])

    df = bfpp._wide_frame(scores, idx, names)

    assert df.loc[dates[1]].isna().all().all()
    assert not df.loc[dates[0]].isna().any().any()


def test_naive_reshape_would_differ_canary():
    """用例自检：旧写法（缺 transpose 的 reshape）必须给出不同值。

    若这条断言失败，说明哨兵矩阵尺寸/取值选得无法暴露错位，前两个用例是空转。
    """
    scores, dates, symbols, names = _sentinel_scores()
    idx = pd.MultiIndex.from_product([dates, symbols], names=["trade_date", "symbol"])

    naive = pd.DataFrame(
        scores.reshape(len(dates) * len(symbols), len(names)), index=idx, columns=names
    )
    diff = sum(
        1
        for d in range(len(dates))
        for s in range(len(symbols))
        for f in range(len(names))
        if naive.loc[(dates[d], symbols[s]), names[f]] != scores[d, f, s]
    )
    assert diff > 0, "旧写法竟与正确值一致 —— 本文件测不出轴序错位"
