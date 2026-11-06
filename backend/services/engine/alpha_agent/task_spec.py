"""Immutable request and execution snapshots for the shared mining queue."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any

RD_AGENT_VERSION = "quantmind-rd/1.0"
STATISTICAL_VERSION = "quantmind-statistical/2.0"
PARAMETER_KEYS = frozenset(
    {
        "algorithm",
        "market",
        "universe",
        "loop_n",
        "direction",
        "directions",
        "direction_mode",
        "direction_selection",
        "num_directions",
        "data_source",
        "mining_start",
        "mining_end",
        "valid_start",
        "valid_end",
        "datasets",
        "freq",
        "quality_gate_mode",
        "doc_id",
        "seed_factor_ids",
        "seed_factors",
        "horizons",
        "top_k",
        "min_names_per_day",
        "min_valid_days",
        "quantiles",
        "fdr_q",
        "require_fdr",
        "oos_min_ratio",
        "oos_min_days",
        "neutralize",
        "tradability",
        "stability_folds",
        "min_fold_sign_consistency",
        "min_fold_days",
        "require_stability",
        "dedup_corr_reject",
        "dedup_corr_warn",
        "require_dedup",
        "composite_enabled",
        "composite_z_window",
        "net_ic_cost_rate",
        "auto_materialize",
    }
)


class IdempotencyConflict(ValueError):
    """An owner's request key is already bound to different input."""


def _canonical(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise ValueError("Mining snapshot must be an object")
    encoded = json.dumps(
        dict(value),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > 256 * 1024:
        raise ValueError("Mining snapshot exceeds 256 KiB")
    return encoded


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MiningTaskSpec:
    """Only JSON strings are held so later dict mutations cannot rewrite history.

    The request hash deliberately excludes server-selected data/version snapshots:
    an HTTP retry with the same input/key reuses the original task after a data
    refresh or deployment. Active-task deduplication uses execution/data facts,
    excluding selection-audit metadata that does not change the chosen direction.
    Scope is enforced by the store, never inferred from these parameters.
    """

    algorithm: str
    algorithm_version: str
    request_json: str
    params_json: str
    data_snapshot_json: str
    request_key: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.algorithm not in {"rdagent", "statistical"}:
            raise ValueError("Unknown mining algorithm")
        if (
            not isinstance(self.algorithm_version, str)
            or not self.algorithm_version.strip()
            or len(self.algorithm_version) > 128
        ):
            raise ValueError("Mining algorithm version is required")
        if self.schema_version != 1:
            raise ValueError("Unknown mining specification schema")
        for field in ("request_json", "params_json", "data_snapshot_json"):
            raw = getattr(self, field)
            if not isinstance(raw, str):
                raise ValueError("Mining snapshot must be canonical JSON")
            decoded = json.loads(raw)
            encoded = _canonical(decoded)
            if field != "data_snapshot_json" and decoded.keys() - PARAMETER_KEYS:
                raise ValueError("Unsupported mining snapshot fields")
            object.__setattr__(self, field, encoded)
        if self.request_key is not None and (
            not isinstance(self.request_key, str)
            or not self.request_key.strip()
            or len(self.request_key) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in self.request_key)
        ):
            raise ValueError("Invalid mining idempotency key")

    @classmethod
    def build(
        cls,
        *,
        algorithm: str,
        algorithm_version: str,
        request: Mapping[str, Any],
        params: Mapping[str, Any],
        data_snapshot: Mapping[str, Any],
        request_key: str | None = None,
    ) -> MiningTaskSpec:
        return cls(
            algorithm,
            algorithm_version,
            _canonical(request),
            _canonical(params),
            _canonical(data_snapshot),
            request_key,
        )

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(self.request_json)

    @property
    def execution_fingerprint(self) -> str:
        data_snapshot = json.loads(self.data_snapshot_json)
        # Capture time is useful audit context but is not a data version. Keep
        # identical input/data facts deduplicable when admitted seconds apart.
        if isinstance(data_snapshot, dict):
            data_snapshot.pop("captured_at", None)
            data_snapshot.pop("snapshot_at", None)
        execution_params = json.loads(self.params_json)
        # The actual selected direction is already an execution parameter. An
        # audit seed/history timestamp must not defeat active-task deduplication.
        execution_params.pop("direction_selection", None)
        return _fingerprint(
            _canonical(
                {
                    "schema_version": self.schema_version,
                    "algorithm": self.algorithm,
                    "algorithm_version": self.algorithm_version,
                    "params": execution_params,
                    "data_snapshot": data_snapshot,
                }
            )
        )
