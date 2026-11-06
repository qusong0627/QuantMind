"""Public task projections never include executor witnesses or artifact paths."""

from collections.abc import Mapping
from datetime import datetime, timezone

from backend.shared.secret_redaction import redact_data
from backend.shared.utc_datetime import to_utc_iso

TASK_FIELDS = frozenset(
    {
        "task_id",
        "tenant_id",
        "user_id",
        "market",
        "universe",
        "data_source",
        "direction",
        "source_doc_id",
        "status",
        "phase",
        "progress",
        "progress_pct",
        "current_loop",
        "loop_n",
        "created_at",
        "updated_at",
        "completed_at",
        "queue_position",
        "error_message",
        "timeline",
        "token_usage",
        "seed_factor_ids",
    }
)
RESULT_FIELDS = frozenset(
    {
        "task_id",
        "market",
        "total_factors",
        "persisted_factors",
        "elapsed_seconds",
        "message",
    }
)


def public_task(task: Mapping) -> dict:
    output = {key: task[key] for key in TASK_FIELDS if key in task}
    for field in ("created_at", "updated_at", "completed_at"):
        if field not in output or output[field] is None:
            continue
        value = output[field]
        try:
            if type(value) in {int, float}:
                value = datetime.fromtimestamp(value, timezone.utc)
            elif isinstance(value, str):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            output[field] = to_utc_iso(value) if isinstance(value, datetime) else None
        except (ValueError, OverflowError, OSError):
            output[field] = None
    result = task.get("result")
    if isinstance(result, Mapping):
        output["result"] = {key: result[key] for key in RESULT_FIELDS if key in result}
    return redact_data(output)
