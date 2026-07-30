from __future__ import annotations

import asyncio
import os

import httpx

from server.inbound_control.worker_auth import issue_worker_token


async def main() -> None:
    secret = os.environ["INBOUND_WORKER_SECRET"]
    token = issue_worker_token(secret, subject="maintenance", scopes=["maintenance:run"])
    async with httpx.AsyncClient(
        base_url=os.environ["INBOUND_CONTROL_URL"].rstrip("/"), timeout=30
    ) as client:
        response = await client.post(
            "/inbound-api/internal/sessions/reap",
            json={"active_grace_seconds": int(os.getenv("INBOUND_ACTIVE_GRACE_SECONDS", "7500"))},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        print(response.json())


if __name__ == "__main__":
    asyncio.run(main())
