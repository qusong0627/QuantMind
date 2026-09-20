"""因子 PFS 侧车测试（`scripts/eval/factor_pfs.py`）。

PFS（扰动保真度）是训练侧唯一实现 `docker/training/data/factor_quality.py` 的产物，
本模块只负责**取数抽样、缓存与评分接线**，绝不复写公式。测试盯三件事：
- 抽样规则必须与训练侧 `_sample_positions` 逐位一致（否则同一因子两处 PFS 不同）；
- 读盘只读抽样日、只读需要的列（2000+ 分区的数据集不能整读）；
- 质量闸门维的缺省与红线（PFS 不达标）如实标注。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.scripts.eval.factor_pfs import (
    PFS_CACHE_NAME,
    PFS_FORMULA_VERSION,
    load_factor_panel,
    pfs_cache_signature,
    quality_gate_dim,
    read_pfs_cache,
    sample_day_dirs,
    write_pfs_cache,
)


def _make_dataset(root: Path, days: list[str], factors: list[str]) -> Path:
    """按 hive 布局写一份最小因子面板：``dt=YYYYMMDD/data.parquet``。"""
    for i, day in enumerate(days):
        part = root / f"dt={day}"
        part.mkdir(parents=True, exist_ok=True)
        n = 40
        frame = pd.DataFrame(
            {
                "symbol": [f"S{j:03d}.SZ" for j in range(n)],
                "time": pd.to_datetime([day] * n),
                **{f: np.linspace(0.0, 1.0, n) + i for f in factors},
            }
        )
        frame.to_parquet(part / "data.parquet", index=False)
    return root


# ── 抽样口径（必须与训练侧同一实现） ────────────────────────────────────


@pytest.mark.unit
def test_sample_day_dirs_is_equidistant_and_keeps_endpoints():
    days = [f"2025{i:04d}" for i in range(300)]

    picked = sample_day_dirs(days, max_days=120)

    positions = [days.index(d) for d in picked]
    assert len(picked) == 120
    assert positions[0] == 0 and positions[-1] == len(days) - 1
    assert positions == sorted(positions)  # 保持出现顺序
    gaps = {b - a for a, b in zip(positions, positions[1:], strict=False)}
    assert max(gaps) - min(gaps) <= 1  # 等距（取整只会差 1）


@pytest.mark.unit
def test_sample_day_dirs_returns_all_when_under_limit():
    days = [f"2025{i:04d}" for i in range(30)]

    assert sample_day_dirs(days, max_days=120) == days


@pytest.mark.unit
def test_sample_day_dirs_matches_training_side_rule():
    """抽样位置必须与训练侧 `_sample_positions` 逐位一致（口径唯一）。"""
    from backend.shared.factor_quality import load_factor_quality

    mod = load_factor_quality()
    if mod is None:
        pytest.skip("docker/training 未挂载：训练侧实现不可用")

    days = [f"2025{i:04d}" for i in range(437)]

    picked = sample_day_dirs(days, max_days=120)
    keep = mod._sample_positions(len(days), 120)

    assert [days.index(d) for d in picked] == sorted(keep)


# ── 取数（只读抽样日 + 列投影） ────────────────────────────────────────


@pytest.mark.unit
def test_load_factor_panel_reads_only_sampled_days_and_requested_columns(
    tmp_path: Path,
):
    days = [f"2025010{i}" for i in range(1, 6)]
    _make_dataset(tmp_path, days, ["f_a", "f_b", "f_unused"])

    panel = load_factor_panel(tmp_path, ["f_a"], days=days[:2])

    assert list(panel.columns) == ["trade_date", "f_a"]  # f_unused/f_b 不读
    assert panel["trade_date"].nunique() == 2
    assert sorted(panel["trade_date"].unique()) == days[:2]


@pytest.mark.unit
def test_load_factor_panel_marks_missing_factor_as_absent_column(tmp_path: Path):
    """面板里没有的因子：不造列、不填 0——如实报告缺失。"""
    days = ["20250101"]
    _make_dataset(tmp_path, days, ["f_a"])

    panel = load_factor_panel(tmp_path, ["f_a", "f_ghost"], days=days)

    assert list(panel.columns) == ["trade_date", "f_a"]
    assert "f_ghost" not in panel.columns


@pytest.mark.unit
def test_load_factor_panel_raises_when_no_partitions(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="分区"):
        load_factor_panel(tmp_path, ["f_a"], days=["20250101"])


# ── 侧车缓存 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_pfs_cache_round_trip_and_signature_change_invalidates(tmp_path: Path):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    values = {"f_a": {"pfs": 0.83, "pfs_gauss": 0.9, "pfs_t": 0.83, "n_days": 120}}
    sig = pfs_cache_signature(dataset="alpha_library", days=["20250101", "20250102"])

    write_pfs_cache(report_dir, values, signature=sig)

    hit = read_pfs_cache(report_dir, signature=sig)
    assert hit == values
    # 面板变了（新增交易日）→ 签名变 → 缓存失效，必须重算
    assert (
        read_pfs_cache(
            report_dir,
            signature=pfs_cache_signature(
                dataset="alpha_library", days=["20250101", "20250102", "20250103"]
            ),
        )
        is None
    )
    assert (report_dir / PFS_CACHE_NAME).is_file()


@pytest.mark.unit
def test_read_pfs_cache_ignores_other_formula_version(tmp_path: Path):
    """公式版本不同 → 旧缓存一律作废（否则口径改了还吃旧数）。"""
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    sig = pfs_cache_signature(dataset="d", days=["20250101"])
    (report_dir / PFS_CACHE_NAME).write_text(
        json.dumps(
            {
                "version": PFS_FORMULA_VERSION + 1,
                "signature": sig,
                "pfs": {"f_a": {"pfs": 0.5}},
            }
        ),
        encoding="utf-8",
    )

    assert read_pfs_cache(report_dir, signature=sig) is None


@pytest.mark.unit
def test_read_pfs_cache_returns_none_on_corrupt_file(tmp_path: Path):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    (report_dir / PFS_CACHE_NAME).write_text("{ not json", encoding="utf-8")

    assert read_pfs_cache(report_dir, signature="x") is None


# ── 质量闸门维 ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_quality_gate_dim_scores_pfs_and_flags_red_line():
    """PFS 低于红线阈值 → 维度分低且 red_line_failed（设计 §2.1「PFS 不达标」）。"""
    weak = quality_gate_dim({"pfs": 0.30, "pfs_gauss": 0.31, "pfs_t": 0.30, "n_days": 120})
    strong = quality_gate_dim(
        {"pfs": 0.95, "pfs_gauss": 0.96, "pfs_t": 0.95, "n_days": 120}
    )

    assert weak.red_line_failed is True
    assert weak.score is not None and weak.score < 40.0
    assert "PFS" in str(weak.detail["red_line"])
    assert strong.red_line_failed is False
    assert strong.score is not None and strong.score >= 90.0
    assert strong.detail["pfs"]["pfs"] == 0.95


@pytest.mark.unit
def test_quality_gate_dim_is_insufficient_with_actionable_note():
    """PFS 缺失 → 如实缺省，note 说明「为什么没有」（不是拼一个 0 分）。"""
    dim = quality_gate_dim(None, note="面板日采样不足（0 个有效分区）")

    assert dim.score is None
    assert dim.detail["insufficient"] is True
    assert dim.detail["note"] == "面板日采样不足（0 个有效分区）"


@pytest.mark.unit
def test_quality_gate_dim_insufficient_when_pfs_value_is_none():
    """因子在面板里但有效日不足 → compute_pfs 给 None，照样缺省。"""
    dim = quality_gate_dim({"pfs": None, "pfs_gauss": None, "pfs_t": None, "n_days": 5})

    assert dim.score is None
    assert dim.detail["insufficient"] is True
    assert "5 个有效交易日" in str(dim.detail["note"])


@pytest.mark.unit
def test_quality_gate_dim_uses_dh_when_pfs_absent():
    """只有 DH（多样性增益）也可评分，但 detail 里要写清用的是哪一项。"""
    dim = quality_gate_dim({"dh": 0.06})

    assert dim.score is not None
    assert dim.detail["pfs"] is None
    assert dim.detail["dh"] == 0.06
