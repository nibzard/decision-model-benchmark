"""Decision-state renderer: the one input format every contender shares.

All contenders serialize the decision state and the option list through
these functions, so the input text cannot drift between adapters. The
output is pinned byte-for-byte by ``tests/test_render.py``; change the
test only with a recorded protocol deviation.
"""

from __future__ import annotations

SYSTEM_PROMPT = (
    "You are a decision module. Read the decision state and choose exactly "
    "one option from the list. You must commit to a single option even if "
    "the state does not determine the answer; in that case choose the least "
    "unreasonable option and report low confidence. Reply with a JSON object "
    'and nothing else: {"choice_index": <integer index into OPTIONS>, '
    '"confidence": <number between 0.0 and 1.0>}. The confidence is your '
    "probability that your chosen option is correct."
)


def render_state_block(state: str) -> str:
    """The canonical state block."""
    return f"DECISION STATE:\n{state.strip()}\n"


def render_options_block(options: list[str]) -> str:
    """The canonical numbered options block."""
    lines = [f"[{i}] {option}" for i, option in enumerate(options)]
    return "OPTIONS:\n" + "\n".join(lines) + "\n"


def render_task_block() -> str:
    """The canonical task instruction."""
    return (
        "TASK: Choose exactly one option for the decision state above. "
        "Reply with JSON only."
    )


def render_prompt(state: str, options: list[str]) -> str:
    """Full user prompt for LLM contenders (JSON mode and schema mode)."""
    return (
        render_state_block(state)
        + "\n"
        + render_options_block(options)
        + "\n"
        + render_task_block()
    )


def render_jev_state(state: str) -> str:
    """State text handed to the jev adapter (no JSON instructions there)."""
    return state.strip()
