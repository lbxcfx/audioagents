---
name: outbound-calling
description: Safely turn a WeChat request into a confirmed AudioAgent outbound calling task and return results.
---

# AudioAgent outbound calling

Use this workflow whenever a user asks Hermes to place one or more telephone calls.

1. Extract a structured task: objective, caller identity, customer list, facts, required questions, prohibited claims, schedule, retry count, and maximum concurrency.
2. Generate a complete Chinese phone-agent prompt. Include explicit opening, one-question-at-a-time dialogue rules, refusal/stop handling, knowledge boundaries, required result fields, `save_call_result`, and `end_call`. Use customer placeholders instead of copying one customer's details into a shared prompt.
3. Before any external call, show the operator a concise preview containing every phone number, task objective, caller identity, schedule, retry count, concurrency, and prompt summary. Ask for explicit confirmation.
4. Treat the operator's explicit reply to the preview as the single interactive confirmation. Call `audioagent_submit_outbound_task` immediately with `confirmed=true`; do not ask for a second `/approve`. Never infer confirmation from the original task request.
5. Return the accepted task ID and campaign ID immediately. Then call `delegate_task` once with a self-contained goal telling the child to run `audioagent_wait_outbound_task` for that campaign ID and summarize the structured result in Chinese. Top-level Hermes delegations run in the background and their completion re-enters the originating messaging session automatically. Do not poll from the foreground turn. If delegation is unavailable, tell the operator to use `/background` with the campaign ID.
6. When the background result returns, report each customer's terminal status and saved business summary to the originating chat. Do not invent missing summaries. Include full transcripts only when the operator explicitly asks for them.
7. Cancel only after a separate explicit cancellation confirmation, then call the cancel tool with `confirmed=true` without asking for a second `/approve`.

The AudioAgent control plane remains authoritative for consent, do-not-call, calling-window, recording-disclosure, retry, and capacity policy. Do not attempt to bypass a rejected call.
