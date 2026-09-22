# Computer-Use Automation System — design write-up

The through-line: **the model discovers once, the artifact becomes the capability, deterministic
replay is how an agent invokes it.** The evidence in [`evidence/`](evidence/README.md) shows
each stage actually running.

## 1. Architecture

One process, a CLI (`cua discover | replay | operator | record | schema`) and a small HTTP
server for the operator console. Boundaries are module seams, not services:

| Module | Responsibility |
|---|---|
| `surface/` | The seam: `observe / describe / resolve / act / screenshot` over semantic targets. `web.py` is the one real implementation (Playwright, frame-aware); `guarded.py` wraps it with the policy gate. |
| `agent/` | LLM observe → decide → act loop, tools, stuck detection, the discovery trace. |
| `recorder/`, `artifact/` | Trace → capability artifact; typed schema, versioned YAML, JSON Schema export, per-app packs. |
| `replay/` | Deterministic executor and the result contract. |
| `policy.py`, `handoff/`, `redaction.py`, `evidence.py`, `session.py` | Guardrails, human control, the redaction choke point, run evidence, sign-in. |

**Perception is accessibility-first**: each frame becomes role + accessible name, plus the
visible label inferred from the adjacent table cell (legacy forms rarely use `<label for>`),
plus table row/column headers. Screenshots are evidence, never targeting — coordinates do not
replay. This is also what a desktop surface exposes (UIA/AX), which is why it is the seam
rather than the DOM.

**The model gets custom tools** (`click`, `type_text`, `select_option`, `extract`, `done`,
`escalate`) instead of coordinates, so each action captures a *semantic* target — the thing
that makes replay deterministic. The loop is stateless per step (goal + history + current
screen): small prompts, no transcript drift, at the cost of re-reading the screen each turn.

**LLM: Gemini `gemini-3.6-flash`**, function calling, temperature 0, behind a one-method
`Decider` protocol — a free tier makes real discovery runs cheap, pinned (not `-latest`) for
reproducibility, swappable in one file. A scripted `Decider` stands in for it in tests, so the
loop's logic is covered deterministically and for free.

**Trade-offs.** No queue or database: the brief rewards judgment over infrastructure, and
everything durable is a file (artifacts, evidence, interventions). A crash loses the browser,
not the evidence. Sync Playwright means one session per process; concurrency would be one
process per session, which also keeps leases and takeover simple.

## 2. Artifact schema

[`member_savings_balance/v1.yaml`](capabilities/legacycore/member_savings_balance/v1.yaml) and
[`open_sub_account/v1.yaml`](capabilities/legacycore/open_sub_account/v1.yaml) are real recorded
output; [`artifact.schema.json`](capabilities/artifact.schema.json) is the machine-readable
contract.

It is a **capability contract, not a step list**: typed `inputs` (with patterns), typed
`outputs` (with `sensitive`), `preconditions`, `entry`, ordered `steps`, a `success` condition,
`detectors`, and metadata (id, version, `status`, app + version fingerprint, `risk_level`,
provenance). A caller can decide whether to invoke it, and what it returns, without reading a
step.

- **Targets are multi-strategy, ordered by robustness**: role + name → visible label → table
  row/column → scoped CSS → positional path. Every candidate is validated *live at record time*
  and kept only if it matches exactly one element — the one acted on. Cells are never addressed
  by their own text: that is data, not identity.
- **No concrete values in steps.** Flagged values become `{{inputs.x}}`, routes containing them
  are canonicalized (`/members/10023` → `^/members/[^/]+$`), and the description is generalized
  too. The recorded value survives only as `inputs.*.example` and in provenance.
- **A checkpoint per step**, derived from the state the app actually reached, so replay verifies
  instead of assuming.
- **The error taxonomy lives in the artifact.** Detectors come from a per-app pack
  ([`apps/legacycore.yaml`](apps/legacycore.yaml)) and are copied in, so the flow and the states
  it must survive are reviewed in one diff and the artifact stands alone.
- **Versioned files** under `capabilities/<app>/<name>/v<N>.yaml`, reviewable in a pull request;
  a registry is the next step, files needed no infrastructure. `status: draft` plus
  `provenance.approvals` / `human_actions` flag anything that needed a person.

**Known limit:** parameterization depends on the model flagging a value. In the write flow it
flagged member id, nickname and deposit but left "Money Market" literal — visible in the YAML,
and a one-line reviewer fix. That is the point of a readable artifact.

## 3. Determinism & error handling

Replay never calls the LLM. Per step: validate inputs against the contract → classify the state
→ resolve the target (first strategy with a unique match) → act through the policy gate → wait
for the checkpoint → verify the success condition.

- **Waits are conditions, never sleeps.** Playwright's `networkidle` returns immediately if the
  page was already idle *before* the action, so a click that starts a frame navigation is
  missed; the surface tracks in-flight requests itself, then waits for each frame to load. (A
  real bug from the first discovery run, now a regression test.)
- **The result is a discriminated union**: `success` (typed outputs), `business_outcome` (code +
  the app's own message), `failed` (code, step, expected, observed, screenshot, `retryable`),
  `escalated` (reason + intervention id).
- **Business outcomes are not failures.** "No member found" returns `MEMBER_NOT_FOUND` after one
  step. Detectors are re-checked on every checkpoint tick, so it surfaces as itself rather than
  as a checkpoint timeout — the most important ordering decision in the executor.
- **Recoverable conditions are not a status.** A blocking notice is acknowledged, a slow page
  waited out, an expired session re-authenticated and the flow restarted; each is listed in
  `recovered[]`. Restart-after-re-auth is refused for irreversible capabilities, which are not
  idempotent.
- **Drift is a signal, not a failure mode**: the winning strategy is recorded per step and a
  fallback winning is logged as `drift_signal`; the version fingerprint is the hook for the
  version check.

Every branch is covered against the live app — success, business outcome, recovered interstitial,
recovered session expiry, hard failure, broken locator, policy violation, escalation — in 66
tests that need no API key.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The artifact speaks only semantic descriptors (role + name, visible
label, table row/column) and a frame name. A `DesktopUIASurface` implements the same five
methods over UIA/AX, where those map almost one-to-one (control type + name, label relation,
grid row/column); OCR + coordinates is the universal last resort and the only one that cannot
replay reliably. Legacy web is already the hard case: framesets, table layouts, no ids, labels
as adjacent cells. Nothing above the surface knows what a DOM is.

**Multi-tenant.** Hundreds of tenants on one vendor product should share one recording: a **base
artifact** keyed by product and version range, plus a **tenant overlay** (label renames —
"Member #" → "Customer No." — route prefix, extra or disabled steps, credentials ref) merged at
load, Kustomize-style. The schema reserves `tenant_overrides`, and the app pack already
separates per-product knowledge (detectors, fingerprint) from per-flow knowledge. **Not
built:** the merge and a second tenant variant are the honest gap.

**Drift management.** Three signals — a fallback locator winning, a checkpoint mismatch, a
fingerprint outside range — flag the artifact, trigger a canary replay, and only then
re-discovery with the model, with the diff reviewed before the new version is approved. A model
in the loop for repair, never for production execution.

## 5. Escalation & handoff

**Detection.** Replay escalates when an action needs approval or it is stuck (target not found,
checkpoint not reached). Discovery escalates on the model calling `escalate`, three repeats of
the same action, four actions with no screen change, four consecutive failures, or the step
limit. Handoffs are bounded to three per run.

**The request** carries what an operator needs without digging: capability or goal, step,
reason, current URLs, a masked screenshot, recent events — written to
`evidence/<run>/interventions/<id>.json` whether or not anyone is listening. With no operator
attached the caller gets `escalated` with that id: a queued request.

**Control transfer is a state machine with a lease**: `automation → awaiting_human →
human_control → verifying → automation`. Only the lease holder hands control back; an approval
is a one-shot grant for exactly the named control. Crucially, **the policy gate refuses every
automated action unless automation holds control** — pause is enforced where actions happen,
not by convention.

**Same live session.** The operator drives the very browser the automation was using (headed
window, or attached over CDP as a remote tool would). Their clicks and edits are captured by a
listener injected into every frame and recorded as `actor: human`; since the automation's own
actions fire identical DOM events, only those made while a human holds control are counted.

**Resume is verified, not assumed.** After hand-back the executor re-observes and checks the
step's checkpoint: met → the human completed it, continue from the next step; not met → retry,
which for an irreversible step asks for approval again. All paths, plus abort, are in the
evidence and the tests.

**Mocked deliberately:** the console UI (one page, no auth, localhost) and remote viewing of the
browser — in production, a CDP screencast or noVNC, authentication, and a queue across runs. The
mechanism (pause, cede, capture, verify, resume) is real.

## 6. Safety

**One policy file** ([`policies/legacycore.yaml`](policies/legacycore.yaml)): allowed origin,
allowed and denied routes (`/admin/*`, `/logout`), allowed action types, the irreversible rule,
sensitive field labels. **Two enforcement layers, both in code:** every action is checked before
it runs (type, where a link or submit would go, the control's risk), and the browser aborts any
request outside the allowlist — catching redirects and script-driven navigation the first layer
cannot see. The prompt mentions policy only so the model reacts gracefully; it is never the
control. Policy errors are deliberately not surface errors, so a block is never retried as a
flaky click.

**Irreversible actions require approval**, rather than block (write flows become
un-automatable) or flag (noticed after the money moves). Risk is the higher of the artifact's
declared risk and a name rule, so a mislabelled Confirm is still caught. The CLI refuses to
start against a target the policy does not list.

**Data.** Credentials come from the environment via a session provider and never reach the
model, the artifact or the logs. Redaction is a single choke point: pattern rules (SSN, long
account/card numbers, dates, email, phone) plus values *learned* from fields the policy marks
sensitive, scrubbed everywhere for the rest of the run. Sensitive cells are hidden from the
model, masked in screenshots (fail-closed: an unscanned document is scanned first) and scrubbed
from evidence — while still being returned to the caller who asked for them. Page text is data,
never instructions.

**Limits, honestly.** Regex and label rules miss free-text PII, and screenshot masking depends
on the same labels. The console has no authentication and the CDP port exposes the live session
locally. The model sees page content — fine for seeded fake data, but a real deployment needs
data minimization and a provider agreement (a consumer free tier is not one). And nothing here
stops a malicious page from talking the model into something the policy already allows.

## 7. Cuts

**Cut deliberately, seams left real:** the desktop surface (protocol only); tenant overlay merge
and a second tenant; console polish, auth and remote browser view; an artifact registry and any
approval workflow beyond `status: draft`; all queueing and scaling infrastructure. Human-performed
steps are captured as evidence and counted in provenance but **not** folded back into the
artifact, so a flow finished by a person records only the model's steps — the provenance flag
tells the reviewer to check.

**Next, in order:** (1) fold human actions into the artifact as proposed steps for review,
closing that gap; (2) expose the capability catalog so agents can discover and invoke by name
with typed args — the JSON Schema is already exported; (3) tenant overlays plus canary replay
and the fingerprint check, which is what makes this scale to thousands of app instances;
(4) confidence scoring from replay history to gate `draft → approved`; (5) bounded,
policy-checked single-step LLM recovery on failure, recorded as evidence.
