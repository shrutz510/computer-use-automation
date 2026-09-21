"""The Surface seam: everything above this line (agent, recorder, replay) talks to a
Surface; only Surface implementations know about DOMs, frames or OS accessibility APIs."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from cua.surface.targets import Strategy, Target

Handle = Any  # an implementation-specific pointer to one live control


class SurfaceError(Exception):
    """An action could not be performed on the live surface."""


class TargetNotFound(SurfaceError):
    def __init__(self, target: Target, attempts: list[str]):
        self.target = target
        self.attempts = attempts
        super().__init__(f"no unique match for {target.description or target}: {'; '.join(attempts)}")


@dataclass
class ControlInfo:
    """What the policy gate needs to know about a control before it is activated."""

    tag: str
    name: str
    form_method: str
    destination: str | None  # absolute URL a link/submit would load, if any
    frame_url: str


@dataclass
class ApprovalRequest:
    action: str
    control: str
    risk: str
    reason: str
    url: str


class PolicyError(Exception):
    """Deliberately not a SurfaceError: a policy decision must never be mistaken for a
    flaky click and retried."""


class PolicyBlocked(PolicyError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ApprovalRequired(PolicyError):
    def __init__(self, request: ApprovalRequest):
        self.request = request
        super().__init__(f"{request.action} {request.control!r} needs human approval ({request.reason})")


@dataclass
class Element:
    ref: str
    frame: str | None
    kind: str  # "control" | "cell"
    role: str
    name: str = ""
    label: str = ""
    text: str = ""
    row_header: str = ""
    col_header: str = ""
    tag: str = ""
    input_type: str = ""
    attr_name: str = ""
    form_action: str = ""
    form_method: str = ""
    options: list[str] | None = None
    css_path: str = ""
    sensitive: bool = False


@dataclass
class FrameView:
    name: str | None
    url: str
    title: str
    text: str


@dataclass
class Snapshot:
    frames: list[FrameView]
    elements: dict[str, Element] = field(default_factory=dict)
    fingerprint: str = ""

    def sensitive_values(self) -> list[str]:
        return [e.text for e in self.elements.values() if e.sensitive and e.text]


@dataclass
class Resolved:
    handle: Handle
    strategy_index: int
    strategy: Strategy


class Surface(Protocol):
    def goto(self, route: str) -> None: ...
    def reload(self) -> None: ...
    def wait_ms(self, ms: int) -> None: ...
    def observe(self) -> Snapshot: ...
    def handle_for_ref(self, ref: str) -> Handle: ...
    def describe(self, ref: str) -> Target: ...
    def resolve(self, target: Target, timeout_s: float = 0) -> Resolved: ...
    def inspect(self, handle: Handle) -> ControlInfo: ...
    def click(self, handle: Handle, risk_hint: str = "safe") -> None: ...
    def fill(self, handle: Handle, text: str) -> None: ...
    def select(self, handle: Handle, option: str) -> None: ...
    def read(self, handle: Handle) -> str: ...
    def screenshot(self, path: Path) -> None: ...
