"""Neutralizer 元数据缺失时必须硬失败，不得静默返回未中性化/空特征。

背景：official_factors.duckdb 在本仓库并不存在，refresh_metadata 只告警返回；
旧行为下 neutralize() 会把所有行按缺失控制变量 dropna 掉，返回空特征，
调用方（service.py）拿到空预测却报 status=success —— 与 signal_side 全 HOLD
事故同类的静默降级。
"""

import pandas as pd
import pytest

from backend.services.engine.inference.neutralizer import Neutralizer

pytestmark = pytest.mark.unit

FEATURES = ["f1", "f2"]


def _df(n: int = 12) -> pd.DataFrame:
    idx = [f"{i:06d}" for i in range(n)]
    return pd.DataFrame({"f1": range(n), "f2": range(n)}, index=idx)


def test_neutralize_raises_when_metadata_never_loaded(tmp_path):
    nz = Neutralizer(db_path=str(tmp_path / "missing.duckdb"))
    nz.refresh_metadata("2026-09-08")  # 文件不存在 → 仅告警，不加载
    with pytest.raises(RuntimeError, match="中性化元数据不可用"):
        nz.neutralize(_df(), FEATURES)


def test_neutralize_empty_df_returns_empty_without_raising(tmp_path):
    nz = Neutralizer(db_path=str(tmp_path / "missing.duckdb"))
    out = nz.neutralize(pd.DataFrame(), FEATURES)
    assert out.empty


def test_neutralize_returns_residuals_after_metadata_loaded(tmp_path):
    nz = Neutralizer(db_path=str(tmp_path / "missing.duckdb"))
    idx = list(_df().index)
    nz._industry_cache = {s: ("银行" if i % 2 else "电子") for i, s in enumerate(idx)}
    nz._mkt_cap_cache = {s: float(i) for i, s in enumerate(idx)}
    nz._last_update_date = "2026-09-08"

    out = nz.neutralize(_df(), FEATURES)

    assert len(out) == len(idx)
    assert "industry" not in out.columns
    assert "log_cap" not in out.columns
    # f1 与 log_cap 完全共线 → 中性化后应被完全解释掉（残差近似 0）
    assert out["f1"].abs().max() < 1e-8
