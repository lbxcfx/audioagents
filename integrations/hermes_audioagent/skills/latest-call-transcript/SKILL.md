---
name: latest-call-transcript
description: Send the exact text transcript from the most recent answered AudioAgent call when a Weixin user asks for the latest call chat history, dialogue, text, or transcription.
---

# Send latest call transcript

1. Load `audioagent_get_latest_call_transcript` with `tool_describe`, then call it immediately.
2. Use `direction=outbound` unless the user explicitly asks for an inbound call or for either direction.
3. If the result has `ok=true`, send `formatted_text` as the Weixin reply without summarizing, rewriting, or inventing missing customer speech.
4. If `transcript` is empty, state exactly that the selected call has no available assistant/customer text; do not substitute an older call.
5. If the result has `ok=false`, report its `error` concisely.

Do not query PostgreSQL, MinIO, or local files directly. The AudioAgent control-plane API is authoritative for project access and decrypted call data.
