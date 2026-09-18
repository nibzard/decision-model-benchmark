"""jev adapter (TypeSafe AI System One API).

Documented schema (docs.typesafe.ai, checked 2026-09-18):

    POST https://api.typesafe.ai/v1/systemone
    Authorization: Bearer <TYPESAFE_API_KEY>
    {
      "state": "<decision state text>",
      "model": "jev-latest",
      "questions": {
        "decision": {
          "type": "choice",
          "instructions": "...",
          "criteria": {"<option>": "<description>", ...}
        }
      }
    }

The answer carries ``choice`` (the criteria key chosen), a probability per
option, and a separate ``confidence``. The adapter maps the chosen key back
to an index into the same options list every other contender receives.

Gated on ``TYPESAFE_API_KEY``: the registry records a skip reason when the
key is absent instead of constructing the contender (SPEC.md T2.5).
"""

from __future__ import annotations

import os

import httpx

from .base import (
    AuthError,
    Contender,
    Decision,
    MalformedReply,
    ProviderRejected,
    RateLimited,
    TransportError,
)
from .jsonmode import jev_usage
from .render import render_jev_instructions, render_jev_state

TIMEOUT = httpx.Timeout(120.0, connect=15.0)
API_URL = "https://api.typesafe.ai/v1/systemone"

# Shared semantic core with the language-model prompt (render.py): the
# uncertainty requirement and the confidence definition cannot drift
# between contender classes. The provider's documented answer field is
# named "confidence"; the provider does not document it as a probability
# of correctness, so manifests label it "provider-defined confidence".
INSTRUCTIONS = render_jev_instructions()


class JevContender(Contender):
    """The decision model under test: ``typesafe:jev``."""

    def __init__(self, model: str = "jev-latest") -> None:
        api_key = os.environ.get("TYPESAFE_API_KEY")
        if not api_key:
            raise AuthError("TYPESAFE_API_KEY not set; jev is gated on access")
        self.name = "typesafe:jev"
        self.provider = "typesafe"
        self.model = model
        self._client = httpx.Client(
            timeout=TIMEOUT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
        )
        self.notes: list[str] = []

    def negotiate(self) -> dict[str, int] | None:
        """Probe with one cheap call; record the observed API surface.

        Returns the probe's reported usage so the runner can bill the
        negotiation call.
        """
        try:
            decision = self._decide("Probe: which storage tier does the nightly backup use?",
                                    ["cold storage", "hot storage"])
        except Exception as exc:  # noqa: BLE001 - negotiation must not crash setup
            self.notes.append(f"negotiation probe failed: {exc}")
            return None
        if decision.input_tokens is None and decision.output_tokens is None:
            return None
        return {
            "input_tokens": decision.input_tokens,
            "output_tokens": decision.output_tokens,
        }

    def _decide(self, state: str, options: list[str]) -> Decision:
        criteria = {option: option for option in options}
        body = {
            "state": render_jev_state(state),
            "model": self.model,
            "questions": {
                "decision": {
                    "type": "choice",
                    "instructions": INSTRUCTIONS,
                    "criteria": criteria,
                }
            },
        }
        response = self._post(body)
        try:
            answer = response["answers"]["decision"]
            choice_key = answer["choice"]
            confidence = answer.get("confidence")
            if choice_key not in criteria:
                raise MalformedReply(
                    f"choice {choice_key!r} not among criteria keys", raw=response
                )
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise MalformedReply(
                    f"confidence missing or not a number: {confidence!r}", raw=response
                )
            confidence = float(confidence)
            if not 0.0 <= confidence <= 1.0:
                raise MalformedReply(
                    f"confidence out of bounds: {confidence}", raw=response
                )
        except MalformedReply as exc:
            # The HTTP call succeeded; bill and record its usage even though
            # the reply never became a decision.
            exc.attach(*jev_usage(response), response)
            raise
        except (KeyError, TypeError) as exc:
            malformed = MalformedReply(f"unexpected response shape: {exc}", raw=response)
            malformed.attach(*jev_usage(response), response)
            raise malformed from exc
        input_tokens, output_tokens = jev_usage(response)
        return Decision(
            choice_index=options.index(choice_key),
            confidence=confidence,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            raw={
                "provider": "typesafe",
                "model": self.model,
                "response": response,
                "notes": list(self.notes),
            },
        )

    def _post(self, body: dict) -> dict:
        try:
            response = self._client.post(API_URL, json=body)
        except httpx.TimeoutException as exc:
            raise TransportError(f"timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"connection error: {exc}") from exc
        if response.status_code == 429:
            raise RateLimited(f"429 (retry-after: {response.headers.get('retry-after', '?')})")
        if response.status_code in (401, 403):
            raise AuthError(f"{response.status_code}: {response.text[:300]}")
        if response.status_code == 400:
            # A well-formed request the provider refuses is a measured
            # outcome (option-count cap, key length cap, and so on).
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
