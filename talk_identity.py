"""Realtime session instructions — the voice preamble and identity assembly.

A Realtime session prompt is re-read on EVERY turn, so the per-section caps
here are a budget, not a preference. The preamble is the behavioural contract
and always ships; host identity sections are optional and additive.

Fail-open is the rule: no sections, unreadable sections, or a host that
cannot answer at all still yields a usable voice prompt.
"""

from __future__ import annotations

import re
import time

DEFAULT_SECTION_CAP = 4_000

#: How much of each host section rides the prompt.
#:
#: These are a BUDGET, not a nicety. Unlike a chat completion, a Realtime
#: session's instructions are resident for the whole call and paid for on
#: every turn — a section that would be a rounding error in a text agent is
#: charged again each time the operator speaks. PERSONA is capped well below
#: what a text agent would carry for exactly that reason: the voice preamble
#: is the behavioural contract here, and SOUL.md is supporting colour.
IDENTITY_CAPS: dict[str, int] = {
    "PERSONA": 4_000,
    "USER": 4_000,
    "MEMORY": 6_000,
    "WORKING": 2_000,
}

#: Render order. PERSONA first: if anything gets skimmed it must not be the rules.
IDENTITY_ORDER: tuple[str, ...] = ("PERSONA", "USER", "MEMORY", "WORKING")

#: What each section IS, so the model knows how to weigh it.
IDENTITY_HEADERS: dict[str, str] = {
    "PERSONA": "Your standing identity and behavior rules",
    "USER": "Who you are talking to",
    "MEMORY": "What you already know (durable memory — do not ask for these)",
    "WORKING": "What is currently open",
}

VOICE_PREAMBLE = (
    "You are Hermes, speaking live over a voice call. Reply conversationally: "
    "natural, spoken-style, one to three sentences unless you are asked for "
    "depth. Everything you say is read aloud, so no markdown, no bullet "
    "lists, no emoji, no code blocks, and no spelling out long file paths or "
    "URLs. If you do not know something, say so plainly. "
    "Use only the function tools explicitly listed for this session. After a tool result, answer "
    "in one to three spoken sentences; never read raw output verbatim. "
    "Judge how much permission an action needs by what it can DAMAGE, not by "
    "how big it feels. Work that only costs tokens and lands somewhere "
    "reversible — a lookup, a draft, or a task handed to delegate_task — you start on "
    "request: say in one short sentence what you are about to do, then do it, "
    "no confirmation. Plain lookups need not even that. But anything that "
    "spends real money, reaches a real person, or changes something live — a "
    "payment, a message or post to someone outside this call, a production "
    "deploy, deleting work that is not yours to delete — you prepare fully, "
    "then STOP and ask before it fires. "
    "When you hand work to delegate_task, the brief you write is the ONLY "
    "thing that agent ever sees. It starts fresh, with no access to this "
    "call, so never pass 'do that', 'what we just discussed', or any other "
    "pointer back to the conversation. Write the task out yourself — what to "
    "do, where it lives, and what done looks like — as if to someone who "
    "never heard a word of it. "
    "When a tool returns a WORK_STARTED receipt, say it is running and move "
    "on: the result is handed back to you when it lands and you summarize it "
    "in a sentence or two. If you are asked how the work is going before "
    "then, use check_work. Do not narrate progress you cannot see, and never "
    "report a result you have not actually been given."
)

_TOOLS_MARKER = "Advertised legacy tools:"
_HOST_TOOLS_MARKER = "Canonical Hermes host tools:"
_TRANSCRIPT_CONTRACT = (
    "This limited legacy provider-owned call keeps a temporary local transcript while live. "
    "It is handed off after the call closes for durable-memory review; it is not a live "
    "searchable or user-facing archive. Current-call search unavailability is not evidence about "
    "whether capture occurred. Canonical core-session persistence is separate; users "
    "who need full Hermes parity should use /talk core join."
)
_HOST_TRANSCRIPT_CONTRACT = (
    "This provider-owned Realtime call executes tools through the canonical Hermes host. "
    "Ordinary speech and native PCM remain provider-owned; they are never routed through "
    "a second canonical Hermes inference turn. The temporary live transcript is handed off "
    "after the call closes for durable-memory review."
)


def _tool_contract(tools: list[dict] | None, *, host_execution: bool = False) -> str:
    """Render the exact schema names supplied to the provider session."""

    names = [
        tool.get("name")
        for tool in tools or []
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    ]
    rendered = ", ".join(names) if names else "none"
    marker = _HOST_TOOLS_MARKER if host_execution else _TOOLS_MARKER
    return f"{marker} {rendered}. Do not claim or simulate tools outside this list."


def advertised_tool_names(instructions: str) -> tuple[str, ...]:
    """Read back the machine-checkable tool-name claim from built instructions."""

    markers = f"(?:{re.escape(_TOOLS_MARKER)}|{re.escape(_HOST_TOOLS_MARKER)})"
    match = re.search(rf"{markers} ([^.]+)\.", instructions)
    if match is None or match.group(1) == "none":
        return ()
    return tuple(name.strip() for name in match.group(1).split(",") if name.strip())


def cap_section(name: str, body: str) -> str:
    """Trim one host section to its budget. Unknown names get the default cap."""

    return body.strip()[: IDENTITY_CAPS.get(name.upper(), DEFAULT_SECTION_CAP)]


def current_moment() -> str:
    """The line that lets the session answer "what day is it".

    Built per call, never a module constant: these instructions are assembled
    once per session but a module-level timestamp would freeze at IMPORT, so a
    long-lived gateway would confidently state the day it booted.
    """

    now = time.localtime()
    zone = time.strftime("%Z", now).strip()
    stamp = time.strftime("%A, %B %d, %Y, %I:%M %p", now).replace(" 0", " ")
    return f"Right now it is {stamp}{' ' + zone if zone else ''}."


def build_instructions(
    host_sections: dict[str, str] | None,
    *,
    tools: list[dict] | None = None,
    host_execution: bool = False,
) -> str:
    """Assemble the Realtime session prompt.

    ``host_sections`` is whatever the host adapter could resolve, keyed by
    uppercase section name. Unknown keys are rendered after the known ones so
    a host that grows a section does not need a change here.
    """

    sections: list[str] = []
    if host_sections:
        # Match known names case-INSENSITIVELY. The obvious spelling of this
        # loop (exact `in` for known, `.upper() not in` for extra) drops a
        # case-variant known key from BOTH lists, silently losing the whole
        # section rather than misordering it.
        by_upper = {name.upper(): name for name in host_sections}
        known = [by_upper[name] for name in IDENTITY_ORDER if name in by_upper]
        extra = sorted(k for k in host_sections if k.upper() not in IDENTITY_ORDER)
        for name in [*known, *extra]:
            body = cap_section(name, str(host_sections.get(name) or ""))
            if not body:
                continue
            header = IDENTITY_HEADERS.get(name.upper(), name.replace("_", " ").title())
            sections.append(f"{header}:\n\n{body}")

    # The clock rides last and unconditionally: it is one line, it is the most
    # perishable thing in the prompt, and a session with no host sections at
    # all should still be able to say what day it is.
    sections.append(current_moment())
    contracts = [
        _tool_contract(tools, host_execution=host_execution),
        _HOST_TRANSCRIPT_CONTRACT if host_execution else _TRANSCRIPT_CONTRACT,
    ]
    return VOICE_PREAMBLE + "\n\n" + "\n\n".join([*contracts, *sections])


__all__ = [
    "DEFAULT_SECTION_CAP",
    "IDENTITY_CAPS",
    "IDENTITY_HEADERS",
    "IDENTITY_ORDER",
    "VOICE_PREAMBLE",
    "advertised_tool_names",
    "build_instructions",
    "cap_section",
    "current_moment",
]
