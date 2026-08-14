"""Tests for Memory API endpoints."""

import pytest
from fastapi.testclient import TestClient

from jarvis.api.http import create_app
from jarvis.config.models import JarvisConfig, MemoryConfig
from jarvis.config.secrets import SecretManager


@pytest.fixture
def client(tmp_path):
    config = JarvisConfig(memory=MemoryConfig(database_path=tmp_path / "memory.sqlite"))
    app = create_app(
        config=config,
        secret_manager=SecretManager({"JARVIS_API_TOKEN": "test-token"}, use_keyring=False),
    )
    with TestClient(app) as client:
        yield client

def test_memory_search_unauthenticated(client):
    response = client.get("/v1/memories?q=test")
    assert response.status_code == 401

def test_memory_lifecycle_api(client):
    headers = {"Authorization": "Bearer test-token"}
    
    # 1. List proposals (empty)
    response = client.get("/v1/memories/proposals", headers=headers)
    assert response.status_code == 200
    assert response.json() == []
    
    # 2. Setup a proposal
    from jarvis.memory.store import MemoryStore
    store: MemoryStore = client.app.state.memory_store
    
    import asyncio
    proposal_id = asyncio.run(store.propose(
        memory_type="fact",
        proposed_content="The Earth is round",
        reason="Science"
    ))
    
    # 3. List proposals
    response = client.get("/v1/memories/proposals", headers=headers)
    assert response.status_code == 200
    proposals = response.json()
    assert len(proposals) == 1
    assert proposals[0]["id"] == proposal_id
    
    # 4. Approve proposal
    response = client.post(
        f"/v1/memories/proposals/{proposal_id}/approve", 
        headers=headers,
        json={"title": "Planet Shape"}
    )
    assert response.status_code == 200
    memory_id = response.json()["id"]
    
    # 5. Search memory (using new browse endpoint)
    response = client.get("/v1/memories?q=Earth", headers=headers)
    assert response.status_code == 200
    results = response.json()
    assert len(results) == 1
    assert results[0]["id"] == memory_id
    assert results[0]["title"] == "Planet Shape"
    
    # 6. List long-term memory
    response = client.get("/v1/memories", headers=headers)
    assert response.status_code == 200
    memories = response.json()
    assert len(memories) == 1
    assert memories[0]["id"] == memory_id
    
    # 7. Delete memory
    response = client.delete(f"/v1/memories/{memory_id}", headers=headers)
    assert response.status_code == 200
    
    # 8. Verify deletion
    response = client.get("/v1/memories?q=Earth", headers=headers)
    assert response.json() == []

def test_deny_proposal_api(client):
    headers = {"Authorization": "Bearer test-token"}
    from jarvis.memory.store import MemoryStore
    store: MemoryStore = client.app.state.memory_store
    
    import asyncio
    proposal_id = asyncio.run(store.propose(
        memory_type="note",
        proposed_content="Delete this",
        reason="Testing denial"
    ))
    
    response = client.post(
        f"/v1/memories/proposals/{proposal_id}/deny",
        headers=headers,
        json={"reason": "Not needed"}
    )
    assert response.status_code == 200
    
    # Verify it's no longer in pending proposals
    response = client.get("/v1/memories/proposals", headers=headers)
    assert response.json() == []

def test_approve_missing_proposal(client):
    headers = {"Authorization": "Bearer test-token"}
    response = client.post(
        "/v1/memories/proposals/missing-id/approve",
        headers=headers,
        json={"title": "Test"}
    )
    assert response.status_code == 400


def test_cli_memory_routes(client):
    headers = {"Authorization": "Bearer test-token"}
    from jarvis.memory.store import MemoryStore
    store: MemoryStore = client.app.state.memory_store
    import asyncio
    
    # 1. Test /v1/memory/proposals
    proposal_id = asyncio.run(store.propose(
        memory_type="preference",
        proposed_content="User prefers Python",
        reason="Observed coding"
    ))
    
    response = client.get("/v1/memory/proposals", headers=headers)
    assert response.status_code == 200
    proposals = response.json()
    assert any(p["id"] == proposal_id for p in proposals)
    
    # 2. Test /v1/memory/proposals/{proposal_id}/approve
    response = client.post(
        f"/v1/memory/proposals/{proposal_id}/approve",
        headers=headers,
        json={"title": "Python Preference"}
    )
    assert response.status_code == 200
    memory_id = response.json()["id"]
    
    # 3. Test /v1/memory/search
    response = client.get("/v1/memory/search?q=Python", headers=headers)
    assert response.status_code == 200
    results = response.json()
    assert len(results) >= 1
    assert results[0]["id"] == memory_id
    assert "relevance_score" in results[0]
    
    # 4. Test /v1/memory/long-term
    response = client.get("/v1/memory/long-term", headers=headers)
    assert response.status_code == 200
    memories = response.json()
    assert any(m["id"] == memory_id for m in memories)
    
    # 5. Test /v1/memory/long-term/{memory_id} (delete)
    response = client.delete(f"/v1/memory/long-term/{memory_id}", headers=headers)
    assert response.status_code == 200
    
    # 6. Test /v1/memory/proposals/{proposal_id}/deny
    proposal_id_2 = asyncio.run(store.propose(
        memory_type="note",
        proposed_content="Temporary note",
        reason="Testing denial"
    ))
    response = client.post(
        f"/v1/memory/proposals/{proposal_id_2}/deny",
        headers=headers,
        json={"reason": "Denied"}
    )
    assert response.status_code == 200

