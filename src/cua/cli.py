"""Command line entry point: `cua discover | record | schema`."""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from cua.agent import DiscoveryAgent
from cua.agent.llm import GeminiDecider
from cua.agent.trace import Trace
from cua.artifact import store
from cua.evidence import RunLog
from cua.recorder import derive_name, record
from cua.redaction import Redactor
from cua.session import FormLogin
from cua.surface.web import WebSurface


def _record(trace: Trace, app: str, name: str | None, evidence_dir: Path | None = None) -> Path:
    pack = store.load_pack(app)
    name = name or derive_name(trace.goal)
    version = store.next_version(app, name)
    artifact = record(trace, pack, name=name, version=version,
                      evidence_path=str(evidence_dir) if evidence_dir else "")
    path = store.save(artifact)
    if evidence_dir:  # keep the evidence directory self-contained
        (evidence_dir / "capability.yaml").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def cmd_discover(args: argparse.Namespace) -> int:
    redactor = Redactor()
    log = RunLog(Path(args.evidence), "discovery", redactor)
    decider = GeminiDecider(model=args.model)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
        surface = WebSurface(page, args.url)
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
