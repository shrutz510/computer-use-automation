"""The replay result contract: what an AI agent gets back when it invokes a capability.

Four shapes, discriminated on `status`, because the caller must not have to guess:
  success          - the flow completed and here are the typed outputs
  business_outcome - the app gave a legitimate answer ("no such member"); not a crash
  failed           - something broke; here is the step, what was expected and what was seen
  escalated        - a human must act; the run is paused, not lost

Recoverable conditions (a dismissed interstitial, a waited-out slow page, a re-auth) are
deliberately NOT a status: they are handled inside the run and listed in `recovered`.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class Recovery(BaseModel):
    handler: str
    step: str
    detail: str = ""


class _Base(BaseModel):
    run_id: str
    capability: str
    version: int
    evidence: str
    steps_completed: int = 0
    recovered: list[Recovery] = []
    # Which locator strategy won per step. A fallback winning is the drift signal.
    strategies_used: dict[str, str] = {}


class ReplaySuccess(_Base):
    status: Literal["success"] = "success"
    outputs: dict[str, Any] = {}


class ReplayBusinessOutcome(_Base):
    status: Literal["business_outcome"] = "business_outcome"
    code: str
    message: str
    step: str


class ReplayFailure(_Base):
    status: Literal["failed"] = "failed"
    code: str  # INPUT_INVALID | LOCATOR_NOT_FOUND | CHECKPOINT_FAILED | OUTPUT_MISSING | PARSE_ERROR |
               # ACTION_FAILED | POLICY_VIOLATION | APP_ERROR | PERMISSION_DENIED | SESSION_FAILED
    step: str | None = None
    expected: str | None = None
    observed: str | None = None
    screenshot: str | None = None
    retryable: bool = False


class ReplayEscalated(_Base):
    status: Literal["escalated"] = "escalated"
    reason: str
    step: str | None = None
    intervention_id: str | None = None
    screenshot: str | None = None


ReplayResult = Annotated[
    ReplaySuccess | ReplayBusinessOutcome | ReplayFailure | ReplayEscalated,
    Field(discriminator="status"),
]
