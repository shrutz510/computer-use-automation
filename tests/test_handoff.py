"""Human-in-the-loop: the control state machine, the console, and real handoffs on a live
session - approve, take over the same browser and resume, resume without finishing, abort -
for replay (against the mock app) and for discovery (driven by a scripted stand-in LLM).

The operator is played by cua.handoff.simulate from another thread, through the same paths a
person uses: the console's HTTP API, and the live browser attached over CDP."""

import json
import re
import socket
import threading
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from cua.agent import DiscoveryAgent
from cua.artifact import store
from cua.evidence import RunLog
from cua.handoff import (
    ControlError, Controller, Handoff, Intervention, OperatorConsole, SessionControl, install_human_capture, simulate,
)
from cua.policy import Policy, load_policy
from cua.redaction import Redactor
from cua.replay import ReplayExecutor
from cua.session import FormLogin
from cua.surface import ApprovalRequest, PolicyBlocked
from cua.surface.guarded import GuardedSurface
from cua.surface.web import WebSurface

WRITE_FLOW = Path("capabilities/legacycore/open_sub_account/v1.yaml")
INPUTS = {"member_id": "10023", "nickname": "Test Fund", "initial_deposit": "300.00"}


def intervention(kind="approval", control="Confirm") -> Intervention:
    return Intervention(id="int-test", kind=kind, run_id="r", subject="s", step="s8", reason="because",
                        url="content:/accounts/review", screenshot=None, recent_events=[], created_at="now",
                        control=control)


def approval_for(name: str) -> ApprovalRequest:
    return ApprovalRequest(action="click", control=name, risk="irreversible", reason="r", url="u")


# ---- the state machine --------------------------------------------------------

def test_approval_grants_exactly_one_click_on_exactly_that_control():
    control = SessionControl()
    control.open(intervention())
    assert control.state == Controller.AWAITING_HUMAN and not control.is_automation()
    control.approve("int-test", "alice")
    assert control.state == Controller.AUTOMATION
    assert not control.consume_approval(approval_for("Delete"))
    assert control.consume_approval(approval_for("Confirm"))
    assert not control.consume_approval(approval_for("Confirm"))  # one shot


def test_takeover_needs_the_lease_to_hand_back():
    control = SessionControl()
    control.open(intervention(kind="stuck", control=None))
    with pytest.raises(ControlError, match="only approval requests"):
        control.approve("int-test", "alice")
    lease = control.claim("int-test", "alice")
    assert control.state == Controller.HUMAN_CONTROL
    with pytest.raises(ControlError, match="already claimed"):
        control.claim("int-test", "bob")
    with pytest.raises(ControlError, match="lease"):
        control.resume("int-test", "not-the-lease")
    control.resume("int-test", lease)
    assert control.state == Controller.VERIFYING
    control.verified(True, "ok")
    assert [h["to"] for h in control.interventions[0].history] == [
        "awaiting_human", "human_control", "verifying", "automation"]


def test_only_actions_during_human_control_count_as_human():
    control = SessionControl()
    control.record_human_action({"kind": "click", "name": "Search"})  # the automation's own click
    control.open(intervention(kind="stuck", control=None))
    control.claim("int-test", "alice")
    control.record_human_action({"kind": "click", "name": "Confirm"})
    assert [a["name"] for a in control.current.human_actions] == ["Confirm"]


def test_illegal_moves_are_refused():
    control = SessionControl()
    with pytest.raises(ControlError):
        control.approve("int-test", "alice")  # nothing open
    control.open(intervention())
    with pytest.raises(ControlError):
        control.open(intervention())  # already waiting for a human
    with pytest.raises(ControlError):
        control.verified(True, "nothing to verify")


def test_waiting_times_out_back_to_automation():
    control = SessionControl()
    control.open(intervention())
    assert control.wait(tick=lambda: None, timeout_s=0.05) == "timeout"
    assert control.state == Controller.AUTOMATION


def test_console_shows_the_request_and_only_the_allowed_moves(tmp_path):
    control = SessionControl()
    console = OperatorConsole(control, tmp_path, port=0).start()
    try:
        control.open(intervention())
        assert simulate.wait_for_open(console.url, timeout_s=5)["reason"] == "because"
        page = urllib.request.urlopen(console.url).read().decode()
        assert "Approval needed" in page and "Approve" in page and "Take control" in page
        assert simulate.act(console.url, "int-test", "approve", "alice")["resolution"] == "approved"
        with pytest.raises(simulate.OperatorError, match="409"):
            simulate.act(console.url, "int-test", "approve", "alice")  # already resolved
    finally:
        console.stop()


# ---- live sessions ---------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def cdp_browser():
    """A browser the operator can attach to over CDP, like a remote operator tool."""
    port = free_port()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=[f"--remote-debugging-port={port}"])
        yield browser, f"http://127.0.0.1:{port}"
        browser.close()


class Human:
    """Runs an operator script on its own thread while the automation waits on the main one."""

    def __init__(self, script):
        self.errors: list[BaseException] = []
        self.console_url = None
        self._script = script
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self._script(self.console_url)
        except BaseException as e:  # surfaced by join()
            self.errors.append(e)

    def start(self, console_url: str) -> "Human":
        self.console_url = console_url
        self._thread.start()
        return self

    def join(self):
        self._thread.join(timeout=60)
        assert not self.errors, self.errors


def session(browser, base_url: str, policy: Policy, log: RunLog, redactor: Redactor, human_attached: bool = True):
    context = browser.new_context()
    control = SessionControl() if human_attached else None
    console = OperatorConsole(control, log.dir, port=0).start() if human_attached else None
    if human_attached:
        install_human_capture(context, control, policy.sensitive_labels, redactor)
    web = WebSurface(context.new_page(), base_url, sensitive_labels=policy.sensitive_labels,
                     request_policy=policy.request_violation)
    surface = GuardedSurface(web, policy, log=log, approver=control.consume_approval if control else None,
                             control=control)
    handoff = Handoff(control, surface, log, redactor, console.url, timeout_s=60) if human_attached else None
    return context, surface, handoff, console


@pytest.fixture
def replay_write_flow(live_app, cdp_browser, tmp_path):
    base_url, _ = live_app
    browser, _ = cdp_browser
    policy = load_policy("legacycore").model_copy(update={"allowed_origins": [base_url]})
    artifact = store.load(WRITE_FLOW)

    def run(human: Human | None):
        redactor = Redactor()
        log = RunLog(tmp_path, "replay", redactor)
        context, surface, handoff, console = session(browser, base_url, policy, log, redactor, human is not None)
        login = FormLogin()
        login.sign_in(surface)
        if human is not None:
            human.start(console.url)
        try:
            result = ReplayExecutor(surface, artifact, log, redactor, session=login, handoff=handoff,
                                    step_timeout_s=8).run(INPUTS)
        finally:
            if human is not None:
                human.join()
                console.stop()
            context.close()
            log.close()
        return result, log.dir

    return run


def test_replay_approved_by_an_operator(replay_write_flow):
    result, evidence = replay_write_flow(Human(lambda url: simulate.run_operator(url, "approve", timeout_s=30)))
    assert result.status == "success"
    assert re.fullmatch(r"710023\d{4}", result.outputs["new_account_number"])
    assert [(h.kind, h.step, h.resolution) for h in result.handoffs] == [("approval", "s8_click_confirm", "approved")]
    events = (evidence / "events.jsonl").read_text()
    assert result.outputs["new_account_number"] not in events  # sensitive output: returned, never logged
    assert '"approval_granted"' in events


def test_replay_resumes_after_a_human_does_the_step_in_the_same_session(replay_write_flow, cdp_browser):
    _, cdp_url = cdp_browser
    result, evidence = replay_write_flow(Human(lambda url: simulate.run_operator(
        url, "takeover", cdp_url=cdp_url, click="Confirm", timeout_s=30)))
    assert result.status == "success"  # checkpoint verified after the human's click, then carried on
    assert re.fullmatch(r"710023\d{4}", result.outputs["new_account_number"])
    [handoff] = result.handoffs
    assert (handoff.resolution, handoff.human_actions >= 1) == ("resumed", True)
    record = json.loads(next((evidence / "interventions").glob("*.json")).read_text())
    assert any(a["kind"] == "click" and a["name"] == "Confirm" for a in record["human_actions"])
    assert [h["to"] for h in record["history"]] == ["awaiting_human", "human_control", "verifying", "automation"]
    human_events = [e for e in map(json.loads, (evidence / "events.jsonl").read_text().splitlines())
                    if e["type"] == "human_action"]
    assert [(e["actor"], e["action"]["name"]) for e in human_events] == [("human", "Confirm")]


def test_resume_without_finishing_the_step_asks_again(replay_write_flow):
    def operator(url):
        simulate.run_operator(url, "takeover", timeout_s=30)  # take control, do nothing, hand back
        simulate.run_operator(url, "abort", timeout_s=30)     # the re-raised approval: abort

    result, _ = replay_write_flow(Human(operator))
    assert result.status == "escalated"
    assert [h.resolution for h in result.handoffs] == ["resumed", "aborted"]
    assert result.intervention_id == result.handoffs[-1].id


def test_without_an_operator_the_request_is_queued_and_the_caller_told(replay_write_flow):
    result, evidence = replay_write_flow(None)
    assert result.status == "escalated" and result.step == "s8_click_confirm"
    queued = json.loads((evidence / "interventions" / f"{result.intervention_id}.json").read_text())
    assert queued["kind"] == "approval" and queued["resolution"] is None and queued["screenshot"]


def test_automation_cannot_act_while_a_human_holds_control(cdp_browser):
    browser, _ = cdp_browser
    control = SessionControl()
    page = browser.new_page()
    page.set_content("<button>Search</button>")
    surface = GuardedSurface(WebSurface(page, "http://unused"), load_policy("legacycore"), control=control)
    control.open(intervention(kind="stuck", control=None))
    control.claim("int-test", "alice")
    with pytest.raises(PolicyBlocked, match="not in control"):
        surface.click(page.get_by_role("button", name="Search"))
    page.close()


# ---- discovery hands over too --------------------------------------------------------

FIXTURE_POLICY = Policy(allowed_origins=["http://test"], allowed_routes=["/page", "/review"])
PAGES = {"/page": '<html><head><title>Review</title></head><body><form method="post" action="/review">'
                  '<input type="submit" value="Confirm"></form></body></html>',
         "/review": "<html><head><title>Done</title></head><body>done</body></html>"}


def discover_with_human(cdp_browser, tmp_path, decider, operator_script):
    browser, _ = cdp_browser
    redactor = Redactor()
    log = RunLog(tmp_path, "discovery", redactor)
    context, surface, handoff, console = session(browser, "http://test", FIXTURE_POLICY, log, redactor)
    context.route("http://test/**", lambda route: route.fulfill(
        content_type="text/html", body=PAGES[route.request.url.removeprefix("http://test")]))
    surface.goto("/page")
    human = Human(operator_script).start(console.url)
    try:
        return DiscoveryAgent(surface, decider, log, redactor, max_steps=5, handoff=handoff).run(
            goal="confirm it", app="test", base_url="http://test", entry_route="/page")
    finally:
        human.join()
        console.stop()
        context.close()
        log.close()


def test_discovery_continues_after_approval(cdp_browser, tmp_path, scripted_decider):
    trace = discover_with_human(cdp_browser, tmp_path, scripted_decider([("click", "Confirm"), ("done", None)]),
                                lambda url: simulate.run_operator(url, "approve", timeout_s=30))
    assert trace.status == "success"
    assert trace.steps[0].ok and trace.steps[0].frames_after[0].title == "Done"  # the model's own click, approved
    assert trace.handoffs[0]["resolution"] == "approved"


def test_discovery_continues_from_where_the_human_left_off(cdp_browser, tmp_path, scripted_decider):
    _, cdp_url = cdp_browser
    decider = scripted_decider([("click", "Confirm"), ("done", None)])
    trace = discover_with_human(cdp_browser, tmp_path, decider, lambda url: simulate.run_operator(
        url, "takeover", cdp_url=cdp_url, click="Confirm", frame=None, timeout_s=30))
    assert trace.status == "success"
    assert trace.steps == [] and trace.human_actions >= 1  # the human did it; not part of the recorded flow
    assert "a human operator took control and did: click Confirm" in decider.prompts[-1]
