"""Tests for the trigger service and reveal token flow."""
import uuid
import base64
import os
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from app.models.models import (
    User, Secret, Recipient, TriggerLog, RevealToken,
)
from app.services.vault import encrypt, encode_for_storage
from app.services.trigger import execute_trigger


class TestServerEncryptedTrigger:
    """Test trigger execution for server-encrypted secrets."""

    async def _setup_trigger(self, db, auth_user):
        user, _ = auth_user
        ct, nonce, tag = encrypt("The master password is: hunter2")
        secret = Secret(
            user_id=user.id,
            title="Master Password",
            encrypted_content=encode_for_storage(ct),
            encryption_nonce=encode_for_storage(nonce),
            encryption_tag=encode_for_storage(tag),
            encryption_type="server",
        )
        db.add(secret)
        await db.flush()

        recipient = Recipient(
            user_id=user.id,
            secret_id=secret.id,
            name="Alice",
            email="alice@example.com",
        )
        db.add(recipient)
        await db.flush()
        return user, secret, recipient

    @patch("app.services.trigger.send_secret_email", new_callable=AsyncMock, return_value=True)
    async def test_trigger_creates_log(self, mock_email, db, auth_user):
        user, secret, recipient = await self._setup_trigger(db, auth_user)
        count = await execute_trigger(user, db)
        assert count == 1

        result = await db.execute(
            select(TriggerLog).where(TriggerLog.user_id == user.id)
        )
        logs = result.scalars().all()
        assert len(logs) == 1
        assert "alice@example.com" in logs[0].action_taken
        mock_email.assert_called_once()

    @patch("app.services.trigger.send_secret_email", new_callable=AsyncMock, return_value=True)
    async def test_trigger_deactivates_user(self, mock_email, db, auth_user):
        user, _, _ = await self._setup_trigger(db, auth_user)
        assert user.is_active is True
        await execute_trigger(user, db)
        assert user.is_active is False

    async def test_trigger_no_recipients(self, db, auth_user):
        user, _ = auth_user
        count = await execute_trigger(user, db)
        assert count == 0
        assert user.is_active is True

    @patch("app.services.trigger.send_secret_email", new_callable=AsyncMock, return_value=False)
    async def test_trigger_failed_email_not_logged(self, mock_email, db, auth_user):
        user, _, _ = await self._setup_trigger(db, auth_user)
        count = await execute_trigger(user, db)
        assert count == 0

        result = await db.execute(
            select(TriggerLog).where(TriggerLog.user_id == user.id)
        )
        assert len(result.scalars().all()) == 0


class TestE2ETrigger:
    """Test trigger execution for E2E encrypted secrets (reveal token flow)."""

    async def _setup_e2e_trigger(self, db, auth_user):
        user, _ = auth_user
        secret = Secret(
            user_id=user.id,
            title="E2E Secret",
            encrypted_content=base64.b64encode(os.urandom(32)).decode(),
            encryption_nonce=base64.b64encode(os.urandom(12)).decode(),
            encryption_tag=base64.b64encode(os.urandom(16)).decode(),
            encryption_type="e2e",
            encryption_salt=base64.b64encode(os.urandom(16)).decode(),
        )
        db.add(secret)
        await db.flush()

        recipient = Recipient(
            user_id=user.id,
            secret_id=secret.id,
            name="Bob",
            email="bob@example.com",
        )
        db.add(recipient)
        await db.flush()
        return user, secret, recipient

    @patch("app.services.trigger.send_email", new_callable=AsyncMock, return_value=True)
    async def test_e2e_trigger_creates_reveal_token(self, mock_email, db, auth_user):
        user, secret, recipient = await self._setup_e2e_trigger(db, auth_user)
        await execute_trigger(user, db)

        result = await db.execute(
            select(RevealToken).where(RevealToken.secret_id == secret.id)
        )
        tokens = result.scalars().all()
        assert len(tokens) == 1
        assert tokens[0].recipient_id == recipient.id
        assert tokens[0].expires_at is None  # tokens never expire — passphrase is the security gate

    @patch("app.services.trigger.send_email", new_callable=AsyncMock, return_value=True)
    async def test_e2e_trigger_creates_log(self, mock_email, db, auth_user):
        user, _, _ = await self._setup_e2e_trigger(db, auth_user)
        count = await execute_trigger(user, db)
        assert count == 1

        result = await db.execute(
            select(TriggerLog).where(TriggerLog.user_id == user.id)
        )
        logs = result.scalars().all()
        assert len(logs) == 1
        assert "E2E" in logs[0].action_taken or "reveal" in logs[0].action_taken.lower()

    @patch("app.services.trigger.send_email", new_callable=AsyncMock, return_value=True)
    async def test_e2e_trigger_deactivates_user(self, mock_email, db, auth_user):
        user, _, _ = await self._setup_e2e_trigger(db, auth_user)
        await execute_trigger(user, db)
        assert user.is_active is False


class TestRevealEndpoints:
    """Test the reveal page and API for E2E secrets."""

    async def _create_reveal_token(self, db, auth_user):
        user, _ = auth_user
        secret = Secret(
            user_id=user.id,
            title="Reveal Test",
            encrypted_content=base64.b64encode(b"encrypted-data").decode(),
            encryption_nonce=base64.b64encode(os.urandom(12)).decode(),
            encryption_tag=base64.b64encode(os.urandom(16)).decode(),
            encryption_type="e2e",
            encryption_salt=base64.b64encode(os.urandom(16)).decode(),
        )
        db.add(secret)
        await db.flush()

        recipient = Recipient(
            user_id=user.id, secret_id=secret.id,
            name="Charlie", email="charlie@example.com",
        )
        db.add(recipient)
        await db.flush()

        token = str(uuid.uuid4())
        reveal = RevealToken(
            secret_id=secret.id,
            recipient_id=recipient.id,
            token=token,
        )
        db.add(reveal)
        await db.flush()

        return token, secret, reveal

    async def test_reveal_page_valid_token(self, client, db, auth_user):
        token, secret, _ = await self._create_reveal_token(db, auth_user)
        resp = await client.get(f"/reveal/{token}")
        assert resp.status_code == 200
        assert "End-to-end encrypted" in resp.text
        assert secret.title in resp.text

    async def test_reveal_page_invalid_token(self, client):
        resp = await client.get("/reveal/nonexistent-token")
        assert resp.status_code == 404

    async def test_reveal_api_valid_token(self, client, db, auth_user):
        token, secret, _ = await self._create_reveal_token(db, auth_user)
        resp = await client.get(f"/api/secrets/reveal/{token}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == secret.title
        assert data["encrypted_content"] is not None
        assert data["encryption_salt"] is not None

    async def test_reveal_token_has_no_expiry(self, client, db, auth_user):
        """Reveal tokens should never expire — passphrase is the security gate."""
        token, _, reveal = await self._create_reveal_token(db, auth_user)
        assert reveal.expires_at is None

        # Should always be accessible
        resp = await client.get(f"/reveal/{token}")
        assert resp.status_code == 200
        resp = await client.get(f"/api/secrets/reveal/{token}")
        assert resp.status_code == 200
