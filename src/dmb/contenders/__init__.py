"""Contender registry: build contenders from env keys, record skips."""

from __future__ import annotations

import os

from .base import Contender
from .baselines import KeywordBaseline, MajorityBaseline, RandomBaseline
from .jev import JevContender
from .llm_anthropic import AnthropicContender
from .llm_cerebras import cerebras_contender
from .llm_deepseek import deepseek_contender
from .llm_openai import openai_contender
from .llm_zai import zai_contender

# (registry key, env var, factory)
LLM_SPECS = [
    ("openai:gpt-5.4-nano", "OPENAI_API_KEY", lambda: openai_contender("gpt-5.4-nano")),
    ("openai:gpt-5.4-mini", "OPENAI_API_KEY", lambda: openai_contender("gpt-5.4-mini")),
    (
        "anthropic:claude-haiku-4-5",
        "ANTHROPIC_AUTH_TOKEN",
        lambda: AnthropicContender("claude-haiku-4-5"),
    ),
    (
        "anthropic:claude-sonnet-4-6",
        "ANTHROPIC_AUTH_TOKEN",
        lambda: AnthropicContender("claude-sonnet-4-6"),
    ),
    ("zai:glm-5.3-flash", "ZAI_API_KEY", lambda: zai_contender("glm-5.3-flash")),
    ("zai:glm-5.3", "ZAI_API_KEY", lambda: zai_contender("glm-5.3")),
    ("deepseek:deepseek-chat", "DEEPSEEK_API_KEY", lambda: deepseek_contender()),
    ("cerebras:auto", "CEREBRAS_API_KEY", cerebras_contender),
]


def build_contenders(
    include_jev: bool = False,
    majority_table: dict[frozenset[str], tuple[str, float]] | None = None,
    negotiate: bool = True,
    wanted: list[str] | None = None,
) -> tuple[list[Contender], list[dict], dict[str, dict[str, int]]]:
    """Build every selected contender whose key is present; skip the rest.

    ``wanted`` is a list of substrings. Only registry keys matching at
    least one substring are considered - baselines included; unselected
    contenders are neither constructed nor negotiated, so filtering never
    spends a provider call.

    Returns ``(contenders, skipped, negotiation_usage)`` where each skip
    carries a recorded reason (SPEC.md T2.5) and ``negotiation_usage``
    maps contender name to the usage its probe call reported (empty for
    contenders whose probe failed or reported nothing). ``negotiate`` runs
    each live contender's cheap probe call so unsupported parameters drop
    out before scoring starts.
    """
    def selected(key: str) -> bool:
        return not wanted or any(w in key for w in wanted)

    contenders: list[Contender] = [
        RandomBaseline(),
        MajorityBaseline(majority_table or {}),
        KeywordBaseline(),
    ]
    contenders = [c for c in contenders if selected(c.name)]
    skipped: list[dict] = []
    negotiation_usage: dict[str, dict[str, int]] = {}
    for key, env_var, factory in LLM_SPECS:
        if not selected(key):
            continue
        if not os.environ.get(env_var):
            skipped.append({"contender": key, "reason": f"env {env_var} not set"})
            continue
        try:
            contender = factory()
        except Exception as exc:  # noqa: BLE001 - construction failure skips, not crashes
            skipped.append({"contender": key, "reason": f"construction failed: {exc}"})
            continue
        if negotiate:
            usage = contender.negotiate()
            if usage:
                negotiation_usage[contender.name] = usage
        contenders.append(contender)
    if include_jev and selected("typesafe:jev"):
        if os.environ.get("TYPESAFE_API_KEY"):
            contender = JevContender()
            if negotiate:
                usage = contender.negotiate()
                if usage:
                    negotiation_usage[contender.name] = usage
            contenders.append(contender)
        else:
            skipped.append(
                {"contender": "typesafe:jev", "reason": "env TYPESAFE_API_KEY not set"}
            )
    return contenders, skipped, negotiation_usage


__all__ = [
    "build_contenders",
    "build_majority_table",
    "JevContender",
    "AnthropicContender",
    "cerebras_contender",
    "deepseek_contender",
    "openai_contender",
    "zai_contender",
    "KeywordBaseline",
    "MajorityBaseline",
    "RandomBaseline",
]
