"""Realtime session instructions — the voice preamble and identity assembly.

A Realtime session prompt is re-read on EVERY turn, so the per-section caps
here are a budget, not a preference. The preamble is the behavioural contract
and always ships; host identity sections are optional and additive.

Fail-open is the rule: no sections, unreadable sections, or a host that
cannot answer at all still yields a usable voice prompt.
"""

from __future__ import annotations

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
    "You have function tools for facts and for actions: use them whenever you "
    "are asked for anything you could not know offhand — what was said in "
    "past sessions, what is saved in memory, what is on the web, the state of "
    "this machine, or real work that needs doing. After a tool result, answer "
    "in one to three spoken sentences; never read raw output verbatim. "
    "Judge how much permission an action needs by what it can DAMAGE, not by "
    "how big it feels. Work that only costs tokens and lands somewhere "
    "reversible — a lookup, research, a draft, code on a branch, a task "
    "handed to delegate_task, a read-only terminal command — you start on "
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


def cap_section(name: str, body: str) -> str:
    """Trim one host section to its budget. Unknown names get the default cap."""

    return body.strip()[: IDENTITY_CAPS.get(name.upper(), DEFAULT_SECTION_CAP)]


def build_instructions(host_sections: dict[str, str] | None) -> str:
    """Assemble the Realtime session prompt.

    ``host_sections`` is whatever the host adapter could resolve, keyed by
    uppercase section name. Unknown keys are rendered after the known ones so
    a host that grows a section does not need a change here.
    """

    sections: list[str] = []
    if host_sections:
        known = [name for name in IDENTITY_ORDER if name in host_sections]
        extra = sorted(k for k in host_sections if k.upper() not in IDENTITY_ORDER)
        for name in [*known, *extra]:
            body = cap_section(name, str(host_sections.get(name) or ""))
            if not body:
                continue
            header = IDENTITY_HEADERS.get(name.upper(), name.replace("_", " ").title())
            sections.append(f"{header}:\n\n{body}")

    if not sections:
        return VOICE_PREAMBLE
    return VOICE_PREAMBLE + "\n\n" + "\n\n".join(sections)


__all__ = [
    "DEFAULT_SECTION_CAP",
    "IDENTITY_CAPS",
    "IDENTITY_HEADERS",
    "IDENTITY_ORDER",
    "VOICE_PREAMBLE",
    "build_instructions",
    "cap_section",
]
