"""Deterministic replay: run a capability artifact with no LLM in the decision loop.

Per step:
  1. observe, and classify the state (recoverable handlers first, then business
     outcomes, then hard failures) before touching anything;
  2. resolve the target by trying its strategies in order, requiring a unique match,
     and recording which one won (a fallback winning is a drift signal);
  3. act, through the policy gate (which holds irreversible steps for a human's approval);
  4. wait for the step's checkpoint on a condition, never a fixed sleep, re-classifying
     the state on every tick so "no such member" surfaces as a business outcome rather
     than a checkpoint timeout;
  5. verify the capability's success condition and return typed outputs.

When a human is attached (a Handoff), a step that needs approval, or that is stuck (target not
found, checkpoint not reached), becomes an intervention on the same live session instead of a
result: the operator approves, or takes control and hands it back, and replay re-verifies the
state before carrying on. Without one, the caller gets `escalated` / `failed` as usual.
"""

import re
import time
from dataclasses import dataclass
from typing import Any

from cua.artifact.schema import CapabilityArtifact, Condition, RecoverableHandler, Step
from cua.evidence import RunLog, capture
from cua.handoff import Handoff, request_record
from cua.redaction import Redactor
from cua.replay.result import (
    HandoffSummary, Recovery, ReplayBusinessOutcome, ReplayEscalated, ReplayFailure, ReplayResult, ReplaySuccess,
)
from cua.session import FormLogin
from cua.surface import (
    ApprovalRequired, PolicyBlocked, PolicyError, Snapshot, Surface, SurfaceError, TargetNotFound,
)
from cua.values import ParseError, parse_value

PARAM_RE = re.compile(r"\{\{inputs\.(\w+)\}\}")
STEP_TIMEOUT_S = 15.0
RESTART = object()  # sentinel: re-authenticated, run the flow again from the top
STUCK_CODES = {"LOCATOR_NOT_FOUND", "CHECKPOINT_FAILED"}  # states a human at the screen can fix


@dataclass
class _NeedsHuman:
    kind: str                            # "approval" | "stuck"
    reason: str
    control: str | None = None           # for approvals: the control awaiting approval
    failure: ReplayFailure | None = None  # for stuck: what the caller gets if nobody helps


class ReplayExecutor:
    def __init__(self, surface: Surface, artifact: CapabilityArtifact, log: RunLog, redactor: Redactor,
                 session=None, step_timeout_s: float = STEP_TIMEOUT_S, handoff: Handoff | None = None,
                 max_handoffs: int = 3):
        # `surface` should be a GuardedSurface: policy (allowlist, irreversible approval)
        # is enforced there, not here, so replay and discovery cannot drift apart.
        self.surface = surface
        self.artifact = artifact
        self.log = log
        self.redactor = redactor
        self.session = session or FormLogin()
        self.step_timeout_s = step_timeout_s
        self.handoff = handoff
        self.max_handoffs = max_handoffs
        self._handoffs: list[HandoffSummary] = []
        self._recovered: list[Recovery] = []
        self._handler_counts: dict[str, int] = {}
        self._strategies: dict[str, str] = {}
        self._steps_done = 0
        self._restarts = 0

    # ---- public ----------------------------------------------------------

    def run(self, raw_inputs: dict[str, str]) -> ReplayResult:
        cap = self.artifact.capability
        self.log.event("replay_started", capability=cap.id, version=cap.version, inputs=raw_inputs,
                       artifact_status=cap.status, risk_level=cap.risk_level)
        result = self._run(raw_inputs)
        # Every terminal status leaves the same evidence behind, rejected inputs included.
        self.log.event("replay_finished",
                       **result.model_dump(mode="json", exclude={"recovered", "strategies_used", "handoffs"}))
        self.log.write_json("result.json", result.model_dump(mode="json"))
        return result

    def _run(self, raw_inputs: dict[str, str]) -> ReplayResult:
        try:
            inputs = self._validate_inputs(raw_inputs)
        except ValueError as e:
            return self._fail("INPUT_INVALID", None, expected=str(e), observed=repr(raw_inputs))
        while True:
            result = self._execute(inputs)
            if result is not RESTART:
                return result

    # ---- the flow --------------------------------------------------------

    def _execute(self, inputs: dict[str, Any]) -> ReplayResult | object:
        self._steps_done = 0
        outputs: dict[str, Any] = {}
        try:
            self.surface.goto(self.artifact.entry)
        except PolicyBlocked as e:
            return self._fail("POLICY_VIOLATION", None, expected="an allowlisted entry route", observed=e.reason)
        except SurfaceError as e:
            return self._fail("SESSION_FAILED", None, expected=f"entry {self.artifact.entry}", observed=str(e))

        for step in self.artifact.steps:
            outcome = self._run_with_handoff(step, inputs, outputs)
            if outcome is not None:
                return outcome
            self._steps_done += 1

        return self._verify_success(outputs)

    def _run_with_handoff(self, step: Step, inputs: dict[str, Any], outputs: dict[str, Any]) -> ReplayResult | object | None:
        while True:
            _, verdict = self._classified_state(step.id)
            if verdict is not None:
                return verdict
            outcome = self._run_step(step, inputs, outputs)
            needs = self._needs_human(outcome)
            if needs is None:
                return outcome
            if self.handoff is None or len(self._handoffs) >= self.max_handoffs:
                if needs.failure is not None:
                    return needs.failure
                return self._escalate(needs.reason, step.id, kind=needs.kind)

            it = self.handoff.escalate(needs.kind, self.artifact.capability.id, step.id, needs.reason, needs.control)
            self._handoffs.append(HandoffSummary(id=it.id, kind=it.kind, step=step.id, resolution=it.resolution or "",
                                                 operator=it.operator, human_actions=len(it.human_actions)))
            if it.resolution == "approved":
                continue  # retry: the gate lets the approved control through exactly once
            if it.resolution == "resumed":
                if self._verify_after_human(step):
                    return None  # the human completed this step; carry on from the next one
                continue  # not done: retry it (an irreversible step asks for approval again)
            return self._escalated_result(f"operator {it.resolution} intervention {it.id}: {needs.reason}",
                                          step.id, intervention_id=it.id, screenshot=it.screenshot)

    def _needs_human(self, outcome) -> _NeedsHuman | None:
        if isinstance(outcome, _NeedsHuman):
            return outcome
        if isinstance(outcome, ReplayFailure) and outcome.code in STUCK_CODES:
            return _NeedsHuman("stuck", f"{outcome.code}: expected {outcome.expected}; observed {outcome.observed}",
                               failure=outcome)
        return None

    def _verify_after_human(self, step: Step) -> bool:
        """VERIFYING: look, do not act. Did the human leave the app where this step should have?"""
        snap = self.surface.observe()
        done = step.checkpoint is not None and self._matches(step.checkpoint, snap)
        self.handoff.verified(done, f"{step.id} checkpoint {self._describe(step.checkpoint)}; "
                                    f"observed {self._describe_state(snap)}")
        return done

    def _run_step(self, step: Step, inputs: dict[str, Any], outputs: dict[str, Any]) -> ReplayResult | object | None:
        try:
            resolved = self.surface.resolve(step.target, timeout_s=self.step_timeout_s)
        except TargetNotFound as e:
            return self._fail("LOCATOR_NOT_FOUND", step.id, expected=step.target.description,
                              observed="; ".join(e.attempts), retryable=False)
        self._strategies[step.id] = f"{resolved.strategy.by}#{resolved.strategy_index}"
        if resolved.strategy_index > 0:
            self.log.event("drift_signal", step=step.id, used=self._strategies[step.id],
                           note="a fallback locator won; the preferred strategy no longer matches")

        value = None
        try:
            if step.action == "type":
                value = self._substitute(step.value or "", inputs)
                self.surface.fill(resolved.handle, value)
            elif step.action == "select":
                value = self._substitute(step.value or "", inputs)
                self.surface.select(resolved.handle, value)
            elif step.action == "click":
                self.surface.click(resolved.handle, risk_hint=step.risk)
            elif step.action == "extract":
                name = (step.into or "").removeprefix("outputs.")
                text = self.surface.read(resolved.handle)
                spec = self.artifact.outputs.get(name)
                if spec and spec.sensitive:
                    self.redactor.learn([text])
                try:
                    outputs[name] = parse_value(text, step.parse or "string")
                except ParseError as e:
                    return self._fail("PARSE_ERROR", step.id, expected=f"{name}: {step.parse}", observed=str(e),
                                      screenshot=self._shot(f"{step.id}-parse-error"))
        except KeyError as e:
            return self._fail("INPUT_INVALID", step.id, expected=f"input {e}", observed="not supplied")
        except ApprovalRequired as e:
            return _NeedsHuman("approval", f"step {step.id}: {e}", control=e.request.control)
        except PolicyBlocked as e:
            return self._fail("POLICY_VIOLATION", step.id, expected=f"{step.action} within policy",
                              observed=e.reason, screenshot=self._shot(f"{step.id}-policy"))
        except SurfaceError as e:
            return self._fail("ACTION_FAILED", step.id, expected=f"{step.action} {step.target.description}",
                              observed=str(e), screenshot=self._shot(f"{step.id}-action-failed"), retryable=True)

        self.log.event("step", step=step.id, action=step.action, value=value, risk=step.risk,
                       strategy=self._strategies[step.id], screenshot=self._shot(step.id))
        if step.checkpoint is not None:
            return self._await_checkpoint(step)
        return None

    def _await_checkpoint(self, step: Step) -> ReplayResult | object | None:
        deadline = time.monotonic() + self.step_timeout_s
        while True:
            snap, verdict = self._classified_state(step.id)
            if verdict is not None:
                return verdict
            if self._matches(step.checkpoint, snap):
                return None
            if time.monotonic() >= deadline:
                return self._fail("CHECKPOINT_FAILED", step.id, expected=self._describe(step.checkpoint),
                                  observed=self._describe_state(snap), screenshot=self._shot(f"{step.id}-checkpoint"))
            self.surface.wait_ms(200)

    def _verify_success(self, outputs: dict[str, Any]) -> ReplayResult:
        success = self.artifact.success
        snap = self.surface.observe()
        if success.checkpoint is not None and not self._matches(success.checkpoint, snap):
            return self._fail("CHECKPOINT_FAILED", "success", expected=self._describe(success.checkpoint),
                              observed=self._describe_state(snap), screenshot=self._shot("success-checkpoint"))
        missing = [name for name in success.outputs_present if name not in outputs]
        if missing:
            return self._fail("OUTPUT_MISSING", "success", expected=f"outputs {success.outputs_present}",
                              observed=f"missing {missing}")
        return ReplaySuccess(outputs=outputs, **self._common())

    # ---- state classification -------------------------------------------

    def _classified_state(self, step_id: str) -> tuple[Snapshot, ReplayResult | object | None]:
        """Observe, running recoverable handlers until the state is stable, then check
        the business-outcome and hard-failure detectors."""
        while True:
            snap = self.surface.observe()
            handler = self._pending_handler(snap)
            if handler is None:
                break
            outcome = self._recover(handler, step_id)
            if outcome is not None:
                return snap, outcome

        for outcome in self.artifact.detectors.business_outcomes:
            if self._matches(outcome.when, snap):
                message = self._message(snap, outcome.when.text_visible or outcome.code)
                self.log.event("business_outcome", step=step_id, code=outcome.code, message=message,
                               screenshot=self._shot(f"{step_id}-{outcome.code.lower()}"))
                return snap, ReplayBusinessOutcome(code=outcome.code, message=message, step=step_id, **self._common())
        for failure in self.artifact.detectors.hard_failures:
            if self._matches(failure.when, snap):
                return snap, self._fail(failure.code, step_id, expected="a normal application screen",
                                        observed=self._message(snap, failure.when.text_visible or failure.code),
                                        screenshot=self._shot(f"{step_id}-{failure.code.lower()}"),
                                        retryable=failure.retryable)
        return snap, None

    def _pending_handler(self, snap: Snapshot) -> RecoverableHandler | None:
        for handler in self.artifact.detectors.recoverable:
            if self._handler_counts.get(handler.id, 0) >= handler.max_times:
                continue
            if self._matches(handler.when, snap):
                return handler
        return None

    def _recover(self, handler: RecoverableHandler, step_id: str) -> ReplayResult | object | None:
        self._handler_counts[handler.id] = self._handler_counts.get(handler.id, 0) + 1
        detail = ""
        before = self._shot(f"{step_id}-{handler.id}-detected")  # evidence of the state we recovered from
        try:
            if handler.do == "click" and handler.target is not None:
                self.surface.click(self.surface.resolve(handler.target, timeout_s=5).handle)
            elif handler.do == "reload":
                self.surface.reload()
            elif handler.do == "reauthenticate":
                if self.artifact.capability.risk_level == "irreversible":
                    return self._escalate(f"session expired during an irreversible flow ({handler.id})", step_id)
                if self._restarts >= 1:  # reachable when a pack allows more than one re-auth
                    return self._fail("SESSION_FAILED", step_id, expected="an authenticated session",
                                      observed="session expired again after re-authenticating")
                self.session.sign_in(self.surface)
                self._restarts += 1
                detail = "re-authenticated and restarted the flow"
            elif handler.do == "escalate":
                return self._escalate(f"handler {handler.id} requires a human", step_id)
        except PolicyError as e:  # a recovery is an action like any other: same gate
            return self._fail("POLICY_VIOLATION", step_id, expected=f"recovery {handler.id} within policy",
                              observed=str(e))
        except (SurfaceError, TargetNotFound) as e:
            return self._fail("ACTION_FAILED", step_id, expected=f"recovery {handler.id}", observed=str(e),
                              screenshot=self._shot(f"{step_id}-recovery-failed"), retryable=True)

        self._recovered.append(Recovery(handler=handler.id, step=step_id, detail=detail))
        self.log.event("recovered", step=step_id, handler=handler.id, do=handler.do, detail=detail,
                       times=self._handler_counts[handler.id], screenshot=before)
        return RESTART if handler.do == "reauthenticate" else None

    # ---- conditions ------------------------------------------------------

    def _matches(self, condition: Condition | None, snap: Snapshot) -> bool:
        if condition is None:
            return True
        for frame in snap.frames:
            if condition.frame is not None and frame.name != condition.frame:
                continue
            if condition.title is not None and frame.title != condition.title:
                continue
            if condition.text_visible is not None and condition.text_visible.lower() not in frame.text.lower():
                continue
            if condition.url_matches is not None and not re.search(condition.url_matches, frame.url):
                continue
            return True
        return False

    def _message(self, snap: Snapshot, needle: str) -> str:
        for frame in snap.frames:
            idx = frame.text.lower().find(needle.lower())
            if idx >= 0:
                return self.redactor.text(frame.text[idx:idx + 160].strip())
        return needle

    @staticmethod
    def _describe(condition: Condition | None) -> str:
        if condition is None:
            return "(none)"
        parts = [f"{k}={v!r}" for k, v in condition.model_dump(exclude_none=True).items()]
        return ", ".join(parts) or "(any state)"

    def _describe_state(self, snap: Snapshot) -> str:
        return self.redactor.text("; ".join(f'{f.name or "top"} url={f.url} title="{f.title}"' for f in snap.frames))

    # ---- inputs ----------------------------------------------------------

    def _validate_inputs(self, raw: dict[str, str]) -> dict[str, Any]:
        unknown = set(raw) - set(self.artifact.inputs)
        if unknown:
            raise ValueError(f"unknown inputs: {sorted(unknown)}")
        values: dict[str, Any] = {}
        for name, spec in self.artifact.inputs.items():
            if name not in raw or raw[name] == "":
                if spec.required:
                    raise ValueError(f"missing required input {name!r}")
                continue
            text = raw[name]
            if spec.pattern and not re.fullmatch(spec.pattern, text):
                raise ValueError(f"input {name!r}={text!r} does not match {spec.pattern}")
            if spec.type in ("decimal", "integer"):
                try:
                    parse_value(text, spec.type)
                except ParseError as e:
                    raise ValueError(str(e)) from e
            if spec.sensitive:
                self.redactor.learn([text])
            values[name] = text
        return values

    def _substitute(self, template: str, inputs: dict[str, Any]) -> str:
        return PARAM_RE.sub(lambda m: str(inputs[m.group(1)]), template)

    # ---- results ---------------------------------------------------------

    def _common(self) -> dict[str, Any]:
        return {"run_id": self.log.run_id, "capability": self.artifact.capability.id,
                "version": self.artifact.capability.version, "evidence": str(self.log.dir),
                "steps_completed": self._steps_done, "recovered": self._recovered,
                "strategies_used": self._strategies, "handoffs": self._handoffs}

    def _fail(self, code: str, step: str | None, expected: str | None = None, observed: str | None = None,
              screenshot: str | None = None, retryable: bool = False) -> ReplayFailure:
        return ReplayFailure(code=code, step=step, expected=expected, observed=observed,
                             screenshot=screenshot or self._shot(f"{step or 'run'}-{code.lower()}"),
                             retryable=retryable, **self._common())

    def _escalate(self, reason: str, step: str | None, kind: str = "stuck") -> ReplayEscalated:
        """No operator attached: write the intervention request anyway, as a queued request an
        operator system could pick up, and return `escalated` pointing at it."""
        it = request_record(self.log, self.surface, self.redactor, kind, self.artifact.capability.id,
                            step or "run", reason)
        self.log.write_json(f"interventions/{it.id}.json", it.to_dict())
        self.log.event("escalation_raised", step=step, reason=reason, intervention=it.id, operator_attached=False)
        return self._escalated_result(reason, step, intervention_id=it.id, screenshot=it.screenshot)

    def _escalated_result(self, reason: str, step: str | None, intervention_id: str | None = None,
                          screenshot: str | None = None) -> ReplayEscalated:
        return ReplayEscalated(reason=reason, step=step, intervention_id=intervention_id,
                               screenshot=screenshot or self._shot(f"{step or 'run'}-escalated"), **self._common())

    def _shot(self, label: str) -> str | None:
        return capture(self.log, self.surface, label)
