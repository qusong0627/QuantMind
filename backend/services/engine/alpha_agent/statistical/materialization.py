"""Safe resolution of statistical-catalogue expressions for materialization."""

from __future__ import annotations

from typing import Any


def trusted_statistical_candidate(
    metadata: Any, expression: str
):
    """Return an exact fixed-catalogue candidate, rejecting arbitrary expressions."""
    if not isinstance(metadata, dict) or metadata.get("algorithm") != "statistical":
        return None
    candidate_key = metadata.get("candidate_key")
    if not isinstance(candidate_key, str) or not candidate_key:
        raise ValueError("Statistical factor is missing its candidate identity")
    from .candidates import CANDIDATE_BY_KEY

    from .composite import COMPOSITE_KEY, build_composite_plan

    if candidate_key == COMPOSITE_KEY:
        evidence = metadata.get("evidence")
        if not isinstance(evidence, dict) or evidence.get("eligible") is not True:
            raise ValueError("Statistical composite has no eligible evidence")
        components = evidence.get("components")
        component_ics = evidence.get("component_rank_ics")
        horizon = evidence.get("horizon")
        z_window = evidence.get("z_window")
        if (
            not isinstance(components, list)
            or not isinstance(component_ics, list)
            or len(components) != len(component_ics)
            or type(horizon) is not int
            or type(z_window) is not int
        ):
            raise ValueError("Statistical composite evidence is invalid")
        catalogue = [CANDIDATE_BY_KEY.get(key) for key in components]
        if any(candidate is None for candidate in catalogue):
            raise ValueError("Statistical composite refers to an unknown component")
        plan = build_composite_plan(
            list(zip(catalogue, component_ics, strict=True)),
            horizon=horizon,
            z_window=z_window,
        )
        if plan is None or plan.expression != expression:
            raise ValueError("Statistical composite expression differs from its evidence")
        return plan.candidate()

    candidate = CANDIDATE_BY_KEY.get(candidate_key)
    if candidate is None or candidate.expression != expression:
        raise ValueError("Statistical factor expression differs from its fixed catalogue")
    return candidate
