"""Renderer pin test (SPEC.md T2.6): byte-exact input format."""

from dmb.contenders.render import (
    SYSTEM_PROMPT,
    render_options_block,
    render_prompt,
    render_state_block,
)


def test_state_block_pinned():
    assert render_state_block("  Ship the parcel.  ") == (
        "DECISION STATE:\nShip the parcel.\n"
    )


def test_options_block_pinned():
    assert render_options_block(["ham", "spam"]) == (
        "OPTIONS:\n[0] ham\n[1] spam\n"
    )


def test_full_prompt_pinned():
    assert render_prompt("Ship it", ["a", "b"]) == (
        "DECISION STATE:\nShip it\n"
        "\n"
        "OPTIONS:\n[0] a\n[1] b\n"
        "\n"
        "TASK: Choose exactly one option for the decision state above. "
        "Reply with JSON only."
    )


def test_system_prompt_pinned():
    assert SYSTEM_PROMPT == (
        "You are a decision module. Read the decision state and choose exactly "
        "one option from the list. You must commit to a single option even if "
        "the state does not determine the answer; in that case choose the least "
        "unreasonable option and report low confidence. Reply with a JSON object "
        'and nothing else: {"choice_index": <integer index into OPTIONS>, '
        '"confidence": <number between 0.0 and 1.0>}. The confidence is your '
        "probability that your chosen option is correct."
    )
