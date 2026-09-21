"""The policy and its enforcement: route rules, both enforcement layers, irreversible
approval, and discovery obeying the gate even when the model asks for something else."""

import pytest
from playwright.sync_api import sync_playwright

from cua.agent import DiscoveryAgent
from cua.evidence import RunLog
from cua.policy import Policy, load_policy
from cua.redaction import Redactor
from cua.surface import ApprovalRequired, PolicyBlocked
from cua.surface.guarded import GuardedSurface
from cua.surface.web import WebSurface

POLICY = Policy(allowed_origins=["http://test"], allowed_routes=["/", "/page", "/review"],
                denied_routes=["/admin/*"])

PAGE = """<html><head><title>Page</title></head><body><table>
<tr><td><a href="http://evil.test/steal">Other site</a></td></tr>
<tr><td><a href="/admin/reset">Admin</a></td></tr>
<tr><td><button type="button" onclick="location.href='/admin/reset'">Sneaky</button></td></tr>
<tr><td>Nickname:</td><td><input name="f2"></td></tr>
<tr><td><form method="post" action="/review"><input type="submit" value="Confirm"></form></td></tr>
<tr><td><form method="post" action="/review"><input type="submit" value="Continue"></form></td></tr>
</table></body></html>"""
PAGES = {"/page": PAGE, "/review": "<html><head><title>Reviewed</title></head><body>reviewed</body></html>",
         "/admin/reset": "<html><body>should never be served</body></html>"}


# ---- rules ---------------------------------------------------------------

def test_route_rules():
    assert POLICY.route_violation("http://test/page") is None
    assert "origin http://evil.test is not allowlisted" in POLICY.route_violation("http://evil.test/page")
    assert "denied" in POLICY.route_violation("http://test/admin/reset")
    assert "not allowlisted" in POLICY.route_violation("http://test/other")


def test_subresources_only_need_an_allowed_origin():
    assert POLICY.request_violation("http://test/favicon.ico", "GET", is_navigation=False) is None
    assert POLICY.request_violation("http://test/favicon.ico", "GET", is_navigation=True) is not None
    assert POLICY.request_violation("http://test/admin/x", "POST", is_navigation=False) is not None
    assert POLICY.request_violation("http://evil.test/pixel.gif", "GET", is_navigation=False) is not None


def test_click_risk():
    assert POLICY.click_risk("Confirm", "post") == "irreversible"
    assert POLICY.click_risk("Search", "post") == "reversible"
    assert POLICY.click_risk("Member Search", "") == "safe"


def test_shipped_policy():
    policy = load_policy("legacycore")
    assert policy.irreversible == "require_approval"
    assert "denied" in policy.route_violation("http://localhost:5001/admin/reset")
    assert policy.route_violation("http://localhost:5001/members/10023") is None


# ---- enforcement against a live page -------------------------------------

@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def served():
    return []


@pytest.fixture
def make_surface(browser, served):
    pages = []

    def make(policy=POLICY, approver=None):
        page = browser.new_page()
        pages.append(page)

        def fulfill(route):
            path = route.request.url.removeprefix("http://test")
            served.append(path)
            route.fulfill(content_type="text/html", body=PAGES.get(path, "<html><body>other</body></html>"))

        page.route("http://test/**", fulfill)  # registered first, so the policy route runs before it
        web = WebSurface(page, "http://test", request_policy=policy.request_violation)
        surface = GuardedSurface(web, policy, approver=approver)
        surface.goto("/page")
        surface.observe()
        return surface

    yield make
    for page in pages:
        page.close()


def button(surface, name):
    return surface.inner.page.get_by_role("button", name=name, exact=True)


def link(surface, name):
    return surface.inner.page.get_by_role("link", name=name, exact=True)


def test_link_to_another_origin_is_refused_before_clicking(make_surface, served):
    surface = make_surface()
    with pytest.raises(PolicyBlocked, match="evil.test"):
        surface.click(link(surface, "Other site"))


def test_link_to_a_denied_route_is_refused(make_surface, served):
    surface = make_surface()
    with pytest.raises(PolicyBlocked, match="denied"):
        surface.click(link(surface, "Admin"))
    assert "/admin/reset" not in served


def test_script_navigation_is_caught_by_the_network_layer(make_surface, served):
    surface = make_surface()
    with pytest.raises(PolicyBlocked, match="/admin/reset"):
        surface.click(button(surface, "Sneaky"))  # no href to pre-check: only the network layer sees it
    assert "/admin/reset" not in served


def test_irreversible_click_needs_approval(make_surface, served):
    surface = make_surface()
    with pytest.raises(ApprovalRequired) as exc:
        surface.click(button(surface, "Confirm"))
    assert exc.value.request.control == "Confirm"
    assert "/review" not in served


def test_irreversible_click_proceeds_once_approved(make_surface, served):
    seen = []
    surface = make_surface(approver=lambda request: seen.append(request.control) or True)
    surface.click(button(surface, "Confirm"))
    assert seen == ["Confirm"] and "/review" in served


def test_reversible_submit_needs_no_approval(make_surface, served):
    surface = make_surface()
    surface.click(button(surface, "Continue"))
    assert "/review" in served


def test_flag_mode_proceeds(make_surface, served):
    surface = make_surface(policy=POLICY.model_copy(update={"irreversible": "flag"}))
    surface.click(button(surface, "Confirm"))
    assert "/review" in served


def test_disallowed_action_type(make_surface):
    surface = make_surface(policy=POLICY.model_copy(update={"allowed_actions": ["navigate", "click"]}))
    with pytest.raises(PolicyBlocked, match="'type' is not allowed"):
        surface.fill(surface.inner.page.locator("input[name=f2]"), "x")


# ---- discovery obeys the gate, whatever the model asks for ---------------

def discover(make_surface, tmp_path, decider_class, script):
    surface = make_surface()
    decider = decider_class(script)
    redactor = Redactor()
    log = RunLog(tmp_path, "discovery", redactor)
    trace = DiscoveryAgent(surface, decider, log, redactor, max_steps=5).run(
        goal="test", app="test", base_url="http://test", entry_route="/page")
    log.close()
    return trace, decider


def test_discovery_reports_a_blocked_action_back_to_the_model(make_surface, tmp_path, served, scripted_decider):
    trace, decider = discover(make_surface, tmp_path, scripted_decider, [("click", "Admin"), ("done", None)])
    assert trace.status == "success"
    assert trace.steps[0].ok is False and "blocked by policy" in trace.steps[0].error
    assert "blocked by policy" in decider.prompts[1]  # the model is told, so it can change course
    assert "/admin/reset" not in served


def test_discovery_escalates_at_an_irreversible_control(make_surface, tmp_path, served, scripted_decider):
    trace, _ = discover(make_surface, tmp_path, scripted_decider, [("click", "Confirm")])
    assert trace.status == "escalated"
    assert "needs human approval" in trace.reason
    assert "/review" not in served
