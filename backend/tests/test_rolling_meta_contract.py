"""rolling_meta 全链路契约测试（P1 · 设计文档 §4.7 白名单）。

覆盖白名单五环中的**可离线验证**部分（不 import admin 包——那份注释里的
_normalize_payload 调用点由端点级验收覆盖）：

1. 请求入口（per-model extra="forbid" 模型）接受 rolling_meta 且不丢键；
2. 归一化白名单 sanitize_rolling_meta：只保留六个合法键；
3. config.yaml 契约（TrainingConfig / dump_contract_dict）：
   - 带 rolling_meta → 原样发射（含二次往返稳定）；
   - 缺失 → 不发射（空配置键面快照契约不被破坏）。
"""

from __future__ import annotations

import pytest

from backend.shared.training.recipe_registry import (
    ROLLING_META_KEYS,
    load_recipe,
    sanitize_rolling_meta,
)
from backend.shared.training.rolling_window import RollingWindow
from backend.shared.training.recipe_registry import build_training_payload
from backend.shared.training.schemas import TrainingConfig, dump_contract_dict

from datetime import date

VALID_META = {
    "campaign_id": "rc_cn_cn_nativetft_base_20261009",
    "window_index": 321,
    "anchor_date": "2026-10-09",
    "recipe_hash": "deadbeef",
    "purge_days": 6,
    "dispatched_by": "retrain_scheduler",
}


def _window() -> RollingWindow:
    return RollingWindow(
        anchor_date=date(2026, 10, 9),
        train_start=date(2022, 1, 4),
        train_end=date(2025, 6, 30),
        valid_start=date(2025, 7, 9),
        valid_end=date(2026, 1, 5),
        test_start=date(2026, 1, 14),
        test_end=date(2026, 10, 9),
        purge_days=6,
        mode="sliding",
        window_index=321,
    )


# ---------------------------------------------------------------------------
# 1) 归一化白名单
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sanitize_rolling_meta_keeps_whitelist_only():
    raw = dict(VALID_META)
    raw["evil_key"] = {"drop": "tables"}
    raw["tenant_id"] = "pwn"
    cleaned = sanitize_rolling_meta(raw)
    assert cleaned == VALID_META
    assert set(cleaned) <= set(ROLLING_META_KEYS)
    # 输入不被改动（纯函数）
    assert "evil_key" in raw


@pytest.mark.unit
@pytest.mark.parametrize("raw", [None, "str", 123, [], {"evil": 1}, {}])
def test_sanitize_rolling_meta_rejects_non_dict_or_empty(raw):
    assert sanitize_rolling_meta(raw) is None


# ---------------------------------------------------------------------------
# 2) per-model 请求入口（extra="forbid"）接受且携带 rolling_meta
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_per_model_request_accepts_rolling_meta():
    from backend.shared.training.per_model import REQUEST_MODELS

    model = REQUEST_MODELS["nativetft"]
    assert "rolling_meta" in model.model_fields
    parsed = model.model_validate(
        {
            "features": ["mom_20"],
            "rolling_meta": VALID_META,
        }
    )
    # extra="forbid" 下未知键会 422；能走到这里即键已被接纳
    assert parsed.rolling_meta == VALID_META


# ---------------------------------------------------------------------------
# 3) config.yaml 契约
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_training_config_roundtrip_preserves_rolling_meta():
    raw = {
        "run_id": "train_x",
        "job_name": "rc_cn_cn_nativetft_base_20261009",
        "rolling_meta": VALID_META,
        "split": {
            "train": ["2022-01-04", "2025-06-30"],
            "valid": ["2025-07-09", "2026-01-05"],
            "test": ["2026-01-14", "2026-10-09"],
        },
    }
    first = dump_contract_dict(TrainingConfig.from_dict(raw))
    assert first["rolling_meta"] == VALID_META
    # split 不受 rolling_meta 影响
    assert first["split"]["test"] == ["2026-01-14", "2026-10-09"]
    # 二次往返稳定（编排器 _validate_config_dict 会做一次额外往返）
    second = dump_contract_dict(TrainingConfig.from_dict(first))
    assert second["rolling_meta"] == VALID_META


@pytest.mark.unit
def test_training_config_absent_rolling_meta_not_emitted():
    out = dump_contract_dict(TrainingConfig.from_dict({"run_id": "train_y"}))
    assert "rolling_meta" not in out


@pytest.mark.unit
def test_dispatch_payload_survives_config_contract():
    """配方合成 payload（含六键 split + rolling_meta）过 config 契约后两者俱在。"""
    recipe = load_recipe("cn_nativetft_base")
    window = _window()
    payload = build_training_payload(recipe, window, "rc_cn_cn_nativetft_base_20261009", "test")
    # 模拟编排器：payload 的窗口键 → config 的 split/rolling_meta
    raw = {
        "run_id": "train_z",
        "rolling_meta": payload["rolling_meta"],
        "split": {
            "train": [payload["train_start"], payload["train_end"]],
            "valid": [payload["valid_start"], payload["valid_end"]],
            "test": [payload["test_start"], payload["test_end"]],
        },
    }
    out = dump_contract_dict(TrainingConfig.from_dict(raw))
    assert out["rolling_meta"] == payload["rolling_meta"]
    assert out["rolling_meta"]["recipe_hash"] == payload["rolling_meta"]["recipe_hash"]
    assert out["split"]["train"] == [payload["train_start"], payload["train_end"]]


# ---------------------------------------------------------------------------
# 4) train.py 落盘接线（两处 metadata 构造点，多模型 + 单模型）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_train_py_stamps_rolling_meta_at_both_metadata_sites():
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2] / "docker" / "training" / "train.py"
    ).read_text(encoding="utf-8")
    stamp = 'cfg.get("rolling_meta") if isinstance(cfg.get("rolling_meta"), dict) else None'
    assert src.count(stamp) == 2, "train.py 两处 metadata 构造点都要把 config.yaml 的 rolling_meta 落盘"
