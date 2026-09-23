# Computer-Use Automation System

An LLM discovers how to do a task in a legacy back-office web app; the successful run is
recorded as a typed, versioned **capability artifact**; that artifact is then replayed
deterministically, with no model in the decision loop, which is how an AI agent would invoke
it in production.

The automation target is **LegacyCore**, a deliberately hostile mock core-banking app bundled
in this repo (frameset, table layouts, no ids or test ids, labels as adjacent table cells,
injectable runtime faults). All of its data is fake.

## Prerequisites

- Python 3.12+
- A Google **Gemini** API key (free tier is enough): https://aistudio.google.com → *Get API key*.
  Only *discovery* calls the model.

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"
.venv/bin/playwright install chromium
cp .env.example .env    # then paste your key into GEMINI_API_KEY
```

`.env` holds the model choice and the mock app's sign-in details. Credentials are read from the
environment by the session provider and never reach the LLM, the artifact or the logs. Never
commit `.env`.

## Run the target app

In one terminal:

```bash
.venv/bin/python -m target_app
```

It serves http://localhost:5001 (sign in with the `TARGET_APP_USER` / `TARGET_APP_PASSWORD`
values from `.env`). No API key needed. Useful data for demos:

| Input | What happens |
|---|---|
| member `10001`–`10050` | exists |
| member `99999` | "No member found" — a business outcome, not a failure |
| member `10013`, `10037` | frozen: "not eligible for new sub-accounts" |
| initial deposit under `$25` | inline validation error |

Faults can be injected at startup (`--fault NAME`) or at runtime:

```bash
curl -X POST localhost:5001/admin/fault -d name=interstitial   # also: slow, session_timeout, permission_denied, app_error
curl -X POST localhost:5001/admin/fault -d name=              # clear
curl -X POST localhost:5001/admin/reset                       # fresh seeded data, no restart
```

`/admin/*` is denied by the policy, so the agent can never reach these hooks.

## Demo: a discovery run

With the target app running, in a second terminal:

```bash
.venv/bin/cua discover --goal "look up member 10023 and read their savings balance"
```

Add `--headed` to watch the browser. Output is a structured result plus a run directory:

```
evidence/discovery/<run_id>/
  events.jsonl        every decision and action, with the model's rationale (redacted)
  trace.json          the actions plus the validated locator strategies for each target
  screenshots/        one per action, with sensitive fields blacked out
```

On success it also records the capability to
`capabilities/<app>/<name>/v<N>.yaml` (add `--no-record` to skip) and copies it into the run
directory. `cua record --trace <path>` rebuilds one from an older trace, and `cua schema`
exports the artifact JSON Schema that a calling agent would read.

## Demo: replay, with no LLM in the loop

```bash
CAP=capabilities/legacycore/member_savings_balance/v1.yaml

.venv/bin/cua replay $CAP --input member_id=10023     # success + typed outputs
.venv/bin/cua replay $CAP --input member_id=99999     # business outcome: MEMBER_NOT_FOUND
.venv/bin/cua replay $CAP --input member_id=123       # rejected against the input contract
.venv/bin/cua replay $CAP --input member_id=10023 --inject-fault interstitial     # recovered
.venv/bin/cua replay $CAP --input member_id=10023 --inject-fault session_timeout  # recovered
.venv/bin/cua replay $CAP --input member_id=10023 --inject-fault slow             # waited out
.venv/bin/cua replay $CAP --input member_id=10023 --inject-fault app_error        # hard failure
```

Replay needs no API key. `--inject-fault` is a test hook that arms a mock-app fault *after*
sign-in, so the fault lands mid-run; `--headed` shows the browser.

The result is one of four shapes, discriminated on `status`: `success` (with typed outputs),
`business_outcome` (a legitimate answer such as "no such member", with its code), `failed`
(with the step, what was expected, what was observed and a screenshot), or `escalated` (a human
must act). Recoverable conditions are not a status: they are handled inside the run and listed
under `recovered`. Exit codes: 0 for success or a business outcome, 1 for a failure, 2 for an
escalation.

Recorded runs for every one of those branches, and the discovery run, are indexed in
[`evidence/README.md`](evidence/README.md).

## Safety policy

[`policies/legacycore.yaml`](policies/legacycore.yaml) is the allowlist: the one origin the
tool may touch, the routes it may load (with `/admin/*` and `/logout` denied), the action types
it may perform, what to do with irreversible controls (`require_approval`), and which fields
count as sensitive. It is enforced in code, in the surface layer, for discovery, sign-in and
replay alike; the LLM's prompt is not a control. Two layers:

1. every action is checked before it runs: action type, where a link or submit would go, and
   the control's risk (a name like *Confirm* or *Transfer*, or a step marked irreversible in
   the artifact, needs a human's approval);
2. the browser aborts any request outside the allowlist, which catches redirects and
   script-driven navigation the first layer cannot see.

The CLI refuses to start against a target the policy does not list:

```bash
.venv/bin/cua replay $CAP --input member_id=10023 --url https://example.com
# refusing to run against https://example.com: origin https://example.com is not allowlisted (policies/legacycore.yaml)
```

`--approve-irreversible` stands in for a human approving every irreversible step, for
unattended demos; a real operator approves them one at a time (next section).

## Human in the loop

`open_sub_account` ends in an irreversible **Confirm**, so it can't finish without a person.
Try it yourself, in a visible browser:

```bash
W=capabilities/legacycore/open_sub_account/v1.yaml
.venv/bin/cua replay $W --input member_id=10023 --input nickname="Rainy Day" --input initial_deposit=250.00 --console --headed
```

When it reaches Confirm, the run pauses and prints `HUMAN NEEDED`. Open
http://127.0.0.1:8765 to see the request: what, which step, why, where, a screenshot, and the
last events. From there, either:

- **Approve** — automation clicks Confirm itself (exactly once) and finishes; or
- **Take control** — you get the lease and use the *same* browser window the automation was
  driving. Click Confirm yourself, then **Resume automation**. Replay checks that the page
  really is "Sub-Account Opened" before extracting the new account number. If you resume
  without finishing the step, it asks again.
- **Abort** — the run ends `escalated`.

Without `--console`, the same run returns `escalated` and writes the request to
`interventions/` as a queued item. Discovery accepts `--console` too: an approval lets the
model's own click through, and after a take-over the model carries on from wherever you left
the app.

Who is in control is a single state with a lease: `automation → awaiting_human →
human_control → verifying → automation`. The policy gate refuses every automated action unless
automation holds control, and only the lease holder can hand it back. Your clicks and field
changes are recorded as `human_action` events with `actor: human`; passwords and sensitive
fields never record a value.

Without a person at the keyboard, `cua operator` stands in for one, through the same console
API and the live browser over CDP:

```bash
.venv/bin/cua replay $W --input member_id=10023 --input nickname="Rainy Day" --input initial_deposit=250.00 --console --cdp-port 9222 &
.venv/bin/cua operator --mode takeover --cdp-url http://127.0.0.1:9222 --click Confirm   # or --mode approve / abort
```

## Tests

```bash
.venv/bin/pytest -q
```

Covers the target app's flows and faults, redaction, locator synthesis/resolution against
hostile fixture HTML, trace → artifact recording, replay against the live app (success,
business outcome, recovered interstitial, recovered session expiry, hard failure, broken
locator, unparseable output, missing output, rejected input, drift signal, escalation on an
irreversible step — `ACTION_FAILED` and `SESSION_FAILED` are the two result codes with no
test), and the policy: route
rules, both enforcement layers, approval, and discovery obeying the gate (driven by a scripted
stand-in for the LLM); and the handoff: the control state machine and lease, the console, and
real approve / take-over / resume-without-finishing / abort handoffs on a live session for
both replay and discovery. No API key needed.

## Status

Built: the mock target app; the Surface abstraction (Playwright, frame-aware); the LLM
observe → decide → act discovery loop with stuck detection; the recorder and the versioned
capability artifact; deterministic replay with the error taxonomy, condition-based waits and
drift signals; the policy allowlist with two enforcement layers and irreversible-action
approval; human-in-the-loop escalation with approve / take over the live session / resume, an
operator console and human-action capture; redaction and run evidence.

See `REPORT.md` for the design and the deliberate cuts.
