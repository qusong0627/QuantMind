"""Strict request contracts shared by the legacy and workspace mining inputs."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .statistical.configuration import StatisticalOverrides


class MiningSubmitRequest(StatisticalOverrides):
    """Non-null JSON fields override legacy query fields.

    RD-Agent and statistical inputs share the task endpoint; each runner validates
    its own complete set before an immutable task specification is admitted.
    """

    model_config = ConfigDict(extra="forbid")
    algorithm: Literal["rdagent", "statistical"] | None = None
    market: Literal["a_share"] | None = None
    universe: str | None = Field(None, min_length=1, max_length=64)
    loop_n: int | None = Field(None, ge=1, le=20, strict=True)
    direction: str | None = Field(None, max_length=20000)
    doc_id: str | None = Field(None, min_length=1, max_length=128)
    directions: list[Annotated[str, Field(max_length=1000)]] | None = Field(
        None, max_length=100
    )
    direction_mode: Literal["selected", "random", "quota"] | None = None
    data_source: Literal["", "qlib_bin", "parquet"] | None = None
    freq: Literal["day"] | None = None
    datasets: list[str] | None = Field(None, max_length=20)
    mining_start: str | None = Field(None, min_length=10, max_length=10)
    mining_end: str | None = Field(None, min_length=10, max_length=10)
    valid_start: str | None = Field(None, min_length=10, max_length=10)
    valid_end: str | None = Field(None, min_length=10, max_length=10)
    auto_materialize: bool = False

    @model_validator(mode="after")
    def validate_statistical_window(self):
        if any((self.mining_start, self.mining_end, self.valid_start, self.valid_end)):
            from backend.services.engine.alpha_agent.mining_windows import (
                build_mining_window_env,
            )

            build_mining_window_env(
                self.mining_start,
                self.mining_end,
                self.valid_start,
                self.valid_end,
            )
        if self.algorithm != "statistical":
            return self
        if not self.mining_start or not self.mining_end:
            raise ValueError("统计派必须提供 mining_start 和 mining_end")
        start, end = (
            date.fromisoformat(self.mining_start),
            date.fromisoformat(self.mining_end),
        )
        if start > end:
            raise ValueError("mining_start 不能晚于 mining_end")
        if (self.valid_start is None) != (self.valid_end is None):
            raise ValueError("valid_start 与 valid_end 必须同时提供")
        if self.valid_start and self.valid_end:
            valid_start = date.fromisoformat(self.valid_start)
            valid_end = date.fromisoformat(self.valid_end)
            if valid_start > valid_end:
                raise ValueError("valid_start 不能晚于 valid_end")
        return self


class FactorVisibilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=500)


class DirectionDecomposeRequest(BaseModel):
    """A bounded research request; identity and LLM credentials are server derived."""

    model_config = ConfigDict(extra="forbid")

    direction: str = Field(min_length=1, max_length=20_000)
    market: Literal["a_share"] = "a_share"
    universe: str = Field(default="all_a", min_length=1, max_length=128)
    max_cards: int | None = Field(default=None, ge=1, le=12, strict=True)
    seed_factor_ids: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_seed_ids(self):
        self.seed_factor_ids = [value.strip() for value in self.seed_factor_ids]
        if any(not value or len(value) > 256 for value in self.seed_factor_ids):
            raise ValueError("Invalid parent factor ID")
        if len(set(self.seed_factor_ids)) != len(self.seed_factor_ids):
            raise ValueError("Parent factor IDs must be unique")
        return self


class DirectionBatchDispatchRequest(BaseModel):
    """A bounded, retryable batch of user-reviewed research directions."""

    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    doc_id: str | None = Field(default=None, min_length=1, max_length=128)
    directions: list[str] = Field(min_length=1, max_length=10)
    market: Literal["a_share"] = "a_share"
    universe: str = Field(default="all_a", min_length=1, max_length=128)
    loop_n: int = Field(default=3, ge=1, le=20, strict=True)
    data_source: Literal["", "qlib_bin", "parquet"] = ""
    freq: Literal["day"] = "day"
    datasets: list[str] | None = Field(default=None, max_length=20)
    mining_start: str | None = Field(default=None, min_length=10, max_length=10)
    mining_end: str | None = Field(default=None, min_length=10, max_length=10)
    valid_start: str | None = Field(default=None, min_length=10, max_length=10)
    valid_end: str | None = Field(default=None, min_length=10, max_length=10)
    seed_factor_ids: list[str | None] | None = Field(default=None, max_length=10)

    @model_validator(mode="after")
    def normalize_and_validate_directions(self):
        from backend.services.engine.alpha_agent.mining_windows import (
            build_mining_window_env,
        )

        build_mining_window_env(
            self.mining_start, self.mining_end, self.valid_start, self.valid_end
        )
        if self.doc_id is not None:
            self.doc_id = self.doc_id.strip()
            if not self.doc_id:
                raise ValueError("Invalid source document ID")
        normalized = [direction.strip() for direction in self.directions]
        if any(not direction for direction in normalized):
            raise ValueError("批量派发的每个方向都不能为空")
        if any(len(direction) > 20_000 for direction in normalized):
            raise ValueError("单个挖掘方向超过 20000 字上限")
        self.directions = normalized
        if self.seed_factor_ids is not None:
            if len(self.seed_factor_ids) != len(normalized):
                raise ValueError("seed_factor_ids must match directions length")
            self.seed_factor_ids = [
                value.strip() if isinstance(value, str) else None
                for value in self.seed_factor_ids
            ]
            if any(
                value is not None and (not value or len(value) > 256)
                for value in self.seed_factor_ids
            ):
                raise ValueError("Invalid parent factor ID")
            if len({value for value in self.seed_factor_ids if value}) > 3:
                raise ValueError("A batch may use at most three unique parent factors")
        return self


class SampledDirectionBatchRequest(DirectionBatchDispatchRequest):
    """Pick distinct reviewed candidates, then freeze the whole batch before admission."""

    directions: list[Annotated[str, Field(max_length=1000)]] = Field(
        min_length=1, max_length=100
    )
    seed_factor_ids: list[str | None] | None = Field(default=None, max_length=100)
    selection_mode: Literal["selected", "random", "quota"] = "random"
    num_directions: int = Field(default=3, ge=1, le=10, strict=True)

    @model_validator(mode="after")
    def validate_candidates(self):
        from .direction_sampling import clean_directions

        clean = clean_directions(self.directions)
        if clean != self.directions:
            raise ValueError("多方向候选须为不同的非空方向；请先去重")
        return self
