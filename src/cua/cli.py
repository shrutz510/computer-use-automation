"""Command line entry point: `cua discover ...`."""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from cua.agent import DiscoveryAgent
from cua.agent.llm import GeminiDecider
from cua.evidence import RunLog
from cua.redaction import Redactor
from cua.session import FormLogin
from cua.surface.web import WebSurface


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
    log.close()
    print(json.dumps(redactor.scrub({"run_id": trace.run_id, "status": trace.status, "reason": trace.reason,
                                     "outputs": trace.outputs, "evidence": str(log.dir)}), indent=2))
    return 0 if trace.status == "success" else 1


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path.cwd() / ".env")
    parser = argparse.ArgumentParser(prog="cua")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="let the LLM accomplish a goal on the live app and record a trace")
    d.add_argument("--goal", required=True)
    d.add_argument("--app", default="legacycore")
    d.add_argument("--url", default="http://localhost:5001")
    d.add_argument("--entry", default="/", help="route to start from after sign-in")
    d.add_argument("--max-steps", type=int, default=25)
    d.add_argument("--model", help="override GEMINI_MODEL")
    d.add_argument("--headed", action="store_true", help="show the browser window")
    d.add_argument("--evidence", default="evidence")
    d.set_defaults(func=cmd_discover)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
