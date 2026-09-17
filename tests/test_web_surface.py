"""Locator synthesis and resolution against hostile fixture HTML (table layout, no ids, no <label>)."""

import pytest
from playwright.sync_api import sync_playwright

from cua.surface import CellStrategy, CssStrategy, LabelStrategy, RoleStrategy, Target, TargetNotFound
from cua.surface.web import WebSurface

FIXTURE = """
<html><head><title>Member Detail</title></head><body>
<form method="post" action="/members/find"><table>
  <tr><td>Member ID:</td><td><input type="text" name="mid"></td></tr>
  <tr><td></td><td><input type="submit" value="Search"></td></tr>
</table></form>
<table border="1">
  <tr><td>Member #</td><td>10023</td></tr>
  <tr><td>Name</td><td>Alex Sample</td></tr>
</table>
<table border="1">
  <tr><td>Type</td><td>Account No.</td><td>Balance</td></tr>
  <tr><td>Share Savings</td><td>7100231234</td><td>$1,234.56</td></tr>
  <tr><td>Checking</td><td>7100235678</td><td>$99.00</td></tr>
</table>
</body></html>
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


@pytest.fixture(scope="module")
def surface(browser):
    page = browser.new_page()
    page.set_content(FIXTURE)
    return WebSurface(page, "http://unused")


def ref_where(snap, **match):
    return next(e.ref for e in snap.elements.values() if all(getattr(e, k) == v for k, v in match.items()))


def test_observe_infers_adjacent_labels_and_headers(surface):
    snap = surface.observe()
    textbox = snap.elements[ref_where(snap, role="textbox")]
    assert textbox.label == "Member ID" and textbox.name == ""
    balance = snap.elements[ref_where(snap, text="$1,234.56")]
    assert (balance.row_header, balance.col_header) == ("Share Savings", "Balance")
    name = snap.elements[ref_where(snap, text="Alex Sample")]
    assert name.col_header == "" and name.sensitive  # key/value table: no bogus header; Name is sensitive


def test_describe_keeps_only_unique_strategies(surface):
    snap = surface.observe()
    textbox = surface.describe(ref_where(snap, role="textbox"))
    assert textbox.strategies[0] == LabelStrategy(text="Member ID")
    button = surface.describe(ref_where(snap, role="button"))
    assert button.strategies[0] == RoleStrategy(role="button", name="Search")
    cell = surface.describe(ref_where(snap, text="$1,234.56"))
    assert cell.strategies[0] == CellStrategy(row_header="Share Savings", column_header="Balance")
    assert all("1,234" not in str(s) for s in cell.strategies)  # never keyed on the value itself


def test_resolve_falls_through_to_first_unique(surface):
    target = Target(strategies=[CssStrategy(value="td"), CellStrategy(row_header="Checking", column_header="Balance")])
    resolved = surface.resolve(target)
    assert resolved.strategy_index == 1
    assert surface.read(resolved.handle) == "$99.00"


FRAMED_PAGES = {
    "/": '<html><frameset cols="100,*"><frame name="nav" src="/nav"><frame name="content" src="/search"></frameset></html>',
    "/nav": "<html><body><a href='/search' target='content'>Member Search</a></body></html>",
    "/search": """<html><head><title>Member Search</title></head><body><form method="post" action="/find">
        <table><tr><td>Member ID:</td><td><input name="mid"></td></tr>
        <tr><td></td><td><input type="submit" value="Search"></td></tr></table></form></body></html>""",
    "/find": """<html><head><title>Member Detail</title></head><body><table>
        <tr><td>Name</td><td>Alex Sample</td></tr></table></body></html>""",
}


def test_click_waits_for_frame_navigation_and_screenshot_masks_fresh_page(browser, tmp_path):
    """Regression: the click used to return before the content frame navigated, and the
    screenshot of the new, unscanned page went out unmasked."""
    page = browser.new_page()
    page.route("http://test/**", lambda route: route.fulfill(
        content_type="text/html", body=FRAMED_PAGES[route.request.url.removeprefix("http://test")]))
    s = WebSurface(page, "http://test")
    s.goto("/")
    s.observe()
    s.fill(s.resolve(Target(frame="content", strategies=[LabelStrategy(text="Member ID")])).handle, "10023")
    s.click(s.resolve(Target(frame="content", strategies=[RoleStrategy(role="button", name="Search")])).handle)
    # Straight to a screenshot, without observing first: it must scan and mask anyway.
    s.screenshot(tmp_path / "shot.png")
    content = page.frame(name="content")
    assert content.evaluate("document.title") == "Member Detail"
    assert content.locator("[data-cua-sensitive]").count() == 1
    page.close()


def test_resolve_reports_attempts_when_nothing_matches(surface):
    with pytest.raises(TargetNotFound) as exc:
        surface.resolve(Target(strategies=[RoleStrategy(role="button", name="Transfer")]))
    assert "role: 0 matches" in str(exc.value)
