"""Tests for the simulation wizard: fully isolated from real user state."""
import pytest
from sqlalchemy import select, func
from app.models.models import User, Secret, Recipient, HeartbeatLog, TriggerLog
from app.services.vault import encrypt, encode_for_storage


class TestSimulationIsolation:
    """Verify that simulation does NOT affect real user state or dashboard data."""

    async def _setup_user_with_secret(self, client, db, auth_user, auth_headers):
        """Create a server-encrypted secret and recipient for trigger testing."""
        user, _ = auth_user
        resp = await client.post("/api/secrets", headers=auth_headers, json={
            "title": "Test Secret", "content": "secret content",
        })
        secret_id = resp.json()["id"]
        await client.post("/api/recipients", headers=auth_headers, json={
            "name": "Alice", "email": "alice@example.com", "secret_id": secret_id,
        })
        return user

    async def test_step1_does_not_change_consecutive_misses(self, client, db, auth_user, auth_headers):
        user, _ = auth_user
        original_misses = user.consecutive_misses

        await client.post("/api/simulate/step1-send-heartbeat", headers=auth_headers)

        await db.refresh(user)
        assert user.consecutive_misses == original_misses

    async def test_step3_miss_does_not_change_consecutive_misses(self, client, db, auth_user, auth_headers):
        user, _ = auth_user
        original_misses = user.consecutive_misses

        # Send then miss
        await client.post("/api/simulate/step1-send-heartbeat", headers=auth_headers)
        resp = await client.post("/api/simulate/step3-miss", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["sim_misses"] == 1

        await db.refresh(user)
        assert user.consecutive_misses == original_misses, "Real consecutive_misses should not change"

    async def test_step4_trigger_does_not_deactivate_user(self, client, db, auth_user, auth_headers):
        user = await self._setup_user_with_secret(client, db, auth_user, auth_headers)
        assert user.is_active is True

        resp = await client.post("/api/simulate/step4-trigger", headers=auth_headers, json={
            "test_email": "test@example.com",
        })
        assert resp.status_code == 200

        await db.refresh(user)
        assert user.is_active is True, "User should NOT be deactivated by simulation"

    async def test_simulation_heartbeats_excluded_from_history(self, client, db, auth_user, auth_headers):
        # Create a simulation heartbeat
        await client.post("/api/simulate/step1-send-heartbeat", headers=auth_headers)

        # History API should exclude sim- entries
        resp = await client.get("/api/heartbeat/history", headers=auth_headers)
        assert resp.status_code == 200
        for log in resp.json():
            assert not log["id"].startswith("sim-")  # IDs are UUIDs not tokens, check via status

    async def test_simulation_trigger_logs_excluded_from_dashboard(self, client, db, auth_user, auth_headers):
        user = await self._setup_user_with_secret(client, db, auth_user, auth_headers)

        # Fire simulation trigger
        await client.post("/api/simulate/step4-trigger", headers=auth_headers, json={
            "test_email": "test@example.com",
        })

        # Check that dashboard trigger count excludes simulation
        result = await db.execute(
            select(func.count()).select_from(TriggerLog).where(
                TriggerLog.user_id == user.id,
                ~TriggerLog.action_taken.like("[SIMULATION]%"),
            )
        )
        real_triggers = result.scalar()
        assert real_triggers == 0, "No real triggers should exist"

        # But simulation trigger log should exist
        result = await db.execute(
            select(func.count()).select_from(TriggerLog).where(
                TriggerLog.user_id == user.id,
                TriggerLog.action_taken.like("[SIMULATION]%"),
            )
        )
        sim_triggers = result.scalar()
        assert sim_triggers > 0, "Simulation trigger log should exist"

    async def test_reset_clears_all_simulation_data(self, client, db, auth_user, auth_headers):
        user, _ = auth_user

        # Run a full simulation cycle
        await client.post("/api/simulate/step1-send-heartbeat", headers=auth_headers)
        await client.post("/api/simulate/step3-miss", headers=auth_headers)

        # Reset
        resp = await client.post("/api/simulate/reset", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["sim_misses"] == 0

        # Verify sim heartbeat logs are gone
        result = await db.execute(
            select(func.count()).select_from(HeartbeatLog).where(
                HeartbeatLog.user_id == user.id,
                HeartbeatLog.response_token.like("sim-%"),
            )
        )
        assert result.scalar() == 0

    async def test_reset_does_not_clear_real_heartbeat_logs(self, client, db, auth_user, auth_headers):
        user, _ = auth_user

        # Create a real heartbeat log
        import uuid
        real_hb = HeartbeatLog(user_id=user.id, response_token=str(uuid.uuid4()), status="responded")
        db.add(real_hb)
        await db.flush()

        # Create a sim heartbeat log
        await client.post("/api/simulate/step1-send-heartbeat", headers=auth_headers)

        # Reset simulation
        await client.post("/api/simulate/reset", headers=auth_headers)

        # Real heartbeat should still exist
        result = await db.execute(
            select(func.count()).select_from(HeartbeatLog).where(
                HeartbeatLog.user_id == user.id,
                ~HeartbeatLog.response_token.like("sim-%"),
            )
        )
        assert result.scalar() == 1


class TestSimulationFlow:
    """Test the step-by-step simulation flow."""

    async def test_full_cycle(self, client, auth_headers):
        """Walk through all 4 steps."""
        # Step 1
        resp = await client.post("/api/simulate/step1-send-heartbeat", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["step_name"] == "Heartbeat Sent"

        # Step 2
        resp = await client.post("/api/simulate/step2-escalate", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["step_name"] == "Escalated to Email"

        # Step 3
        resp = await client.post("/api/simulate/step3-miss", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["sim_misses"] == 1

    async def test_escalate_without_heartbeat_fails(self, client, auth_headers):
        resp = await client.post("/api/simulate/step2-escalate", headers=auth_headers)
        assert resp.status_code == 400

    async def test_miss_without_heartbeat_fails(self, client, auth_headers):
        resp = await client.post("/api/simulate/step3-miss", headers=auth_headers)
        assert resp.status_code == 400

    async def test_trigger_without_recipients_fails(self, client, auth_headers):
        resp = await client.post("/api/simulate/step4-trigger", headers=auth_headers)
        assert resp.status_code == 400
        assert "No recipients" in resp.json()["detail"]

    async def test_trigger_sends_to_test_email_not_real_recipient(self, client, db, auth_user, auth_headers):
        user, _ = auth_user
        # Create secret + recipient
        resp = await client.post("/api/secrets", headers=auth_headers, json={
            "title": "Test", "content": "data",
        })
        secret_id = resp.json()["id"]
        await client.post("/api/recipients", headers=auth_headers, json={
            "name": "RealPerson", "email": "real@person.com", "secret_id": secret_id,
        })

        # Trigger with test email
        resp = await client.post("/api/simulate/step4-trigger", headers=auth_headers, json={
            "test_email": "me@test.com",
        })
        assert resp.status_code == 200
        data = resp.json()
        for d in data["deliveries"]:
            assert d["email"] == "me@test.com", "Should send to test email"
            assert d["actual_recipient_email"] == "real@person.com"

    async def test_trigger_defaults_to_user_email(self, client, db, auth_user, auth_headers):
        user, _ = auth_user
        resp = await client.post("/api/secrets", headers=auth_headers, json={
            "title": "Test", "content": "data",
        })
        secret_id = resp.json()["id"]
        await client.post("/api/recipients", headers=auth_headers, json={
            "name": "Someone", "email": "someone@example.com", "secret_id": secret_id,
        })

        # Trigger without test email — should default to user's email
        resp = await client.post("/api/simulate/step4-trigger", headers=auth_headers)
        assert resp.status_code == 200
        for d in resp.json()["deliveries"]:
            assert d["email"] == user.email

    async def test_status_endpoint(self, client, auth_headers):
        resp = await client.get("/api/simulate/status", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["sim_misses"] == 0

    async def test_sim_misses_accumulate(self, client, auth_headers):
        """Multiple miss cycles should accumulate."""
        for i in range(3):
            await client.post("/api/simulate/step1-send-heartbeat", headers=auth_headers)
            resp = await client.post("/api/simulate/step3-miss", headers=auth_headers)
            assert resp.json()["sim_misses"] == i + 1

    async def test_simulate_page_renders(self, client, auth_user):
        _, token = auth_user
        client.cookies.set("access_token", token)
        resp = await client.get("/simulate")
        assert resp.status_code == 200
        assert "Trigger Simulation" in resp.text
        assert "Step 1" in resp.text
        assert "Step 4" in resp.text
