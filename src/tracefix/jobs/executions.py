"""Pure execution observations shared by the host authority and ticket-only workers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class TestExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    call_id: str = Field(min_length=1, max_length=256)
    invocation_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    owner_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    phase: Literal["reserved", "dispatched", "settled", "not_started", "unknown"] = "reserved"
    process_started: StrictBool | None = False
    tree_exit_confirmed: StrictBool = False

    @model_validator(mode="after")
    def observations(self) -> TestExecution:
        if self.phase == "reserved" and (
            self.process_started is not False or self.tree_exit_confirmed
        ):
            raise ValueError("reservation cannot contain execution observations")
        if self.phase == "dispatched" and (
            self.process_started is False or self.tree_exit_confirmed
        ):
            raise ValueError("dispatch requires pending or confirmed process start")
        if self.phase in {"settled", "not_started"} and (
            self.process_started is not (self.phase == "settled") or not self.tree_exit_confirmed
        ):
            raise ValueError("settled execution requires explicit start and cleanup observations")
        return self
