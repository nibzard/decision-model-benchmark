"""Shared machinery for chat-completions providers with JSON output.

Every JSON-mode adapter shares this path: same prompt renderer, same
response parsing, same bounds checks, same error classification. Provider
subclasses supply the URL, headers, model, and response_format flavor.

Capability negotiation: some providers reject ``response_format`` or
``temperature``. On a 400 that names the rejected parameter, the adapter
drops it for the rest of the run and records the adaptation in
``self.notes``. Negotiation happens once, at construction time, via
``negotiate()`` - never during scored items.
"""

from __future__ import annotations

import json
import re
from threading import local
from typing import Any

import httpx

from .base import (
    AuthError,
    Contender,
    ContenderError,
    Decision,
    MalformedReply,
    RateLimited,
    TransportError,
    negotiate_calls,
)
from .render import SYSTEM_PROMPT, render_prompt

TIMEOUT = httpx.Timeout(120.0, connect=15.0)


class JSONModeContender(Contender):
    """Chat-completions contender with JSON-mode or JSON-schema output."""

    def __init__(
        self,
        name: str,
        model: str,
        url: str,
        headers: dict[str, str],
        response_format: dict[str, Any] | None = None,
        temperature: float | None = 0.0,
        max_tokens: int = 512,
        max_tokens_param: str = "max_tokens",
        extra_body: dict[str, Any] | None = None,
        price_name: str | None = None,
    ) -> None:
        self.name = name
        self.provider = name.split(":", 1)[0]
        self.model = model
        self.url = url
        self._headers = headers
        self._response_format = response_format
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_tokens_param = max_tokens_param
        self._extra_body = dict(extra_body or {})
        self._response_context = local()
        self.notes: list[str] = []
        self._price_name = price_name or name
        self._client = httpx.Client(timeout=TIMEOUT)

    # ---- protocol -------------------------------------------------------

    def negotiate(self, before_attempt=None, on_attempt=None) -> dict:
        return negotiate_calls(self, before_attempt, on_attempt)

    def effective_configuration(self) -> dict:
        return {
            "model": self.model,
            "temperature": self._temperature,
            "response_format": self._response_format,
            "max_tokens": self._max_tokens,
            "max_tokens_param": self._max_tokens_param,
            "extra_body": dict(self._extra_body),
        }

    def _decide(self, state: str, options: list[str]) -> Decision:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": render_prompt(state, options)},
            ],
        }
        if self._temperature is not None:
            body["temperature"] = self._temperature
        if self._response_format is not None:
            body["response_format"] = self._response_format
        body[self._max_tokens_param] = self._max_tokens
        body.update(self._extra_body)

        response = self._post(body)
        try:
            content = self._extract_content(response)
            choice_index, confidence = parse_decision_payload(content, len(options))
        except MalformedReply as exc:
            # The HTTP call succeeded; bill and record its usage even though
            # the reply never became a decision.
            exc.attach(*openai_style_usage(response), response, usage_details(response, "openai"))
            raise
        input_tokens, output_tokens = openai_style_usage(response)
        return Decision(
            choice_index=choice_index,
            confidence=confidence,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_details=usage_details(response, "openai"),
            raw={
                "provider": self.provider,
                "model": self.model,
                "response": response,
                "notes": list(self.notes),
                "price_contender": self._price_name,
            },
        )

    def close(self) -> None:
        self._client.close()

    # ---- HTTP -----------------------------------------------------------

    def _post(self, body: dict) -> dict:
        try:
            return self._post_response(body)
        except ContenderError as exc:
            response = getattr(self._response_context, "response", None)
            if response is not None:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                    exc.response = response.text
                if payload is not None:
                    exc.attach(
                        *openai_style_usage(payload), payload, usage_details(payload, "openai")
                    )
            raise

    def _post_response(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST and classify failures. Never retries."""
        self._response_context.response = None
        try:
            response = self._client.post(self.url, headers=self._headers, json=body)
        except httpx.TimeoutException as exc:
            raise TransportError(f"timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"connection error: {exc}") from exc

        self._response_context.response = response
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after", "?")
            raise RateLimited(f"429 (retry-after: {retry_after})")
        if response.status_code in (401, 403):
            raise AuthError(f"{response.status_code}: {response.text[:300]}")
        if response.status_code >= 500:
            raise TransportError(f"{response.status_code}: {response.text[:300]}")
        if response.status_code == 400:
            raise _bad_request_error(response.text, self._drop_unsupported)
        if response.status_code != 200:
            raise TransportError(f"{response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise MalformedReply(f"non-JSON HTTP body: {exc}", raw=response.text[:2000]) from exc

    def _drop_unsupported(self, parameter: str) -> None:
        """Drop a rejected parameter for the rest of the run."""
        if not getattr(self, "_negotiating", False):
            return
        if parameter == "response_format":
            self._response_format = None
            self.notes.append("dropped response_format after provider 400")
        elif parameter == "temperature":
            self._temperature = None
            self.notes.append("dropped temperature=0 after provider 400; provider default applies")
        elif parameter == "max_tokens":
            if self._max_tokens_param == "max_tokens":
                self._max_tokens_param = "max_completion_tokens"
                self.notes.append("renamed max_tokens -> max_completion_tokens after provider 400")
            else:
                self._max_tokens = 10_000  # provider cap; harmless for tiny outputs
                self.notes.append("raised max token budget after provider 400")
        else:
            self.notes.append(f"provider rejected unknown parameter {parameter}")

    def _extract_content(self, response: dict[str, Any]) -> str:
        try:
            choice = response["choices"][0]
            if choice.get("finish_reason") == "length":
                raise MalformedReply("truncated at max_tokens (finish_reason=length)", raw=response)
            message = choice["message"]
            content = message.get("content")
            if content is None and message.get("refusal"):
                raise MalformedReply(f"refusal: {message['refusal'][:200]}", raw=response)
            if not isinstance(content, str) or not content.strip():
                raise MalformedReply("empty content", raw=response)
            return content
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise MalformedReply(f"unexpected response shape: {exc}", raw=response) from exc


_BAD_PARAM_PATTERNS: list[tuple[str, str]] = [
    ("response_format", "response_format"),
    ("response format", "response_format"),
    ("temperature", "temperature"),
    ("max_tokens", "max_tokens"),
    ("max completion tokens", "max_tokens"),
]


def _bad_request_error(body: str, dropper: Any) -> Exception:
    """Classify a 400. Returns an error; may drop the named parameter."""
    lowered = body.lower()
    for needle, parameter in _BAD_PARAM_PATTERNS:
        if needle in lowered:
            dropper(parameter)
            return TransportError(f"400 rejected {parameter}: {body[:300]}")
    return TransportError(f"400: {body[:300]}")


# ---- usage extraction ------------------------------------------------------


def usage_details(response: Any, style: str = "openai") -> dict[str, Any]:
    """Preserve provider usage and normalize cache semantics without guessing.

    OpenAI/DeepSeek input totals include cache reads. Anthropic input_tokens
    exclude separately reported cache reads and writes.
    """
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return {}
    details = dict(usage)
    details["input_tokens_include_cache"] = style != "anthropic"
    nested = usage.get("prompt_tokens_details")
    if isinstance(nested, dict) and "cached_tokens" in nested:
        details["cache_read_input_tokens"] = nested["cached_tokens"]
    for key in ("prompt_tokens_details", "input_tokens_details"):
        nested = usage.get(key)
        if isinstance(nested, dict):
            if "cached_tokens" in nested:
                details["cache_read_input_tokens"] = nested["cached_tokens"]
            if "cache_write_tokens" in nested:
                details["cache_write_input_tokens"] = nested["cache_write_tokens"]
    if "prompt_cache_hit_tokens" in usage:
        details["cache_read_input_tokens"] = usage["prompt_cache_hit_tokens"]
    if "cache_read_input_tokens" in usage:
        details["cache_read_input_tokens"] = usage["cache_read_input_tokens"]
    if "cache_creation_input_tokens" in usage:
        details["cache_write_input_tokens"] = usage["cache_creation_input_tokens"]
    return details


def _usage_pair(response: Any, input_key: str, output_key: str) -> tuple[int | None, int | None]:
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None, None

    def count(key):
        value = usage.get(key)
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )

    return count(input_key), count(output_key)


def openai_style_usage(response: Any) -> tuple[int | None, int | None]:
    return _usage_pair(response, "prompt_tokens", "completion_tokens")


def anthropic_usage(response: Any) -> tuple[int | None, int | None]:
    return _usage_pair(response, "input_tokens", "output_tokens")


def jev_usage(response: Any) -> tuple[int | None, int | None]:
    return _usage_pair(response, "input_tokens", "output_tokens")


# ---- parsing ---------------------------------------------------------------

_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first JSON object from a reply. Raises MalformedReply."""
    text = text.strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    match = _OBJECT_RE.search(text)
    if match:
        try:
            value = json.loads(match.group(0))
            if isinstance(value, dict):
                return value
        except ValueError as exc:
            raise MalformedReply(f"unparseable JSON object: {exc}", raw=text[:2000]) from exc
    raise MalformedReply("no JSON object in reply", raw=text[:2000])


def parse_decision_payload(content: str, n_options: int) -> tuple[int, float]:
    """Extract and validate ``{choice_index, confidence}`` from a reply."""
    return validate_decision_dict(extract_json_object(content), n_options)


def validate_decision_dict(payload: dict[str, Any], n_options: int) -> tuple[int, float]:
    """Validate a parsed decision object against schema and bounds.

    Bounds: ``0 <= choice_index < n_options`` and
    ``0.0 <= confidence <= 1.0``. Booleans are rejected as numbers.
    """
    choice = payload.get("choice_index")
    confidence = payload.get("confidence")
    if isinstance(choice, bool) or not isinstance(choice, int):
        raise MalformedReply(f"choice_index missing or not an integer: {choice!r}", raw=payload)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise MalformedReply(f"confidence missing or not a number: {confidence!r}", raw=payload)
    confidence = float(confidence)
    if not 0.0 <= choice < n_options:
        raise MalformedReply(
            f"choice_index out of bounds: {choice} for {n_options} options", raw=payload
        )
    if not 0.0 <= confidence <= 1.0:
        raise MalformedReply(f"confidence out of bounds: {confidence}", raw=payload)
    return choice, confidence
