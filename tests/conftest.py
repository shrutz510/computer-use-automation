"""Shared fixtures: the live mock app on a free port, and a scripted stand-in for the LLM."""

import os
import re
import threading

import pytest
from werkzeug.serving import make_server

from cua.agent.llm import ToolCall
from target_app import create_app


@pytest.fixture(scope="session")
def live_app():
    os.environ["TARGET_APP_USER"] = "teller"
    os.environ["TARGET_APP_PASSWORD"] = "teller-pass"
    app = create_app()
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}", app
    srv.shutdown()
    thread.join(timeout=5)


class ScriptedDecider:
    """Stands in for the LLM: clicks controls by name, recording every prompt it was shown."""

    model = "scripted"

    def __init__(self, script: list[tuple[str, str | None]]):
        self.script = list(script)
        self.prompts: list[str] = []

    def decide(self, system, prompt, tools):
        self.prompts.append(prompt)
        tool, name = self.script.pop(0)
        if tool != "click":
            return ToolCall(tool, {"summary": "done", "reason": name or "stop"})
        ref = re.search(rf'\[(f\d+-\d+)\] \w+ "{re.escape(name)}"', prompt).group(1)
        return ToolCall("click", {"ref": ref, "rationale": f"click {name}"})


@pytest.fixture
def scripted_decider():
    return ScriptedDecider
