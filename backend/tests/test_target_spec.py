"""LabelTargetSpec 契约测试（W1）。

覆盖：字段边界、family/mode 一致性、治理锁、hash 稳定性、覆盖白名单、
legacy 往返、内置目标合法性、target_ref 解析。
"""

from __future__ import annotations

import json

import pytest

from backend.shared.training.target_registry import (
    BUILTIN_TARGETS_V1,
    TargetRegistryError,
    builtin_specs,
    parse_target_ref,
)
from backend.shared.training.target_spec import (
    LabelTargetSpec,
    TargetSpecError,
    apply_overrides,
    compute_spec_hash,
    label_fields_match,
    spec_from_dict,
    spec_from_label_fields,
)


def _forward_return(**overrides) -> dict:
    raw = {
        "target_id": "cn_ret_5d_open_rank",
        "version": 1,
        "market": "CN",
        "family": "forward_return",
        "mode": "regression",
        "horizon_days": 5,
        "entry": {"lag_days": 1, "price": "open"},
        "exit": {"price": "close"},
        "transform": {"type": "rank", "top_pct": 0.2, "drop_extreme_pct": 0.0},
        "description": "test",
    }
    raw.update(overrides)
    return raw


def _classification(**overrides) -> dict:
    raw = _forward_return(
        target_id="cn_updown_5d_open",
        family="classification_binary",
        mode="classification",
    )
    raw.update(overrides)
    return raw


def _quantile(**overrides) -> dict:
    raw = _forward_return(
        target_id="cn_ret_5d_open_quantile",
        family="quantile_return",
        mode="quantile",
        transform={"type": "raw", "top_pct": 0.2, "drop_extreme_pct": 0.0},
    )
    raw.update(overrides)
    return raw


def _code(fn, *args, **kwargs) -> str:
    with pytest.raises(TargetSpecError) as exc:
        fn(*args, **kwargs)
    return exc.value.code


def test_valid_spec_hash_is_sha256_hex() -> None:
    spec = spec_from_dict(_forward_return())
    digest = spec.spec_hash()
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert digest == compute_spec_hash(spec.canonical_dict())


def test_hash_is_key_order_independent() -> None:
    a = spec_from_dict(_forward_return())
    raw = _forward_return()
    reordered = {k: raw[k] for k in reversed(list(raw))}
    b = spec_from_dict(reordered)
    assert a.spec_hash() == b.spec_hash()


def test_hash_changes_on_any_semantic_field() -> None:
    base = spec_from_dict(_forward_return()).spec_hash()
    assert spec_from_dict(_forward_return(horizon_days=10)).spec_hash() != base
    assert (
        spec_from_dict(_forward_return(version=2)).spec_hash() != base
    )
    assert (
        spec_from_dict(_forward_return(target_id="cn_ret_5d_open_rank_v2")).spec_hash()
        != base
    )
    assert (
        spec_from_dict(
            _forward_return(transform={"type": "zscore", "top_pct": 0.2, "drop_extreme_pct": 0.0})
        ).spec_hash()
        != base
    )


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (_forward_return(target_id="Bad-ID"), "target_id_invalid"),
        (
            _forward_return(family="classification_binary", mode="regression"),
            "target_family_mode_mismatch",
        ),
        (
            _forward_return(entry={"lag_days": 0, "price": "open"}),
            "target_execution_lag_locked",
        ),
        (
            _forward_return(
                transform={"type": "rank", "top_pct": 0.3, "drop_extreme_pct": 0.0}
            ),
            "target_top_pct_without_top_tail",
        ),
        (
            _forward_return(
                transform={"type": "top_tail", "top_pct": 0.2, "drop_extreme_pct": 0.1}
            ),
            "target_drop_extreme_unsupported_transform",
        ),
        (
            _classification(
                transform={"type": "top_tail", "top_pct": 0.2, "drop_extreme_pct": 0.0}
            ),
            "target_classification_transform_unsupported",
        ),
        (
            _classification(
                transform={"type": "rank", "top_pct": 0.2, "drop_extreme_pct": 0.1}
            ),
            "target_drop_extreme_unsupported_transform",
        ),
        (
            _quantile(
                transform={"type": "rank", "top_pct": 0.2, "drop_extreme_pct": 0.0}
            ),
            "target_quantile_transform_unsupported",
        ),
    ],
)
def test_semantic_errors_have_stable_codes(raw: dict, code: str) -> None:
    assert _code(spec_from_dict, raw) == code


def test_pydantic_range_errors_are_wrapped() -> None:
    assert _code(spec_from_dict, _forward_return(horizon_days=0)) == "target_invalid"
    assert _code(spec_from_dict, _forward_return(horizon_days=31)) == "target_invalid"
    assert (
        _code(
            spec_from_dict,
            _forward_return(
                transform={"type": "top_tail", "top_pct": 0.51, "drop_extreme_pct": 0.0}
            ),
        )
        == "target_invalid"
    )


def test_legacy_synthesis_keeps_close_default() -> None:
    spec = spec_from_label_fields(
        {
            "target_horizon_days": 5,
            "target_mode": "return",
            "label_transform": "rank",
            "label_top_pct": 0.2,
            "label_drop_extreme_pct": 0.0,
        },
        market="CN",
    )
    assert spec.entry.price == "close"  # 旧口径：不用 EntrySpec 的 open 默认
    assert spec.to_label_fields()["execution_price"] == "close"
    assert spec.version == 0 and spec.target_id == "legacy"


def test_legacy_classification_forces_rank_transform() -> None:
    spec = spec_from_label_fields(
        {
            "target_horizon_days": 3,
            "target_mode": "classification",
            "label_transform": "top_tail",
            "label_top_pct": 0.2,
            "label_drop_extreme_pct": 0.0,
        },
        market="CN",
    )
    assert spec.family == "classification_binary"
    assert spec.transform.type == "rank"


@pytest.mark.parametrize(
    "fields",
    [
        {
            "target_horizon_days": 1,
            "target_mode": "return",
            "execution_price": "open",
            "label_transform": "rank",
            "label_top_pct": 0.2,
            "label_drop_extreme_pct": 0.0,
        },
        {
            "target_horizon_days": 5,
            "target_mode": "return",
            "execution_price": "close",
            "label_transform": "raw",
            "label_top_pct": 0.2,
            "label_drop_extreme_pct": 0.1,
        },
        {
            "target_horizon_days": 5,
            "target_mode": "return",
            "execution_price": "open",
            "label_transform": "top_tail",
            "label_top_pct": 0.2,
            "label_drop_extreme_pct": 0.0,
        },
        {
            "target_horizon_days": 5,
            "target_mode": "classification",
            "execution_price": "open",
            "label_transform": "rank",
            "label_top_pct": 0.2,
            "label_drop_extreme_pct": 0.0,
        },
    ],
)
def test_legacy_roundtrip_is_lossless(fields: dict) -> None:
    spec = spec_from_label_fields(fields, market="CN")
    assert spec.to_label_fields() == fields


def test_overrides_forward_return() -> None:
    spec = spec_from_dict(_forward_return())
    out = apply_overrides(
        spec,
        {
            "horizon_days": 10,
            "label_transform": "zscore",
            "label_top_pct": 0.2,
            "label_drop_extreme_pct": 0.05,
        },
    )
    assert out.horizon_days == 10
    assert out.transform.type == "zscore"
    assert out.transform.drop_extreme_pct == 0.05
    # 覆盖不得改身份
    assert (out.target_id, out.version) == (spec.target_id, spec.version)


def test_overrides_reject_unknown_and_identity_fields() -> None:
    spec = spec_from_dict(_forward_return())
    for bad in (
        {"entry": {"price": "close"}},
        {"family": "classification_binary"},
        {"version": 2},
        {"not_a_field": 1},
    ):
        with pytest.raises(TargetSpecError) as exc:
            apply_overrides(spec, bad)
        assert exc.value.code == "target_override_not_allowed"


def test_overrides_classification_only_horizon() -> None:
    spec = spec_from_dict(_classification())
    apply_overrides(spec, {"horizon_days": 10})
    for bad in ({"label_transform": "raw"}, {"label_top_pct": 0.3}):
        with pytest.raises(TargetSpecError) as exc:
            apply_overrides(spec, bad)
        assert exc.value.code == "target_override_not_allowed"


def test_overrides_quantile_none_allowed() -> None:
    spec = spec_from_dict(_quantile())
    with pytest.raises(TargetSpecError) as exc:
        apply_overrides(spec, {"horizon_days": 10})
    assert exc.value.code == "target_override_not_allowed"
    assert apply_overrides(spec, None) is spec


def test_label_fields_match_detects_mismatch() -> None:
    spec = spec_from_dict(_forward_return())
    assert label_fields_match(spec, spec.to_label_fields()) == []
    bad = dict(spec.to_label_fields())
    bad["target_horizon_days"] = 10
    assert label_fields_match(spec, bad) == ["target_horizon_days"]
    bad2 = dict(spec.to_label_fields())
    bad2.pop("execution_price")
    assert label_fields_match(spec, bad2) == ["execution_price"]


def test_builtin_targets_are_valid_and_unique() -> None:
    specs = builtin_specs()
    assert len(specs) == len(BUILTIN_TARGETS_V1) == 6
    keys = {(s.target_id, s.version) for s in specs}
    assert len(keys) == len(specs)
    for spec in specs:
        assert spec.market == "CN"
        assert len(spec.spec_hash()) == 64
    # 内置定义不得非法
    for raw in BUILTIN_TARGETS_V1:
        spec_from_dict(raw)


def test_parse_target_ref() -> None:
    assert parse_target_ref({"id": "abc", "version": 2}) == ("abc", 2)
    assert parse_target_ref({"target_id": "abc", "version": "3"}) == ("abc", 3)
    for ref, code in (
        (None, "target_ref_invalid"),
        ({}, "target_ref_invalid"),
        ({"id": "abc"}, "target_version_required"),
        ({"id": "abc", "version": "x"}, "target_ref_invalid"),
        ({"id": "abc", "version": 0}, "target_ref_invalid"),
    ):
        with pytest.raises(TargetRegistryError) as exc:
            parse_target_ref(ref)
        assert exc.value.code == code


def test_spec_json_serializable() -> None:
    spec = spec_from_dict(_forward_return())
    json.dumps(spec.canonical_dict(), ensure_ascii=False)
    # canonical 序列化不允许 NaN
    assert "NaN" not in json.dumps(spec.canonical_dict())
