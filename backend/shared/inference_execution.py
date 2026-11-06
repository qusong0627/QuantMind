"""Inference execution result shared by engine and its HTTP callers."""

from dataclasses import dataclass, field


@dataclass
class ExecutionResult:
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    signals_count: int = 0
    run_id: str = ""
    error: str = ""
    signals: list[dict] = field(default_factory=list)
    fallback_used: bool = False
    fallback_reason: str = ""
    failure_stage: str = ""
    active_model_id: str = ""
    active_data_source: str = ""
    data_trade_date: str = ""
    prediction_trade_date: str = ""
    execution_mode: str = ""
    model_switch_used: bool = False
    model_switch_reason: str = ""
