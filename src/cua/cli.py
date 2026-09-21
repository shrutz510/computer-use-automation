"""Command line entry point: `cua discover | replay | record | schema`."""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page, sync_playwright

from cua.agent import DiscoveryAgent
from cua.agent.llm import GeminiDecider
from cua.agent.trace import Trace
from cua.artifact import store
from cua.evidence import RunLog
from cua.policy import Policy, load_policy
from cua.recorder import derive_name, record
from cua.redaction import Redactor
from cua.replay import ReplayExecutor
from cua.session import FormLogin
from cua.surface import ApprovalRequest
from cua.surface.guarded import Approver, GuardedSurface
from cua.surface.web import WebSurface

EXIT_CODES = {"success": 0, "business_outcome": 0, "failed": 1, "escalated": 2}


def _policy_for(app: str, url: str) -> Policy:
    """Refuse to start at all against a target the policy does not allowlist."""
    policy = load_policy(app)
    reason = policy.route_violation(url.rstrip("/") + "/")
    if reason:
        raise SystemExit(f"refusing to run against {url}: {reason} (policies/{app}.yaml)")
    return policy


def _surface(page: Page, url: str, policy: Policy, log: RunLog, approver: Approver | None = None) -> GuardedSurface:
    web = WebSurface(page, url, sensitive_labels=policy.sensitive_labels, request_policy=policy.request_violation)
    return GuardedSurface(web, policy, log=log, approver=approver)


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
        browser = pw.chromium.launch(headless=not args.headed)
        page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
        surface = _surface(page, args.url, policy, log)  # no approver: discovery never commits
        FormLogin().sign_in(surface)
        log.event("session_ready", provider="FormLogin")  # credentials are never logged
        surface.goto(args.entry)
        trace = DiscoveryAgent(surface, decider, log, redactor, max_steps=args.max_steps).run(
            goal=args.goal, app=args.app, base_url=args.url, entry_route=args.entry)
        browser.close()

    result = {"run_id": trace.run_id, "status": trace.status, "reason": trace.reason,
              "outputs": trace.outputs, "evidence": str(log.dir)}
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

    approver: Approver | None = None
    if args.approve_irreversible:
        def approver(request: ApprovalRequest) -> bool:
            return True  # stand-in for a human; recorded as approval_granted in the event log

    redactor = Redactor()
    log = RunLog(Path(args.evidence), "replay", redactor)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
        surface = _surface(page, args.url, policy, log, approver)
        session = FormLogin()
        if "authenticated_session" in artifact.preconditions:
            session.sign_in(surface)
            log.event("session_ready", provider="FormLogin")
        if args.inject_fault:
            _set_target_fault(args.url, args.inject_fault)  # after sign-in: the fault lands mid-run
            log.event("fault_injected", fault=args.inject_fault, note="test hook, not part of replay")
        try:
            result = ReplayExecutor(surface, artifact, log, redactor, session=session).run(inputs)
        finally:
            if args.inject_fault:
                _set_target_fault(args.url, "")
        browser.close()
    log.close()
    print(json.dumps(result.model_dump(), indent=2, default=str))
    return EXIT_CODES[result.status]


def cmd_record(args: argparse.Namespace) -> int:
    trace = Trace.model_validate_json(Path(args.trace).read_text(encoding="utf-8"))
    path = _record(trace, args.app, args.name, Path(args.trace).parent)
    print(json.dumps({"artifact": str(path), "from_run": trace.run_id}, indent=2))
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    print(str(store.export_json_schema(Path(args.out))))
    return 0


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
    d.set_defaults(func=cmd_discover)

    p = sub.add_parser("replay", help="run a saved capability with inputs, no LLM in the loop")
    p.add_argument("artifact", help="path to capabilities/<app>/<name>/v<N>.yaml")
    p.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    p.add_argument("--url", default="http://localhost:5001")
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("--approve-irreversible", action="store_true",
                   help="stand in for a human approving irreversible steps")
    p.add_argument("--inject-fault", metavar="NAME",
                   help="test hook: arm a mock-app fault after sign-in (slow, interstitial, "
                        "session_timeout, permission_denied, app_error)")
    p.add_argument("--evidence", default="evidence")
    p.set_defaults(func=cmd_replay)

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
