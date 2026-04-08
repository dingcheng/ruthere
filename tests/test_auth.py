"""Tests for authentication: register, login, logout, profile, JWT, password hashing."""
import pytest
from app.services.auth import hash_password, verify_password, create_access_token, decode_token


class TestPasswordHashing:
    def test_hash_and_verify(self):
        hashed = hash_password("mypassword")
        assert verify_password("mypassword", hashed)

    def test_wrong_password_fails(self):
        hashed = hash_password("mypassword")
        assert not verify_password("wrongpassword", hashed)

    def test_hash_is_unique(self):
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2  # different salts


class TestJWT:
    def test_create_and_decode(self):
        token = create_access_token("user-123")
        assert decode_token(token) == "user-123"

    def test_invalid_token_returns_none(self):
        assert decode_token("garbage.token.here") is None

    def test_empty_token_returns_none(self):
        assert decode_token("") is None


class TestAuthAPI:
    async def test_register_success(self, client):
        resp = await client.post("/api/auth/register", json={
            "email": "new@example.com",
            "password": "password123",
            "display_name": "New User",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "new@example.com"
        assert "access_token" in data
        assert "user_id" in data

    async def test_register_duplicate_email(self, client, auth_user):
        user, _ = auth_user
        resp = await client.post("/api/auth/register", json={
            "email": user.email,
            "password": "password123",
        })
        assert resp.status_code == 400
        assert "already registered" in resp.json()["detail"].lower()

    async def test_register_invalid_email(self, client):
        resp = await client.post("/api/auth/register", json={
            "email": "not-an-email",
            "password": "password123",
        })
        assert resp.status_code == 422

    async def test_login_success(self, client, db):
        from tests.conftest import create_test_user
        user, _ = await create_test_user(db, email="login@example.com", password="mypass123")
        await db.commit()

        resp = await client.post("/api/auth/login", json={
            "email": "login@example.com",
            "password": "mypass123",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "login@example.com"
        assert "access_token" in data
        # Cookie should be set
        assert "access_token" in resp.cookies

    async def test_login_wrong_password(self, client, auth_user):
        user, _ = auth_user
        resp = await client.post("/api/auth/login", json={
            "email": user.email,
            "password": "wrongpassword",
        })
        assert resp.status_code == 401

    async def test_login_nonexistent_user(self, client):
        resp = await client.post("/api/auth/login", json={
            "email": "nobody@example.com",
            "password": "password",
        })
        assert resp.status_code == 401

    async def test_logout(self, client):
        resp = await client.post("/api/auth/logout")
        assert resp.status_code == 200

    async def test_get_profile(self, client, auth_headers, auth_user):
        user, _ = auth_user
        resp = await client.get("/api/auth/profile", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == user.email
        assert data["display_name"] == user.display_name

    async def test_update_profile(self, client, auth_headers):
        resp = await client.put("/api/auth/profile", headers=auth_headers, json={
            "display_name": "Updated Name",
        })
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Updated Name"

    async def test_unauthenticated_access(self, client):
        resp = await client.get("/api/auth/profile")
        assert resp.status_code == 401

    async def test_invalid_token_rejected(self, client):
        resp = await client.get("/api/auth/profile", headers={
            "Authorization": "Bearer invalid.jwt.token"
        })
        assert resp.status_code == 401
