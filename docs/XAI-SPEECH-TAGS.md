# xAI speech tags — field notes

Grok's native voice lane turns Hermes results into speech. xAI also accepts
speech markup in its text-to-speech API. This reference collects the full
Switchboard speech-tag catalog contributed by @zoidypuh: **114 distinct inline
spellings and 13 phrase wrappers**, including variants beyond the documented
baseline.

These are community listening observations from 2026-09-09, 2026-09-13, and
2026-09-15, not an xAI compatibility guarantee. The catalog was transcribed and
cross-checked on 2026-10-04. Model, voice, language, context, and provider changes
can affect the result.
An accepted request or nonempty audio response alone does not prove a tag had
its intended audible effect.

## Where this fits in hermes-talk

The existing flow is:

```text
user speech -> Grok -> Hermes tool/background work -> result text -> Grok -> audio
```

For immediate tool results, `SubmitToolResult` becomes a
`conversation.item.create` item of type `function_call_output`, followed by
`response.create`. Background completions use `run_finished_commands()` and
`_announcement_commands()` to supply the result as quoted data and request a
brief spoken announcement with tools disabled. The Grok adapter sends these
through the speech-to-speech WebSocket and streams the returned audio.

This is model-generated speech from result text, rather than verbatim synthesis
of the entire Hermes answer. `VOICE_PREAMBLE` explicitly says: "After a tool
result, answer in one to three spoken sentences; never read raw output
verbatim." Tags in a worker result may therefore be omitted, paraphrased, or
spoken literally. A tag in tool output is data, not permission to change the
voice manager's instructions or execute another action.

The relevant implementation is in [talk_relay.py](../talk_relay.py),
[talk_cli.py](../talk_cli.py), [talk_grok_realtime.py](../talk_grok_realtime.py),
and [talk_identity.py](../talk_identity.py).

| Path | What these notes mean there |
| --- | --- |
| Native Grok Talk | Experimental delivery vocabulary to try when explicitly requesting a voice style. No guarantee that tags in Hermes result text pass through unchanged. |
| xAI `POST /v1/tts` | Takes exact text with speech tags. This external API is the relevant path for testing a fixed tagged script; Talk does not register an xAI REST TTS provider today. |
| xAI realtime `force_message` | xAI documents verbatim TTS-synthesized utterances on the WebSocket. Talk does not currently implement a command for it; an integration would require its own playback/barge-in/lifecycle work and live receipt. |
| Talk's bonus `talk_openai` TTS provider | Uses OpenAI's `/v1/audio/speech`, not xAI. Do not assume these xAI tags transfer. |
| Talk's ElevenLabs cascade | OpenAI text output plus ElevenLabs TTS. Use that backend's syntax; this xAI catalog does not enable a Grok cascade. |

## Inline grammar

Place an effect where it should occur, within a phrase:

```text
That is funny [chuckle]. We can try it again [breath].
```

Preserve each spelling literally, including unusual forms such as
`[chuckleing]`, `[giggleing]`, `[sobing]`, `[breatheing]`, and `[chokeing]`.
The listening notes report different performances for variants in a family;
they are not aliases to normalize. Pick one effect per slot rather than
stacking equivalent variants.

### Reported working in Switchboard: 92 spellings

The original retest list contained 90 positive spellings. The 2026-09-15 update
also confirmed `[pause]` and `[long-pause]`, bringing this section to 92.
**Status: working in the contributor's listening tests.** These labels describe
the Switchboard observations; they are not a promise for every xAI model.

```text
[laugh] [chuckle] [giggle] [kiss] [sigh] [breath] [cry] [hum-tune] [laughing] [laughs]
[chuckleing] [chortle] [chortleing] [cackle] [cackleing] [giggleing] [giggling] [guffaw]
[guffawing] [snicker] [soft-laugh] [nervous-laugh] [evil-laugh] [crying] [sob] [sobbing]
[sobing] [whimper] [whimpering] [squeal] [squealing] [shriek] [shrieking] [disgusted]
[breathing] [deep-breath] [heavy-breathing] [inhaleing] [gasps] [pant] [panting]
[sighing] [wheeze] [wheezeing] [yawn] [yawning] [yawns] [snore] [snoreing] [hum]
[huming] [whistle] [shush] [shushing] [clear throat] [clear-throat] [clearing throat]
[clears throat] [clears-throat] [throat-clear] [cough] [coughing] [coughs] [hiccuping]
[hiccups] [sneeze] [sneezeing] [sneezes] [sniffle] [sniffleing] [snort] [chokeing] [gag]
[gaging] [retch] [retching] [groan] [groaning] [grunt] [grunting] [grunts] [raspberry]
[raspberrying] [spit] [teeth-chatter] [moan] [moaning] [hiss] [hissing] [howling]
[pause] [long-pause]
```

### Reported working, but temperamental: 20 spellings

These are the original 22 temperamental/short-effect entries, with the two
confirmed pause tags moved above. **Status: reported working, with variable or
very short effects that were difficult to detect reliably.**
`[tongue-click]` and `[lip-smack]` were short-transient observations rather than
prior clear listening confirmations in the retest notes.

```text
[inhale] [exhale] [tsk] [tongue-click] [lip-smack] [wail] [breatheing] [exhaleing]
[gasp] [gasping] [whistles] [hiccup] [sniff] [croak] [belching] [sucking] [purr]
[purring] [meowing] [howl]
```

### Additional spellings in later notes: 2

The later, context-specific notes also list these exact spellings. They were
not assigned to either retest bin, so they are recorded separately without
inventing a listening verdict. **Status: listed, no separate retest rating.**

```text
[choke] [belch]
```

That accounts for all 114 distinct inline spellings in the supplied catalog.
Some overlap xAI's documented tags; the extended variants are experimental.

## Phrase wrappers: 13

Use an opening tag and its matching closing tag around a short phrase.
All spellings below are available in the field notes; the effect descriptions
are intended delivery cues, not a promise of a particular sound.

| Wrapper syntax | Intended delivery | Evidence in the supplied notes |
| --- | --- | --- |
| `<soft>We can try that.</soft>` | Soft delivery | Listed for use; no separate retest rating |
| `<whisper>We can try that.</whisper>` | Whispered delivery | Listed for use; also in xAI's documented examples |
| `<loud>We can try that.</loud>` | Louder delivery | Listed for use; no separate retest rating |
| `<emphasis>We can try that.</emphasis>` | Emphasized delivery | Listed for use; no separate retest rating |
| `<slow>We can try that.</slow>` | Slower delivery | Working: duration measured in the notes |
| `<fast>We can try that.</fast>` | Faster delivery | Working: duration measured in the notes |
| `<higher-pitch>We can try that.</higher-pitch>` | Higher pitch | Listed for use; no separate retest rating |
| `<lower-pitch>We can try that.</lower-pitch>` | Lower pitch | Listed for use; no separate retest rating |
| `<build-intensity>We can try that.</build-intensity>` | Increasing intensity | Working: duration measured in the notes |
| `<decrease-intensity>We can try that.</decrease-intensity>` | Decreasing intensity | Working: duration measured in the notes |
| `<sing-song>We can try that.</sing-song>` | Sing-song delivery | Working: duration measured in the notes |
| `<singing>We can try that.</singing>` | Sung delivery | Working: duration measured in the notes |
| `<laugh-speak>We can try that.</laugh-speak>` | Speech with laughter | Working: explicitly confirmed as lively on 2026-09-15 |

Use one wrapper per phrase or sentence. Close it before moving on. Do not nest,
overlap, or apply successive wrappers to the same sentence. In particular,
`<soft>...</soft>` is a phrase wrapper; `[soft]` and `[soft]...[/soft]` are not
its syntax. `[soft-laugh]` is a separate inline effect.

```text
<soft>We can try that.</soft> Next, [pause] tell me what you heard.
```

Historical measurements for the same sentence were: plain 1.75 s, slow 3.59 s,
fast 1.35 s, build-intensity 2.63 s, decrease-intensity 2.55 s, sing-song 3.51 s,
and singing 3.59 s. The notes do not specify enough model/voice/script settings
to reproduce these as a benchmark; use them only as observations.

The notes also report that `[kiss]` sounds like a cartoon smack, and that
`[moan]` depends strongly on the surrounding words or spelled vocalization.
Do not infer an exact performance from the tag name alone.

## Trying a fixed script

For a native Talk experiment, explicitly request the intended delivery style
in your conversation and listen to what Grok actually generates.

To isolate the synthesis behavior of a literal script, use xAI's documented
TTS endpoint with your own xAI API key. REST TTS billing/auth is separate from
a Talk subscription session; a subscription login is not a promise of free
REST TTS access.

```python
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

payload = {
    "text": "That is funny [chuckle]. <soft>We can try that.</soft>",
    "voice_id": "eve",
    "language": "en",
    "output_format": {"codec": "mp3"},
}
request = Request(
    "https://api.x.ai/v1/tts",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {os.environ['XAI_API_KEY']}",
        "Content-Type": "application/json",
    },
    method="POST",
)
with urlopen(request, timeout=120) as response:
    Path("speech-tags.mp3").write_bytes(response.read())
```

Use the same sentence, voice, model if selectable, language, and audio format
for a plain control and a tagged variant. Listen to both; for timing effects,
measure the duration as well. Record the exact spelling, date, settings, and
observed result. Keep synthesis text intact when streaming; a phrase wrapper
split across isolated TTS requests may change delivery. If a UI hides speech
tags, strip them only from the display copy, not from the synthesis payload.

## Sources

- [xAI text-to-speech: speech tags](https://docs.x.ai/developers/model-capabilities/audio/text-to-speech#speech-tags)
- [xAI speech-to-speech: force message](https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech#force-message)
- [Talk provider and authentication details](PROVIDERS.md)
- [Talk cascade behavior](CASCADE.md)
- Switchboard field notes contributed by @zoidypuh, with observations dated
  2026-09-09, 2026-09-13, and 2026-09-15. Extended spellings and listening
  classifications come from those notes, not an independently verified xAI
  support list. Public xAI documentation checked 2026-10-04.
