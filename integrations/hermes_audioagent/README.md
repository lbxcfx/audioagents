# Hermes AudioAgent plugin

This standalone Hermes plugin turns a confirmed messaging task into an
AudioAgent campaign. One immutable prompt snapshot is shared by the task's
customers; customer variables remain isolated per call.

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
```

Use `AUDIOAGENT_BEARER_TOKEN` outside development. `AUDIOAGENT_USER_ID` is only
for the AudioAgent development authentication mode.

For Weixin, set `WEIXIN_DM_POLICY=allowlist`, populate `WEIXIN_ALLOWED_USERS`,
and disable unrelated high-privilege tools on the `hermes-weixin` surface.
The submit and cancel tools independently require an explicit confirmation
boolean.

After submission, delegate one self-contained wait task with `delegate_task`.
Top-level Hermes delegation is asynchronous and its result re-enters the same
messaging session, so the final summary is delivered back to Weixin without
blocking the foreground turn. If delegation is disabled, the operator can use
`/background` with the returned campaign ID.
