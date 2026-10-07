"""Alpha Agent 因子回测链路单测：日度 IC 指标口径（含 Rank ICIR）。

alpha_agent 路由依赖 fastapi/DB，本地轻量环境 import 失败时整体跳过
（与 test_alpha_agent_quality_gate.py 同策略）；在 OSS 容器内
运行 `python -m pytest backend/tests/` 时全量生效。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # pragma: no cover - 环境相关
    from backend.services.engine.routers import alpha_agent as aa
except Exception as _exc:  # noqa: BLE001
    aa = None
    _IMPORT_ERR = _exc


pytestmark = pytest.mark.skipif(
    aa is None, reason="alpha_agent 依赖不可用（需容器环境）"
)


def test_vectorized_ic_returns_rank_icir() -> None:
    """回测要落库 ICIR / Rank ICIR（口径与挖掘阶段一致：同除日度 IC 标准差）。"""
    rng = np.random.default_rng(0)
    n_stocks, n_days = 40, 60
    index = pd.MultiIndex.from_product(
        [range(n_stocks), pd.date_range("2024-01-01", periods=n_days)],
        names=["instrument", "datetime"],
    )
    f = pd.Series(rng.normal(size=len(index)), index=index)
    r = pd.Series(rng.normal(size=len(index)), index=index)

    ic_mean, rank_ic_median, icir, rank_icir, n_obs = aa._vectorized_daily_spearman_ic(  # noqa: SLF001
        f, r
    )

    assert n_obs > 0
    assert np.isfinite(icir) and np.isfinite(rank_icir)
    # icir 与 rank_icir 同分母（日度 IC 标准差），符号分别与 ic_mean / rank_ic_median 一致
    assert (icir > 0) == (ic_mean > 0)
    assert (rank_icir > 0) == (rank_ic_median > 0)
