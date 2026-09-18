"""Z.ai (GLM) contenders, JSON mode via the OpenAI-compatible endpoint."""

from __future__ import annotations

import os

from .jsonmode import JSONModeContender

BASE_URL = "https://api.z.ai/api/paas/v4/chat/completions"


def zai_contender(model: str) -> JSONModeContender:
    """One Z.ai contender: ``zai:<model>``."""
    return JSONModeContender(
        name=f"zai:{model}",
        model=model,
        url=BASE_URL,
        headers={"Authorization": f"Bearer {os.environ['ZAI_API_KEY']}"},
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=1024,
    )
