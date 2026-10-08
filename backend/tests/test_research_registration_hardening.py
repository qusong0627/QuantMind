"""注册链路的安全加固（2026-10-07 安全评审后的修复，纯函数，无 IO）。

三条都是「不报错、只悄悄做错事」的类型：

1. **路径穿越**。``dataset`` 会经 ``store.artifact_dir`` 拼进文件系统路径，原先
   的实现是 ``DATASET_DIRS.get(dataset, dataset)`` —— 未命中的取值被原样当路径
   组件，而 ``Path`` 保留 ``..``（由内核在 ``open()`` 时解析）。实测
   ``artifact_dir("../../../../tmp")`` → ``<quantdb>/../../../../tmp``。同一个洞
   在 ``POST /build`` 上还会 ``mkdir(parents=True)`` 出库外目录。现在两侧都收死：
   ``artifact_dir`` fail-closed + FastAPI 层 ``Literal`` 值域。

2. **请求/响应放大**。``codes`` 只限了**列表长度**（500），元素不限长，而每个未
   命中的 code 会被逐字回显在 ``skipped`` 里 —— ``["A"*5_000_000] * 500`` 就是
   一次请求打爆内存。

3. **半匹配别名**。映射表上有两条唯一约束（``feature_key`` / ``source_column``），
   而 ``PUT /mappings`` 允许二者不等。只命中其中一条时 ``ON CONFLICT`` 的目标不
   命中，INSERT 撞另一条约束抛 IntegrityError → 整个批次 500 回滚。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.services.api.routers.admin.quantdb_factor_catalog import (
    RegisterFromResearchRequest,
)
from backend.services.api.routers.admin.research_factor_registration import (
    RegistrationCandidate,
    _alias_conflict,
    resolve_registrable,
)
from backend.services.engine.factor_research import store

# 任何能穿出数据根的取值都应被拒。`..` 单独一条也要拒（它等价于数据根父目录）。
_TRAVERSAL_DATASETS = [
    "..",
    "../../../../etc",
    "../factor_research",
    "/etc",
    "factor_research/../..",
]

_GOOD_COLUMN = "mom_ret_5d"


# ---------------------------------------------------------------------------
# 1. artifact_dir：fail-closed，不兜底
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", _TRAVERSAL_DATASETS)
def test_artifact_dir_rejects_unknown_dataset(bad):
    """未知 dataset 必须抛错，绝不能回落成「原样当路径组件」。"""
    with pytest.raises(ValueError, match="未知因子数据集"):
        store.artifact_dir(bad)


def test_artifact_dir_stays_inside_data_root_for_known_datasets():
    """合法取值仍解析到数据根之下（确认收口没有误伤正常路径）。"""
    root = store.artifact_dir("classic").parent

    for ds in ("classic", "private"):
        resolved = store.artifact_dir(ds).resolve()
        assert resolved.parent == root.resolve(), f"{ds} 解析到了数据根之外：{resolved}"


def test_factor_dataset_literal_matches_dataset_dirs():
    """``FactorDataset`` 与 ``DATASET_DIRS`` 同源——加数据集时忘改一处即失败。"""
    from typing import get_args

    assert set(get_args(store.FactorDataset)) == set(store.DATASET_DIRS)


# ---------------------------------------------------------------------------
# 2. 请求模型：值域与长度
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", _TRAVERSAL_DATASETS)
def test_request_model_rejects_traversal_dataset(bad):
    """FastAPI 层第一道闸：越界 dataset 直接 422，而不是 500 或穿越。"""
    with pytest.raises(ValidationError):
        RegisterFromResearchRequest(dataset=bad, codes=[_GOOD_COLUMN])


def test_request_model_accepts_known_datasets():
    for ds in ("classic", "private"):
        req = RegisterFromResearchRequest(dataset=ds, codes=[_GOOD_COLUMN])
        assert req.dataset == ds


def test_request_model_rejects_over_long_code_element():
    """列表长度合规不代表元素合规——元素也要限长。"""
    with pytest.raises(ValidationError):
        RegisterFromResearchRequest(dataset="private", codes=["A" * 5_000])


def test_request_model_rejects_too_many_codes():
    with pytest.raises(ValidationError):
        RegisterFromResearchRequest(
            dataset="private", codes=[f"c{i}" for i in range(501)]
        )


def test_request_model_rejects_empty_code_element():
    with pytest.raises(ValidationError):
        RegisterFromResearchRequest(dataset="private", codes=[""])


# ---------------------------------------------------------------------------
# 3. 解析层：列宽与标识符长度
# ---------------------------------------------------------------------------
def _factor(code: str, lib: str, **extra) -> dict:
    return {"code": code, "l2": lib, "display_name": code, **extra}


def test_display_name_is_clamped_to_column_width():
    """display_name 列宽 256；超宽会让 INSERT 抛 StringDataRightTruncation → 整批 500。"""
    factors = [_factor(_GOOD_COLUMN, "l1_factors", display_name="长" * 400)]

    candidates, skipped = resolve_registrable([_GOOD_COLUMN], factors)

    assert skipped == []
    assert len(candidates[0].display_name) == 256


def test_category_name_and_id_are_clamped():
    """分类字段同属展示列，同样按列宽截断（丢的只是文案）。"""
    factors = [_factor(_GOOD_COLUMN, "l1_factors")]

    candidates, _ = resolve_registrable([_GOOD_COLUMN], factors)

    assert len(candidates[0].category_id) <= 64
    assert len(candidates[0].category_name) <= 128


def test_over_long_code_is_skipped_not_truncated():
    """标识符超宽只能拒：截断会写出一个既不对应真实列、也不报错的特征名。"""
    long_code = "x" * 200

    candidates, skipped = resolve_registrable(
        [long_code], [_factor(long_code, "l1_factors")]
    )

    assert candidates == []
    assert len(skipped) == 1
    assert "超过 128 字符" in skipped[0].reason


# ---------------------------------------------------------------------------
# 4. 半匹配别名：从 500 变成可读的逐条跳过
# ---------------------------------------------------------------------------
def _cand(column: str, key: str | None = None) -> RegistrationCandidate:
    return RegistrationCandidate(
        source_dataset="l1_factors",
        source_column=column,
        feature_key=key or column,
        display_name=column,
        category_id="cat",
        category_name="分类",
        sort_order=0,
    )


def test_alias_conflict_none_when_draft_is_empty():
    assert _alias_conflict(_cand("c"), {}, {}) is None


def test_alias_conflict_none_on_full_match_so_reregistration_updates():
    """两条都命中 = 正常更新（重复注册必须保持幂等）。"""
    assert _alias_conflict(_cand("c"), {"c": "c"}, {"c": "c"}) is None


def test_alias_conflict_when_column_is_aliased_to_another_key():
    """该列已被人工改名 → 再注册会覆盖别名，跳过并说明。"""
    reason = _alias_conflict(_cand("c"), {"c": "别的名字"}, {"别的名字": "c"})

    assert reason is not None
    assert "已命名为 别的名字" in reason


def test_alias_conflict_when_feature_key_is_owned_by_another_column():
    """特征名被别的列占用 → INSERT 必撞 feature_key 唯一约束，跳过。"""
    reason = _alias_conflict(_cand("c"), {"other": "c"}, {"c": "other"})

    assert reason is not None
    assert "已被该草稿的 other 占用" in reason


def test_alias_conflict_covers_both_constraints():
    """两条唯一约束各自都能触发半匹配，不能只查一侧。"""
    # 列侧：草稿已有 (source_column=a → feature_key=x)
    assert _alias_conflict(_cand("a", "b"), {"a": "x"}, {"x": "a"}) is not None
    # 键侧：草稿已有 (source_column=y → feature_key=b)
    assert _alias_conflict(_cand("a", "b"), {"y": "b"}, {"b": "y"}) is not None


def test_alias_conflict_ignores_unrelated_swap():
    """已存在的行把我们两个键都换了个位置用（b→a），与我们并不冲突——别误伤。"""
    assert _alias_conflict(_cand("a", "b"), {"b": "a"}, {"a": "b"}) is None
