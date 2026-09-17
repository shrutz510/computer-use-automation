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
```

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

A recorded run is in [`evidence/discovery/`](evidence/discovery/).

## Tests

```bash
.venv/bin/pytest -q
```

Covers the target app's flows and faults, redaction, and locator synthesis/resolution against
hostile fixture HTML.

## Status

Built: mock target app, the Surface abstraction (Playwright, frame-aware), the LLM
observe → decide → act discovery loop with stuck detection, redaction, and run evidence.

Next: the recorder (trace → capability artifact), deterministic replay with the error taxonomy,
the policy allowlist, and human escalation/handoff. See `REPORT.md` for the design and the
deliberate cuts.
