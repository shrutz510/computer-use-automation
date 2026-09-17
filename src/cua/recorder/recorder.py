"""Recorder: a successful discovery trace becomes a capability artifact.

What it does beyond copying steps:
  * parameterizes typed values the model flagged (member_id -> "{{inputs.member_id}}")
    and infers each input's pattern from the example, so nothing concrete is stored in a step;
  * canonicalizes routes that contain a parameter value (/members/10023 -> ^/members/[^/]+$),
    which is what lets one recording serve every member;
  * attaches a checkpoint to each step from the state the app actually reached;
  * drops failed actions: the model's dead ends are evidence, not part of the contract;
  * copies the app's detector pack in, so the error taxonomy ships with the artifact.
"""

import re
from datetime import datetime, timezone

from cua.agent.trace import FrameState, Trace
from cua.artifact.schema import (
    AppPack, Capability, CapabilityArtifact, Condition, InputSpec, OutputSpec, Provenance, Step, Success,
)
from cua.policy import click_risk, highest
from cua.values import OutputType

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "for", "to", "their", "them", "this", "that", "its", "his", "her",
    "in", "on", "at", "with", "from", "into", "then", "please", "me", "my", "look", "up", "read", "get",
    "find", "show", "check", "fetch", "current", "reach", "screen", "page",
}
ACTION_BY_TOOL = {"type_text": "type", "select_option": "select", "click": "click", "extract": "extract"}


def derive_name(goal: str, max_words: int = 4) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", goal.lower()) if w not in STOPWORDS and not w.isdigit()]
    return "_".join(words[:max_words]) or "capability"


def _slug(text: str, limit: int = 24) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")[:limit] or "step"


def _input_spec(value: str) -> InputSpec:
    if re.fullmatch(r"\d+", value):
        return InputSpec(type="string", pattern=f"^\\d{{{len(value)}}}$", example=value)
    if re.fullmatch(r"\$?[\d,]+(\.\d{2})?", value):
        return InputSpec(type="decimal", example=value)
    return InputSpec(type="string", example=value)


def _changed_frames(before: list[FrameState], after: list[FrameState]) -> list[FrameState]:
    was = {f.name: (f.url, f.title) for f in before}
    return [f for f in after if was.get(f.name) != (f.url, f.title)]


def _canonical_url(url: str, values: list[str]) -> str:
    """/members/10023 -> ^/members/[^/]+$ so the checkpoint holds for any member."""
    marked = url
    for value in values:
        if value:
            marked = marked.replace(value, "\x00")
    return "^" + "[^/]+".join(re.escape(part) for part in marked.split("\x00")) + "$"


def _checkpoint(before: list[FrameState], after: list[FrameState], values: list[str]) -> Condition | None:
    changed = _changed_frames(before, after)
    if not changed:
        return None  # e.g. typing into a field: nothing to verify beyond the field itself
    frame = changed[0]
    return Condition(frame=frame.name, title=frame.title or None, url_matches=_canonical_url(frame.url, values))


def record(trace: Trace, pack: AppPack, name: str | None = None, version: int = 1,
           evidence_path: str = "") -> CapabilityArtifact:
    if trace.status != "success":
        raise ValueError(f"only a successful run can be recorded (status={trace.status})")

    steps: list[Step] = []
    inputs: dict[str, InputSpec] = {}
    outputs: dict[str, OutputSpec] = {}
    risks: list[str] = []
    last_checkpoint: Condition | None = None

    for step in trace.steps:
        if not step.ok or step.tool not in ACTION_BY_TOOL or step.target is None:
            continue
        action = ACTION_BY_TOOL[step.tool]
        value = step.value
        if action in ("type", "select"):
            if step.param_name:
                inputs.setdefault(step.param_name, _input_spec(step.value or ""))
                value = f"{{{{inputs.{step.param_name}}}}}"
        elif action == "extract":
            value = None

        risk = click_risk(step.element_name or "", step.element_form_method or "") if action == "click" else "safe"
        risks.append(risk)
        step_id = f"s{len(steps) + 1}_{action}_{_slug(step.element_name or step.param_name or step.output_name or '')}"
        checkpoint = _checkpoint(step.frames_before, step.frames_after,
                                 [s.value for s in trace.steps if s.param_name and s.value])
        if action == "extract" and step.output_name:
            outputs[step.output_name] = OutputSpec(type=_output_type(step.output_type), sensitive=step.element_sensitive,
                                                   source_step=step_id)
        steps.append(Step(id=step_id, action=action, target=step.target, value=value,
                          into=f"outputs.{step.output_name}" if action == "extract" and step.output_name else None,
                          parse=_output_type(step.output_type) if action == "extract" else None,
                          risk=risk, checkpoint=checkpoint))
        last_checkpoint = checkpoint or last_checkpoint

    if not steps:
        raise ValueError("trace has no successful actions to record")

    # The description is the goal with the concrete values it happened to use swapped for
    # their parameters, so it describes the capability rather than one invocation of it.
    description = trace.goal
    for step in trace.steps:
        if step.param_name and step.value:
            description = description.replace(step.value, f"{{{{inputs.{step.param_name}}}}}")

    capability = Capability(
        id=f"{pack.app.key}.{name or derive_name(trace.goal)}",
        name=name or derive_name(trace.goal),
        version=version,
        status="draft",
        description=description,
        app=pack.app,
        risk_level=highest(risks),
        provenance=Provenance(recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), goal=trace.goal,
                              discovery_run=trace.run_id, model=trace.model,
                              evidence_path=evidence_path or f"evidence/discovery/{trace.run_id}"),
    )
    return CapabilityArtifact(
        capability=capability, entry=trace.entry_route, inputs=inputs, outputs=outputs, steps=steps,
        success=Success(checkpoint=last_checkpoint, outputs_present=list(outputs)),
        detectors_pack=f"{pack.pack}@{pack.version}", detectors=pack.detectors,
    )


def _output_type(value: str | None) -> OutputType:
    return value if value in ("string", "decimal", "integer", "date") else "string"  # type: ignore[return-value]
