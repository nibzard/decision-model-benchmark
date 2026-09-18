"""Z.ai (GLM) contenders, JSON mode via the OpenAI-compatible endpoint.

GLM-5.3 models always engage thinking; it cannot be disabled. The API
offers levels low / high / max and serves ``high`` by default. DMB runs
``low`` - the provider's minimum - so the suites fit the wall-time limit;
this deviation from as-served defaults is recorded in every run manifest.

Thinking tokens count toward ``completion_tokens`` and can exceed 1,000
even at level low, so the token budget is 8,192. A reply that hits the
budget is recorded as malformed, never silently trimmed.
"""

from __future__ import annotations

import os

from .jsonmode import JSONModeContender

BASE_URL = "https://api.z.ai/api/paas/v4/chat/completions"


def zai_contender(model: str) -> JSONModeContender:
    """One Z.ai contender: ``zai:<model>``."""
    contender = JSONModeContender(
        name=f"zai:{model}",
        model=model,
        url=BASE_URL,
        headers={"Authorization": f"Bearer {os.environ['ZAI_API_KEY']}"},
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=8192,
        extra_body={"thinking": {"type": "enabled", "level": "low"}},
    )
    contender.deviation = (
        f"zai:{model}: thinking cannot be disabled; adapter pins level 'low' "
        "(provider minimum), temperature 0"
    )
    return contender
