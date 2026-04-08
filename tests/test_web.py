"""Tests for web UI pages: basic rendering and auth redirects."""
import pytest


class TestPublicPages:
    async def test_root_redirects_to_login(self, client):
        resp = await client.get("/", follow_redirects=False)
        assert resp.status_code == 200  # renders login page directly

    async def test_login_page_renders(self, client):
        resp = await client.get("/login")
        assert resp.status_code == 200
        assert "Sign In" in resp.text

    async def test_register_page_renders(self, client):
        resp = await client.get("/register")
        assert resp.status_code == 200
        assert "Create Account" in resp.text

    async def test_health_endpoint(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_favicon(self, client):
        resp = await client.get("/favicon.ico")
        assert resp.status_code == 200

    async def test_heartbeat_respond_bad_token_html(self, client):
        resp = await client.get("/heartbeat/respond/fake-token")
        assert resp.status_code == 404
        assert "Invalid" in resp.text


class TestAuthenticatedPages:
    async def test_dashboard_unauthenticated_redirects(self, client):
        resp = await client.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["location"]

    async def test_dashboard_authenticated(self, client, auth_user):
        _, token = auth_user
        client.cookies.set("access_token", token)
        resp = await client.get("/dashboard")
        assert resp.status_code == 200
        assert "Dashboard" in resp.text
        assert "Upcoming Heartbeats" in resp.text

    async def test_secrets_page_authenticated(self, client, auth_user):
        _, token = auth_user
        client.cookies.set("access_token", token)
        resp = await client.get("/manage/secrets")
        assert resp.status_code == 200
        assert "Vault Secrets" in resp.text
        assert "End-to-end encrypted" in resp.text  # E2E option should be visible

    async def test_recipients_page_authenticated(self, client, auth_user):
        _, token = auth_user
        client.cookies.set("access_token", token)
        resp = await client.get("/manage/recipients")
        assert resp.status_code == 200
        assert "Recipients" in resp.text

    async def test_settings_page_authenticated(self, client, auth_user):
        _, token = auth_user
        client.cookies.set("access_token", token)
        resp = await client.get("/settings")
        assert resp.status_code == 200
        assert "Settings" in resp.text
        assert "Timezone" in resp.text
        assert "Active Hours" in resp.text

    async def test_settings_page_unauthenticated_redirects(self, client):
        resp = await client.get("/settings", follow_redirects=False)
        assert resp.status_code == 302
