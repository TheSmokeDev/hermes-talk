# Third-party notices

Hermes Talk is licensed under the [MIT License](LICENSE). The following source
and contributor notices apply to this integration.

## OpenClaw GPT-Live subscription integration

The subscription request shape and model/voice catalog in `talk_live_config.py`
and `talk_live_transport.py` adapt the OpenClaw implementation reviewed in
[openclaw/openclaw PR #133079](https://github.com/openclaw/openclaw/pull/133079),
authored by [steipete](https://github.com/steipete).
Source revision: [`76378ddb777eacbe2c7f65c4247692b2f5830e97`](https://github.com/openclaw/openclaw/tree/76378ddb777eacbe2c7f65c4247692b2f5830e97).
The source file is
[`extensions/openai/realtime-quicksilver.ts`](https://github.com/openclaw/openclaw/blob/76378ddb777eacbe2c7f65c4247692b2f5830e97/extensions/openai/realtime-quicksilver.ts).

The applicable upstream license is reproduced below:

```text
MIT License

Copyright (c) 2026 OpenClaw Foundation

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The upstream repository maintains its own additional notices in
[THIRD_PARTY_NOTICES.md](https://github.com/openclaw/openclaw/blob/76378ddb777eacbe2c7f65c4247692b2f5830e97/THIRD_PARTY_NOTICES.md).

## Hermes Talk PR #135

Credit to [JakeStevenson](https://github.com/JakeStevenson) for the GPT-Live
client-delegation proposal and implementation in
[hermes-talk PR #135](https://github.com/TheSmokeDev/hermes-talk/pull/135).
The replacement integration selectively incorporates its Live-mode and browser
transport ideas; this notice does not represent a wholesale merge of that PR.
The contribution was proposed under this repository's MIT license.

## Codex protocol references

The Codex worker is an original Python client of the public
[Codex app-server interface](https://learn.chatgpt.com/docs/app-server).
No Codex Rust implementation is copied or vendored by this integration. Codex
runs as a separately installed program and retains its own distribution notices.
Protocol references do not grant access to a provider model or subscription.
