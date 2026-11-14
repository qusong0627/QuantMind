"""行情 canonical 去重（唯一 (symbol, time) 口径）。

背景：``daily_forward`` 在 2024-09-02~2026-05-08 同时存在 ``paid_hk``（付费
历史，原始不复权）与 ``akshare``（补充/新数据）两批 release。直接
``read_parquet`` 会把同一股票日读成两行；按成交量/成交额求和会翻倍，按
``keep="last"`` 又会随机选中 akshare 的 1/100 价格（0755.HK）或 0 价格
（4338.HK）。本模块把「一个股票日一行」的裁决规则收敛到唯一实现。

优先级：``paid_hk`` > ``akshare`` > 未知 release；同一 release 内按
``published_at`` 最新优先。选择 paid 优先是**最小破坏**：付费历史是既有
研究序列，akshare 只负责补 paid 未覆盖的日期/标的。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

#: release 优先级（数字越小越优先）。新增数据源必须显式登记，未知源排最后。
RELEASE_PRIORITY: dict[str, int] = {
    "paid_hk": 0,
    "akshare": 1,
}

#: 未登记 release 的默认优先级（最低）。
DEFAULT_PRIORITY = 100

DEFAULT_KEYS: tuple[str, ...] = ("symbol", "time")


def canonicalize(
    frame: pd.DataFrame,
    *,
    keys: Sequence[str] = DEFAULT_KEYS,
    release_col: str = "release_id",
    published_col: str = "published_at",
    priority: Mapping[str, int] | None = None,
) -> pd.DataFrame:
    """返回每个 ``keys`` 组合只保留一行的副本。

    - 先按 release 优先级、再按 ``published_at`` 倒序排序，``keep="first"``；
    - 缺少 ``release_col`` / ``published_col`` 时对应维度视为最低/空，不丢行；
    - 空表原样返回副本；输出索引重置，保证幂等。
    """
    if frame.empty:
        return frame.copy()

    prio_map = dict(RELEASE_PRIORITY if priority is None else priority)
    out = frame.copy()

    if release_col in out.columns:
        out["_qm_release_prio"] = (
            out[release_col].map(prio_map).fillna(DEFAULT_PRIORITY).astype("int64")
        )
    else:
        out["_qm_release_prio"] = DEFAULT_PRIORITY

    if published_col in out.columns:
        # 数据内 published_at 是 ISO 字符串；按字符串倒序即时间倒序。
        # 统一转 string 以兼容 None/NaN/时区混用，避免 to_datetime 整列失败。
        out["_qm_published"] = out[published_col].astype("string").fillna("")
    else:
        out["_qm_published"] = ""

    out = out.sort_values(
        ["_qm_release_prio", "_qm_published"],
        ascending=[True, False],
        kind="mergesort",
        na_position="last",
    )
    out = out.drop_duplicates(subset=list(keys), keep="first")
    out = out.drop(columns=["_qm_release_prio", "_qm_published"])
    return out.reset_index(drop=True)


def release_priority(release_id: Any) -> int:
    """单值查询 release 优先级（供脚本统计/断言用）。"""
    if pd.isna(release_id):
        return DEFAULT_PRIORITY
    return RELEASE_PRIORITY.get(str(release_id), DEFAULT_PRIORITY)
