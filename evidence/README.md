# Evidence

Every run directory holds `events.jsonl` (what happened, in order), `screenshots/` (sensitive
fields blacked out) and, for replays, `result.json` (the contract returned to the caller).
Discovery runs also hold `trace.json` and the `capability.yaml` they produced.

## Discovery — the real LLM run

| Run | Goal | Result |
|---|---|---|
| [discovery-20260917-145918-e415](discovery/discovery-20260917-145918-e415) | "look up member 10023 and read their savings balance" | success; recorded [capabilities/legacycore/member_savings_balance/v1.yaml](../capabilities/legacycore/member_savings_balance/v1.yaml) |

Model: `gemini-3.6-flash`. Three actions: type the member ID (flagged as the `member_id`
parameter), click Search, extract the Share Savings / Balance cell.

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

## Escalation

Covered by tests today, not yet by a captured run: an irreversible step returns
`escalated` unless a human approves it (`tests/test_replay.py`). The human-takeover
handoff is the next piece of work.
