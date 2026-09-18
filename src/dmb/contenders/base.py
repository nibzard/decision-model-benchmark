"""Contender protocol and shared result types.

Every contender - decision model, constrained LLM, deterministic baseline -
receives the same serialized decision state and returns a ``Decision``
against the schema fixed in SPEC.md: ``{choice_index: int, confidence: float}``.
"""

from __future__ import annotations

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
    raw: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    malformed: bool = False
    error: str | None = None
    retries: int = 0


class ContenderError(Exception):
    """Base class for contender failures. Never counted as a wrong answer."""


class MalformedReply(ContenderError):
    """Reply failed the schema or bounds check."""

    def __init__(self, message: str, raw: Any = None) -> None:
        super().__init__(message)
        self.raw = raw


class RateLimited(ContenderError):
    """Provider returned a rate-limit or overload response."""


class ProviderRejected(ContenderError):
    """Provider refused a well-formed request (for example an option-count

    cap). A measured outcome, not a client bug: the message ships in the
    raw log."""


class TransportError(ContenderError):
    """Network, timeout, or server error."""


class AuthError(ContenderError):
    """Authentication or permission failure. Not retryable."""


class Contender:
    """A named decision-maker with the one-call protocol from SPEC.md.

    Subclasses implement ``decide``. Implementations must not retry; the
    runner owns the retry protocol (one retry on malformed, one backoff
    retry on rate limit).
    """

    name: str = "contender"
    provider: str = "unknown"

    def decide(self, state: str, options: list[str]) -> Decision:
        """Pick one option for the state. Raises ContenderError on failure."""
        start = time.perf_counter()
        decision = self._decide(state, options)
        decision.latency_ms = (time.perf_counter() - start) * 1000.0
        return decision

    def _decide(self, state: str, options: list[str]) -> Decision:
        raise NotImplementedError

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
