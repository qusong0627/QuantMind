"""复权因子步长修复：把 inf/异常 step 修成 1.0，按 canonical 行情校验真实拆并股。

问题形态（2026-11-14 实测）：因子文件里 83.7 万行 `adj_factor=inf`、817,621 行
`daily_backward.close=inf`。根因是 Yahoo/付费源在某天给出 inf 或与 canonical
行情不一致的巨大 step（0501.HK 2026-05-08 step=3400，但当天 canonical 收盘
89.8→87.6，只是 -2.4%），cumprod 后整段被污染。

修复纪律：
- step 非有限/≤0 → 1.0；
- step 极端（>10 或 <0.1）时必须与 canonical 前收比一致（比值 0.2~5 倍内），
  否则视为源噪声 → 1.0；
- 真实拆并股（如 0755.HK 2020-04-21 step=100 且 canonical 也是 100 倍）保留；
- 以原因子首个有限值为基准重算 cumprod，保证量级不漂移。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

EXTREME_HIGH = 10.0
EXTREME_LOW = 0.1
CONSISTENCY_LOW = 0.2
CONSISTENCY_HIGH = 5.0


def repair_factor_frame(
    frame: pd.DataFrame,
    forward_close: pd.Series,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """返回修复后的因子表与计数。

    Args:
        frame: 至少含 ``time`` / ``adj_step`` / ``adj_factor``。
        forward_close: index=交易日（可转 Timestamp），value=canonical 不复权收盘。
    """
    if frame.empty:
        return frame.copy(), {"rows": 0, "repaired": 0, "inf_before": 0, "inf_after": 0}

    out = frame.copy()
    out["time"] = pd.to_datetime(out["time"], errors="coerce")
    out = out.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    if out.empty:
        return out, {"rows": 0, "repaired": 0, "inf_before": 0, "inf_after": 0}

    step = pd.to_numeric(out["adj_step"], errors="coerce")
    old_factor = pd.to_numeric(out["adj_factor"], errors="coerce")
    inf_before = int((~np.isfinite(old_factor)).sum())

    # canonical 前收比（按自然交易日，而不是因子文件的行间隔）
    fwd = pd.to_numeric(forward_close, errors="coerce").copy()
    fwd.index = pd.to_datetime(fwd.index, errors="coerce")
    fwd = fwd[~fwd.index.isna()].sort_index()
    prev = fwd.shift(1)
    raw_ratio = fwd / prev

    ratio_at = raw_ratio.reindex(out["time"]).to_numpy(dtype="float64")
    step_arr = step.to_numpy(dtype="float64")
    finite_step = np.isfinite(step_arr) & (step_arr > 0)
    finite_ratio = np.isfinite(ratio_at) & (ratio_at > 0)

    extreme = (step_arr > EXTREME_HIGH) | (step_arr < EXTREME_LOW)
    safe_step = np.where(finite_step, step_arr, 1.0)
    consistency = ratio_at / safe_step
    consistent = finite_ratio & (consistency >= CONSISTENCY_LOW) & (
        consistency <= CONSISTENCY_HIGH
    )

    repaired = (~finite_step) | (extreme & ~consistent)
    new_step = step.where(~repaired, 1.0).astype("float64")
    # 首行没有前收可比：step 保留（若极端则已在上面判为不一致）
    repaired_count = int(repaired.sum())

    base_candidates = old_factor[
        np.isfinite(old_factor) & (old_factor > 0) & (~repaired)
    ]
    base = float(base_candidates.iloc[0]) if len(base_candidates) else 1.0
    factor = base * new_step.cumprod()
    first = float(factor.iloc[0]) if len(factor) else 1.0
    if np.isfinite(first) and first > 0:
        factor = factor * (base / first)
    else:
        factor = pd.Series(1.0, index=new_step.index)

    out["adj_step"] = new_step
    out["adj_factor"] = factor.astype("float64")
    inf_after = int((~np.isfinite(out["adj_factor"])).sum())
    return out, {
        "rows": int(len(out)),
        "repaired": repaired_count,
        "inf_before": inf_before,
        "inf_after": inf_after,
    }
