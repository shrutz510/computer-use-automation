"""Redaction: the single choke point every log line, trace and LLM prompt passes through.

Two layers:
  1. Pattern rules for well-shaped identifiers (SSN, long account/card numbers, dates, email, phone).
  2. Learned values: text read from fields the surface marked sensitive (e.g. the "Name"
     row) is remembered for the run and scrubbed everywhere it reappears.
Known limit: free-text PII that matches neither layer gets through.
"""

import re
from typing import Any

DEFAULT_SENSITIVE_LABELS = (
    "SSN", "Social Security Number", "Date of Birth", "DOB", "Name",
    "Account No.", "Account Number", "New Account Number",
)

PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{3}-\d{2}-(\d{4})\b"), r"***-**-\1"),                     # SSN, keep last 4
    (re.compile(r"\b\d{6,13}(\d{4})\b"), r"****\1"),                              # account / card numbers
    (re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b"), "[DATE]"),                     # ISO dates (DOB)
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"\(?\b\d{3}\)?[-. ]\d{3}-\d{4}\b"), "[PHONE]"),
]
REDACTED = "[REDACTED]"


class Redactor:
    def __init__(self) -> None:
        self._learned: set[str] = set()

    def learn(self, values: list[str]) -> None:
        self._learned.update(v for v in values if len(v) >= 3)

    def text(self, s: str) -> str:
        for value in sorted(self._learned, key=len, reverse=True):
            s = s.replace(value, REDACTED)
        for pattern, repl in PATTERNS:
            s = pattern.sub(repl, s)
        return s

    def scrub(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {k: self.scrub(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.scrub(v) for v in obj]
        return obj
