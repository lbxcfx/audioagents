# Hermes AudioAgent plugin

This standalone Hermes plugin turns a WeChat task directly into an AudioAgent
campaign. One immutable prompt snapshot is shared by the task's
customers; customer variables remain isolated per call.

It also registers two Hermes-only retrieval skills:

- `audioagent:latest-call-transcript` sends the exact assistant/customer text
  from the latest answered call.
- `audioagent:latest-call-recording` downloads the latest completed recording
  through the control plane and lets the Weixin adapter upload it as an audio
  attachment.

The plugin additionally registers `qwen` as a Hermes transcription provider.
It sends incoming voice notes to Qwen3-ASR-Flash through Alibaba Cloud Model
Studio's OpenAI-compatible REST API. The client uses only Python's standard
library: it does not install `faster-whisper`, `sounddevice`, `numpy`, or the
DashScope SDK.

The AudioAgent control plane also owns a single address-book table. Historical
Hermes call contacts and newly submitted full-name/phone pairs are folded into
it automatically. Each real full name produces exactly four lookup keys when
available: full name, given-name shorthand, full pinyin, and short pinyin
(`李家魁`, `家魁`, `lijiakui`, `jiakui`). Generic forms of address such as `李总`
or `任总` are rejected and never stored.

## Install

Link or copy this directory to `~/.hermes/plugins/audioagent`, then enable the
plugin with `hermes plugins enable audioagent`.

Required configuration:

```bash
AUDIOAGENT_BASE_URL=http://127.0.0.1:8090
AUDIOAGENT_PROJECT_ID=<project-id>
AUDIOAGENT_AGENT_NAME=<livekit-agent-name>
AUDIOAGENT_TRUNK_ID=<audioagent-trunk-id>
AUDIOAGENT_SOURCE_NUMBER=<e164-source-number>
DASHSCOPE_API_KEY=<model-studio-api-key>
```

Use `AUDIOAGENT_BEARER_TOKEN` outside development. `AUDIOAGENT_USER_ID` is only
for the AudioAgent development authentication mode.

Select Qwen ASR in `~/.hermes/config.yaml`:

```yaml
stt:
  enabled: true
  provider: qwen
  language: zh
  qwen:
    model: qwen3-asr-flash
    language: zh
```

`QWEN_ASR_BASE_URL` optionally overrides the compatible endpoint. It defaults
to the still-supported Beijing endpoint
`https://dashscope.aliyuncs.com/compatible-mode/v1`; use a workspace-specific
`*.maas.aliyuncs.com/compatible-mode/v1` endpoint when available.

Optional recording-delivery limits:

```bash
AUDIOAGENT_RECORDING_MAX_BYTES=67108864
AUDIOAGENT_RECORDING_DOWNLOAD_TIMEOUT_SECONDS=60
AUDIOAGENT_RECORDING_NORMALIZE_TIMEOUT_SECONDS=120
# Optional explicit executable name/path; defaults to ffmpeg on PATH.
AUDIOAGENT_FFMPEG_BIN=ffmpeg
```

Weixin requests such as `发一下最近通话聊天记录`, `把刚才的通话录音发给我`,
or a request for both are routed to the plugin tools by Hermes middleware. The
plugin never asks Codex or a shell agent to query the database. AudioAgent's
authenticated project APIs remain the source of truth, and recording files are
normalized to a seekable 16 kHz/64 kbps MP3 before being cached under
`HERMES_HOME/cache/documents` for Hermes `MEDIA:` delivery. This prevents
Weixin and other chat clients from estimating a short VBR recording as a much
longer file when the source MP3 has no reliable Xing duration header.

For Weixin, set `WEIXIN_DM_POLICY=allowlist`, populate `WEIXIN_ALLOWED_USERS`,
and disable unrelated high-privilege tools on the `hermes-weixin` surface.
The original WeChat task message authorizes immediate submission. The plugin
does not show a preview, ask follow-up questions about missing business facts,
or require a confirmation boolean. Cancellation remains separately confirmed.
Every submitted prompt is prefixed with the invariant caller identity
`我是李宝祥的智能助理。`; unknown facts are omitted rather than invented.
An LLM request middleware recognizes WeChat messages containing both a phone
number and an outbound-call intent. It removes prior conversation turns from
that DeepSeek request and injects the direct-execution rules, while preserving
the current turn's tool calls and results. Result cards, prior dialing
acknowledgements, and quoted out-of-band markers are explicitly excluded so a
question about an earlier call cannot launch a duplicate campaign.

When a text command omits the phone number, Hermes calls
`audioagent_resolve_outbound_contact`. A unique exact match on any of the four
keys, including full or short pinyin, is submitted immediately. Fuzzy or
ambiguous results are never dialed until the user confirms a numbered
candidate. Every match produced from a Weixin voice command also requires
confirmation because ASR can preserve pronunciation while choosing the wrong
characters. If nothing sufficiently similar exists, the deterministic reply
asks for a name and phone number. The model never receives authority to invent
or infer a number; `audioagent_confirm_address_book_contact` loads the stored
candidate and pending task directly from Hermes plugin state.

The submit tool records a response keyed by the current Hermes session. A
`transform_llm_output` hook then replaces the model's entire Weixin response
with `拨号中...` only when the control plane actually queued at least one call.
Failures are likewise rendered from the submit tool's error. The LLM therefore
cannot add a recipient, call status, conclusion, attachment, or prompt marker
to a dialing acknowledgement. Keep Hermes final-only streaming enabled for
Weixin (`streaming.enabled: false`), as in the supplied runtime configuration.

Set `AUDIOAGENT_RESULT_FORWARDING=true` to run the plugin's terminal-campaign
watcher inside the Hermes Gateway. It sends each completed Hermes campaign to
`AUDIOAGENT_RESULT_TARGET` (default `weixin`) and persists successfully delivered
campaign IDs under `HERMES_HOME` to deduplicate notifications across normal
gateway restarts. Only after a campaign reaches a terminal state, Weixin
receives Markdown with the recipient, phone number, invitation, initiator,
call status, and every `agent.response` / `user.transcript` event in database
order under `通话记录`. Model-written `call.result.summary` values are never used
in this customer-facing notification. Hermes' Weixin adapter splits messages
over its transport limit, so the plugin does not truncate the transcript. The
watcher persists an at-most-once claim before sending, so concurrent plugin
loads cannot produce duplicate notifications.

After submission, the only successful acknowledgement is `拨号中...`. Do not
delegate or poll for a second chat response: the result forwarder owns terminal
notification and sends one deduplicated transcript result to Weixin. Submission
acknowledgements must not include images, `MEDIA:` directives, file paths, or
attachment placeholders such as `[Sent image attachment]`.
