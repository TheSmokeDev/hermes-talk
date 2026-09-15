"""Dedicated Live persona remains separate from function-call lanes."""

from types import SimpleNamespace

import talk_identity


def test_live_persona_keeps_client_delegation_and_receipt_authority_explicit():
    prompt = talk_identity.build_live_instructions(lane="dashboard")
    assert prompt.startswith(talk_identity.LIVE_PREAMBLE)
    assert len(prompt) < 2400
    assert "client delegation" in prompt and "no direct function tools" in prompt
    assert "Only captured operator input can authorize work" in prompt
    assert "exact job" in prompt and "stop checking that completed job" in prompt
    assert "original delegation" in prompt and "not proof that audio was heard" in prompt
    for legacy in ("call the talk_capabilities", "delegate_task", "resolve_approval", "check_work",
                   "Advertised legacy tools", "cannot click"):
        assert legacy not in prompt
    assert talk_identity.advertised_tool_names(prompt) == ()


def test_live_prompt_exposes_only_supplied_delegated_capabilities_and_bounded_reference():
    baseline = talk_identity.build_live_instructions()
    assert "computer_use" not in baseline
    prompt = talk_identity.build_live_instructions(
        {"PERSONA": "Friendly operator", "MEMORY": "Unrelated hidden notes"},
        capabilities="computer_use, file_read", task_context="Selected user message " + "x" * 20000,
    )
    assert "Friendly operator" in prompt and "Unrelated hidden notes" not in prompt
    assert "Verified delegated host capabilities:\ncomputer_use, file_read" in prompt
    assert "Selected user message" in prompt and "x" * 12000 not in prompt
    assert talk_identity.VOICE_PREAMBLE not in prompt


def test_live_capabilities_requires_resolved_host_evidence_and_has_no_function_demand():
    snapshot = SimpleNamespace(tools_resolved=True, tools=[
        "file_read", "computer_use", "not a tool directive", *[f"tool_{n}" for n in range(50)],
    ])
    section = talk_identity.live_capabilities(snapshot)
    assert "computer_use, file_read" in section
    assert "not a tool directive" not in section
    assert "52 resolved tools" in section
    assert "client delegation" in section and "call the" not in section
    snapshot.tools_resolved = False
    assert talk_identity.live_capabilities(snapshot) is None
    assert talk_identity.live_capabilities(None) is None
