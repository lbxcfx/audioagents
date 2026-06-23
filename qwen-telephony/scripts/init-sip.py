from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit import api


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "qwen-telephony"
load_dotenv(ROOT / ".env")
load_dotenv(APP / "config" / "local.env", override=False)


async def main() -> None:
    url = os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880")
    api_key = os.getenv("LIVEKIT_API_KEY", "devkey")
    api_secret = os.getenv("LIVEKIT_API_SECRET", "secret")
    room_name = os.getenv("QWEN_AGENT_ROOM", "qwen-phone-room")
    inbound_number = os.getenv("SIP_INBOUND_NUMBER", "1000")
    trunk_name = os.getenv("SIP_TRUNK_NAME", "microsip-local-inbound")
    rule_name = os.getenv("SIP_DISPATCH_RULE_NAME", "microsip-to-qwen-agent")

    async with api.LiveKitAPI(url, api_key, api_secret) as lkapi:
        trunks = await lkapi.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
        trunk = next((item for item in trunks.items if item.name == trunk_name), None)
        if trunk is None:
            trunk = await lkapi.sip.create_inbound_trunk(
                api.CreateSIPInboundTrunkRequest(
                    trunk=api.SIPInboundTrunkInfo(
                        name=trunk_name,
                        numbers=[],
                        allowed_addresses=[
                            "127.0.0.1/32",
                            "172.16.0.0/12",
                            "192.168.0.0/16",
                        ],
                        allowed_numbers=[],
                    )
                )
            )
            print("Created SIP inbound trunk:", trunk.sip_trunk_id)
        else:
            print("SIP inbound trunk already exists:", trunk.sip_trunk_id)

        rules = await lkapi.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
        rule = None
        for item in rules.items:
            direct = item.rule.dispatch_rule_direct
            if (
                item.name == rule_name
                and direct.room_name == room_name
                and trunk.sip_trunk_id in item.trunk_ids
            ):
                rule = item
                break

        if rule is None:
            rule = await lkapi.sip.create_dispatch_rule(
                api.CreateSIPDispatchRuleRequest(
                    rule=api.SIPDispatchRule(
                        dispatch_rule_direct=api.SIPDispatchRuleDirect(room_name=room_name)
                    ),
                    trunk_ids=[trunk.sip_trunk_id],
                    name=rule_name,
                )
            )
            print("Created SIP dispatch rule:", rule.sip_dispatch_rule_id)
        else:
            print("SIP dispatch rule already exists:", rule.sip_dispatch_rule_id)

    print("Room:", room_name)


if __name__ == "__main__":
    asyncio.run(main())
