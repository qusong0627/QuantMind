"""Explicit private-resource scope, derived from verified identity by callers.

Constructing this value validates completeness, not authorization. HTTP callers
must obtain both dimensions from the existing session validation chain.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class OwnerScope:
    tenant_id: str
    user_id: str

    def __post_init__(self) -> None:
        for name in ("tenant_id", "user_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Both owner dimensions are required")
            object.__setattr__(self, name, value.strip())

    def sql_params(self) -> dict[str, str]:
        return {"tenant_id": self.tenant_id, "user_id": self.user_id}
