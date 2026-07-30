from __future__ import annotations

import asyncio
import json
import os
import re

import httpx
from livekit import api

from server.inbound_control.worker_auth import issue_worker_token


def rule_name(binding_id: str) -> str:
    return "inbound-" + re.sub(r"[^a-zA-Z0-9_-]", "", binding_id)[:80]


async def main() -> None:
    control_url = os.environ["INBOUND_CONTROL_URL"].rstrip("/")
    worker_secret = os.environ["INBOUND_WORKER_SECRET"]
    livekit_url = os.environ["LIVEKIT_URL"]
    livekit_key = os.environ["LIVEKIT_API_KEY"]
    livekit_secret = os.environ["LIVEKIT_API_SECRET"]
    def auth_headers() -> dict[str, str]:
        return {"Authorization": "Bearer " + issue_worker_token(
            worker_secret, subject="dispatch-sync", scopes=["dispatch:sync"]
        )}

    async with httpx.AsyncClient(base_url=control_url, timeout=15) as control:
        response = await control.get("/inbound-api/internal/sip/bindings", headers=auth_headers())
        response.raise_for_status()
        bindings = response.json().get("items", [])
        async with api.LiveKitAPI(livekit_url, livekit_key, livekit_secret) as livekit:
            existing_response = await livekit.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
            existing = {item.name: item for item in existing_response.items}
            desired_names = {rule_name(str(binding["id"])) for binding in bindings}
            for stale_name, stale in existing.items():
                if stale_name.startswith("inbound-") and stale_name not in desired_names:
                    await livekit.sip.delete_dispatch_rule(
                        api.DeleteSIPDispatchRuleRequest(
                            sip_dispatch_rule_id=stale.sip_dispatch_rule_id
                        )
                    )
            for binding in bindings:
                name = rule_name(str(binding["id"]))
                item = existing.get(name)
                agent_name = (
                    os.getenv("INBOUND_PUBLIC_AGENT_NAME", "public-demo-agent")
                    if binding["kind"] == "public_demo"
                    else os.getenv("INBOUND_ENTERPRISE_AGENT_NAME", "tenant-voice-agent")
                )
                if item is not None and str(binding["trunk_id"]) not in item.trunk_ids:
                    await livekit.sip.delete_dispatch_rule(
                        api.DeleteSIPDispatchRuleRequest(
                            sip_dispatch_rule_id=item.sip_dispatch_rule_id
                        )
                    )
                    item = None
                if item is None:
                    item = await livekit.sip.create_dispatch_rule(
                        api.CreateSIPDispatchRuleRequest(
                            rule=api.SIPDispatchRule(
                                dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                                    room_prefix=f"in-{str(binding['id'])[:8]}-"
                                )
                            ),
                            trunk_ids=[str(binding["trunk_id"])],
                            inbound_numbers=[str(binding["destination"])],
                            name=name,
                            room_config=api.RoomConfiguration(
                                agents=[
                                    api.RoomAgentDispatch(
                                        agent_name=agent_name,
                                        metadata=json.dumps({"kind": "sip_inbound"}, separators=(",", ":")),
                                    )
                                ]
                            ),
                        )
                    )
                synced = await control.post(
                    "/inbound-api/internal/sip/dispatch-synced",
                    json={
                        "binding_id": binding["id"],
                        "dispatch_rule_id": item.sip_dispatch_rule_id,
                    },
                    headers=auth_headers(),
                )
                synced.raise_for_status()
                print(f"{binding['id']} -> {item.sip_dispatch_rule_id}")


if __name__ == "__main__":
    asyncio.run(main())
