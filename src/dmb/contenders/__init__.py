"""Contender registry: build contenders from env keys, record skips."""

from __future__ import annotations

import os

from .base import Contender
from .baselines import KeywordBaseline, MajorityBaseline, RandomBaseline, build_majority_table
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
) -> tuple[list[Contender], list[dict]]:
    """Build every contender whose key is present; skip the rest.

    Returns ``(contenders, skipped)`` where each skip carries a recorded
    reason (SPEC.md T2.5). ``negotiate`` runs each live contender's cheap
    probe call so unsupported parameters drop out before scoring starts.
    """
    contenders: list[Contender] = [
        RandomBaseline(),
        MajorityBaseline(majority_table or {}),
        KeywordBaseline(),
    ]
    skipped: list[dict] = []
    for key, env_var, factory in LLM_SPECS:
        if not os.environ.get(env_var):
            skipped.append({"contender": key, "reason": f"env {env_var} not set"})
            continue
        try:
            contender = factory()
        except Exception as exc:  # noqa: BLE001 - construction failure skips, not crashes
            skipped.append({"contender": key, "reason": f"construction failed: {exc}"})
            continue
        if negotiate:
            contender.negotiate()
        contenders.append(contender)
    if include_jev:
        if os.environ.get("TYPESAFE_API_KEY"):
            contender = JevContender()
            if negotiate:
                contender.negotiate()
            contenders.append(contender)
        else:
            skipped.append(
                {"contender": "typesafe:jev", "reason": "env TYPESAFE_API_KEY not set"}
            )
    return contenders, skipped


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
