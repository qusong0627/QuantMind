"""训练 metadata 的 target/label 单一构造点（无重依赖，API/容器/测试共用）。"""

from __future__ import annotations

import os
from typing import Any, Mapping

from backend.shared.training.target_contract import load_target_contract


def build_target_metadata(
    cfg: Mapping[str, Any],
    *,
    lag_days: int = 1,
    diagnostics: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """构造 metadata 的 label/target 字段。

    - 新路径：写 target 三元组 + 完整 spec；
    - legacy：合成 legacy spec/hash（只追溯，不改训练口径）；
    - label_stats：训练前诊断；同时写 contract_flags 标明实际生效的闸门。
    """
    label = cfg.get("label") if isinstance(cfg.get("label"), Mapping) else {}
    label = dict(label or {})
    environ = env if env is not None else os.environ
    meta: dict[str, Any] = {
        "target_horizon_days": int(label.get("target_horizon_days") or 1),
        "execution_lag_days": int(lag_days),
        "target_mode": str(label.get("target_mode") or "return"),
        "label_formula": str(label.get("label_formula") or ""),
        "effective_trade_date": str(label.get("effective_trade_date") or ""),
        "training_window": str(label.get("training_window") or ""),
    }
    target_id = str(label.get("target_id") or "").strip()
    if target_id:
        meta.update(
            {
                "target_id": target_id,
                "target_version": int(label.get("target_version") or 0),
                "target_hash": str(label.get("target_hash") or ""),
                "target_base_hash": str(label.get("target_base_hash") or ""),
                "target_status": str(label.get("target_status") or "active"),
                "target_spec": label.get("target_spec")
                if isinstance(label.get("target_spec"), Mapping)
                else None,
                "target_overrides": label.get("target_overrides")
                if isinstance(label.get("target_overrides"), Mapping)
                else {},
                "label_contract_version": int(
                    label.get("label_contract_version") or 1
                ),
            }
        )
    else:
        try:
            contract = load_target_contract(cfg)
            meta.update(
                {
                    "target_id": "legacy",
                    "target_version": 0,
                    "target_hash": contract.effective_hash,
                    "target_base_hash": contract.base_hash,
                    "target_status": "legacy",
                    "target_spec": contract.spec.canonical_dict(),
                    "label_contract_version": contract.contract_version,
                }
            )
        except Exception:  # noqa: BLE001 - 兼容镜像/异常时仍保留平铺字段
            pass

    if diagnostics is not None:
        meta["label_stats"] = dict(diagnostics)
        meta["contract_flags"] = {
            "capability_gate": str(
                environ.get("QM_TARGET_CAPABILITY_GATE") or "strict"
            ).strip().lower(),
            "diagnostic_gate": str(diagnostics.get("gate") or "strict"),
        }
    return meta


__all__ = ["build_target_metadata"]
