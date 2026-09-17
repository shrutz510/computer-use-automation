"""Discovery: the LLM-driven observe -> decide -> act loop."""

from datetime import datetime, timezone

from cua.agent.llm import Decider, DeciderError, ToolCall
from cua.agent.tools import SYSTEM_PROMPT, TOOLS
from cua.agent.trace import FrameState, Trace, TraceStep
from cua.evidence import RunLog
from cua.redaction import Redactor
from cua.surface import Snapshot, Surface, SurfaceError

MAX_REPEATS = 3          # same action on the same target this many times in a row -> stuck
MAX_NO_CHANGE = 4        # this many actions in a row without the screen changing -> stuck
MAX_CONSECUTIVE_ERRORS = 4


def _frames(snap: Snapshot) -> list[FrameState]:
    return [FrameState(name=f.name, url=f.url, title=f.title) for f in snap.frames]


def render_screen(snap: Snapshot, redactor: Redactor) -> str:
    lines: list[str] = []
    for frame in snap.frames:
        lines.append(f'== frame {frame.name or "(top)"}  url={frame.url}  title="{frame.title}"')
        controls = [e for e in snap.elements.values() if e.frame == frame.name and e.kind == "control"]
        cells = [e for e in snap.elements.values() if e.frame == frame.name and e.kind == "cell"]
        for e in controls:
            desc = f'[{e.ref}] {e.role}'
            if e.name:
                desc += f' "{e.name}"'
            if e.label and e.label != e.name:
                desc += f' label="{e.label}"'
            if e.options:
                desc += f" options={e.options}"
            lines.append("  " + desc)
        for e in cells:
            where = ", ".join(p for p in (f'row "{e.row_header}"' if e.row_header else "",
                                          f'column "{e.col_header}"' if e.col_header else "") if p)
            text = "[REDACTED]" if e.sensitive else e.text
            lines.append(f'  [{e.ref}] cell "{text}"' + (f"  ({where})" if where else ""))
        lines.append(f"  text: {frame.text[:1500]}")
    return redactor.text("\n".join(lines))


class DiscoveryAgent:
    def __init__(self, surface: Surface, decider: Decider, log: RunLog, redactor: Redactor, max_steps: int = 25):
        self.surface = surface
        self.decider = decider
        self.log = log
        self.redactor = redactor
        self.max_steps = max_steps

    def run(self, goal: str, app: str, base_url: str, entry_route: str) -> Trace:
        trace = Trace(run_id=self.log.run_id, goal=goal, app=app, base_url=base_url, entry_route=entry_route,
                      model=self.decider.model, started_at=datetime.now(timezone.utc).isoformat())
        self.log.event("discovery_started", goal=goal, app=app, entry_route=entry_route, model=self.decider.model)
        history: list[str] = []
        recent_actions: list[tuple] = []
        no_change = errors = 0

        snap = self.surface.observe()
        for i in range(1, self.max_steps + 1):
            self.redactor.learn(snap.sensitive_values())
            prompt = (f"GOAL: {goal}\nSTEP {i} of at most {self.max_steps}\n\nACTIONS SO FAR:\n"
                      + ("\n".join(history) or "  (none)") + "\n\nCURRENT SCREEN:\n" + render_screen(snap, self.redactor))
            try:
                call = self.decider.decide(SYSTEM_PROMPT, prompt, TOOLS)
            except DeciderError as e:
                return self._finish(trace, "failed", f"LLM error: {e}")
            self.log.event("decision", step=i, tool=call.name, args=call.args, screen=snap.fingerprint)

            if call.name == "done":
                self._shot(trace, "done")
                return self._finish(trace, "success", call.args.get("summary", ""))
            if call.name == "escalate":
                self._shot(trace, "escalate")
                return self._finish(trace, "escalated", call.args.get("reason", ""))

            key = (call.name, call.args.get("ref") and snap.elements.get(call.args["ref"]) and
                   snap.elements[call.args["ref"]].css_path, call.args.get("text") or call.args.get("option"))
            recent_actions = (recent_actions + [key])[-MAX_REPEATS:]
            if len(recent_actions) == MAX_REPEATS and len(set(recent_actions)) == 1:
                self._shot(trace, "stuck")
                return self._finish(trace, "escalated", f"stuck: repeated {call.name} on the same target {MAX_REPEATS} times")

            step, after = self._act(i, call, snap)
            trace.steps.append(step)
            history.append(self._history_line(step, snap, after))
            errors = 0 if step.ok else errors + 1
            if errors >= MAX_CONSECUTIVE_ERRORS:
                return self._finish(trace, "escalated", f"stuck: {errors} failed actions in a row, last: {step.error}")
            no_change = no_change + 1 if (after.fingerprint == snap.fingerprint and call.name != "extract") else 0
            if no_change >= MAX_NO_CHANGE:
                return self._finish(trace, "escalated", f"stuck: screen unchanged after {no_change} actions")
            snap = after

        self._shot(trace, "max-steps")
        return self._finish(trace, "escalated", f"stuck: reached max steps ({self.max_steps})")

    def _act(self, i: int, call: ToolCall, snap: Snapshot) -> tuple[TraceStep, Snapshot]:
        ref = call.args.get("ref", "")
        step = TraceStep(index=i, tool=call.name, rationale=call.args.get("rationale", ""),
                         param_name=call.args.get("param_name"), frames_before=_frames(snap))
        element = snap.elements.get(ref)
        if element is None:
            step.ok, step.error = False, f"unknown ref {ref!r}"
            self.log.event("action_error", step=i, error=step.error)
            return step, snap
        step.element_role = element.role
        try:
            step.target = self.surface.describe(ref)
            handle = self.surface.handle_for_ref(ref)
            if call.name == "click":
                self.surface.click(handle)
            elif call.name == "type_text":
                step.value = call.args.get("text", "")
                self.surface.fill(handle, step.value)
            elif call.name == "select_option":
                step.value = call.args.get("option", "")
                self.surface.select(handle, step.value)
            elif call.name == "extract":
                step.output_name, step.output_type = call.args.get("output_name"), call.args.get("output_type", "string")
                step.value = self.surface.read(handle)
                if element.sensitive:
                    self.redactor.learn([step.value])
            else:
                raise SurfaceError(f"unknown tool {call.name!r}")
        except SurfaceError as e:
            step.ok, step.error = False, str(e)
        after = self.surface.observe()
        step.frames_after = _frames(after)
        step.screenshot = self._shot(None, f"step{i:02d}-{call.name}")
        self.log.event("action", step=i, tool=call.name, target=step.target.model_dump() if step.target else None,
                       value=step.value, param_name=step.param_name, output_name=step.output_name,
                       ok=step.ok, error=step.error, rationale=step.rationale, screen_after=after.fingerprint)
        return step, after

    def _history_line(self, step: TraceStep, before: Snapshot, after: Snapshot) -> str:
        target = step.target.description if step.target else "?"
        line = f"  {step.index}. {step.tool} {target}"
        if step.value is not None:
            line += f' value="{step.value}"'
        if not step.ok:
            line += f"  => FAILED: {step.error}"
        elif after.fingerprint == before.fingerprint:
            line += "  => ok (screen unchanged)"
        else:
            line += "  => ok, now at " + ", ".join(f"{f.name or 'top'}:{f.url}" for f in after.frames)
        return self.redactor.text(line)

    def _shot(self, trace: Trace | None, label: str) -> str | None:
        path = self.log.screenshot_path(label)
        try:
            self.surface.screenshot(path)
        except Exception as e:  # evidence must never break the run
            self.log.event("screenshot_failed", error=str(e))
            return None
        return self.log.rel(path)

    def _finish(self, trace: Trace, status: str, reason: str) -> Trace:
        trace.status = status  # type: ignore[assignment]
        trace.reason = reason
        trace.finished_at = datetime.now(timezone.utc).isoformat()
        trace.outputs = {s.output_name: s.value for s in trace.steps if s.tool == "extract" and s.ok and s.output_name and s.value is not None}
        self.log.event("discovery_finished", status=status, reason=reason, outputs=trace.outputs)
        self.log.write_json("trace.json", trace.model_dump())
        return trace
