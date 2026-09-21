"""Who controls the live session, and the only legal ways that can change.

    AUTOMATION ──stuck, or an action needs approval──► AWAITING_HUMAN
    AWAITING_HUMAN ──approve──► AUTOMATION          automation performs the approved action itself
    AWAITING_HUMAN ──claim (takes the lease)──► HUMAN_CONTROL
    HUMAN_CONTROL ──resume (lease holder only)──► VERIFYING
    VERIFYING ──automation re-checks the state──► AUTOMATION
                  (checkpoint met: carry on; not met: a new intervention)
    AWAITING_HUMAN / HUMAN_CONTROL ──abort or timeout──► AUTOMATION   the run ends escalated

One `state` field plus a lease token. The policy gate refuses every automated action unless the
state is AUTOMATION, and only the lease holder can hand control back. Thread-safe: the operator
console calls in from its own thread while the automation thread waits.
"""

import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from cua.surface import ApprovalRequest


class Controller(str, Enum):
    AUTOMATION = "automation"
    AWAITING_HUMAN = "awaiting_human"
    HUMAN_CONTROL = "human_control"
    VERIFYING = "verifying"


ALLOWED = {
    Controller.AUTOMATION: {Controller.AWAITING_HUMAN},
    Controller.AWAITING_HUMAN: {Controller.AUTOMATION, Controller.HUMAN_CONTROL},
    Controller.HUMAN_CONTROL: {Controller.VERIFYING, Controller.AUTOMATION},
    Controller.VERIFYING: {Controller.AUTOMATION},
}


class ControlError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Intervention:
    """An intervention request: everything an operator needs to act without digging."""

    id: str
    kind: str                      # "approval" | "stuck"
    run_id: str
    subject: str                   # capability id, or the discovery goal
    step: str
    reason: str
    url: str
    screenshot: str | None
    recent_events: list[dict]
    created_at: str
    control: str | None = None     # for approvals: the control awaiting approval
    resolution: str | None = None  # approved | resumed | aborted | timeout
    operator: str | None = None
    human_actions: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class SessionControl:
    def __init__(self, lease_ttl_s: float = 900):
        self.lease_ttl_s = lease_ttl_s
        self.state = Controller.AUTOMATION
        self.current: Intervention | None = None
        self.interventions: list[Intervention] = []
        self.on_human_action: Callable[[dict], None] | None = None
        self._cond = threading.Condition()
        self._lease: str | None = None
        self._lease_expires = 0.0
        self._granted: str | None = None  # one-shot approval for a named control

    # ---- automation side -----------------------------------------------------

    def is_automation(self) -> bool:
        with self._cond:
            return self.state == Controller.AUTOMATION

    def open(self, intervention: Intervention) -> None:
        with self._cond:
            if self.state != Controller.AUTOMATION:
                raise ControlError(f"cannot open an intervention while {self.state.value}")
            self.current = intervention
            self.interventions.append(intervention)
            self._move(Controller.AWAITING_HUMAN, "automation", intervention.reason)

    def wait(self, tick: Callable[[], None], timeout_s: float) -> str:
        """Block until an operator resolves the current intervention. `tick` keeps the browser
        pumping events meanwhile, which is what lets the human's actions be captured."""
        deadline = time.monotonic() + timeout_s
        while True:
            with self._cond:
                if self.current is not None and self.current.resolution:
                    return self.current.resolution
                if time.monotonic() >= deadline:
                    self.current.resolution = "timeout"
                    self._lease = None
                    self._move(Controller.AUTOMATION, "automation", "no operator acted in time")
                    return "timeout"
            tick()

    def verified(self, ok: bool, note: str) -> None:
        with self._cond:
            if self.state != Controller.VERIFYING:
                raise ControlError(f"nothing to verify while {self.state.value}")
            self._move(Controller.AUTOMATION, "automation", ("checkpoint met: " if ok else "checkpoint not met: ") + note)

    def consume_approval(self, request: ApprovalRequest) -> bool:
        """The policy gate's approver hook: true once, for the control an operator approved."""
        with self._cond:
            if self._granted is not None and self._granted == request.control:
                self._granted = None
                return True
            return False

    def record_human_action(self, action: dict) -> None:
        with self._cond:
            if self.state != Controller.HUMAN_CONTROL or self.current is None:
                return  # the automation's own clicks fire the same DOM events; only count the human's
            action = {"ts": _now(), **action}
            self.current.human_actions.append(action)
            callback = self.on_human_action
        if callback is not None:
            callback(action)

    # ---- operator side ------------------------------------------------------

    def approve(self, intervention_id: str, operator: str) -> Intervention:
        with self._cond:
            it = self._open_intervention(intervention_id)
            if it.kind != "approval":
                raise ControlError("only approval requests can be approved; take control instead")
            if self.state != Controller.AWAITING_HUMAN:
                raise ControlError(f"cannot approve while {self.state.value}")
            self._granted = it.control
            it.operator, it.resolution = operator, "approved"
            self._move(Controller.AUTOMATION, operator, f"approved {it.control!r}")
            return it

    def claim(self, intervention_id: str, operator: str) -> str:
        with self._cond:
            it = self._open_intervention(intervention_id)
            if self.state == Controller.HUMAN_CONTROL:
                if time.monotonic() < self._lease_expires:
                    raise ControlError(f"already claimed by {it.operator}")
                it.history.append({"ts": _now(), "event": "lease expired; re-claimed", "actor": operator})
            elif self.state != Controller.AWAITING_HUMAN:
                raise ControlError(f"cannot claim while {self.state.value}")
            self._lease = secrets.token_urlsafe(16)
            self._lease_expires = time.monotonic() + self.lease_ttl_s
            it.operator = operator
            if self.state == Controller.AWAITING_HUMAN:
                self._move(Controller.HUMAN_CONTROL, operator, "took control of the live session")
            return self._lease

    def resume(self, intervention_id: str, lease: str | None) -> Intervention:
        with self._cond:
            it = self._open_intervention(intervention_id)
            if self.state != Controller.HUMAN_CONTROL:
                raise ControlError(f"cannot resume while {self.state.value}")
            self._check_lease(lease)
            it.resolution = "resumed"
            self._lease = None
            self._move(Controller.VERIFYING, it.operator or "operator", "handed control back")
            return it

    def abort(self, intervention_id: str, operator: str, lease: str | None = None) -> Intervention:
        with self._cond:
            it = self._open_intervention(intervention_id)
            if self.state == Controller.HUMAN_CONTROL:
                self._check_lease(lease)
            it.operator, it.resolution = it.operator or operator, "aborted"
            self._lease = None
            self._move(Controller.AUTOMATION, operator, "aborted the run")
            return it

    def snapshot(self) -> dict:
        with self._cond:
            return {"state": self.state.value,
                    "current": self.current.to_dict() if self.current and not self.current.resolution else None,
                    "interventions": [i.to_dict() for i in self.interventions]}

    # ---- internals -------------------------------------------------------------

    def _open_intervention(self, intervention_id: str) -> Intervention:
        if self.current is None or self.current.id != intervention_id or self.current.resolution:
            raise ControlError(f"no open intervention {intervention_id!r}")
        return self.current

    def _check_lease(self, lease: str | None) -> None:
        if not lease or lease != self._lease:
            raise ControlError("only the operator holding the lease can do that")
        if time.monotonic() >= self._lease_expires:
            raise ControlError("lease expired; claim again")

    def _move(self, to: Controller, actor: str, note: str) -> None:
        if to not in ALLOWED[self.state]:
            raise ControlError(f"illegal transition {self.state.value} -> {to.value}")
        if self.current is not None:
            self.current.history.append({"ts": _now(), "from": self.state.value, "to": to.value,
                                         "actor": actor, "note": note})
        self.state = to
        self._cond.notify_all()
