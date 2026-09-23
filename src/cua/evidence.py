"""Structured run evidence: one directory per run with events.jsonl, screenshots and JSON files.
Everything written here goes through the Redactor first."""

import json
import re
import secrets
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cua.redaction import Redactor


def now_iso(timespec: str = "milliseconds") -> str:
    return datetime.now(timezone.utc).isoformat(timespec=timespec)


class RunLog:
    def __init__(self, root: Path, kind: str, redactor: Redactor):
        self.run_id = f"{kind}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        self.dir = root / kind / self.run_id
        (self.dir / "screenshots").mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self.recent: deque[dict] = deque(maxlen=12)  # context for intervention requests
        self._seq = 0
        self._events = (self.dir / "events.jsonl").open("a", encoding="utf-8")

    def event(self, type_: str, **data: Any) -> None:
        self._seq += 1
        envelope = {"ts": now_iso(), "run_id": self.run_id, "seq": self._seq, "type": type_}
        # The envelope always wins: a payload field called "type" or "ts" must not relabel an event.
        record = {**envelope, **{k: v for k, v in self.redactor.scrub(data).items() if k not in envelope}}
        self._events.write(json.dumps(record, default=str) + "\n")
        self._events.flush()
        self.recent.append({k: v for k, v in record.items() if k not in ("run_id",)})

    def screenshot_path(self, label: str) -> Path:
        return self.dir / "screenshots" / f"{self._seq + 1:03d}-{label}.png"

    def write_json(self, name: str, obj: Any) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.redactor.scrub(obj), indent=2, default=str), encoding="utf-8")
        return path

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.dir))

    def close(self) -> None:
        self._events.close()


def capture(log: RunLog, surface, label: str) -> str | None:
    """Screenshot into the run's evidence directory. Evidence must never break a run."""
    path = log.screenshot_path(re.sub(r"[^a-z0-9._-]+", "-", label.lower()))
    try:
        surface.screenshot(path)
    except Exception as e:
        log.event("screenshot_failed", error=str(e))
        return None
    return log.rel(path)
