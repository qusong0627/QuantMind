"""多算法子模型元数据修复的口径回归测试。

背景（两处静默失效，都是在真实产物上炸过才发现的）：

1. **「GRU 校验 GRU」恒过**：13 个子模型的磁盘 metadata.json 全部写着父模型的
   `model_type='gru'`。若检测时以磁盘自身的 `model_type` 当算法身份，就是在用
   GRU 的期望值校验一份 GRU 元数据 → 恒过、永不上报。必须以 **DB 身份**为准
   （`detect_algo_issues(..., model_type=...)`）。

2. **脏字段被搬回**：磁盘副本比 DB 旧，合并时若把「new_meta 里没有的键」一律当
   「磁盘独有」保留，则修复刚删掉的 `model_class_name='GRU'` / `is_sequence_model`
   又被原样搬回 → mlp 仍带着 GRU 身份、加载走进 Qlib DL 分支报
   `Unknown Qlib model class`。算法私有字段在「算法身份修复」场景下只能认 DB 侧结果
   （`merge_disk_meta` 的 `algo_repair` 分支）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_repair_module():
    path = REPO_ROOT / "backend" / "scripts" / "repair_multi_model_metadata.py"
    spec = importlib.util.spec_from_file_location("repair_multi_model_metadata", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def repair():
    return _load_repair_module()


# ── 1. 磁盘副本必须以 DB 身份校验（否则 GRU 校验 GRU 恒过）─────────────

_GRU_CONTAMINATED_DISK = {
    "model_type": "gru",
    "framework": "pytorch",
    "model_class_name": "GRU",
    "is_sequence_model": True,
    "model_params": {"d_feat": 48, "hidden_size": 64},
    "input_spec": {"features": ["f1"], "window": 60},
}


def test_disk_meta_self_check_is_vacuous(repair):
    """不加 DB 身份时磁盘自查恒过 —— 这正是首版漏报的原因，先钉住这个前提。"""
    assert repair.detect_algo_issues(_GRU_CONTAMINATED_DISK) == []


def test_disk_meta_checked_against_db_identity_reports(repair):
    issues = repair.detect_algo_issues(_GRU_CONTAMINATED_DISK, model_type="mlp")

    assert issues, "以 DB 身份（mlp）校验被污染的磁盘副本必须报出问题"
    assert any("model_class_name" in i for i in issues)


def test_healthy_disk_meta_passes_db_identity_check(repair):
    healthy = {"model_type": "mlp", "framework": "pytorch"}
    assert repair.detect_algo_issues(healthy, model_type="mlp") == []


# ── 2. 大小写归一（NativeTFT → nativetft）──────────────────────────────


@pytest.mark.parametrize(
    "meta,expected",
    [
        ({"model_type": "NativeTFT"}, "nativetft"),
        ({"model_type": "nativetft"}, None),
        ({"model_type": ""}, None),
        ({}, None),
    ],
)
def test_detect_case_issue(repair, meta, expected):
    assert repair.detect_case_issue(meta) == expected


# ── 3. 磁盘合并口径（脏字段不得搬回）─────────────────────────────────────


def test_algo_repair_does_not_resurrect_deleted_algo_fields(repair):
    """回归：修复中删掉的算法私有字段，不能因为「磁盘里有、new_meta 里没有」搬回来。"""
    new_meta = {"model_type": "mlp", "framework": "pytorch"}  # 已剔除 GRU 身份
    disk_meta = dict(_GRU_CONTAMINATED_DISK)

    merged = repair.merge_disk_meta(new_meta, disk_meta, algo_repair=True)

    assert "model_class_name" not in merged
    assert "is_sequence_model" not in merged
    assert "model_params" not in merged
    assert "input_spec" not in merged
    assert merged["model_type"] == "mlp"


def test_algo_repair_keeps_non_algo_disk_only_keys(repair):
    """非算法字段 DB 从不落库（is_ensemble / pool_* / factor_selection …），一律保留。"""
    new_meta = {"model_type": "mlp", "framework": "pytorch"}
    disk_meta = {
        "model_class_name": "GRU",          # 算法字段：丢弃
        "is_ensemble": True,                 # 非算法字段：保留
        "pool_filter": {"market_cap_min": 1e9},
    }

    merged = repair.merge_disk_meta(new_meta, disk_meta, algo_repair=True)

    assert "model_class_name" not in merged
    assert merged["is_ensemble"] is True
    assert merged["pool_filter"] == {"market_cap_min": 1e9}


def test_non_algo_repair_keeps_disk_only_keys(repair):
    """纯大小写归一（algo_repair=False）不得顺手删磁盘上的算法字段：
    standalone 模型的算法字段本就由训练端正确写出，只是没进 DB 快照。"""
    new_meta = {"model_type": "nativetft", "framework": "pytorch"}
    disk_meta = {"model_arch": {"hidden": 64}, "feat_norm": {"mean": 0.0}}

    merged = repair.merge_disk_meta(new_meta, disk_meta, algo_repair=False)

    assert merged["model_arch"] == {"hidden": 64}
    assert merged["feat_norm"] == {"mean": 0.0}
    assert merged["model_type"] == "nativetft"


def test_new_meta_wins_on_conflict(repair):
    new_meta = {"model_type": "mlp", "framework": "pytorch", "model_file": "model.pkl"}
    disk_meta = {"model_file": "model_gru.pth", "extra_disk_key": 1}

    merged = repair.merge_disk_meta(new_meta, disk_meta, algo_repair=True)

    assert merged["model_file"] == "model.pkl"
    assert merged["extra_disk_key"] == 1
