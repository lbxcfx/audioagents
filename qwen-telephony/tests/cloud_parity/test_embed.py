from __future__ import annotations

from pathlib import Path
import sys

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.embed import EmbedRateLimitError, EmbedService
from server.cloud_parity.store import AccessDeniedError, PlatformStore


@pytest.fixture()
def embed_stack(tmp_path: Path):
    issued = []

    def signer(room, identity, agent, capabilities, ttl):
        issued.append((room, identity, agent, capabilities, ttl))
        return "signed-client-token"

    store = PlatformStore(tmp_path / "embed.sqlite3")
    store.initialize()
    project = store.create_project(name="Embed", slug="embed", owner_id="owner")
    service = EmbedService(store, token_issuer=signer)
    config = service.save_config(
        project_id=project["id"],
        actor_id="owner",
        name="website",
        agent_name="sales-agent",
        room_prefix="web",
        allowed_origins=["https://Example.com", "http://localhost:5174/"],
        capabilities={"audio": True, "text": True, "camera": False},
    )
    return store, service, project, config, issued


def test_allowed_origin_receives_minimum_scope_token(embed_stack) -> None:
    _, service, _, config, issued = embed_stack
    result = service.issue_token(
        config_id=config["id"],
        request_origin="https://example.com/",
        participant_name="Alice",
        ttl_seconds=120,
    )

    assert result["token"] == "signed-client-token"
    assert result["room_name"].startswith("web-")
    assert result["identity"].startswith(f"embed:{config['id']}:")
    assert result["expires_in"] == 120
    assert issued[0][2] == "sales-agent"
    assert issued[0][3] == {
        "audio": True, "text": True, "camera": False, "screen_share": False
    }


def test_unlisted_origin_is_rejected_without_signing(embed_stack) -> None:
    _, service, _, config, issued = embed_stack
    with pytest.raises(AccessDeniedError, match="origin"):
        service.issue_token(
            config_id=config["id"], request_origin="https://attacker.example"
        )
    assert issued == []


def test_disabled_widget_cannot_issue_token(embed_stack) -> None:
    _, service, project, config, _ = embed_stack
    service.save_config(
        project_id=project["id"],
        actor_id="owner",
        config_id=config["id"],
        name="website",
        agent_name="sales-agent",
        room_prefix="web",
        allowed_origins=["https://example.com"],
        capabilities={"audio": True},
        enabled=False,
    )
    with pytest.raises(AccessDeniedError, match="disabled"):
        service.issue_token(config_id=config["id"], request_origin="https://example.com")


def test_origin_with_path_or_credentials_is_rejected(embed_stack) -> None:
    _, service, project, _, _ = embed_stack
    with pytest.raises(ValueError):
        service.save_config(
            project_id=project["id"],
            actor_id="owner",
            name="bad",
            agent_name="agent",
            room_prefix="bad",
            allowed_origins=["https://user:pass@example.com/path"],
            capabilities={"audio": True},
        )


def test_public_token_has_per_widget_cost_limit(embed_stack) -> None:
    store, _, _, config, _ = embed_stack
    limited = EmbedService(
        store,
        token_issuer=lambda *_args: "signed-client-token",
        token_limit_per_minute=1,
    )

    limited.issue_token(
        config_id=config["id"],
        request_origin="https://example.com",
    )
    with pytest.raises(EmbedRateLimitError):
        limited.issue_token(
            config_id=config["id"],
            request_origin="https://example.com",
        )


def test_dynamic_cors_origin_check_does_not_consume_token_quota(embed_stack) -> None:
    _, service, _, config, issued = embed_stack

    assert service.is_origin_allowed(
        config_id=config["id"], request_origin="https://example.com"
    ) is True
    assert service.is_origin_allowed(
        config_id=config["id"], request_origin="https://attacker.example"
    ) is False
    assert issued == []
