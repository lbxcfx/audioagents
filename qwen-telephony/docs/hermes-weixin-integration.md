# Hermes / Weixin outbound calling integration

## Data flow

```text
Weixin DM
  -> Hermes gateway
  -> operator preview and native tool approval
  -> audioagent_submit_outbound_task
  -> AudioAgent campaign + task prompt snapshot
  -> dispatcher allow-listed metadata
  -> Qwen Phone Agent
  -> Insights transcript / call.result
  -> delegate_task -> audioagent_wait_outbound_task
  -> Hermes asynchronous result delivery to the originating Weixin chat
```

The maintained Hermes plugin is in `integrations/hermes_audioagent`. Do not
vendor the upstream Hermes repository into this project.

## Prompt and customer isolation

Each Hermes task stores an immutable prompt snapshot in campaign metadata:

```json
{
  "integration": "hermes",
  "task": {
    "id": "hermes-...",
    "prompt_snapshot": "...",
    "scene_id": 42
  }
}
```

Campaign materialization copies that task snapshot into every call and merges
only that contact's `name`, `company`, and `profile` into `customer`. The
dispatcher does not forward delivery routing or arbitrary metadata to LiveKit;
it forwards only the task ID, prompt snapshot, optional scene ID, and customer
variables. The Phone Agent loads this per-call prompt before opening the
Realtime session. Editing a global prompt file is not part of this workflow.

This supports both:

- one prompt with multiple simultaneous customers (one campaign); and
- multiple simultaneous tasks with different prompts (one prompt snapshot per
  campaign/call).

## Hermes plugin setup

Install Hermes separately, then link the plugin:

```bash
ln -s /absolute/path/to/audioagents/integrations/hermes_audioagent \
  ~/.hermes/plugins/audioagent
hermes plugins enable audioagent
```

Configure `~/.hermes/.env`:

```bash
AUDIOAGENT_BASE_URL=http://127.0.0.1:8090
AUDIOAGENT_PROJECT_ID=<project-id>
AUDIOAGENT_AGENT_NAME=<livekit-agent-name>
AUDIOAGENT_TRUNK_ID=<audioagent-trunk-id>
AUDIOAGENT_SOURCE_NUMBER=<e164-source-number>
# Production only; prefer a scoped bearer token over development X-User-ID.
AUDIOAGENT_BEARER_TOKEN=<scoped-token>

WEIXIN_DM_POLICY=allowlist
WEIXIN_ALLOWED_USERS=<operator-weixin-user-id>
```

Run `hermes gateway setup`, select Weixin, scan the iLink QR code, and start the
gateway. Restrict the `hermes-weixin` surface to the AudioAgent toolset and any
minimal conversational tools required by the deployment. Do not expose shell,
terminal, filesystem write, or unrelated high-privilege tools to an open DM
policy.

## Operator workflow

1. The operator sends the task, customers, schedule, and desired concurrency.
2. Hermes generates a full prompt and displays a preview containing every
   destination number and the task parameters.
3. The operator explicitly confirms. The plugin also triggers Hermes' native
   tool approval gate before the submission executes.
4. Hermes submits the campaign and returns `task_id` and `campaign_id`.
5. Hermes delegates one self-contained wait task containing the campaign ID.
   Top-level delegation runs asynchronously; its completion re-enters the
   originating session and Hermes sends the structured summary back to the same
   Weixin chat. If delegation is disabled, use `/background` as the fallback.

The AudioAgent control plane is authoritative for calling windows, consent,
do-not-call entries, recording disclosure, retry limits, trunk limits, and
project capacity. A Hermes request cannot bypass a control-plane rejection.

## Result contract

The result tool returns, per customer:

- call status and failure details;
- human/voicemail classification and disposition;
- saved `call.result` summary and intent label;
- recording state; and
- an optional transcript when explicitly requested.

If the model did not save `call.result`, the tool marks `summary_missing=true`
and returns a small set of the latest customer utterances instead of inventing
a result.

## Verification checklist

- Unit tests pass for campaign metadata snapshots, dispatcher filtering,
  per-call prompt precedence, worker console permissions, terminal call state,
  and all plugin handlers.
- Start the Operations API, dispatcher, and Phone Agent with the same project,
  agent, and trunk configuration.
- Validate one confirmed call first, then two customers using one prompt, then
  two simultaneous campaigns using different prompts.
- Confirm the final Weixin reply contains the right customer's result and no
  other task's prompt, customer profile, or delivery chat ID.
