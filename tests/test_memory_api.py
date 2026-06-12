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
    response = client.get("/v1/memory/search?q=test")
    assert response.status_code == 401

def test_memory_lifecycle_api(client):
    headers = {"Authorization": "Bearer test-token"}
    
    # 1. List proposals (empty)
    response = client.get("/v1/memory/proposals", headers=headers)
    assert response.status_code == 200
    assert response.json() == []
    
    # 2. Setup a proposal (via Store directly since we don't have a POST /proposals yet)
    # Actually, the requirement didn't ask for POST /proposals, 
    # but we need it for testing the lifecycle via API.
    # I'll use the store from app.state.
    from jarvis.memory.store import MemoryStore
    store: MemoryStore = client.app.state.memory_store
    
    import asyncio
    proposal_id = asyncio.run(store.propose(
        memory_type="fact",
        proposed_content="The Earth is round",
        reason="Science"
    ))
    
    # 3. List proposals
    response = client.get("/v1/memory/proposals", headers=headers)
    assert response.status_code == 200
    proposals = response.json()
    assert len(proposals) == 1
    assert proposals[0]["id"] == proposal_id
    
    # 4. Approve proposal
    response = client.post(
        f"/v1/memory/proposals/{proposal_id}/approve", 
        headers=headers,
        json={"title": "Planet Shape"}
    )
    assert response.status_code == 201
    memory_id = response.json()["id"]
    
    # 5. Search memory
    response = client.get("/v1/memory/search?q=Earth", headers=headers)
    assert response.status_code == 200
    results = response.json()
    assert len(results) == 1
    assert results[0]["id"] == memory_id
    assert results[0]["title"] == "Planet Shape"
    
    # 6. List long-term memory
    response = client.get("/v1/memory/long-term", headers=headers)
    assert response.status_code == 200
    memories = response.json()
    assert len(memories) == 1
    assert memories[0]["id"] == memory_id
    
    # 7. Delete memory
    response = client.delete(f"/v1/memory/long-term/{memory_id}", headers=headers)
    assert response.status_code == 200
    
    # 8. Verify deletion
    response = client.get("/v1/memory/search?q=Earth", headers=headers)
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
        f"/v1/memory/proposals/{proposal_id}/deny",
        headers=headers,
        json={"reason": "Not needed"}
    )
    assert response.status_code == 200
    
    # Verify it's no longer in pending proposals
    response = client.get("/v1/memory/proposals", headers=headers)
    assert response.json() == []

def test_approve_missing_proposal(client):
    headers = {"Authorization": "Bearer test-token"}
    response = client.post(
        "/v1/memory/proposals/missing-id/approve",
        headers=headers,
        json={"title": "Test"}
    )
    assert response.status_code == 400 # Store raises ValueError for missing proposal
