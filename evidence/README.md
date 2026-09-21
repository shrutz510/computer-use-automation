# Evidence

Every run directory holds `events.jsonl` (what happened, in order), `screenshots/` (sensitive
fields blacked out) and, for replays, `result.json` (the contract returned to the caller).
Discovery runs also hold `trace.json` and the `capability.yaml` they produced.

## Discovery — the real LLM run

| Run | Goal | Result |
|---|---|---|
| [discovery-20260917-145918-e415](discovery/discovery-20260917-145918-e415) | "look up member 10023 and read their savings balance" | success; recorded [member_savings_balance/v1](../capabilities/legacycore/member_savings_balance/v1.yaml) |
| [discovery-20260921-134651-76c5](discovery/discovery-20260921-134651-76c5) | "For member 10023, open a new Money Market sub-account nicknamed Rainy Day with an initial deposit of 250.00, and return the new account number from the confirmation screen" | success after **one human approval**; recorded [open_sub_account/v1](../capabilities/legacycore/open_sub_account/v1.yaml) |

Model: `gemini-3.6-flash`. The read flow took three actions: type the member ID (flagged as
the `member_id` parameter), click Search, extract the Share Savings / Balance cell.

The write flow took nine. At step 8 the model asked to click **Confirm**; the policy gate, not
the model, classified that as irreversible and raised intervention
[`int-31b460`](discovery/discovery-20260921-134651-76c5/interventions/int-31b460.json) on the
operator console. It was approved there, the gate let exactly that click through once, and the
model carried on to extract the new account number. The recorded artifact carries
`provenance.approvals: 1` and marks the Confirm step `irreversible`.

## Replay — no LLM in the decision loop

All six replay the same artifact. Faults are armed after sign-in with `--inject-fault`, so they
land mid-run.

| Run | Input / fault | Result | Notes |
|---|---|---|---|
| [replay-20260917-153300-7d48](replay/replay-20260917-153300-7d48) | `member_id=10023` | `success`, `savings_balance=10527.64` | every step resolved on its preferred locator |
| [replay-20260917-153325-4139](replay/replay-20260917-153325-4139) | `member_id=99999` | `business_outcome` `MEMBER_NOT_FOUND` | a legitimate answer, not a failure; stops after step 1 |
| [replay-20260917-153530-bbf5](replay/replay-20260917-153530-bbf5) | interstitial | `success`, `recovered: system_notice` | `*-system_notice-detected.png` shows the blocking notice; acknowledged, then continued |
| [replay-20260917-153534-2220](replay/replay-20260917-153534-2220) | session_timeout | `success`, `recovered: session_expired` | re-authenticated and restarted the flow |
| [replay-20260917-153350-94f4](replay/replay-20260917-153350-94f4) | slow (2–5s per page) | `success` | waited on conditions; no status of its own |
| [replay-20260917-153406-a427](replay/replay-20260917-153406-a427) | app_error | `failed` `APP_ERROR`, `retryable: true` | expected vs observed recorded, with a screenshot |

Recoverable conditions never appear as a status: they are handled inside the run and listed in
`recovered`.

## Human in the loop — replaying the write flow

`open_sub_account` ends in an irreversible Confirm, so replay always needs a person there. The
same inputs (`member_id=10023`, `nickname=Rainy Day`, `initial_deposit=250.00`), three ways:

| Run | Operator | Result |
|---|---|---|
| [replay-20260921-135449-960b](replay/replay-20260921-135449-960b) | none attached | `escalated` at `s8_click_confirm`; the request is still written, as a queued [intervention](replay/replay-20260921-135449-960b/interventions) |
| [replay-20260921-135459-c649](replay/replay-20260921-135459-c649) | **approves** on the console | `success`: the gate let Confirm through once; `new_account_number` returned to the caller (redacted in the evidence) |
| [replay-20260921-135630-0b43](replay/replay-20260921-135630-0b43) | **takes control** of the same live session and clicks Confirm by hand, then resumes | `success`: replay verified the step's checkpoint ("Sub-Account Opened") before continuing to the extract step |

In the take-over run, the intervention record
([`interventions/*.json`](replay/replay-20260921-135630-0b43/interventions)) holds the request
(step, reason, where, a screenshot of the review screen, the last events), the full control
history `awaiting_human → human_control → verifying → automation`, and the human's own action
(`click "Confirm"` on `/accounts/review`). The same click appears in `events.jsonl` as a
`human_action` with `actor: human`.

The operator here is `cua operator`, a stand-in that uses exactly what a person uses: the
console's API for approve / claim / resume, and the live browser attached over CDP for the
click. See the README for doing it yourself in a visible browser.
