"""Replay against the real mock app: the happy path plus every branch of the error taxonomy.

These drive the artifact that discovery actually recorded, so a recording that cannot be
replayed fails the suite.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from cua.artifact import store
from cua.evidence import RunLog
from cua.policy import load_policy
from cua.redaction import Redactor
from cua.replay import ReplayExecutor
from cua.session import FormLogin
from cua.surface.guarded import GuardedSurface
from cua.surface.web import WebSurface

ARTIFACT = Path("capabilities/legacycore/member_savings_balance/v1.yaml")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def replay(live_app, browser, tmp_path):
    base_url, app = live_app
    base_artifact = store.load(ARTIFACT)
    policy = load_policy("legacycore").model_copy(update={"allowed_origins": [base_url]})

    def run(inputs, fault=None, artifact=None, approve=False, **kwargs):
        page = browser.new_page()
        redactor = Redactor()
        log = RunLog(tmp_path, "replay", redactor)
        web = WebSurface(page, base_url, sensitive_labels=policy.sensitive_labels,
                         request_policy=policy.request_violation)
        surface = GuardedSurface(web, policy, log=log, approver=(lambda request: True) if approve else None)
        session = FormLogin()
        session.sign_in(surface)
        app.config["FAULT"] = fault  # inject after sign-in: the fault hits the run, not the login
        try:
            return ReplayExecutor(surface, artifact or base_artifact, log, redactor, session=session,
                                  step_timeout_s=8, **kwargs).run(inputs)
        finally:
            log.close()
            page.close()
            app.config["FAULT"] = None
            run.evidence = log.dir  # the last run's evidence directory, for assertions

    return run


def test_success_returns_typed_outputs(replay):
    result = replay({"member_id": "10023"})
    assert result.status == "success"
    assert result.outputs == {"savings_balance": Decimal("10527.64")}
    assert result.steps_completed == 3
    # every step resolved on its preferred (index 0) strategy: no drift
    assert all(used.endswith("#0") for used in result.strategies_used.values())


def test_unknown_member_is_a_business_outcome_not_a_failure(replay):
    result = replay({"member_id": "99999"})
    assert result.status == "business_outcome"
    assert result.code == "MEMBER_NOT_FOUND"
    assert "No member found for ID 99999" in result.message
    assert result.step == "s2_click_search"


def test_input_that_breaks_the_contract_is_rejected_before_acting(replay):
    result = replay({"member_id": "123"})
    assert (result.status, result.code) == ("failed", "INPUT_INVALID")
    assert result.steps_completed == 0
    written = json.loads((replay.evidence / "result.json").read_text())  # evidence, like any other status
    assert written["code"] == "INPUT_INVALID"


def test_unparseable_output_is_a_failure_with_context(replay):
    artifact = store.load(ARTIFACT)
    artifact.steps[2].parse = "integer"  # the balance cell holds "$10,527.64"
    result = replay({"member_id": "10023"}, artifact=artifact)
    assert (result.status, result.code, result.step) == ("failed", "PARSE_ERROR", "s3_extract_savings_balance")


def test_a_declared_output_that_never_arrives_fails_the_run(replay):
    artifact = store.load(ARTIFACT)
    artifact.steps = artifact.steps[:2]  # contract still promises savings_balance
    result = replay({"member_id": "10023"}, artifact=artifact)
    assert (result.status, result.code) == ("failed", "OUTPUT_MISSING")


def test_a_fallback_locator_winning_is_logged_as_drift(replay):
    artifact = store.load(ARTIFACT)
    artifact.steps[1].target.strategies[0].name = "Find"  # the preferred strategy stops matching
    result = replay({"member_id": "10023"}, artifact=artifact)
    assert result.status == "success"  # the CSS fallback still resolves, so the run completes
    assert result.strategies_used["s2_click_search"] == "css#1"
    events = [json.loads(line) for line in (replay.evidence / "events.jsonl").read_text().splitlines()]
    assert [e["step"] for e in events if e["type"] == "drift_signal"] == ["s2_click_search"]


def test_interstitial_is_recovered_inside_the_run(replay):
    result = replay({"member_id": "10023"}, fault="interstitial")
    assert result.status == "success"
    assert [r.handler for r in result.recovered] == ["system_notice"]


def test_expired_session_is_recovered_by_re_authenticating(replay):
    result = replay({"member_id": "10023"}, fault="session_timeout")
    assert result.status == "success"
    assert [r.handler for r in result.recovered] == ["session_expired"]


@pytest.mark.parametrize("fault, code, retryable", [
    ("app_error", "APP_ERROR", True),
    ("permission_denied", "PERMISSION_DENIED", False),
])
def test_hard_failures_stop_with_a_debuggable_error(replay, fault, code, retryable):
    result = replay({"member_id": "10023"}, fault=fault)
    assert (result.status, result.code, result.retryable) == ("failed", code, retryable)
    assert result.screenshot is not None


def test_broken_locator_reports_what_it_tried(replay):
    artifact = store.load(ARTIFACT)
    artifact.steps[1].target.strategies = [s for s in artifact.steps[1].target.strategies if s.by == "role"]
    artifact.steps[1].target.strategies[0].name = "Find It"
    result = replay({"member_id": "10023"}, artifact=artifact)
    assert (result.status, result.code) == ("failed", "LOCATOR_NOT_FOUND")
    assert "role: 0 matches" in result.observed


def test_irreversible_step_escalates_unless_approved(replay):
    artifact = store.load(ARTIFACT)
    artifact.steps[1].risk = "irreversible"  # a reviewer can mark any step irreversible
    escalated = replay({"member_id": "10023"}, artifact=artifact)
    assert escalated.status == "escalated"
    assert "needs human approval" in escalated.reason
    assert escalated.step == "s2_click_search"
    assert replay({"member_id": "10023"}, artifact=artifact, approve=True).status == "success"


def test_entry_outside_the_allowlist_is_a_policy_violation(replay):
    artifact = store.load(ARTIFACT)
    artifact.entry = "/admin/reset"  # a tampered artifact
    result = replay({"member_id": "10023"}, artifact=artifact)
    assert (result.status, result.code) == ("failed", "POLICY_VIOLATION")
    assert "denied" in result.observed
