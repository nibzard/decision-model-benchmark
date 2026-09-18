"""Anthropic contenders with forced tool use for structured output."""

from __future__ import annotations

import os

import httpx

from .base import AuthError, Contender, Decision, MalformedReply, RateLimited, TransportError
from .jsonmode import anthropic_usage, validate_decision_dict
from .render import SYSTEM_PROMPT, render_prompt

TIMEOUT = httpx.Timeout(120.0, connect=15.0)

TOOL_SCHEMA: dict = {
    "name": "record_decision",
    "description": "Record the chosen option index and your confidence in it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "choice_index": {
                "type": "integer",
                "description": "Index into the OPTIONS list, 0-based.",
            },
            "confidence": {
                "type": "number",
                "description": "Probability that the chosen option is correct, 0.0 to 1.0.",
            },
        },
        "required": ["choice_index", "confidence"],
    },
}


class AnthropicContender(Contender):
    """One Anthropic contender: ``anthropic:<model>`` via forced tool use."""

    def __init__(self, model: str) -> None:
        self.name = f"anthropic:{model}"
        self.provider = "anthropic"
        self.model = model
        self._client = httpx.Client(
            base_url="https://api.anthropic.com",
            timeout=TIMEOUT,
            headers={
                "Authorization": f"Bearer {os.environ['ANTHROPIC_AUTH_TOKEN']}",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        self.notes: list[str] = []

    def negotiate(self) -> None:
        """Probe with one cheap call; adapt to rejected parameters."""
        try:
            self._decide("Probe: which storage tier does the nightly backup use?",
                         ["cold storage", "hot storage"])
        except Exception as exc:  # noqa: BLE001 - negotiation must not crash setup
            self.notes.append(f"negotiation probe failed: {exc}")

    def _decide(self, state: str, options: list[str]) -> Decision:
        body: dict = {
            "model": self.model,
            "max_tokens": 1024,
            "temperature": 0,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": render_prompt(state, options)}],
            "tools": [TOOL_SCHEMA],
            "tool_choice": {"type": "tool", "name": "record_decision"},
        }
        if getattr(self, "_temperature_off", False):
            body.pop("temperature")
        response = self._post(body)
        tool_input = self._extract_tool_input(response)
        if not isinstance(tool_input, dict):
            raise MalformedReply(f"tool input is not an object: {tool_input!r}", raw=response)
        choice_index, confidence = validate_decision_dict(tool_input, len(options))
        input_tokens, output_tokens = anthropic_usage(response)
        return Decision(
            choice_index=choice_index,
            confidence=confidence,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            raw={
                "provider": "anthropic",
                "model": self.model,
                "response": response,
                "notes": list(self.notes),
            },
        )

    def _post(self, body: dict) -> dict:
        try:
            response = self._client.post("/v1/messages", json=body)
        except httpx.TimeoutException as exc:
            raise TransportError(f"timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"connection error: {exc}") from exc
        if response.status_code == 429:
            raise RateLimited(f"429 (retry-after: {response.headers.get('retry-after', '?')})")
        if response.status_code in (401, 403):
            raise AuthError(f"{response.status_code}: {response.text[:300]}")
        if response.status_code >= 500:
            raise TransportError(f"{response.status_code}: {response.text[:300]}")
        if response.status_code == 400:
            if "temperature" in response.text.lower() and not getattr(
                self, "_temperature_off", False
            ):
                self._temperature_off = True
                self.notes.append("dropped temperature=0 after provider 400")
                raise TransportError(f"400 rejected temperature: {response.text[:300]}")
            raise TransportError(f"400: {response.text[:300]}")
        if response.status_code != 200:
            raise TransportError(f"{response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise MalformedReply(f"non-JSON HTTP body: {exc}", raw=response.text[:2000]) from exc

    def _extract_tool_input(self, response: dict) -> object:
        try:
            for block in response["content"]:
                if block.get("type") == "tool_use" and block.get("name") == "record_decision":
                    return block["input"]
        except (KeyError, TypeError) as exc:
            raise MalformedReply(f"unexpected response shape: {exc}", raw=response) from exc
        raise MalformedReply("no tool_use block in response", raw=response)

    def close(self) -> None:
        self._client.close()
