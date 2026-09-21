"""Operator stand-ins: for tests, and for producing evidence without a person at the keyboard.

They use exactly the paths a real operator uses: the console's HTTP API for the control moves,
and the live browser session itself for the manual steps, attached over CDP the way a remote
operator tool would attach. Nothing here reaches into the automation's process.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright


class OperatorError(RuntimeError):
    pass


def _request(url: str, data: dict | None = None) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise OperatorError(f"{e.code}: {e.read().decode()}") from e


def state(console_url: str) -> dict:
    return _request(f"{console_url}/api/state")


def wait_for_open(console_url: str, timeout_s: float = 120) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            snapshot = state(console_url)
        except (urllib.error.URLError, ConnectionError):
            snapshot = {}  # console not up yet
        if snapshot.get("state") == "awaiting_human" and snapshot.get("current"):
            return snapshot["current"]
        time.sleep(0.3)
    raise OperatorError(f"no intervention opened within {timeout_s}s")


def act(console_url: str, intervention_id: str, action: str, operator: str = "operator-sim",
        lease: str | None = None) -> dict:
    data = {"operator": operator}
    if lease:
        data["lease"] = lease
    return _request(f"{console_url}/api/interventions/{intervention_id}/{action}", data)


def click_in_live_session(cdp_url: str, button: str, frame: str | None = "content") -> None:
    """Attach to the automation's own browser over CDP and click, as the human would."""
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        page = next(p for c in browser.contexts for p in c.pages if p.url.startswith("http"))
        target = (page.frame(name=frame) if frame else None) or page.main_frame
        target.get_by_role("button", name=button, exact=True).click()
        page.wait_for_timeout(1000)  # let the navigation the click started land before resuming


def run_operator(console_url: str, mode: str, cdp_url: str | None = None, click: str | None = None,
                 frame: str | None = "content", operator: str = "operator-sim", timeout_s: float = 120) -> dict:
    """Wait for the next intervention and resolve it: approve | takeover | abort.
    takeover = claim the lease, optionally click something in the live session, then resume."""
    it = wait_for_open(console_url, timeout_s)
    if mode == "approve":
        return act(console_url, it["id"], "approve", operator)
    if mode == "abort":
        return act(console_url, it["id"], "abort", operator)
    if mode == "takeover":
        lease = act(console_url, it["id"], "claim", operator)["lease"]
        if click:
            if not cdp_url:
                raise OperatorError("--click needs --cdp-url to reach the live session")
            click_in_live_session(cdp_url, click, frame)
        return act(console_url, it["id"], "resume", operator, lease=lease)
    raise OperatorError(f"unknown mode {mode!r}")
