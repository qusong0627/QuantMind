"""WP6 晋升闸：严格评估缺失/为负默认阻断，allow_strict_fail 才放行。"""

from __future__ import annotations

from backend.services.engine.services.model_rollout_service import strict_report_gate


def _row(total: float, *, hac: float = 2.5, psr: float = 0.9, status: str = "ready"):
    return {
        "status": status,
        "report": {
            "results": {
                "test": {
                    "top20": {
                        "batches": 17,
                        "cost_25bp": {"total": total, "hac_t": hac, "psr": psr},
                    }
                }
            }
        },
    }


def test_no_run_id_skips_gate():
    blocked, reason, warnings = strict_report_gate(source_run_id="", report_row=None)
    assert blocked is False
    assert warnings and "source_run_id" in warnings[0]


def test_missing_report_blocks_by_default():
    blocked, reason, _ = strict_report_gate(source_run_id="train_x", report_row=None)
    assert blocked is True
    assert "strict-report" in reason


def test_missing_report_allowed_with_flag():
    blocked, reason, warnings = strict_report_gate(
        source_run_id="train_x", report_row=None, allow_fail=True
    )
    assert blocked is False
    assert warnings and "allow_strict_fail" in warnings[0]


def test_positive_report_passes_with_significance_warnings():
    blocked, reason, warnings = strict_report_gate(
        source_run_id="train_x", report_row=_row(0.12, hac=1.2, psr=0.7)
    )
    assert blocked is False
    assert any("HAC" in w for w in warnings)
    assert any("PSR" in w for w in warnings)


def test_negative_report_blocks():
    blocked, reason, _ = strict_report_gate(
        source_run_id="train_x", report_row=_row(-0.03)
    )
    assert blocked is True
    assert "≤ 0" in reason


def test_negative_report_allowed_with_flag():
    blocked, reason, warnings = strict_report_gate(
        source_run_id="train_x", report_row=_row(-0.03), allow_fail=True
    )
    assert blocked is False
    assert any("负收益" in w for w in warnings)


def test_running_report_blocks():
    blocked, reason, _ = strict_report_gate(
        source_run_id="train_x", report_row=_row(0.1, status="running")
    )
    assert blocked is True
    assert "running" in reason
