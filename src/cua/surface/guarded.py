"""The policy enforcement point.

Everything that acts on the app (the discovery agent, the session provider, replay) goes
through a GuardedSurface, so the LLM can ask for anything and this layer decides. Two layers:
  1. every action is checked before it runs: action type, where a link or submit would go,
     and the control's risk (irreversible -> block / require approval / flag);
  2. the browser aborts any request outside the allowlist (WebSurface request_policy),
     which catches server redirects and script-driven navigation the first layer cannot see.
"""

from pathlib import Path
from typing import Callable

from cua.evidence import RunLog
from cua.policy import Action, Policy, highest
from cua.surface.base import (
    ApprovalRequest, ApprovalRequired, ControlInfo, Handle, PolicyBlocked, Resolved, Snapshot, SurfaceError,
)
from cua.surface.targets import Target
from cua.surface.web import WebSurface

Approver = Callable[[ApprovalRequest], bool]


class GuardedSurface:
    def __init__(self, inner: WebSurface, policy: Policy, log: RunLog | None = None, approver: Approver | None = None,
                 control=None):
        self.inner = inner
        self.policy = policy
        self.log = log
        self.approver = approver
        # cua.handoff.SessionControl, when a human can take over: automation may only act
        # while it holds control of the session.
        self.control = control

    # ---- checked actions ------------------------------------------------------

    def goto(self, route: str) -> None:
        self._require(action="navigate")
        reason = self.policy.route_violation(self.inner.base_url + route)
        if reason:
            self._blocked("navigate", route, reason)
        self._guarded(lambda: self.inner.goto(route))

    def reload(self) -> None:
        self._require(action="navigate")
        self._guarded(self.inner.reload)

    def click(self, handle: Handle, risk_hint: str = "safe") -> None:
        self._require(action="click")
        info = self.inner.inspect(handle)
        if info.destination:
            reason = self.policy.route_violation(info.destination)
            if reason:
                self._blocked("click", info.name, reason)
        risk = highest([risk_hint, self.policy.click_risk(info.name, info.form_method)])
        if risk == "irreversible":
            self._gate_irreversible(info, risk_hint)
        self._guarded(lambda: self.inner.click(handle))

    def fill(self, handle: Handle, text: str) -> None:
        self._require(action="type")
        self._guarded(lambda: self.inner.fill(handle, text))

    def select(self, handle: Handle, option: str) -> None:
        self._require(action="select")
        self._guarded(lambda: self.inner.select(handle, option))

    def read(self, handle: Handle) -> str:
        self._require(action="extract")
        return self.inner.read(handle)

    # ---- pass-through (observation and targeting change nothing) -------------

    @property
    def base_url(self) -> str:
        return self.inner.base_url

    def observe(self) -> Snapshot:
        return self.inner.observe()

    def handle_for_ref(self, ref: str) -> Handle:
        return self.inner.handle_for_ref(ref)

    def describe(self, ref: str) -> Target:
        return self.inner.describe(ref)

    def resolve(self, target: Target, timeout_s: float = 0) -> Resolved:
        return self.inner.resolve(target, timeout_s)

    def inspect(self, handle: Handle) -> ControlInfo:
        return self.inner.inspect(handle)

    def wait_ms(self, ms: int) -> None:
        self.inner.wait_ms(ms)

    def screenshot(self, path: Path) -> None:
        self.inner.screenshot(path)

    # ---- internals ---------------------------------------------------------

    def _require(self, action: Action) -> None:
        if self.control is not None and not self.control.is_automation():
            self._blocked(action, "", f"automation is not in control of the session ({self.control.state.value})")
        reason = self.policy.action_violation(action)
        if reason:
            self._blocked(action, "", reason)

    def _guarded(self, act: Callable[[], None]) -> None:
        """Run an action, then fail it if the network layer refused anything it caused."""
        seen = len(self.inner.violations)
        try:
            act()
        except SurfaceError:
            if len(self.inner.violations) == seen:
                raise
        refused = self.inner.violations[seen:]
        if refused:
            self._blocked("request", "", "; ".join(refused))

    def _gate_irreversible(self, info: ControlInfo, risk_hint: str) -> None:
        why = "declared irreversible in the artifact" if risk_hint == "irreversible" else "matches the irreversible-name rule"
        request = ApprovalRequest(action="click", control=info.name, risk="irreversible", reason=why, url=info.frame_url)
        mode = self.policy.irreversible
        if mode == "block":
            self._blocked("click", info.name, f"irreversible actions are blocked by policy ({why})")
        if mode == "flag":
            self._event("irreversible_flagged", control=info.name, reason=why)
            return
        self._event("approval_requested", control=info.name, reason=why, url=info.frame_url)
        if self.approver is not None and self.approver(request):
            self._event("approval_granted", control=info.name)
            return
        self._event("approval_missing", control=info.name)
        raise ApprovalRequired(request)

    def _blocked(self, action: str, target: str, reason: str) -> None:
        self._event("policy_blocked", action=action, target=target, reason=reason)
        raise PolicyBlocked(f"{action} {target!r}: {reason}" if target else f"{action}: {reason}")

    def _event(self, type_: str, **data) -> None:
        if self.log is not None:
            self.log.event(type_, policy=f"{self.policy.policy}@{self.policy.version}", **data)
