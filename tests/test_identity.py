"""Identity — the voice preamble and host-section assembly."""

from __future__ import annotations

import talk_identity
import talk_tools


def test_preamble_plus_the_clock_when_the_host_has_nothing():
    """A host with nothing to say still yields preamble + the one line the
    session cannot answer without: what day it is."""

    for empty in (None, {}, {"PERSONA": "   "}):
        built = talk_identity.build_instructions(empty)
        assert built.startswith(talk_identity.VOICE_PREAMBLE)
        remainder = built[len(talk_identity.VOICE_PREAMBLE) :]
        assert "Advertised legacy tools: none." in remainder
        assert remainder.strip().endswith(talk_identity.current_moment())


def test_the_clock_is_built_per_call_not_at_import():
    """A module-level timestamp would freeze at import, so a gateway running
    for a week would confidently state the day it booted."""

    import time as _time

    frozen = talk_identity.current_moment()
    real = _time.localtime
    try:
        talk_identity.time.localtime = lambda *a: real(1_000_000_000)
        assert talk_identity.current_moment() != frozen
        assert "2001" in talk_identity.build_instructions(None)
    finally:
        talk_identity.time.localtime = real


def test_preamble_states_the_load_bearing_rules():
    text = talk_identity.VOICE_PREAMBLE
    assert text.startswith("You are Hermes, speaking live over a voice call.")
    assert "no markdown" in text
    assert "DAMAGE" in text
    assert "delegate_task" in text
    assert "WORK_STARTED" in text
    # The port must not carry the source system's identity across.
    for leaked in ("Homie", "Pedro", "Archon", "CLUTCH"):
        assert leaked not in text


def test_legacy_capability_claims_are_derived_from_the_advertised_tool_schemas():
    tools = talk_tools.default_talk_tools()
    instructions = talk_identity.build_instructions(None, tools=tools)

    assert {tool["name"] for tool in tools} == set(
        talk_identity.advertised_tool_names(instructions)
    )
    for unavailable_claim in (
        "what is on the web",
        "state of this machine",
        "code on a branch",
        "read-only terminal command",
        "HUD",
        "Discord tools",
        "full Hermes tools",
    ):
        assert unavailable_claim not in instructions


def test_legacy_prompt_tells_the_truth_about_current_call_transcripts():
    instructions = talk_identity.build_instructions(None, tools=talk_tools.default_talk_tools())

    assert "temporary local transcript" in instructions
    assert "after the call closes" in instructions
    assert "durable-memory review" in instructions
    assert "not a live searchable or user-facing archive" in instructions
    assert "no transcript exists" not in instructions


def test_sections_render_in_priority_order():
    instructions = talk_identity.build_instructions(
        {"WORKING": "one open thread", "PERSONA": "be terse", "USER": "calls you Hermes"}
    )

    assert instructions.startswith(talk_identity.VOICE_PREAMBLE)
    persona_at = instructions.index("be terse")
    user_at = instructions.index("calls you Hermes")
    working_at = instructions.index("one open thread")
    # Rules first: whatever the model skims, it must not be the contract.
    assert persona_at < user_at < working_at
    assert talk_identity.IDENTITY_HEADERS["PERSONA"] in instructions


def test_sections_are_capped_per_name():
    body = "x" * 50_000
    instructions = talk_identity.build_instructions({"WORKING": body})
    # -1 is the clock line now; the section body is the block before it.
    rendered = instructions.split("\n\n")[-2]
    assert len(rendered) == talk_identity.IDENTITY_CAPS["WORKING"]


def test_all_four_sections_render_in_priority_order():
    """WORKING last on purpose: curated operator context is the section that
    can be skimmed without losing the behavioural contract or who is on the
    call. Its header must also describe what now lives there — the slot was
    reserved for open-thread state and carries operator identity too."""

    instructions = talk_identity.build_instructions(
        {
            "WORKING": "Dograh is the voice stack",
            "MEMORY": "ships at night",
            "PERSONA": "be terse",
            "USER": "calls you Hermes",
        }
    )

    positions = [
        instructions.index(body)
        for body in (
            "be terse",
            "calls you Hermes",
            "ships at night",
            "Dograh is the voice stack",
        )
    ]
    assert positions == sorted(positions)
    assert "Operator identity" in instructions


def test_a_case_variant_known_section_is_not_dropped():
    """The obvious spelling of the render loop (exact match for known names,
    ``.upper() not in`` for unknown) drops a lowercase known key from BOTH
    lists — losing the entire section rather than misordering it."""

    instructions = talk_identity.build_instructions(
        {"memory": "the vault says X", "PERSONA": "be terse"}
    )
    assert "the vault says X" in instructions
    assert instructions.index("be terse") < instructions.index("the vault says X")


def test_unknown_sections_are_kept_but_ranked_last():
    instructions = talk_identity.build_instructions(
        {"HABITS": "runs at dawn", "PERSONA": "be terse"}
    )
    assert instructions.index("be terse") < instructions.index("runs at dawn")
    assert "Habits:" in instructions


def test_unknown_section_uses_the_default_cap():
    assert len(talk_identity.cap_section("HABITS", "y" * 99_999)) == (
        talk_identity.DEFAULT_SECTION_CAP
    )


def test_cap_section_trims_whitespace():
    assert talk_identity.cap_section("PERSONA", "  be terse  ") == "be terse"
