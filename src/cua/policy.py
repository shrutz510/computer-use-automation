"""The policy: what automation may touch and do. One set of rules for discovery, the
recorder and replay. Enforcement happens in cua.surface.guarded, never in a prompt."""

import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel

from cua.redaction import DEFAULT_SENSITIVE_LABELS

Action = Literal["navigate", "click", "type", "select", "extract"]
Risk = Literal["safe", "reversible", "irreversible"]
POLICY_ROOT = Path("policies")

# Controls whose activation a bank cannot take back. Conservative on purpose: a false
# "irreversible" costs one human approval, a false "safe" costs a wrong transaction.
DEFAULT_IRREVERSIBLE_NAMES = (
    r"(?i)\b(confirm|submit|post|approve|authorize|transfer|send|pay|delete|remove|close|void|reverse)\b")
UNCHECKED_SCHEMES = {"about", "data", "blob", "javascript", "chrome-error"}


class Policy(BaseModel):
    policy: str = "default"
    version: int = 1
    allowed_origins: list[str] = []
    allowed_routes: list[str] = []
    denied_routes: list[str] = []
    allowed_actions: list[Action] = ["navigate", "click", "type", "select", "extract"]
    irreversible: Literal["block", "require_approval", "flag"] = "require_approval"
    irreversible_names: str = DEFAULT_IRREVERSIBLE_NAMES
    sensitive_labels: list[str] = list(DEFAULT_SENSITIVE_LABELS)

    def route_violation(self, url: str) -> str | None:
        """None if a page navigation / state-changing request to `url` is allowed, else why not."""
        parts = urlsplit(url)
        if parts.scheme in UNCHECKED_SCHEMES:
            return None
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.allowed_origins:
            return f"origin {origin} is not allowlisted"
        path = parts.path or "/"
        for pattern in self.denied_routes:
            if fnmatch(path, pattern):
                return f"route {path} is denied ({pattern})"
        if not any(fnmatch(path, pattern) for pattern in self.allowed_routes):
            return f"route {path} is not allowlisted"
        return None

    def request_violation(self, url: str, method: str, is_navigation: bool) -> str | None:
        """Network-layer check. Page navigations and anything that is not a GET get the full
        route check; plain subresource GETs (favicon, images) only need an allowed origin."""
        if is_navigation or method.upper() != "GET":
            return self.route_violation(url)
        parts = urlsplit(url)
        if parts.scheme in UNCHECKED_SCHEMES:
            return None
        origin = f"{parts.scheme}://{parts.netloc}"
        return None if origin in self.allowed_origins else f"origin {origin} is not allowlisted"

    def action_violation(self, action: Action) -> str | None:
        return None if action in self.allowed_actions else f"action {action!r} is not allowed"

    def click_risk(self, control_name: str, form_method: str = "") -> Risk:
        """safe: navigation only. reversible: a form submission that may change state.
        irreversible: a control whose name says it commits something."""
        if re.search(self.irreversible_names, control_name or ""):
            return "irreversible"
        if (form_method or "").lower() == "post":
            return "reversible"
        return "safe"


def load_policy(app: str, root: Path = POLICY_ROOT) -> Policy:
    return Policy.model_validate(yaml.safe_load((root / f"{app}.yaml").read_text(encoding="utf-8")))


def highest(risks: list[str]) -> str:
    order = ["safe", "reversible", "irreversible"]
    return max(risks, key=order.index, default="safe")
