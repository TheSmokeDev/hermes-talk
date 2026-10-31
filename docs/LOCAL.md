# Local models — the `local` lane

`TALK_PROVIDER=local` points Talk at a self-hosted server that speaks the
OpenAI Realtime WebSocket protocol, so speech, reasoning and tool calls can
run on your own GPU with no API key and no per-minute bill. Talk keeps
Hermes tools (`delegate_task`, `check_work`, `stop_work`, …), barge-in,
transcripts, `hermes talk doctor` and `hermes talk check`.

The lane is server-agnostic. It was built and verified against
[huggingface/speech-to-speech](https://github.com/huggingface/speech-to-speech)
1.1.0 (Apache-2.0): a cascade of Silero VAD and Smart Turn for turn-taking,
Parakeet for speech-to-text, any LLM you choose, and Qwen3-TTS or Kokoro for
speech.

## Quick start

1. Run a model server for the LLM. Any OpenAI-compatible
   `/v1/chat/completions` server with tool calling works; Ollama is the
   shortest path:

   ```bash
   ollama pull gemma4:12b
   ```

2. Install and start the voice server, handing the LLM to Ollama:

   ```bash
   pip install speech-to-speech
   speech-to-speech serve \
       --host 127.0.0.1 --port 8765 \
       --llm_backend chat-completions \
       --model_name gemma4:12b \
       --responses_api_base_url http://127.0.0.1:11434/v1 \
       --responses_api_api_key ollama
   ```

   Ollama ignores the key; the flag just has to be non-empty in every shell.

3. Talk:

   ```bash
   TALK_PROVIDER=local hermes talk check   # one real spoken turn, proves the lane
   TALK_PROVIDER=local hermes talk
   ```

## Why a separate LLM server

speech-to-speech can also run its LLM in process (the default, Qwen3.5-4B via
Transformers). That is fine for chat, but it is not reliable enough for an
agent: in Talk's multi-turn tool test (2026-10-26) the in-process 4B model
kept generating past its turn, invented user lines, and mixed tool-call
formats, so markup was spoken aloud and some turns ran to the token limit.
The `chat-completions` backend sends tools through the server's native
tool-calling API, where the model server parses calls with the model's own
chat template.

## Hardware

Measured on an RTX 3090 (24 GB), Windows 11, 2026-10-26, with Parakeet,
Kokoro and `gemma4:12b` in Ollama all on the GPU: about 9 GB for the whole
stack (13 GB in use including the desktop). A 16 GB card has headroom; the
upstream README's smaller recipe (Gemma 4 E4B at Q4) budgets 16 GB with
Qwen3-TTS.

In Talk's six-turn agent check on that setup (delegate, delegate, progress,
delegate, stop, small talk in one session), every turn picked a sensible
Hermes tool or none, nothing was spoken as markup, tool decisions took 1–2 s,
and spoken replies started 0.6–1.1 s after the request.

A remote GPU works too (for example a Colab or rented H100): serve
speech-to-speech there behind TLS and set `TALK_LOCAL_URL=wss://…`.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `TALK_PROVIDER` | `openai` | Set to `local` to select this lane. |
| `TALK_LOCAL_URL` | `ws://127.0.0.1:8765/v1/realtime` | Server socket. **Fail-closed**: plain `ws://` is accepted only for loopback (`127.0.0.1`, `::1`, `localhost`); any other host must use `wss://`, because the tool loop and transcripts would otherwise cross the network unencrypted. No query string. |
| `TALK_LOCAL_MODEL` | `local` | Sent as the `?model=` query. speech-to-speech ignores it and uses its own `--model_name`. |
| `TALK_LOCAL_VOICE` | `default` | `default` omits the voice so the server keeps its own speaker. Other values pass through unchanged — voice names belong to the server's TTS (Qwen3-TTS speakers include `Aiden`, `Vivian`, `Ryan`). |
| `TALK_LOCAL_API_KEY` | unset | Optional bearer. Unset sends no `Authorization` header (speech-to-speech has no auth); set-but-blank refuses. |
| `TALK_TURN_DETECTION` | `provider_native` | `provider_native` or `server_vad`; `semantic_vad` refuses. |

The session update drops the OpenAI-hosted ids a local server cannot resolve
(the `gpt-4o-transcribe` input transcription model and OpenAI noise
reduction); the server runs its own STT.

`hermes talk doctor` reports the lane offline: URL policy, key presence, model
and voice. It never requires OpenAI credentials when `local` is selected.
`hermes talk check` opens a real session and proves one spoken turn.

## Windows

speech-to-speech is not in its own Windows CI, but runs natively (no WSL)
with these steps, verified on Windows 11 with an RTX 3090 on 2026-10-26:

```powershell
uv venv --python 3.12 .venv
# PyPI's Windows torch wheel is CPU-only; install the CUDA build first.
uv pip install --python .venv\Scripts\python.exe torch torchaudio --index-url https://download.pytorch.org/whl/cu128
uv pip install --python .venv\Scripts\python.exe "speech-to-speech[kokoro]"
.venv\Scripts\speech-to-speech.exe serve --host 127.0.0.1 --port 8765 --tts kokoro `
    --llm_backend chat-completions --model_name gemma4:12b `
    --responses_api_base_url http://127.0.0.1:11434/v1 --responses_api_api_key ollama
```

Qwen3-TTS's default `ggml` backend has no Windows wheel, and its `torch`
backend currently fails on Transformers 5.x (`MimiConfig` has no
`rope_theta`), so use `--tts kokoro` on Windows.

## Limits

- Verified on the terminal lane. The Discord lane honors
  `TALK_PROVIDER=local` but has not been verified against a local server yet.
  The lane is not registered as a Hermes-core realtime provider, so the
  dashboard and Desktop surfaces do not use it.
- speech-to-speech serves one session per pipeline by default (`--num_pipelines`);
  an extra socket is refused with close code 1008.
- Tool-calling quality is the LLM's. Run the bundled multi-turn check before
  trusting a new model with real work.
