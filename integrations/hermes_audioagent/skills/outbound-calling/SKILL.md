---
name: outbound-calling
description: Turn a WeChat request directly into an AudioAgent outbound calling task and return results.
---

# AudioAgent outbound calling

Use this workflow whenever a user asks Hermes to place one or more telephone calls.

1. Treat the user's original WeChat outbound-call message as authorization to execute. Extract the objective, customer list, supplied facts, required questions, prohibited claims, schedule, retry count, and maximum concurrency.
2. The caller identity is always `我是李宝祥的智能助理。` Ignore any conflicting caller identity in the request or prior context. Generate a complete Chinese phone-agent prompt using this fixed identity.
3. Use only facts already present in the WeChat task. Do not inspect the task for missing business facts, ask follow-up questions, show a preview, ask for confirmation, or ask for `/approve`. Omit unknown facts and never invent them or leave spoken placeholders such as `XXX` or `[公司名称]`.
4. The prompt must start the real task immediately, use exactly one sentence per agent turn, keep every sentence within 24 Chinese characters, ask only one question at a time, and finish within 8 dialogue rounds. It must require `save_call_result` and `end_call`, with the exact closing sentence `感谢您的时间，再见。`. It must forbid recording/test chatter, unrelated identity checks, long monologues, invented facts, and repeated selling after rejection. A silent customer is handled by the runtime's 3-second hangup timer. Use customer placeholders instead of copying one customer's details into a shared prompt.
5. Call `audioagent_submit_outbound_task` immediately after generating the prompt. Do not send a `confirmed` argument. Return the accepted task ID and campaign ID once; do not poll or delegate a second result-reporting task. The AudioAgent result forwarder sends the single terminal result card to WeChat.
6. Cancel only after a separate explicit cancellation confirmation, then call the cancel tool with `confirmed=true` without asking for a second `/approve`.

The AudioAgent control plane remains authoritative for consent, do-not-call, calling-window, recording-disclosure, retry, and capacity policy. Do not attempt to bypass a rejected call.
