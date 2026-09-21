"""The discovery trace: what the agent did, and the live-validated target of every action.
This (not the raw model transcript) is what the recorder turns into a capability artifact."""

from typing import Literal

from pydantic import BaseModel

from cua.surface import Target


class FrameState(BaseModel):
    name: str | None
    url: str
    title: str


class TraceStep(BaseModel):
    index: int
    tool: str
    rationale: str = ""
    target: Target | None = None
    element_role: str | None = None
    element_name: str | None = None         # accessible name of the control, for risk rules
    element_form_method: str | None = None  # get/post: a POST submission may change state
    element_sensitive: bool = False         # the value read was marked sensitive
    value: str | None = None
    param_name: str | None = None
    output_name: str | None = None
    output_type: str | None = None
    ok: bool = True
    error: str | None = None
    frames_before: list[FrameState] = []
    frames_after: list[FrameState] = []
    screenshot: str | None = None


class Trace(BaseModel):
    run_id: str
    goal: str
    app: str
    base_url: str
    entry_route: str
    model: str
    started_at: str
    finished_at: str | None = None
    status: Literal["running", "success", "escalated", "failed"] = "running"
    reason: str = ""
    steps: list[TraceStep] = []
    outputs: dict[str, str] = {}
    handoffs: list[dict] = []  # interventions during discovery; full records under interventions/
    human_actions: int = 0     # actions a person performed; not part of the recorded flow
