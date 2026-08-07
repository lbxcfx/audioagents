---
name: latest-call-recording
description: Upload the most recent answered AudioAgent call recording to Weixin when a user asks for the latest call audio, recording, voice file, or 通话录音.
---

# Send latest call recording

1. Load `audioagent_get_latest_call_recording` with `tool_describe`, then call it immediately.
2. Use `direction=outbound` unless the user explicitly asks for an inbound call or for either direction.
3. If the result has `ok=true`, copy `media_directive` exactly into the final reply on its own line. Do not quote it, wrap it in a code block, convert it to a link, or merely describe the local path. The Weixin adapter consumes this directive and uploads the audio as a native attachment.
4. Add at most one short caption such as `最近一通通话录音：`; never expose the temporary signed download URL.
5. If the result has `ok=false`, report its `error` concisely and do not emit a `MEDIA:` directive.

Do not query PostgreSQL, MinIO, or local files directly. The tool uses the AudioAgent control plane and writes only to Hermes' approved media cache.
