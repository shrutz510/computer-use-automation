"""The only module that talks to an LLM provider. Swap providers by adding another Decider."""

import os
import time
from dataclasses import dataclass
from typing import Any, Protocol

from google import genai
from google.genai import errors, types

DEFAULT_MODEL = "gemini-3.6-flash"  # pinned, not "-latest", so discovery runs are reproducible
RETRYABLE = {429, 500, 503}


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


class DeciderError(Exception):
    pass


class Decider(Protocol):
    model: str

    def decide(self, system: str, prompt: str, tools: list[dict[str, Any]]) -> ToolCall: ...


class GeminiDecider:
    """Stateless per step: each call gets the goal, the action history and the current
    screen, and must answer with exactly one tool call (function-calling mode ANY)."""

    def __init__(self, model: str | None = None, max_attempts: int = 6):
        self.model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        self.max_attempts = max_attempts
        self._client = genai.Client()

    def decide(self, system: str, prompt: str, tools: list[dict[str, Any]]) -> ToolCall:
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t["name"], description=t["description"], parameters_json_schema=t["parameters"])
                for t in tools
            ])],
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY")),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        last_error = "no tool call returned"
        for attempt in range(self.max_attempts):
            try:
                resp = self._client.models.generate_content(model=self.model, contents=prompt, config=config)
            except errors.APIError as e:
                if e.code not in RETRYABLE:
                    raise DeciderError(f"{self.model}: {e.code} {e.message}") from e
                last_error = f"{e.code} {e.message}"
                time.sleep(min(60, 5 * 2 ** attempt))  # free tier rate limits: back off and retry
                continue
            if resp.function_calls:
                call = resp.function_calls[0]
                return ToolCall(name=call.name or "", args=dict(call.args or {}))
        raise DeciderError(f"{self.model}: gave up after {self.max_attempts} attempts ({last_error})")
