"""Contender protocol and shared result types.

Every contender - decision model, constrained LLM, deterministic baseline -
receives the same serialized decision state and returns a ``Decision``
against the schema fixed in SPEC.md: ``{choice_index: int, confidence: float}``.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from pydantic import BaseModel, Field


class Decision(BaseModel):
    """One decision for one item.

    ``ok`` is False when the reply failed schema or bounds after the single
    protocol retry (``malformed``) or when the call failed for transport or
    rate-limit reasons (``error`` set). Malformed and errored items are never
    counted as wrong answers.
    """

    choice_index: int | None = None
    confidence: float | None = None
    latency_ms: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_details: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    malformed: bool = False
    error: str | None = None
    retries: int = 0


class ContenderError(Exception):
    """Base class for contender failures. Never counted as a wrong answer.

    When a provider response exists but the decision could not be parsed
    or validated from it, adapters attach the reported usage and the
    response so the runner can bill and record the attempt. The metadata
    travels as attributes; credentials never appear here.
    """

    category: str = "unknown"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.response: Any = None
        self.usage_details: dict[str, Any] = {}

    def attach(
        self,
        input_tokens: int | None,
        output_tokens: int | None,
        response: Any,
        usage_details: dict[str, Any] | None = None,
    ) -> None:
        """Record the usage and response of the call that failed."""
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.response = response
        self.usage_details = dict(usage_details or {})
        return


class MalformedReply(ContenderError):
    """Reply failed the schema or bounds check."""

    category = "schema"

    def __init__(self, message: str, raw: Any = None) -> None:
        super().__init__(message)
        self.raw = raw


class RateLimited(ContenderError):
    """Provider returned a rate-limit or overload response."""

    category = "transport"


class ProviderRejected(ContenderError):
    """Provider refused a well-formed request (for example an option-count

    cap). A measured outcome, not a client bug: the message ships in the
    raw log."""

    category = "provider"


class TransportError(ContenderError):
    """Network, timeout, or server error."""

    category = "transport"


class AuthError(ContenderError):
    """Authentication or permission failure. Not retryable."""

    category = "auth"


class Contender:
    """A named decision-maker with the one-call protocol from SPEC.md.

    Subclasses implement ``decide``. Implementations must not retry; the
    runner owns the retry protocol (one retry on malformed, one backoff
    retry on rate limit).
    """

    name: str = "contender"
    provider: str = "unknown"
    deviation: str | None = None
    """Protocol deviation this adapter pins, recorded in every run manifest."""

    def decide(self, state: str, options: list[str]) -> Decision:
        """Pick one option for the state. Raises ContenderError on failure."""
        start = time.perf_counter()
        decision = self._decide(state, options)
        decision.latency_ms = (time.perf_counter() - start) * 1000.0
        return decision

    def _decide(self, state: str, options: list[str]) -> Decision:
        raise NotImplementedError

    def negotiate(self) -> dict[str, int] | None:
        """Probe the provider once before scoring; return reported usage.

        The base implementation is a no-op for local contenders. Network
        contenders override it: the probe call consumes tokens, so the
        runner bills the returned usage as negotiation spend, kept separate
        from scored decisions.
        """
        return None

    @property
    def configuration_fingerprint(self) -> str:
        configuration = self.effective_configuration()
        return hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()

    def effective_configuration(self) -> dict[str, Any]:
        return {"name": self.name, "provider": self.provider}

    def close(self) -> None:  # noqa: B027 - optional cleanup
        """Release resources. Default: nothing."""


class MockContender(Contender):
    """Contender with scripted replies, for runner and report tests.

    ``replies`` entries are ``Decision`` objects (returned as-is) or
    ``ContenderError`` instances (raised). When the script is exhausted the
    last entry repeats. Helper constructors cover the common cases.
    """

    def __init__(
        self,
        name: str = "mock",
        replies: list[Decision | ContenderError] | None = None,
        latency_ms: float = 0.0,
    ) -> None:
        self.name = name
        self.provider = "mock"
        self.replies: list[Decision | ContenderError] = list(replies or [])
        self.latency_ms = latency_ms
        self.calls: list[tuple[str, list[str]]] = []

    @classmethod
    def oracle(cls, name: str = "mock-oracle") -> MockContender:
        """Always picks the first option with confidence 1.0. Test helper."""

        def _make(state: str, options: list[str]) -> Decision:
            return Decision(choice_index=0, confidence=1.0)

        mock = cls(name)
        mock._fn = _make  # noqa: SLF001 - test helper
        return mock

    @classmethod
    def always_zero(cls, name: str = "mock-zero", confidence: float = 0.9) -> MockContender:
        """Always picks option 0 with the given confidence. Test helper."""

        def _make(state: str, options: list[str]) -> Decision:
            return Decision(choice_index=0, confidence=confidence)

        mock = cls(name)
        mock._fn = _make  # noqa: SLF001 - test helper
        return mock

    def _decide(self, state: str, options: list[str]) -> Decision:
        self.calls.append((state, list(options)))
        time.sleep(self.latency_ms / 1000.0)
        fn = getattr(self, "_fn", None)
        if fn is not None:
            return fn(state, options)
        if not self.replies:
            return Decision(choice_index=0, confidence=0.5)
        reply = self.replies[min(self._i, len(self.replies) - 1)]
        self._i += 1
        if isinstance(reply, ContenderError):
            raise reply
        return reply.model_copy()

    _i = 0


def negotiate_calls(
    contender: Contender, before_attempt=None, on_attempt=None, max_attempts: int = 4
) -> dict[str, Any]:
    """Bounded setup probes. Callbacks run before/after each started probe.

    before_attempt returns False to stop setup; after_attempt receives the
    complete attempt record. Only explicit parameter adaptations get a new probe.
    """
    if getattr(contender, "_configuration_frozen", False):
        raise ValueError("configuration already frozen; create a new contender to renegotiate")
    attempts = []
    contender._negotiating = True
    try:
        for _ in range(max_attempts):
            if before_attempt is not None and before_attempt() is False:
                break
            configuration = contender.effective_configuration()
            try:
                decision = contender.decide(
                    "Probe: which storage tier does the nightly backup use?",
                    ["cold storage", "hot storage"],
                )
                record = {
                    "ok": True,
                    "input_tokens": decision.input_tokens,
                    "output_tokens": decision.output_tokens,
                    "usage_details": decision.usage_details,
                    "error": None,
                    "category": None,
                    "raw": decision.raw,
                }
            except ContenderError as exc:
                record = {
                    "ok": False,
                    "input_tokens": exc.input_tokens,
                    "output_tokens": exc.output_tokens,
                    "usage_details": exc.usage_details,
                    "error": str(exc),
                    "category": exc.category,
                    "raw": {
                        "response": exc.response,
                        "malformed_payload": getattr(exc, "raw", None),
                    },
                }
                contender.notes.append(f"negotiation probe failed: {exc}")
            record["effective_configuration"] = configuration
            attempts.append(record)
            if on_attempt is not None:
                on_attempt(record)
            if record["ok"] or configuration == contender.effective_configuration():
                break
    finally:
        contender._negotiating = False
        contender._configuration_frozen = True
    configuration = contender.effective_configuration()
    totals = {
        key: sum(a[key] for a in attempts)
        if attempts and all(a[key] is not None for a in attempts)
        else None
        for key in ("input_tokens", "output_tokens")
    }
    return {
        **totals,
        "usage_details": {},
        "attempts": attempts,
        "effective_configuration": configuration,
        "configuration_fingerprint": hashlib.sha256(
            json.dumps(configuration, sort_keys=True).encode()
        ).hexdigest(),
    }
