"""A deliberately bare operator console on 127.0.0.1.

Lists the open intervention with its context (what, which step, why, where, a screenshot, what
just happened) and offers the moves the state machine allows: approve, take control, resume,
abort. The UI is mock quality on purpose; the control model behind it is the real thing. A
JSON API under /api/ exposes the same moves for tools and tests.

Not built: operator authentication, multiple sessions, remote viewing of the browser (in
production: a CDP screencast or noVNC). Bound to localhost for that reason.
"""

import html
import json
import threading
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from cua.handoff.control import ControlError, Controller, SessionControl

LEASE_COOKIE = "cua_lease"


class OperatorConsole:
    def __init__(self, control: SessionControl, evidence_dir: Path, host: str = "127.0.0.1", port: int = 8765):
        self.control = control
        self.evidence_dir = evidence_dir
        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._server.console = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "OperatorConsole":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _Handler(BaseHTTPRequestHandler):
    server_version = "cua-console"

    @property
    def console(self) -> OperatorConsole:
        return self.server.console  # type: ignore[attr-defined]

    def log_message(self, *args) -> None:  # keep the automation's stderr readable
        pass

    # ---- GET -----------------------------------------------------------------

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _page(self.console.control.snapshot(), self._lease()).encode())
        elif self.path == "/api/state":
            self._json(200, self.console.control.snapshot())
        elif self.path.startswith("/interventions/") and self.path.endswith("/screenshot"):
            self._screenshot(self.path.split("/")[2])
        else:
            self._json(404, {"error": "not found"})

    def _screenshot(self, intervention_id: str) -> None:
        match = [i for i in self.console.control.interventions if i.id == intervention_id and i.screenshot]
        path = (self.console.evidence_dir / match[0].screenshot) if match else None
        if path is None or not path.is_file():
            self._json(404, {"error": "no screenshot"})
            return
        self._send(200, "image/png", path.read_bytes())

    # ---- POST: the operator's moves -----------------------------------------

    def do_POST(self) -> None:
        parts = [p for p in self.path.split("/") if p]
        api = parts[:1] == ["api"]
        parts = parts[1:] if api else parts
        if len(parts) != 3 or parts[0] != "interventions":
            self._json(404, {"error": "not found"})
            return
        intervention_id, action = parts[1], parts[2]
        length = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        operator = form.get("operator") or "operator"
        lease = form.get("lease") or self._lease()
        control = self.console.control
        set_lease = None
        try:
            if action == "approve":
                result = control.approve(intervention_id, operator).to_dict()
            elif action == "claim":
                set_lease = control.claim(intervention_id, operator)
                result = {"lease": set_lease, "intervention": intervention_id}
            elif action == "resume":
                result = control.resume(intervention_id, lease).to_dict()
            elif action == "abort":
                result = control.abort(intervention_id, operator, lease).to_dict()
            else:
                self._json(404, {"error": f"unknown action {action!r}"})
                return
        except ControlError as e:
            if api:
                self._json(409, {"error": str(e)})
            else:
                self._send(409, "text/html; charset=utf-8", _message(str(e)).encode())
            return
        if api:
            self._json(200, result)
            return
        self.send_response(303)
        self.send_header("Location", "/")
        if set_lease:
            self.send_header("Set-Cookie", f"{LEASE_COOKIE}={set_lease}; Path=/; HttpOnly; SameSite=Strict")
        self.end_headers()

    # ---- helpers ---------------------------------------------------------------

    def _lease(self) -> str | None:
        jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
        return jar[LEASE_COOKIE].value if LEASE_COOKIE in jar else None

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, "application/json", json.dumps(payload, default=str).encode())

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


# ---- HTML -------------------------------------------------------------------

STYLE = """
body{font:14px/1.45 -apple-system,Segoe UI,sans-serif;margin:24px auto;max-width:900px;padding:0 16px;color:#1d1d1f;background:#f6f6f3}
h1{font-size:20px;margin:0 0 4px} h2{font-size:15px;margin:24px 0 8px}
.state{display:inline-block;padding:2px 8px;border-radius:10px;font-weight:600;font-size:12px;background:#ddd}
.state.automation{background:#d9f0dd;color:#14532d}.state.awaiting_human{background:#fde7c7;color:#7c2d12}
.state.human_control{background:#dbe7fb;color:#1e3a8a}.state.verifying{background:#eee;color:#333}
.card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:16px;margin-top:16px}
.card h2{margin-top:0} th{text-align:left;color:#666;font-weight:500;padding:3px 12px 3px 0;vertical-align:top} td{padding:3px 12px 3px 0}
img{max-width:100%;border:1px solid #ccc;border-radius:4px;margin-top:8px}
form{display:inline} button{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid #888;background:#fff;cursor:pointer;margin:12px 8px 0 0}
button.primary{background:#1e3a8a;color:#fff;border-color:#1e3a8a} button.danger{color:#991b1b;border-color:#991b1b}
pre{background:#f3f3f0;padding:8px;border-radius:4px;overflow-x:auto;font-size:12px;white-space:pre-wrap}
.muted{color:#666}
"""


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _button(intervention_id: str, action: str, label: str, css: str = "") -> str:
    return (f'<form method="post" action="/interventions/{_e(intervention_id)}/{action}">'
            f'<button class="{css}">{_e(label)}</button></form>')


def _page(snapshot: dict, my_lease: str | None) -> str:
    state = snapshot["state"]
    current = snapshot["current"]
    body = [f'<h1>Operator console</h1><p>Session controller: <span class="state {state}">{_e(state)}</span></p>']
    if current is None:
        body.append('<div class="card"><p class="muted">No open intervention. Automation is in control.</p></div>')
    else:
        body.append(_card(current, state, my_lease))
    history = [i for i in snapshot["interventions"] if i["resolution"]]
    if history:
        rows = "".join(f"<tr><td>{_e(i['id'])}</td><td>{_e(i['kind'])}</td><td>{_e(i['step'])}</td>"
                       f"<td>{_e(i['resolution'])}</td><td>{_e(i['operator'])}</td><td>{len(i['human_actions'])}</td></tr>"
                       for i in reversed(history))
        body.append("<h2>Resolved</h2><table><tr><th>id</th><th>kind</th><th>step</th><th>resolution</th>"
                    f"<th>operator</th><th>human actions</th></tr>{rows}</table>")
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="2">'
            f"<title>Operator console</title><style>{STYLE}</style></head><body>{''.join(body)}</body></html>")


def _card(it: dict, state: str, my_lease: str | None) -> str:
    title = "Approval needed" if it["kind"] == "approval" else "Automation is stuck"
    rows = [("What", it["subject"]), ("Step", it["step"]), ("Why", it["reason"]), ("Where", it["url"]),
            ("Raised", it["created_at"])]
    table = "".join(f"<tr><th>{k}</th><td>{_e(v)}</td></tr>" for k, v in rows)
    shot = f'<img src="/interventions/{_e(it["id"])}/screenshot" alt="screen when the run stopped">' if it["screenshot"] else ""
    recent = "\n".join(f"{e.get('type')}: " + json.dumps({k: v for k, v in e.items() if k not in ('ts', 'seq', 'type')},
                                                          default=str)[:160] for e in it["recent_events"][-6:])
    parts = [f'<div class="card"><h2>{title} <span class="muted">{_e(it["id"])}</span></h2><table>{table}</table>{shot}',
             f"<h2>Just before</h2><pre>{_e(recent)}</pre>"]
    if state == Controller.AWAITING_HUMAN.value:
        if it["kind"] == "approval":
            parts.append(_button(it["id"], "approve", f"Approve “{it['control']}”", "primary"))
        parts.append(_button(it["id"], "claim", "Take control"))
        parts.append(_button(it["id"], "abort", "Abort run", "danger"))
    elif state == Controller.HUMAN_CONTROL.value:
        actions = "\n".join(f"{a.get('kind')} {a.get('name') or a.get('label') or a.get('tag')}"
                            + (f" = {a['value']}" if a.get("value") else "") + f"  ({a.get('url')})"
                            for a in it["human_actions"]) or "(nothing yet)"
        parts.append(f"<h2>{_e(it['operator'])} has control</h2><p>Use the automation's browser window (or a "
                     f"CDP-attached tool). Every action is recorded:</p><pre>{_e(actions)}</pre>")
        if my_lease:
            parts.append(_button(it["id"], "resume", "Resume automation", "primary"))
            parts.append(_button(it["id"], "abort", "Abort run", "danger"))
        else:
            parts.append('<p class="muted">Only the operator holding the lease can resume or abort.</p>')
    else:
        parts.append('<p class="muted">Verifying the state after the handover...</p>')
    parts.append("</div>")
    return "".join(parts)


def _message(text: str) -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>Operator console</title><style>{STYLE}</style>'
            f'</head><body><div class="card"><p>{_e(text)}</p><p><a href="/">Back</a></p></div></body></html>')
