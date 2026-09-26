"""Cerebras contender: the "fast LLM" wing, JSON mode.

Cerebras serves open models on OpenAI-compatible endpoints. The model is
selectable via ``CEREBRAS_MODEL``; the default is resolved from the
account's model list at build time (``resolve_model``).
"""

from __future__ import annotations

import os

import httpx

from ..prices import price_for
from .jsonmode import JSONModeContender

BASE_URL = "https://api.cerebras.ai/v1/chat/completions"

# Preferred open models in order; first available wins.
PREFERRED_MODELS = ["gpt-oss-120b"]


def resolve_model() -> str:
    """Resolve the Cerebras model from env or the account's model list."""
    env_model = os.environ.get("CEREBRAS_MODEL")
    if env_model:
        price_for(f"cerebras:{env_model}")
        return env_model
    try:
        response = httpx.get(
            "https://api.cerebras.ai/v1/models",
            headers={"Authorization": f"Bearer {os.environ['CEREBRAS_API_KEY']}"},
            timeout=15.0,
        )
        response.raise_for_status()
        available = {model["id"] for model in response.json().get("data", [])}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return PREFERRED_MODELS[0]
    for candidate in PREFERRED_MODELS:
        if candidate in available:
            return candidate
    raise ValueError("Cerebras account has no model with a verified price")


def cerebras_contender() -> JSONModeContender:
    """One Cerebras contender: ``cerebras:<open-model>``."""
    model = resolve_model()
    return JSONModeContender(
        name=f"cerebras:{model}",
        model=model,
        url=BASE_URL,
        headers={"Authorization": f"Bearer {os.environ['CEREBRAS_API_KEY']}"},
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=1024,
    )
