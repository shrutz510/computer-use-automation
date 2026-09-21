"""Raising an intervention and waiting for a human, from the automation's side."""

import secrets
import sys

from cua.evidence import RunLog
from cua.handoff.control import Intervention, SessionControl, _now
from cua.redaction import Redactor
from cua.surface import Surface


class Handoff:
    def __init__(self, control: SessionControl, surface: Surface, log: RunLog, redactor: Redactor,
                 console_url: str | None = None, timeout_s: float = 900):
        self.control = control
        self.surface = surface
        self.log = log
        self.redactor = redactor
        self.console_url = console_url
        self.timeout_s = timeout_s
        control.on_human_action = lambda action: log.event(
            "human_action", actor="human", intervention=control.current.id if control.current else None, action=action)

    def escalate(self, kind: str, subject: str, step: str, reason: str, control: str | None = None) -> Intervention:
        """Pause automation on the same live session until an operator approves, takes
        control and resumes, or aborts. Returns the resolved intervention."""
        intervention = request_record(self.log, self.surface, self.redactor, kind, subject, step, reason, control)
        self.control.open(intervention)
        self.log.write_json(f"interventions/{intervention.id}.json", intervention.to_dict())
        self.log.event("intervention_opened", intervention=intervention.id, kind=kind, step=step,
                       reason=intervention.reason, console=self.console_url)
        print(f"\n>>> HUMAN NEEDED [{intervention.id}] {intervention.reason}\n"
              f"    open {self.console_url} to approve, take control of the live browser, or abort\n",
              file=sys.stderr, flush=True)

        resolution = self.control.wait(tick=lambda: self.surface.wait_ms(250), timeout_s=self.timeout_s)
        self.log.write_json(f"interventions/{intervention.id}.json", intervention.to_dict())
        self.log.event("intervention_resolved", intervention=intervention.id, resolution=resolution,
                       operator=intervention.operator, human_actions=len(intervention.human_actions))
        return intervention

    def verified(self, ok: bool, note: str) -> None:
        self.control.verified(ok, note)
        self.log.event("handoff_verified", ok=ok, note=note)
        it = self.control.current
        if it is not None:  # the record should end with the hand-back, not at "verifying"
            self.log.write_json(f"interventions/{it.id}.json", it.to_dict())


def request_record(log: RunLog, surface: Surface, redactor: Redactor, kind: str, subject: str, step: str,
                   reason: str, control: str | None = None) -> Intervention:
    """The intervention request itself: capability/goal, step, why, where, what it looks like,
    and what just happened. Also written when no operator is attached, as a queued request."""
    shot_path = log.screenshot_path(f"{step}-intervention")
    try:
        surface.screenshot(shot_path)
        shot = log.rel(shot_path)
    except Exception:  # evidence must never break a run
        shot = None
    try:
        where = "; ".join(f"{f.name or 'top'}:{f.url}" for f in surface.observe().frames)
    except Exception:
        where = "unknown"
    return Intervention(id=f"int-{secrets.token_hex(3)}", kind=kind, run_id=log.run_id, subject=subject, step=step,
                        reason=redactor.text(reason), url=where, screenshot=shot, recent_events=list(log.recent),
                        created_at=_now(), control=control)
