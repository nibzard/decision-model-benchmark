"""Native Gemini generateContent adapter with billed thinking/cache usage."""

from __future__ import annotations

import os

from .base import ContenderError, Decision, MalformedReply
from .jsonmode import JSONModeContender, parse_decision_payload
from .llm_openai import DECISION_SCHEMA
from .render import SYSTEM_PROMPT, render_prompt


def gemini_usage(response: object) -> tuple[int | None, int | None, dict]:
    usage = response.get("usageMetadata") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None, None, {}

    def count(key, default=None):
        value = usage.get(key, default)
        return value if type(value) is int and value >= 0 else None

    inp = count("promptTokenCount")
    candidates = count("candidatesTokenCount")
    thoughts = count("thoughtsTokenCount", 0)
    total = count("totalTokenCount")
    out = candidates + thoughts if candidates is not None and thoughts is not None else None
    # totalTokenCount includes thinking even when a separate thought count is absent.
    if total is not None and inp is not None and total >= inp:
        out = total - inp
    details = dict(usage)
    details.update(
        cache_read_input_tokens=count("cachedContentTokenCount", 0),
        input_tokens_include_cache=True,
    )
    return inp, out, details


class GeminiContender(JSONModeContender):
    """One pinned Gemini endpoint; no tools, grounding or explicit cache storage."""

    def __init__(self, model: str) -> None:
        super().__init__(
            name=f"gemini:{model}",
            model=model,
            url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
            temperature=0.0,
            max_tokens=4000,
        )
        self._thinking_level = "minimal" if "flash-lite" in model else "low"
        self.deviation = (
            f"{self.name}: thinking pinned to {self._thinking_level} (minimum supported)"
        )
        self.notes = ["standard paid-tier list prices; actual account tier/invoice not reconciled"]

    def effective_configuration(self) -> dict:
        return {
            "model": self.model,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "thinking": {"level": self._thinking_level},
            "response_format": DECISION_SCHEMA,
        }

    def _decide(self, state: str, options: list[str]) -> Decision:
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": render_prompt(state, options)}]}],
            "generationConfig": {
                "temperature": self._temperature,
                "maxOutputTokens": self._max_tokens,
                "thinkingConfig": {"thinkingLevel": self._thinking_level},
                "responseMimeType": "application/json",
                "responseJsonSchema": DECISION_SCHEMA,
            },
        }
        response = self._post(body)
        inp, out, details = gemini_usage(response)
        try:
            candidate = response["candidates"][0]
            if candidate.get("finishReason") != "STOP":
                raise MalformedReply("Gemini did not finish a complete decision", raw=response)
            content = "".join(
                part["text"]
                for part in candidate["content"]["parts"]
                if not part.get("thought", False) and isinstance(part.get("text"), str)
            )
            choice, confidence = parse_decision_payload(content, len(options))
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            malformed = MalformedReply("unexpected Gemini response shape", raw=response)
            malformed.attach(inp, out, response, details)
            raise malformed from exc
        except MalformedReply as exc:
            exc.attach(inp, out, response, details)
            raise
        return Decision(
            choice_index=choice,
            confidence=confidence,
            input_tokens=inp,
            output_tokens=out,
            usage_details=details,
            raw={"provider": self.provider, "model": self.model, "response": response},
        )

    def _post(self, body: dict) -> dict:
        try:
            return self._post_response(body)
        except ContenderError as exc:
            response = getattr(self._response_context, "response", None)
            if response is not None:
                try:
                    payload = response.json()
                except ValueError:
                    exc.response = response.text
                else:
                    inp, out, details = gemini_usage(payload)
                    exc.attach(inp, out, payload, details)
            raise
