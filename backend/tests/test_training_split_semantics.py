"""数据集切分 `_split_data` 行为测试。

两个要害：

1. **语义**：train < val < test 三段隔离，且每段尾部要裁掉
   `target_horizon_days + _EXECUTION_LAG_DAYS` 个交易日（embargo）。标签是未来
   horizon 日收益，不裁则 train 末尾样本的标签落在 val 区间，价格信息经标签渗回
   训练集——这是跨段泄漏。
2. **内存**：切分是全窗口帧上最大的一次性拷贝大户。10.72M 行 × 291 列的帧约
   12.1G，每多一次「整段分配」就多一份 ~9.65G 瞬时拷贝。2026-09-23 的 283 特征
   训练两次死在切分段（宿主全局 OOM）。因此这里立两条回归契约：

   - 每段只从源帧**切一次**，不得对已切出的段再切；
   - 选完帧**不得再付**第二次整段拷贝（`.reset_index(drop=True)` 在 pandas 2.3
     无 CoW 下就是一次整段深拷，索引归零改由 `_take_rows` 就地赋值）。
"""
from __future__ import annotations

import functools
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_TRAINING_DIR = Path(__file__).resolve().parents[2] / "docker" / "training"
if str(_TRAINING_DIR) not in sys.path:
    sys.path.insert(0, str(_TRAINING_DIR))

from data.splits import _EXECUTION_LAG_DAYS, _split_data, _take_rows  # noqa: E402

_HORIZON = 5
_EMBARGO_DAYS = _HORIZON + _EXECUTION_LAG_DAYS  # 6


def _make_frame(n_days: int = 60, n_syms: int = 3, start: str = "2020-01-01") -> pd.DataFrame:
    """按交易日 × 标的造帧（列结构与真实训练帧一致：元数据 + float32 特征 + label）。"""
    days = pd.bdate_range(start, periods=n_days)
    n = n_days * n_syms
    syms = np.tile([f"SH60000{i}" for i in range(n_syms)], n_days)
    return pd.DataFrame(
        {
            "symbol": pd.Series(syms, dtype=object),
            "trade_date": pd.Series(np.repeat(days.to_numpy(), n_syms)),
            "f1": np.arange(n, dtype=np.float32),
            "f2": np.arange(n, dtype=np.float32) * 0.5,
            "label": np.linspace(-1.0, 1.0, n, dtype=np.float32),
        }
    )


def _explicit_cfg(df: pd.DataFrame, train_days, valid_days, test_days) -> dict:
    def rng(sel):
        return [str(sel[0].date()), str(sel[-1].date())]

    days = pd.DatetimeIndex(sorted(df["trade_date"].unique()))
    return {
        "model": {"val_ratio": 0.15},
        "label": {"target_horizon_days": _HORIZON},
        "split": {
            "train": rng(days[train_days]),
            "valid": rng(days[valid_days]),
            "test": rng(days[test_days]),
        },
    }


def _spy_row_takes(monkeypatch) -> list[int]:
    """记录每一次「整段行选择」作用在多长的帧上。

    两种写法都算：`df[bool_series]`（老）与 `df.take(pos)`（新）——二者都是
    「一次整段分配」，任意一种多来一次就是多一份 ~9G 瞬时拷贝。
    同一次选择内部的嵌套调用只记一次（`df[bool]` 内部还会再调 `take`）；
    列选择（str / list 键）不计。
    """
    sizes: list[int] = []
    depth = 0
    orig_getitem = pd.DataFrame.__getitem__
    orig_take = pd.DataFrame.take

    @functools.wraps(orig_take)
    def take(self, indices, axis=0, **kwargs):
        nonlocal depth
        if depth == 0:
            sizes.append(len(self))
        return orig_take(self, indices, axis=axis, **kwargs)

    @functools.wraps(orig_getitem)
    def getitem(self, key):
        nonlocal depth
        if not isinstance(key, (pd.Series, np.ndarray)):
            return orig_getitem(self, key)
        if depth == 0:
            sizes.append(len(self))
        depth += 1
        try:
            return orig_getitem(self, key)
        finally:
            depth -= 1

    monkeypatch.setattr(pd.DataFrame, "take", take)
    monkeypatch.setattr(pd.DataFrame, "__getitem__", getitem)
    return sizes


# ── 语义：三段隔离 ──────────────────────────────────────────────────────────

def test_explicit_split_segments_are_disjoint_and_ordered() -> None:
    df = _make_frame()
    days = pd.DatetimeIndex(sorted(df["trade_date"].unique()))
    cfg = _explicit_cfg(df, range(0, 38), range(40, 50), range(51, 60))

    train_df, val_df, test_df = _split_data(df, cfg)

    assert train_df["trade_date"].max() < val_df["trade_date"].min()
    assert val_df["trade_date"].max() < test_df["trade_date"].min()
    assert train_df["trade_date"].min() >= days[0]
    assert test_df["trade_date"].max() <= days[-1]
    # 三段无重叠：按 (symbol, trade_date) 计数应等于行数之和
    keys = lambda f: set(zip(f["symbol"], f["trade_date"], strict=True))  # noqa: E731
    assert not (keys(train_df) & keys(val_df))
    assert not (keys(val_df) & keys(test_df))
    assert not (keys(train_df) & keys(test_df))


def test_embargo_trims_segment_tail_by_horizon_plus_lag() -> None:
    df = _make_frame()
    days = pd.DatetimeIndex(sorted(df["trade_date"].unique()))
    cfg = _explicit_cfg(df, range(0, 38), range(40, 50), range(51, 60))

    train_df, val_df, test_df = _split_data(df, cfg)

    train_days = pd.DatetimeIndex(sorted(train_df["trade_date"].unique()))
    val_days = pd.DatetimeIndex(sorted(val_df["trade_date"].unique()))
    test_days = pd.DatetimeIndex(sorted(test_df["trade_date"].unique()))
    # train 原为 days[0..37]，裁掉尾部 6 个交易日 → days[0..31]
    assert train_days.equals(days[0 : 38 - _EMBARGO_DAYS])
    # val 原为 days[40..49]，裁掉尾部 6 个 → days[40..43]
    assert val_days.equals(days[40 : 50 - _EMBARGO_DAYS])
    # test 不裁（其标签已越出数据窗口，无下游可泄漏对象）
    assert test_days.equals(days[51:60])
    assert len(train_df) == (38 - _EMBARGO_DAYS) * 3


def test_embargo_cutoff_uses_segment_own_calendar() -> None:
    """裁剪阈值取**该段自身**的交易日序列，而非全局日历。

    若误用全局日历尾部，val 段（在日历中靠后）会被整段裁空。
    """
    df = _make_frame()
    days = pd.DatetimeIndex(sorted(df["trade_date"].unique()))
    cfg = _explicit_cfg(df, range(0, 12), range(20, 40), range(45, 60))

    train_df, val_df, _ = _split_data(df, cfg)

    assert pd.DatetimeIndex(sorted(train_df["trade_date"].unique())).equals(days[0 : 12 - _EMBARGO_DAYS])
    assert pd.DatetimeIndex(sorted(val_df["trade_date"].unique())).equals(days[20 : 40 - _EMBARGO_DAYS])


def test_embargo_skipped_when_segment_too_short(caplog) -> None:
    df = _make_frame()
    cfg = _explicit_cfg(df, range(0, 30), range(30, 34), range(40, 60))  # val 仅 4 天 < 6

    with caplog.at_level(logging.WARNING, logger="quantmind.train"):
        _, val_df, _ = _split_data(df, cfg)

    assert len(val_df) == 4 * 3  # 未裁剪
    assert any("Embargo skipped" in r.message for r in caplog.records)


def test_val_ratio_mode_makes_three_way_split_with_embargo() -> None:
    df = _make_frame(n_days=60)
    cfg = {"model": {"val_ratio": 0.2}, "label": {"target_horizon_days": _HORIZON}}

    train_df, val_df, test_df = _split_data(df, cfg)

    days = pd.DatetimeIndex(sorted(df["trade_date"].unique()))
    # 60 天：val_start_idx=48，test_start_idx=54
    assert pd.DatetimeIndex(sorted(val_df["trade_date"].unique()))[0] == days[48]
    assert pd.DatetimeIndex(sorted(test_df["trade_date"].unique()))[0] == days[54]
    # train 裁尾 6 天
    assert pd.DatetimeIndex(sorted(train_df["trade_date"].unique())).equals(days[0 : 48 - _EMBARGO_DAYS])
    assert train_df["trade_date"].max() < val_df["trade_date"].min() < test_df["trade_date"].max()


# ── 语义：泄漏防护与错误路径 ────────────────────────────────────────────────

def test_rejects_train_overlapping_valid() -> None:
    df = _make_frame()
    cfg = _explicit_cfg(df, range(0, 40), range(38, 50), range(51, 60))
    with pytest.raises(RuntimeError, match="leak"):
        _split_data(df, cfg)


def test_rejects_missing_test_section() -> None:
    df = _make_frame()
    cfg = _explicit_cfg(df, range(0, 38), range(40, 50), range(51, 60))
    cfg["split"].pop("test")

    with pytest.raises(RuntimeError, match="split.test is required"):
        _split_data(df, cfg)


def test_rejects_test_overlapping_valid() -> None:
    df = _make_frame()
    cfg = _explicit_cfg(df, range(0, 38), range(40, 50), range(48, 60))
    with pytest.raises(RuntimeError, match="must be strictly after"):
        _split_data(df, cfg)


def test_empty_segment_raises_with_available_range() -> None:
    df = _make_frame(n_days=20)
    days = pd.DatetimeIndex(sorted(df["trade_date"].unique()))
    cfg = {
        "model": {"val_ratio": 0.15},
        "label": {"target_horizon_days": _HORIZON},
        "split": {
            "train": [str(days[0].date()), str(days[9].date())],
            "valid": [str(days[10].date()), str(days[14].date())],
            "test": ["2021-01-01", "2021-06-30"],  # 越出可用窗口 → 空段
        },
    }
    with pytest.raises(RuntimeError, match="EMPTY|empty segment"):
        _split_data(df, cfg)


# ── 内存契约 ────────────────────────────────────────────────────────────────

def test_each_segment_is_row_selected_exactly_once_from_source(monkeypatch) -> None:
    """**内存回归契约**：切分只允许对源帧做「每段一次」的行选择。

    多一次整段行选择 = 多一份 ~9G 瞬时拷贝（10.72M 行 × 291 列）。历史上
    embargo 是在已切出的 train/val 上再切一刀，等于对 train 又付两份拷贝；
    2026-09-23 的 283 特征训练就死在这段（切分段 HWM 47.4G）。
    """
    df = _make_frame()
    cfg = _explicit_cfg(df, range(0, 38), range(40, 50), range(51, 60))
    sizes = _spy_row_takes(monkeypatch)

    _split_data(df, cfg)

    assert sizes == [len(df)] * 3, (
        f"期望「每段只从源帧切一次」＝ 3 次等长行选择，实测 {len(sizes)} 次：{sizes}。"
        "对已切出的段再做行选择会翻倍瞬时内存。"
    )


def test_val_ratio_mode_also_row_selects_once_per_segment(monkeypatch) -> None:
    df = _make_frame()
    cfg = {"model": {"val_ratio": 0.2}, "label": {"target_horizon_days": _HORIZON}}
    sizes = _spy_row_takes(monkeypatch)

    _split_data(df, cfg)

    assert sizes == [len(df)] * 3


def test_take_rows_matches_mask_selection_bit_for_bit() -> None:
    """`_take_rows` 必须与老写法 `df[mask].reset_index(drop=True)` **逐位**一致。

    换写法只为省一份整段拷贝（实测 2.01 份段 → 1.01 份段），取值语义一字不改：
    值 / dtype / 列序 / 索引全等，索引归零，`_is_copy` 清空，且与源帧脱钩。
    这里直接拿老写法当判据。
    """
    df = _make_frame()
    mask = (df["trade_date"] >= pd.Timestamp("2020-01-15")) & \
           (df["trade_date"] <= pd.Timestamp("2020-02-15"))
    expected = df[mask].reset_index(drop=True)

    got = _take_rows(df, mask, "probe")

    pd.testing.assert_frame_equal(got, expected, check_exact=True)
    assert isinstance(got.index, pd.RangeIndex)
    assert list(got.index) == list(range(len(got)))
    assert getattr(got, "_is_copy", None) is None

    before = df.copy(deep=True)
    got["f1"] = np.float32(0.0)  # 原地写：不得回灌源帧
    pd.testing.assert_frame_equal(df, before, check_exact=True)


def test_take_rows_ignores_source_frame_index_offset() -> None:
    """`take` 取的是**位置**，源帧索引非 RangeIndex 时也不能错位。"""
    df = _make_frame()
    shuffled = df.iloc[::-1]  # 索引倒序，位置与标签不再一致
    mask = (shuffled["trade_date"] >= pd.Timestamp("2020-01-15")) & \
           (shuffled["trade_date"] <= pd.Timestamp("2020-02-15"))

    got = _take_rows(shuffled, mask, "probe")

    pd.testing.assert_frame_equal(
        got, shuffled[mask].reset_index(drop=True), check_exact=True
    )


def test_split_does_not_pay_a_second_full_copy(monkeypatch) -> None:
    """**内存回归契约（二）**：切分不得再走 `.reset_index(drop=True)`。

    它读着像「归零索引」，实际在 pandas 2.3（无 CoW）下是一次整段深拷
    （`reset_index` 内部的 `self.copy(deep=None)`），且与 `df[mask]` 的结果
    **同时驻留**——10.72M 行 × 287 列时白烧 9.65G 瞬时内存。索引归零改由
    `_take_rows` 就地赋值完成（零拷贝）。
    """
    df = _make_frame()
    cfg = _explicit_cfg(df, range(0, 38), range(40, 50), range(51, 60))
    calls: list[int] = []
    orig = pd.DataFrame.reset_index

    @functools.wraps(orig)
    def spy(self, *args, **kwargs):
        calls.append(len(self))
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "reset_index", spy)
    _split_data(df, cfg)

    assert calls == [], (
        f"切分里出现了 {len(calls)} 次 reset_index（作用在 {calls} 行的帧上）。"
        "在 pandas 2.3 无 CoW 下它是一次整段深拷，与选帧结果同时驻留；"
        "索引归零请用 `_take_rows` 的就地赋 index。"
    )


def test_returned_segments_are_detached_from_source_frame() -> None:
    """返回段必须与源帧脱钩且不带 `_is_copy`：下游 `_prepare_arrays` 是**原地**写
    （截面前处理），写到切片上会静默不落盘或触发 chained-assignment 警告。"""
    df = _make_frame()
    cfg = _explicit_cfg(df, range(0, 38), range(40, 50), range(51, 60))
    df_before = df.copy(deep=True)

    train_df, val_df, test_df = _split_data(df, cfg)

    for seg in (train_df, val_df, test_df):
        # pandas 3.0（CoW 强制）已移除该标志；2.3.x（训练镜像）在掩码选帧后会置上，
        # 置上则下游原地写会走 chained-assignment 告警路径。
        assert getattr(seg, "_is_copy", None) is None
        assert isinstance(seg.index, pd.RangeIndex)
        assert list(seg.index) == list(range(len(seg)))
    pd.testing.assert_frame_equal(df, df_before, check_exact=True)

    train_df["f1"] = np.float32(0.0)  # 原地写：不得回灌源帧
    pd.testing.assert_frame_equal(df, df_before, check_exact=True)
