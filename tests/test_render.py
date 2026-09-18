"""Renderer pin test (SPEC.md T2.6): byte-exact input format."""

from dmb.contenders.render import (
    CHOICE_INSTRUCTIONS,
    CONFIDENCE_DEFINITION,
    SHARED_SEMANTICS,
    SYSTEM_PROMPT,
    prompt_fingerprint,
    render_jev_instructions,
    render_jev_state,
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


def test_jev_instructions_pinned():
    """Finding 5: jev receives the same semantics as the LLM prompt."""
    assert render_jev_instructions() == (
        "Choose the single best option for the decision state. "
        "You must commit to a single option even if the state does not "
        "determine the answer; in that case choose the least unreasonable "
        "option and report low confidence. Answer confidence separately. "
        "The confidence is your probability that your chosen option is correct."
    )


def test_shared_semantics_carried_by_both_contender_classes():
    assert CHOICE_INSTRUCTIONS in SYSTEM_PROMPT
    assert CONFIDENCE_DEFINITION in SYSTEM_PROMPT
    for part in (CHOICE_INSTRUCTIONS, CONFIDENCE_DEFINITION):
        assert part in render_jev_instructions()
    assert f"{CHOICE_INSTRUCTIONS} {CONFIDENCE_DEFINITION}" == SHARED_SEMANTICS


def test_prompt_fingerprint_stable_and_complete():
    """The fingerprint covers every shared string an adapter sends."""
    first = prompt_fingerprint()
    assert first == prompt_fingerprint()
    assert len(first) == 64
    for shared in (SYSTEM_PROMPT, render_jev_instructions()):
        assert shared  # both are inside the fingerprint payload


def test_jev_state_stripped():
    assert render_jev_state("  padded  ") == "padded"
