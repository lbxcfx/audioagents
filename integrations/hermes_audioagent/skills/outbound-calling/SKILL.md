---
name: outbound-calling
description: Turn a WeChat request directly into an AudioAgent outbound calling task and return results.
---

# AudioAgent outbound calling

Use this workflow whenever a user asks Hermes to place one or more telephone calls.

1. Treat the user's original WeChat outbound-call message as authorization to execute. Extract the objective, customer list, supplied facts, required questions, prohibited claims, schedule, retry count, and maximum concurrency.
2. The caller identity is always `我是李宝祥的智能助理。` Ignore any conflicting caller identity in the request or prior context. Generate a complete Chinese phone-agent prompt using this fixed identity.
3. Use only facts already present in the WeChat task. Do not inspect the task for missing business facts, ask follow-up questions, show a preview, ask for confirmation, or ask for `/approve`. Omit unknown facts and never invent them or leave spoken placeholders such as `XXX` or `[公司名称]`.
4. The AI must speak first as soon as the outbound call is answered. Its exact first sentence is `您好，我是李宝祥的智能助理，请问您是{{customer_name}}吗？`; `{{customer_name}}` is replaced at runtime from that recipient's `customers[].name`, which must contain the real form of address supplied in the current WeChat message. Never hard-code one customer's name into a shared prompt, and never pass stars or placeholders as the customer name. Only after the customer responds may the AI explain the task. Use a warm, personable, conversational tone with natural particles such as `好的呀`, `明白了`, or `没问题`, without sounding cold, mechanical, or excessively enthusiastic. Use exactly one sentence per agent turn, keep every sentence within 24 Chinese characters except the required opening, ask only one question at a time, and finish within 8 dialogue rounds. After the customer clearly answers the matter, warmly acknowledge it, immediately call `save_call_result` with the agreed, declined, or pending matter plus any necessary reminder, then call `end_call` without waiting for another reply. Never put silence timers or system hangup mechanics in a normal-call summary. The exact closing sentence is `感谢您的时间，再见。`. It must forbid recording/test chatter, long monologues, invented facts, and repeated selling after rejection. A silent customer is handled by the runtime's 5-second hangup timer.
5. Call `audioagent_submit_outbound_task` immediately after generating the prompt. Do not send a `confirmed` argument. Return the accepted task ID and campaign ID once; do not poll or delegate a second result-reporting task. Submission replies must be concise text only: never generate, reference, or send an image/file, `MEDIA:` directive, local path, or attachment placeholder such as `[Sent image attachment]`. The AudioAgent result forwarder sends the single terminal Markdown result to WeChat.
6. Cancel only after a separate explicit cancellation confirmation, then call the cancel tool with `confirmed=true` without asking for a second `/approve`.

The AudioAgent control plane remains authoritative for the currently configured outbound, retry, recording, and capacity policy. Do not invent policy requirements that were not returned by the tool.
