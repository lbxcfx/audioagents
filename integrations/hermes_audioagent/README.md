# Hermes AudioAgent plugin

This standalone Hermes plugin turns a WeChat task directly into an AudioAgent
campaign. One immutable prompt snapshot is shared by the task's
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
The original WeChat task message authorizes immediate submission. The plugin
does not show a preview, ask follow-up questions about missing business facts,
or require a confirmation boolean. Cancellation remains separately confirmed.
Every submitted prompt is prefixed with the invariant caller identity
`我是李宝祥的智能助理。`; unknown facts are omitted rather than invented.

Set `AUDIOAGENT_RESULT_FORWARDING=true` to run the plugin's terminal-campaign
watcher inside the Hermes Gateway. It sends each completed Hermes campaign to
`AUDIOAGENT_RESULT_TARGET` (default `weixin`) and persists successfully delivered
campaign IDs under `HERMES_HOME` to deduplicate notifications across normal
gateway restarts. Weixin receives one self-contained PNG result card. The
watcher persists an at-most-once claim before sending, so concurrent plugin
loads or a partial media send cannot produce duplicate cards. If card rendering
is unavailable, the same single attempt falls back to the complete text result.

After submission, return the task and campaign IDs once. Do not delegate or poll
for a second chat response: the result forwarder owns terminal notification and
sends one deduplicated result card to Weixin.
