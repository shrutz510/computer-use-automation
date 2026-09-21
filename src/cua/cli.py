"""Command line entry point: `cua discover | replay | operator | record | schema`."""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Browser, Playwright, sync_playwright

from cua.agent import DiscoveryAgent
from cua.agent.llm import GeminiDecider
from cua.agent.trace import Trace
from cua.artifact import store
from cua.evidence import RunLog
from cua.handoff import Handoff, OperatorConsole, SessionControl, install_human_capture
from cua.handoff.simulate import OperatorError, run_operator
from cua.policy import Policy, load_policy
from cua.recorder import derive_name, record
from cua.redaction import Redactor
from cua.replay import ReplayExecutor
from cua.session import FormLogin
from cua.surface import ApprovalRequest
from cua.surface.guarded import GuardedSurface
from cua.surface.web import WebSurface

EXIT_CODES = {"success": 0, "business_outcome": 0, "failed": 1, "escalated": 2}


def _policy_for(app: str, url: str) -> Policy:
    """Refuse to start at all against a target the policy does not allowlist."""
    policy = load_policy(app)
    reason = policy.route_violation(url.rstrip("/") + "/")
    if reason:
        raise SystemExit(f"refusing to run against {url}: {reason} (policies/{app}.yaml)")
    return policy


@dataclass
class _Session:
    browser: Browser
    surface: GuardedSurface
    handoff: Handoff | None
    console: OperatorConsole | None

    def close(self) -> None:
        if self.console is not None:
            self.console.stop()
        self.browser.close()


def _open_session(pw: Playwright, args: argparse.Namespace, policy: Policy, log: RunLog, redactor: Redactor,
                  auto_approve: bool = False) -> _Session:
    """One live browser session, guarded by the policy, optionally with a human attached."""
    launch_args = [f"--remote-debugging-port={args.cdp_port}"] if args.cdp_port else []
    browser = pw.chromium.launch(headless=not args.headed, args=launch_args)
    context = browser.new_context(viewport={"width": 1280, "height": 800})

    control = console = None
    if args.console:
        control = SessionControl(lease_ttl_s=args.handoff_timeout)
        console = OperatorConsole(control, log.dir, port=args.console_port).start()
        install_human_capture(context, control, policy.sensitive_labels, redactor)  # before any page loads
        where = "the browser window" if args.headed else (
            f"a CDP-attached tool at http://127.0.0.1:{args.cdp_port}" if args.cdp_port else "nothing: add --headed or --cdp-port to allow take-over")
        print(f"operator console: {console.url}  (take-over via {where})", file=sys.stderr, flush=True)

    if auto_approve:
        def approver(request: ApprovalRequest) -> bool:
            return True  # stand-in for a human; recorded as approval_granted in the event log
    else:
        approver = control.consume_approval if control is not None else None

    web = WebSurface(context.new_page(), args.url, sensitive_labels=policy.sensitive_labels,
                     request_policy=policy.request_violation)
    surface = GuardedSurface(web, policy, log=log, approver=approver, control=control)
    handoff = Handoff(control, surface, log, redactor, console.url, args.handoff_timeout) if control else None
    return _Session(browser, surface, handoff, console)


def _record(trace: Trace, app: str, name: str | None, evidence_dir: Path | None = None) -> Path:
    pack = store.load_pack(app)
    name = name or derive_name(trace.goal)
    version = store.next_version(app, name)
    artifact = record(trace, pack, name=name, version=version,
                      evidence_path=str(evidence_dir) if evidence_dir else "", policy=load_policy(app))
    path = store.save(artifact)
    if evidence_dir:  # keep the evidence directory self-contained
        (evidence_dir / "capability.yaml").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _set_target_fault(base_url: str, name: str) -> None:
    """Test hook for generating evidence: arm one of the mock app's faults mid-run.
    Goes straight to the app over HTTP, outside the guarded browser, by design."""
    data = urllib.parse.urlencode({"name": name}).encode()
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/admin/fault", data=data, timeout=5) as resp:
        resp.read()


def cmd_discover(args: argparse.Namespace) -> int:
    policy = _policy_for(args.app, args.url)
    redactor = Redactor()
    log = RunLog(Path(args.evidence), "discovery", redactor)
    decider = GeminiDecider(model=args.model)
    with sync_playwright() as pw:
        session = _open_session(pw, args, policy, log, redactor)  # no auto-approval: discovery never commits alone
        try:
            FormLogin().sign_in(session.surface)
            log.event("session_ready", provider="FormLogin")  # credentials are never logged
            session.surface.goto(args.entry)
            trace = DiscoveryAgent(session.surface, decider, log, redactor, max_steps=args.max_steps,
                                   handoff=session.handoff).run(goal=args.goal, app=args.app, base_url=args.url,
                                                                entry_route=args.entry)
        finally:
            session.close()

    result = {"run_id": trace.run_id, "status": trace.status, "reason": trace.reason,
              "outputs": trace.outputs, "handoffs": trace.handoffs, "evidence": str(log.dir)}
    if trace.status == "success" and not args.no_record:
        path = _record(trace, args.app, args.name, log.dir)
        log.event("artifact_recorded", path=str(path))
        result["artifact"] = str(path)
    log.close()
    print(json.dumps(redactor.scrub(result), indent=2))
    return 0 if trace.status == "success" else 1


def cmd_replay(args: argparse.Namespace) -> int:
    artifact = store.load(Path(args.artifact))
    policy = _policy_for(artifact.capability.app.key, args.url)
    inputs: dict[str, str] = {}
    for pair in args.input:
        if "=" not in pair:
            raise SystemExit(f"--input expects name=value, got {pair!r}")
        name, value = pair.split("=", 1)
        inputs[name] = value

    redactor = Redactor()
    log = RunLog(Path(args.evidence), "replay", redactor)
    with sync_playwright() as pw:
        session = _open_session(pw, args, policy, log, redactor, auto_approve=args.approve_irreversible)
        try:
            login = FormLogin()
            if "authenticated_session" in artifact.preconditions:
                login.sign_in(session.surface)
                log.event("session_ready", provider="FormLogin")
            if args.inject_fault:
                _set_target_fault(args.url, args.inject_fault)  # after sign-in: the fault lands mid-run
                log.event("fault_injected", fault=args.inject_fault, note="test hook, not part of replay")
            try:
                result = ReplayExecutor(session.surface, artifact, log, redactor, session=login,
                                        handoff=session.handoff).run(inputs)
            finally:
                if args.inject_fault:
                    _set_target_fault(args.url, "")
        finally:
            session.close()
    log.close()
    # stdout is the result handed to the caller (who owns the outputs), not a log: printed as is.
    print(json.dumps(result.model_dump(), indent=2, default=str))
    return EXIT_CODES[result.status]


def cmd_operator(args: argparse.Namespace) -> int:
    try:
        resolved = run_operator(args.console_url, args.mode, cdp_url=args.cdp_url, click=args.click,
                                frame=args.frame or None, operator=args.name, timeout_s=args.timeout)
    except OperatorError as e:
        print(f"operator: {e}", file=sys.stderr)
        return 1
    print(json.dumps(resolved, indent=2, default=str))
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    trace = Trace.model_validate_json(Path(args.trace).read_text(encoding="utf-8"))
    path = _record(trace, args.app, args.name, Path(args.trace).parent)
    print(json.dumps({"artifact": str(path), "from_run": trace.run_id}, indent=2))
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    print(str(store.export_json_schema(Path(args.out))))
    return 0


def _human_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--console", action="store_true",
                   help="attach a human: raise interventions to an operator console and wait, instead of "
                        "returning escalated")
    p.add_argument("--console-port", type=int, default=8765)
    p.add_argument("--handoff-timeout", type=float, default=900, help="seconds to wait for an operator")
    p.add_argument("--cdp-port", type=int,
                   help="expose the live browser over CDP so a remote operator tool can take over the same session")


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path.cwd() / ".env")
    parser = argparse.ArgumentParser(prog="cua")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="let the LLM accomplish a goal on the live app, then record the capability")
    d.add_argument("--goal", required=True)
    d.add_argument("--app", default="legacycore")
    d.add_argument("--url", default="http://localhost:5001")
    d.add_argument("--entry", default="/", help="route to start from after sign-in")
    d.add_argument("--max-steps", type=int, default=25)
    d.add_argument("--model", help="override GEMINI_MODEL")
    d.add_argument("--headed", action="store_true", help="show the browser window")
    d.add_argument("--name", help="capability name (default: derived from the goal)")
    d.add_argument("--no-record", action="store_true", help="keep the trace only, do not write an artifact")
    d.add_argument("--evidence", default="evidence")
    _human_args(d)
    d.set_defaults(func=cmd_discover)

    p = sub.add_parser("replay", help="run a saved capability with inputs, no LLM in the loop")
    p.add_argument("artifact", help="path to capabilities/<app>/<name>/v<N>.yaml")
    p.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    p.add_argument("--url", default="http://localhost:5001")
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("--approve-irreversible", action="store_true",
                   help="stand in for a human approving irreversible steps (no console needed)")
    p.add_argument("--inject-fault", metavar="NAME",
                   help="test hook: arm a mock-app fault after sign-in (slow, interstitial, "
                        "session_timeout, permission_denied, app_error)")
    p.add_argument("--evidence", default="evidence")
    _human_args(p)
    p.set_defaults(func=cmd_replay)

    o = sub.add_parser("operator", help="stand-in for a human operator: resolve the next intervention")
    o.add_argument("--mode", choices=["approve", "takeover", "abort"], required=True)
    o.add_argument("--console-url", default="http://127.0.0.1:8765")
    o.add_argument("--cdp-url", help="the live session, e.g. http://127.0.0.1:9222 (needed for --click)")
    o.add_argument("--click", help="takeover: the button to click in the live session, as the human")
    o.add_argument("--frame", default="content", help="frame holding that button ('' for the top document)")
    o.add_argument("--name", default="operator-sim", help="operator identity recorded in the evidence")
    o.add_argument("--timeout", type=float, default=300)
    o.set_defaults(func=cmd_operator)

    r = sub.add_parser("record", help="build a capability artifact from a saved discovery trace")
    r.add_argument("--trace", required=True, help="path to evidence/discovery/<run>/trace.json")
    r.add_argument("--app", default="legacycore")
    r.add_argument("--name")
    r.set_defaults(func=cmd_record)

    s = sub.add_parser("schema", help="export the artifact JSON Schema (the contract for calling agents)")
    s.add_argument("--out", default="capabilities/artifact.schema.json")
    s.set_defaults(func=cmd_schema)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
