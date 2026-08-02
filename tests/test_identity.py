"""Identity — the voice preamble and host-section assembly."""

from __future__ import annotations

import talk_identity


def test_preamble_alone_when_the_host_has_nothing():
    assert talk_identity.build_instructions(None) == talk_identity.VOICE_PREAMBLE
    assert talk_identity.build_instructions({}) == talk_identity.VOICE_PREAMBLE
    assert talk_identity.build_instructions({"PERSONA": "   "}) == talk_identity.VOICE_PREAMBLE


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
    rendered = instructions.split("\n\n")[-1]
    assert len(rendered) == talk_identity.IDENTITY_CAPS["WORKING"]


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
