"""Structured run evidence: one directory per run with events.jsonl, screenshots and JSON files.
Everything written here goes through the Redactor first."""

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cua.redaction import Redactor


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RunLog:
    def __init__(self, root: Path, kind: str, redactor: Redactor):
        self.run_id = f"{kind}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        self.dir = root / kind / self.run_id
        (self.dir / "screenshots").mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self._seq = 0
        self._events = (self.dir / "events.jsonl").open("a", encoding="utf-8")

    def event(self, type_: str, **data: Any) -> None:
        self._seq += 1
        record = {"ts": _now(), "run_id": self.run_id, "seq": self._seq, "type": type_, **self.redactor.scrub(data)}
        self._events.write(json.dumps(record, default=str) + "\n")
        self._events.flush()

    def screenshot_path(self, label: str) -> Path:
        return self.dir / "screenshots" / f"{self._seq + 1:03d}-{label}.png"

    def write_json(self, name: str, obj: Any) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(self.redactor.scrub(obj), indent=2, default=str), encoding="utf-8")
        return path

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.dir))

    def close(self) -> None:
        self._events.close()
