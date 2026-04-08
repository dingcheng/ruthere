"""Tests for secrets API: CRUD, server-encrypted and E2E encrypted, delete protection."""
import pytest
import base64
import os


class TestServerEncryptedSecrets:
    async def test_create_secret(self, client, auth_headers):
        resp = await client.post("/api/secrets", headers=auth_headers, json={
            "title": "My Password",
            "content": "hunter2",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "My Password"
        assert data["encryption_type"] == "server"
        assert "id" in data

    async def test_list_secrets(self, client, auth_headers):
        # Create two secrets
        await client.post("/api/secrets", headers=auth_headers, json={
            "title": "Secret 1", "content": "content1",
        })
        await client.post("/api/secrets", headers=auth_headers, json={
            "title": "Secret 2", "content": "content2",
        })

        resp = await client.get("/api/secrets", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert all(s["encryption_type"] == "server" for s in data)

    async def test_get_secret_decrypted(self, client, auth_headers):
        create_resp = await client.post("/api/secrets", headers=auth_headers, json={
            "title": "My Secret", "content": "the-actual-secret",
        })
        secret_id = create_resp.json()["id"]

        resp = await client.get(f"/api/secrets/{secret_id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == "the-actual-secret"
        assert data["encryption_type"] == "server"

    async def test_update_secret_title(self, client, auth_headers):
        create_resp = await client.post("/api/secrets", headers=auth_headers, json={
            "title": "Old Title", "content": "content",
        })
        secret_id = create_resp.json()["id"]

        resp = await client.put(f"/api/secrets/{secret_id}", headers=auth_headers, json={
            "title": "New Title",
        })
        assert resp.status_code == 200
        assert resp.json()["title"] == "New Title"

    async def test_update_secret_content(self, client, auth_headers):
        create_resp = await client.post("/api/secrets", headers=auth_headers, json={
            "title": "Title", "content": "old content",
        })
        secret_id = create_resp.json()["id"]

        await client.put(f"/api/secrets/{secret_id}", headers=auth_headers, json={
            "content": "new content",
        })

        resp = await client.get(f"/api/secrets/{secret_id}", headers=auth_headers)
        assert resp.json()["content"] == "new content"

    async def test_delete_secret(self, client, auth_headers):
        create_resp = await client.post("/api/secrets", headers=auth_headers, json={
            "title": "To Delete", "content": "bye",
        })
        secret_id = create_resp.json()["id"]

        resp = await client.delete(f"/api/secrets/{secret_id}", headers=auth_headers)
        assert resp.status_code == 204

        resp = await client.get(f"/api/secrets/{secret_id}", headers=auth_headers)
        assert resp.status_code == 404

    async def test_delete_secret_with_recipients_blocked(self, client, auth_headers):
        # Create secret
        create_resp = await client.post("/api/secrets", headers=auth_headers, json={
            "title": "Protected", "content": "data",
        })
        secret_id = create_resp.json()["id"]

        # Link a recipient
        await client.post("/api/recipients", headers=auth_headers, json={
            "name": "Bob", "email": "bob@example.com", "secret_id": secret_id,
        })

        # Try to delete — should fail with 409
        resp = await client.delete(f"/api/secrets/{secret_id}", headers=auth_headers)
        assert resp.status_code == 409
        assert "assigned to" in resp.json()["detail"].lower()

    async def test_get_nonexistent_secret(self, client, auth_headers):
        resp = await client.get("/api/secrets/nonexistent-id", headers=auth_headers)
        assert resp.status_code == 404

    async def test_cannot_access_other_users_secret(self, client, db, auth_headers):
        """A user should not be able to access another user's secrets."""
        from tests.conftest import create_test_user

        # Create a second user's secret
        user2, token2 = await create_test_user(db, email="other@example.com")
        headers2 = {"Authorization": f"Bearer {token2}"}
        create_resp = await client.post("/api/secrets", headers=headers2, json={
            "title": "Other's Secret", "content": "private",
        })
        secret_id = create_resp.json()["id"]

        # Try to access with first user's auth
        resp = await client.get(f"/api/secrets/{secret_id}", headers=auth_headers)
        assert resp.status_code == 404


class TestE2EEncryptedSecrets:
    """Tests for end-to-end encrypted secrets (server stores ciphertext only)."""

    def _make_fake_e2e_payload(self):
        """Generate fake E2E encrypted data (simulating what the browser would produce)."""
        return {
            "encrypted_content": base64.b64encode(os.urandom(32)).decode(),
            "encryption_nonce": base64.b64encode(os.urandom(12)).decode(),
            "encryption_tag": base64.b64encode(os.urandom(16)).decode(),
            "encryption_salt": base64.b64encode(os.urandom(16)).decode(),
        }

    async def test_create_e2e_secret(self, client, auth_headers):
        payload = self._make_fake_e2e_payload()
        resp = await client.post("/api/secrets", headers=auth_headers, json={
            "title": "E2E Secret",
            "encryption_type": "e2e",
            **payload,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["encryption_type"] == "e2e"

    async def test_get_e2e_secret_returns_ciphertext(self, client, auth_headers):
        payload = self._make_fake_e2e_payload()
        create_resp = await client.post("/api/secrets", headers=auth_headers, json={
            "title": "E2E Secret",
            "encryption_type": "e2e",
            **payload,
        })
        secret_id = create_resp.json()["id"]

        resp = await client.get(f"/api/secrets/{secret_id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["encryption_type"] == "e2e"
        assert data["encrypted_content"] == payload["encrypted_content"]
        assert data["encryption_nonce"] == payload["encryption_nonce"]
        assert data["encryption_tag"] == payload["encryption_tag"]
        assert data["encryption_salt"] == payload["encryption_salt"]
        assert data["content"] is None  # server cannot decrypt

    async def test_create_e2e_missing_fields_rejected(self, client, auth_headers):
        resp = await client.post("/api/secrets", headers=auth_headers, json={
            "title": "Bad E2E",
            "encryption_type": "e2e",
            # Missing required E2E fields
        })
        assert resp.status_code == 400

    async def test_create_server_secret_missing_content_rejected(self, client, auth_headers):
        resp = await client.post("/api/secrets", headers=auth_headers, json={
            "title": "Bad Server",
            "encryption_type": "server",
            # Missing content
        })
        assert resp.status_code == 400

    async def test_e2e_and_server_secrets_coexist(self, client, auth_headers):
        # Create one of each
        await client.post("/api/secrets", headers=auth_headers, json={
            "title": "Server Secret", "content": "plain",
        })
        payload = self._make_fake_e2e_payload()
        await client.post("/api/secrets", headers=auth_headers, json={
            "title": "E2E Secret", "encryption_type": "e2e", **payload,
        })

        resp = await client.get("/api/secrets", headers=auth_headers)
        data = resp.json()
        assert len(data) == 2
        types = {s["encryption_type"] for s in data}
        assert types == {"server", "e2e"}
