"""Risk classification. The allowlist half of the policy lands here too (next step);
keeping both in one module means discovery, the recorder and replay share one set of rules.
"""

import re

# Controls whose activation a bank cannot take back. Conservative on purpose: a false
# "irreversible" costs one human approval, a false "safe" costs a wrong transaction.
IRREVERSIBLE_NAME = re.compile(
    r"(?i)\b(confirm|submit|post|approve|authorize|transfer|send|pay|delete|remove|close|void|reverse)\b")


def click_risk(control_name: str, form_method: str = "") -> str:
    """safe: navigation only. reversible: a form submission that may change state.
    irreversible: a control whose name says it commits something."""
    if IRREVERSIBLE_NAME.search(control_name or ""):
        return "irreversible"
    if (form_method or "").lower() == "post":
        return "reversible"
    return "safe"


def highest(risks: list[str]) -> str:
    order = ["safe", "reversible", "irreversible"]
    return max(risks, key=order.index, default="safe")
