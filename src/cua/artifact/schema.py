"""The capability artifact: a contract an AI agent can call, not a step list.

Shape, and why:
  * inputs/outputs are typed and declared up front, so a caller knows what to pass
    and what it gets back without reading the steps;
  * every step targets a control semantically (see cua.surface.targets) and carries
    its own checkpoint, so replay verifies each state instead of assuming clicks worked;
  * values are never stored, only {{inputs.x}} references;
  * detectors classify runtime states into recoverable / business outcome / hard failure,
    so the error taxonomy lives in the reviewable artifact rather than in code;
  * versioned and status-gated, so a human reviews before unattended replay.
"""

from typing import Literal

from pydantic import BaseModel, Field

from cua.surface.targets import Target
from cua.values import OutputType

SCHEMA_VERSION = "1.0"

ActionType = Literal["type", "select", "click", "extract"]
Risk = Literal["safe", "reversible", "irreversible"]
Surface = Literal["web", "legacy_web", "desktop"]
Status = Literal["draft", "approved", "deprecated"]


class AppInfo(BaseModel):
    key: str
    vendor: str
    product: str
    version_fingerprint: str | None = None  # text that identifies the app version, for drift checks
    surface: Surface = "legacy_web"


class Provenance(BaseModel):
    recorded_at: str
    goal: str
    discovery_run: str
    model: str
    evidence_path: str
    approvals: int = 0      # irreversible actions a human approved during discovery
    human_actions: int = 0  # actions a human performed during discovery. Not captured as steps:
                            # if non-zero, a reviewer must check the flow is complete.


class InputSpec(BaseModel):
    type: Literal["string", "decimal", "integer", "date"] = "string"
    required: bool = True
    pattern: str | None = None
    sensitive: bool = False
    example: str | None = None
    description: str | None = None


class OutputSpec(BaseModel):
    type: OutputType = "string"
    sensitive: bool = False
    source_step: str | None = None
    description: str | None = None


class Condition(BaseModel):
    """A state matcher. All set fields must hold."""

    frame: str | None = None
    title: str | None = None
    text_visible: str | None = None
    url_matches: str | None = None


class Step(BaseModel):
    id: str
    action: ActionType
    target: Target
    value: str | None = None           # literal, or "{{inputs.member_id}}"
    into: str | None = None            # for extract: the output name
    parse: OutputType | None = None
    risk: Risk = "safe"
    checkpoint: Condition | None = None  # verified after the action


class RecoverableHandler(BaseModel):
    id: str
    when: Condition
    do: Literal["click", "reload", "reauthenticate", "escalate"]
    target: Target | None = None
    max_times: int = 2


class BusinessOutcome(BaseModel):
    """A legitimate answer for the caller (e.g. no such member), never a crash."""

    code: str
    when: Condition
    retryable: bool = False


class HardFailure(BaseModel):
    code: str
    when: Condition
    retryable: bool = False


class Detectors(BaseModel):
    recoverable: list[RecoverableHandler] = []
    business_outcomes: list[BusinessOutcome] = []
    hard_failures: list[HardFailure] = []


class Success(BaseModel):
    checkpoint: Condition | None = None
    outputs_present: list[str] = []


class Capability(BaseModel):
    id: str
    name: str
    version: int = 1
    status: Status = "draft"
    description: str
    app: AppInfo
    risk_level: Risk = "safe"
    provenance: Provenance


class CapabilityArtifact(BaseModel):
    schema_version: str = SCHEMA_VERSION
    capability: Capability
    preconditions: list[str] = ["authenticated_session"]
    entry: str = "/"
    inputs: dict[str, InputSpec] = {}
    outputs: dict[str, OutputSpec] = {}
    steps: list[Step]
    success: Success = Field(default_factory=Success)
    detectors_pack: str | None = None  # e.g. "legacycore@1", for provenance
    detectors: Detectors = Field(default_factory=Detectors)
    # Per-tenant specialisation: label renames, route prefixes, disabled steps.
    # Merged over the base artifact at load time. Empty here by design; see REPORT.md.
    tenant_overrides: dict[str, dict] = {}


class AppPack(BaseModel):
    """Per-app knowledge that is not flow-specific: what the app looks like and which
    runtime states it can show. Shared by every capability recorded against that app."""

    pack: str
    version: int = 1
    app: AppInfo
    detectors: Detectors = Field(default_factory=Detectors)
