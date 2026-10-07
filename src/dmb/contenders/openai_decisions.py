"""OpenAI Decisions API adapter.

Published contract (developers.openai.com/api/docs/guides/decisions,
checked 2026-10-07; API changelog entry dated 2026-10-06):

    POST https://api.openai.com/v1/decisions
    Authorization: Bearer <OPENAI_API_KEY>
    {
      "model": "gpt-6-luna",
      "input": "<decision state text>",
      "questions": [
        {
          "name": "decision",
          "type": "choice",
          "instructions": "...",
          "choices": [{"value": "<option>", "description": "<option>"}, ...]
        }
      ]
    }

The response carries an ``answers`` array; each answer echoes the question
``name`` and may instead be a ``refusal``. A ``choice`` answer carries
``choice`` (a supplied value), a probability per option, and a separate
``confidence``. The adapter maps the chosen value back to an index into
the same options list every other contender receives.

The guide names ``gpt-6-luna`` as the only model and documents no rate
limits. Input costs $0.10 per 1M tokens and only input tokens are billed.
The guide does not show the usage object, so ``decisions_usage`` accepts
either documented field-naming convention and the raw response is kept in
full for verification after a run.

Gated on ``OPENAI_API_KEY``: the registry records a skip reason when the
key is absent instead of constructing the contender (SPEC.md T2.5).
"""

from __future__ import annotations

import os
from threading import local
from typing import Any

import httpx

from .base import (
    AuthError,
    Contender,
    ContenderError,
    Decision,
    MalformedReply,
    ProviderRejected,
    RateLimited,
    TransportError,
    negotiate_calls,
)
from .jsonmode import usage_details
from .render import render_choice_instructions, render_plain_state

TIMEOUT = httpx.Timeout(120.0, connect=15.0)
API_URL = "https://api.openai.com/v1/decisions"

# The one question this adapter asks; the answer is matched by this name.
QUESTION_NAME = "decision"

# Same semantic core as the language-model prompt and the jev question
# (render.py). The guide documents a separate ``confidence`` field on
# choice answers but does not define it as a probability of correctness,
# so manifests label it "provider-defined confidence".
INSTRUCTIONS = render_choice_instructions()


def decisions_usage(response: Any) -> tuple[int | None, int | None]:
    """Input and output usage, or None where the response reports nothing.

    The guide does not show the usage object, so both the
    ``input_tokens``/``output_tokens`` and the chat-style
    ``prompt_tokens``/``completion_tokens`` names are accepted. Nothing is
    substituted for absent fields; the raw response is preserved so a run
    can be reconciled against the invoice.
    """
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None, None

    def count(*keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    return count("input_tokens", "prompt_tokens"), count("output_tokens", "completion_tokens")


class OpenAIDecisionsContender(Contender):
    """The Decisions API under test: ``openai-decisions:gpt-6-luna``."""

    def __init__(self, model: str = "gpt-6-luna") -> None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise AuthError("OPENAI_API_KEY not set; openai-decisions is gated on access")
        self.name = "openai-decisions:gpt-6-luna"
        self.provider = "openai-decisions"
        self.model = model
        self._client = httpx.Client(
            timeout=TIMEOUT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
        )
        self._response_context = local()
        self.notes: list[str] = []

    def negotiate(self, before_attempt=None, on_attempt=None) -> dict:
        return negotiate_calls(self, before_attempt, on_attempt)

    def effective_configuration(self) -> dict:
        return {"model": self.model, "confidence_semantics": "provider-defined confidence"}

    def _decide(self, state: str, options: list[str]) -> Decision:
        body = {
            "model": self.model,
            "input": render_plain_state(state),
            "questions": [
                {
                    "name": QUESTION_NAME,
                    "type": "choice",
                    "instructions": INSTRUCTIONS,
                    "choices": [{"value": option, "description": option} for option in options],
                }
            ],
        }
        response = self._post(body)
        try:
            answer = _answer_for(response, QUESTION_NAME)
            if answer.get("type") == "refusal":
                raise MalformedReply("refusal answer", raw=response)
            choice_value = answer["choice"]
            confidence = answer.get("confidence")
            if choice_value not in options:
                raise MalformedReply(
                    f"choice {choice_value!r} not among supplied values", raw=response
                )
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise MalformedReply(
                    f"confidence missing or not a number: {confidence!r}", raw=response
                )
            confidence = float(confidence)
            if not 0.0 <= confidence <= 1.0:
                raise MalformedReply(f"confidence out of bounds: {confidence}", raw=response)
        except MalformedReply as exc:
            # The HTTP call succeeded; bill and record its usage even though
            # the reply never became a decision.
            exc.attach(
                *decisions_usage(response), response, usage_details(response, "openai")
            )
            raise
        except (KeyError, TypeError, AttributeError, IndexError) as exc:
            malformed = MalformedReply(f"unexpected response shape: {exc}", raw=response)
            malformed.attach(
                *decisions_usage(response), response, usage_details(response, "openai")
            )
            raise malformed from exc
        input_tokens, output_tokens = decisions_usage(response)
        return Decision(
            choice_index=options.index(choice_value),
            confidence=confidence,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_details=usage_details(response, "openai"),
            raw={
                "provider": self.provider,
                "model": self.model,
                "response": response,
                "notes": list(self.notes),
            },
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
                    payload = None
                    exc.response = response.text
                if payload is not None:
                    exc.attach(
                        *decisions_usage(payload), payload, usage_details(payload, "openai")
                    )
            raise

    def _post_response(self, body: dict) -> dict:
        self._response_context.response = None
        try:
            response = self._client.post(API_URL, json=body)
        except httpx.TimeoutException as exc:
            raise TransportError(f"timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"connection error: {exc}") from exc
        self._response_context.response = response
        if response.status_code == 429:
            raise RateLimited(f"429 (retry-after: {response.headers.get('retry-after', '?')})")
        if response.status_code in (401, 403):
            raise AuthError(f"{response.status_code}: {response.text[:300]}")
        if response.status_code == 400:
            # A well-formed request the provider refuses is a measured
            # outcome (question-count cap, choice-count cap, and so on).
            raise ProviderRejected(f"400: {response.text[:500]}")
        if response.status_code >= 500:
            raise TransportError(f"{response.status_code}: {response.text[:300]}")
        if response.status_code != 200:
            raise TransportError(f"{response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise MalformedReply(f"non-JSON HTTP body: {exc}", raw=response.text[:2000]) from exc

    def close(self) -> None:
        self._client.close()


def _answer_for(response: Any, name: str) -> dict:
    """The answer whose name matches the question asked."""
    answers = response["answers"]
    if not isinstance(answers, list):
        raise TypeError("answers is not a list")
    for answer in answers:
        if isinstance(answer, dict) and answer.get("name") == name:
            return answer
    raise KeyError(f"no answer named {name!r}")
