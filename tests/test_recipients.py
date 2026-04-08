"""Tests for recipients API: CRUD, linked secret validation."""
import pytest


class TestRecipientsAPI:
    async def _create_secret(self, client, auth_headers, title="Test Secret"):
        resp = await client.post("/api/secrets", headers=auth_headers, json={
            "title": title, "content": "secret content",
        })
        return resp.json()["id"]

    async def test_create_recipient(self, client, auth_headers):
        secret_id = await self._create_secret(client, auth_headers)
        resp = await client.post("/api/recipients", headers=auth_headers, json={
            "name": "Alice", "email": "alice@example.com", "secret_id": secret_id,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Alice"
        assert data["email"] == "alice@example.com"
        assert data["secret_id"] == secret_id

    async def test_create_recipient_invalid_secret(self, client, auth_headers):
        resp = await client.post("/api/recipients", headers=auth_headers, json={
            "name": "Bob", "email": "bob@example.com", "secret_id": "nonexistent-id",
        })
        assert resp.status_code == 404

    async def test_create_recipient_other_users_secret(self, client, db, auth_headers):
        """Cannot link a recipient to another user's secret."""
        from tests.conftest import create_test_user
        user2, token2 = await create_test_user(db, email="other@example.com")
        headers2 = {"Authorization": f"Bearer {token2}"}

        # Create secret as user2
        resp = await client.post("/api/secrets", headers=headers2, json={
            "title": "Other Secret", "content": "private",
        })
        secret_id = resp.json()["id"]

        # Try to link as user1
        resp = await client.post("/api/recipients", headers=auth_headers, json={
            "name": "Eve", "email": "eve@example.com", "secret_id": secret_id,
        })
        assert resp.status_code == 404

    async def test_list_recipients(self, client, auth_headers):
        secret_id = await self._create_secret(client, auth_headers)
        await client.post("/api/recipients", headers=auth_headers, json={
            "name": "Alice", "email": "alice@example.com", "secret_id": secret_id,
        })
        await client.post("/api/recipients", headers=auth_headers, json={
            "name": "Bob", "email": "bob@example.com", "secret_id": secret_id,
        })

        resp = await client.get("/api/recipients", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_update_recipient(self, client, auth_headers):
        secret_id = await self._create_secret(client, auth_headers)
        create_resp = await client.post("/api/recipients", headers=auth_headers, json={
            "name": "Alice", "email": "alice@example.com", "secret_id": secret_id,
        })
        rec_id = create_resp.json()["id"]

        resp = await client.put(f"/api/recipients/{rec_id}", headers=auth_headers, json={
            "name": "Alice Updated",
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "Alice Updated"

    async def test_delete_recipient(self, client, auth_headers):
        secret_id = await self._create_secret(client, auth_headers)
        create_resp = await client.post("/api/recipients", headers=auth_headers, json={
            "name": "Alice", "email": "alice@example.com", "secret_id": secret_id,
        })
        rec_id = create_resp.json()["id"]

        resp = await client.delete(f"/api/recipients/{rec_id}", headers=auth_headers)
        assert resp.status_code == 204

        resp = await client.get("/api/recipients", headers=auth_headers)
        assert len(resp.json()) == 0

    async def test_delete_nonexistent_recipient(self, client, auth_headers):
        resp = await client.delete("/api/recipients/fake-id", headers=auth_headers)
        assert resp.status_code == 404
