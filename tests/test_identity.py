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


def test_preamble_collapses_repeated_confirmation_into_one_approval():
    """The permit binds what was approved; the preamble is what stops the model
    re-asking for it. Without this the operator confirms the same action twice.
    """

    text = talk_identity.VOICE_PREAMBLE
    assert "say the plan once" in text
    assert "do not restate the plan or ask a second time" in text
    assert "summarize the new version and ask again" in text


def test_legacy_capability_claims_are_derived_from_the_advertised_tool_schemas():
    tools = talk_tools.default_talk_tools()
    instructions = talk_identity.build_instructions(None, tools=tools)

    assert {tool["name"] for tool in tools} == set(
        talk_identity.advertised_tool_names(instructions)
    )
    # The delegation ceiling (#64) is the one sanctioned mention of the full
    # toolset — it belongs to DELEGATED agents, never to this session itself.
    delegation = "agents you delegate to run the full Hermes toolset"
    assert delegation in instructions
    scrubbed = instructions.replace(delegation, "")
    for unavailable_claim in (
        "what is on the web",
        "state of this machine",
        "code on a branch",
        "read-only terminal command",
        "HUD",
        "Discord tools",
        "full Hermes tools",
    ):
        assert unavailable_claim not in scrubbed


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
    # The lane line and the clock trail every prompt; the section body is the
    # block before them.
    rendered = instructions.split("\n\n")[-3]
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


# --- self-knowledge: lane, capability steer, delegation ceiling (issue #64) ----
#
# An orphaned CLI canary (the 2026-08-26 dogfood transcript) could not say
# where it was running, recited its capabilities from memory, and flat-refused
# computer use. The lane line and the two preamble facts ship on EVERY prompt,
# exactly like the clock — a lane with no host sections is precisely where
# they are needed.


def test_each_known_lane_renders_its_exact_sentence():
    expected = {
        "cli": (
            "You are running as a `hermes talk` session in a terminal on the "
            "operator's own machine — Ctrl+C in that terminal ends the call."
        ),
        "discord": (
            "You are live in a Discord voice channel — `/talk leave` or the "
            "operator leaving the channel ends the call."
        ),
        "dashboard": (
            "You are live in the Hermes dashboard browser tab — the hang-up "
            "control or closing the tab ends the call."
        ),
    }
    # The map is the contract: a renamed key must fail here, not on a call.
    assert set(talk_identity.LANE_LINES) == set(expected)
    for lane, sentence in expected.items():
        instructions = talk_identity.build_instructions(None, lane=lane)
        assert sentence in instructions
        for other in set(expected.values()) - {sentence}:
            assert other not in instructions
        assert talk_identity.GENERIC_LANE_LINE not in instructions


def test_an_unknown_or_absent_lane_never_invents_a_hangup_control():
    for lane in (None, "", "gateway"):
        instructions = talk_identity.build_instructions(None, lane=lane)
        assert talk_identity.GENERIC_LANE_LINE in instructions
        for known in talk_identity.LANE_LINES.values():
            assert known not in instructions
        # The generic line names no surface, so it can name no control.
        assert "Ctrl+C" not in instructions
        assert "/talk leave" not in instructions


def test_lane_names_are_case_and_whitespace_insensitive():
    assert talk_identity.lane_line(" CLI ") == talk_identity.LANE_LINES["cli"]


def test_the_capability_steer_and_delegation_ceiling_ship_on_every_prompt():
    """Both facts ride the preamble: the one carrier no ctx gate, pinned
    include list, or failed scan can ever drop — including a prompt with no
    host sections at all."""

    for sections in (None, {}, {"PERSONA": "be terse"}):
        instructions = talk_identity.build_instructions(sections)
        assert "call the talk_capabilities tool" in instructions
        assert "never recite capabilities from memory" in instructions
        assert "full Hermes toolset" in instructions
        assert "computer use" in instructions
        assert "Never answer" in instructions
        assert 'the honest answer is "I can hand that to an agent."' in instructions


def test_the_clock_still_trails_and_the_lane_line_just_precedes_it():
    built = talk_identity.build_instructions(
        {"PERSONA": "be terse"},
        lane="discord",
        host_summary="Hermes host attached: 3 skills enabled, 1 toolsets active.",
    )

    assert built.strip().endswith(talk_identity.current_moment())
    assert built.index("Hermes host attached:") < built.index(
        talk_identity.LANE_LINES["discord"]
    )
    assert built.index(talk_identity.LANE_LINES["discord"]) < built.index(
        talk_identity.current_moment()
    )


def test_host_summary_renders_when_provided():
    line = "Hermes host attached: 12 skills enabled, 4 toolsets active."
    instructions = talk_identity.build_instructions(None, host_summary=line)

    assert line in instructions


def test_host_summary_renders_nothing_when_absent_or_blank():
    assert "Hermes host attached" not in talk_identity.build_instructions(None)
    blank = talk_identity.build_instructions(None, host_summary="   ")

    # A blank summary is no summary at all — never an empty prompt block.
    assert blank == talk_identity.build_instructions(None)


def test_host_summary_is_capped_at_200_chars():
    instructions = talk_identity.build_instructions(None, host_summary="s" * 999)

    assert "s" * talk_identity.HOST_SUMMARY_CAP in instructions
    assert "s" * (talk_identity.HOST_SUMMARY_CAP + 1) not in instructions
