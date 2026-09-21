"""Deterministic replay: run a capability artifact with no LLM in the decision loop.

Per step:
  1. observe, and classify the state (recoverable handlers first, then business
     outcomes, then hard failures) before touching anything;
  2. resolve the target by trying its strategies in order, requiring a unique match,
     and recording which one won (a fallback winning is a drift signal);
  3. refuse irreversible steps unless a human has approved;
  4. act;
  5. wait for the step's checkpoint on a condition, never a fixed sleep, re-classifying
     the state on every tick so "no such member" surfaces as a business outcome rather
     than a checkpoint timeout;
  6. verify the capability's success condition and return typed outputs.
"""

import re
import time
from typing import Any

from cua.artifact.schema import CapabilityArtifact, Condition, RecoverableHandler, Step
from cua.evidence import RunLog
from cua.redaction import Redactor
from cua.replay.result import (
    Recovery, ReplayBusinessOutcome, ReplayEscalated, ReplayFailure, ReplayResult, ReplaySuccess,
)
from cua.session import FormLogin
from cua.surface import (
    ApprovalRequired, PolicyBlocked, PolicyError, Snapshot, Surface, SurfaceError, TargetNotFound,
)
from cua.values import ParseError, parse_value

PARAM_RE = re.compile(r"\{\{inputs\.(\w+)\}\}")
STEP_TIMEOUT_S = 15.0
RESTART = object()  # sentinel: re-authenticated, run the flow again from the top


class ReplayExecutor:
    def __init__(self, surface: Surface, artifact: CapabilityArtifact, log: RunLog, redactor: Redactor,
                 session=None, step_timeout_s: float = STEP_TIMEOUT_S):
        # `surface` should be a GuardedSurface: policy (allowlist, irreversible approval)
        # is enforced there, not here, so replay and discovery cannot drift apart.
        self.surface = surface
        self.artifact = artifact
        self.log = log
        self.redactor = redactor
        self.session = session or FormLogin()
        self.step_timeout_s = step_timeout_s
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
        try:
            inputs = self._validate_inputs(raw_inputs)
        except ValueError as e:
            return self._fail("INPUT_INVALID", None, expected=str(e), observed=repr(raw_inputs))

        while True:
            result = self._execute(inputs)
            if result is RESTART:
                continue
            self.log.event("replay_finished", **result.model_dump(mode="json", exclude={"recovered", "strategies_used"}))
            self.log.write_json("result.json", result.model_dump(mode="json"))
            return result

    # ---- the flow --------------------------------------------------------

    def _execute(self, inputs: dict[str, Any]) -> ReplayResult | object:
        self._steps_done = 0
        outputs: dict[str, Any] = {}
        try:
            self.surface.goto(self.artifact.entry)
        except PolicyBlocked as e:
            return self._fail("POLICY_VIOLATION", None, expected=f"an allowlisted entry route", observed=e.reason)
        except SurfaceError as e:
            return self._fail("SESSION_FAILED", None, expected=f"entry {self.artifact.entry}", observed=str(e))

        for step in self.artifact.steps:
            snap, verdict = self._classified_state(step.id)
            if verdict is not None:
                return verdict
            outcome = self._run_step(step, inputs, outputs)
            if outcome is not None:
                return outcome
            self._steps_done += 1

        return self._verify_success(outputs)

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
            return self._escalate(f"step {step.id}: {e}", step.id)
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
                if self._restarts >= 1:
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
                "strategies_used": self._strategies}

    def _fail(self, code: str, step: str | None, expected: str | None = None, observed: str | None = None,
              screenshot: str | None = None, retryable: bool = False) -> ReplayFailure:
        return ReplayFailure(code=code, step=step, expected=expected, observed=observed,
                             screenshot=screenshot or self._shot(f"{step or 'run'}-{code.lower()}"),
                             retryable=retryable, **self._common())

    def _escalate(self, reason: str, step: str | None) -> ReplayEscalated:
        self.log.event("escalation_raised", step=step, reason=reason)
        return ReplayEscalated(reason=reason, step=step, screenshot=self._shot(f"{step or 'run'}-escalated"),
                               **self._common())

    def _shot(self, label: str) -> str | None:
        path = self.log.screenshot_path(re.sub(r"[^a-z0-9._-]+", "-", label.lower()))
        try:
            self.surface.screenshot(path)
        except Exception as e:  # evidence must never break a run
            self.log.event("screenshot_failed", error=str(e))
            return None
        return self.log.rel(path)
