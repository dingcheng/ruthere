"""Tests for heartbeat: respond endpoint, settings, status, history."""
import uuid
import pytest
from datetime import datetime, timezone, timedelta
from app.models.models import HeartbeatLog


class TestHeartbeatRespond:
    """Tests for the public one-click heartbeat response endpoint."""

    async def test_respond_valid_token(self, client, db, auth_user):
        user, _ = auth_user
        token = str(uuid.uuid4())
        hb = HeartbeatLog(
            user_id=user.id,
            response_token=token,
            status="sent",
        )
        db.add(hb)
        await db.flush()

        resp = await client.get(f"/heartbeat/respond/{token}")
        assert resp.status_code == 200
        assert "Confirmed" in resp.text

    async def test_respond_resets_miss_counter(self, client, db, auth_user):
        user, _ = auth_user
        user.consecutive_misses = 2
        await db.flush()

        token = str(uuid.uuid4())
        hb = HeartbeatLog(user_id=user.id, response_token=token, status="sent")
        db.add(hb)
        await db.flush()

        await client.get(f"/heartbeat/respond/{token}")
        await db.refresh(user)
        assert user.consecutive_misses == 0

    async def test_respond_invalid_token(self, client):
        resp = await client.get("/heartbeat/respond/nonexistent-token")
        assert resp.status_code == 404
        assert "Invalid" in resp.text

    async def test_respond_already_responded(self, client, db, auth_user):
        user, _ = auth_user
        token = str(uuid.uuid4())
        hb = HeartbeatLog(
            user_id=user.id,
            response_token=token,
            status="responded",
            responded_at=datetime.now(timezone.utc),
        )
        db.add(hb)
        await db.flush()

        resp = await client.get(f"/heartbeat/respond/{token}")
        assert resp.status_code == 200
        assert "Already" in resp.text

    async def test_respond_missed_token(self, client, db, auth_user):
        user, _ = auth_user
        token = str(uuid.uuid4())
        hb = HeartbeatLog(user_id=user.id, response_token=token, status="missed")
        db.add(hb)
        await db.flush()

        resp = await client.get(f"/heartbeat/respond/{token}")
        assert resp.status_code == 200
        assert "Expired" in resp.text

    async def test_respond_via_post(self, client, db, auth_user):
        """ntfy sends POST requests for the 'I'm here' action button."""
        user, _ = auth_user
        token = str(uuid.uuid4())
        hb = HeartbeatLog(user_id=user.id, response_token=token, status="sent")
        db.add(hb)
        await db.flush()

        resp = await client.post(f"/heartbeat/respond/{token}")
        assert resp.status_code == 200
        assert "Confirmed" in resp.text


class TestHeartbeatStatus:
    async def test_get_status(self, client, auth_headers, auth_user):
        user, _ = auth_user
        resp = await client.get("/api/heartbeat/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_active"] is True
        assert data["heartbeat_interval_hours"] == 4
        assert data["missed_threshold"] == 3
        assert data["consecutive_misses"] == 0
        assert data["timezone"] == "America/Los_Angeles"
        assert data["active_hours_start"] == 8
        assert data["active_hours_end"] == 22

    async def test_get_status_unauthenticated(self, client):
        resp = await client.get("/api/heartbeat/status")
        assert resp.status_code == 401


class TestHeartbeatSettings:
    async def test_update_interval(self, client, auth_headers):
        resp = await client.put("/api/heartbeat/settings", headers=auth_headers, json={
            "heartbeat_interval_hours": 8,
        })
        assert resp.status_code == 200
        assert resp.json()["heartbeat_interval_hours"] == 8

    async def test_update_timezone(self, client, auth_headers):
        resp = await client.put("/api/heartbeat/settings", headers=auth_headers, json={
            "timezone": "Europe/London",
        })
        assert resp.status_code == 200
        assert resp.json()["timezone"] == "Europe/London"

    async def test_update_active_hours(self, client, auth_headers):
        resp = await client.put("/api/heartbeat/settings", headers=auth_headers, json={
            "active_hours_start": 9,
            "active_hours_end": 21,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_hours_start"] == 9
        assert data["active_hours_end"] == 21

    async def test_invalid_timezone_rejected(self, client, auth_headers):
        resp = await client.put("/api/heartbeat/settings", headers=auth_headers, json={
            "timezone": "Not/A/Timezone",
        })
        assert resp.status_code == 400

    async def test_invalid_interval_rejected(self, client, auth_headers):
        resp = await client.put("/api/heartbeat/settings", headers=auth_headers, json={
            "heartbeat_interval_hours": 0,
        })
        assert resp.status_code == 400

    async def test_invalid_threshold_rejected(self, client, auth_headers):
        resp = await client.put("/api/heartbeat/settings", headers=auth_headers, json={
            "missed_threshold": 0,
        })
        assert resp.status_code == 400

    async def test_deactivate_clears_next_heartbeat(self, client, auth_headers):
        resp = await client.put("/api/heartbeat/settings", headers=auth_headers, json={
            "is_active": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_active"] is False
        assert data["next_heartbeat_at"] is None

    async def test_reactivate_schedules_next_heartbeat(self, client, auth_headers):
        # Deactivate first
        await client.put("/api/heartbeat/settings", headers=auth_headers, json={
            "is_active": False,
        })
        # Reactivate
        resp = await client.put("/api/heartbeat/settings", headers=auth_headers, json={
            "is_active": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_active"] is True
        assert data["next_heartbeat_at"] is not None


class TestHeartbeatHistory:
    async def test_empty_history(self, client, auth_headers):
        resp = await client.get("/api/heartbeat/history", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_history_returns_logs(self, client, db, auth_user, auth_headers):
        user, _ = auth_user
        for i in range(3):
            hb = HeartbeatLog(
                user_id=user.id,
                response_token=str(uuid.uuid4()),
                status="responded" if i < 2 else "sent",
            )
            db.add(hb)
        await db.flush()

        resp = await client.get("/api/heartbeat/history", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    async def test_history_limit(self, client, db, auth_user, auth_headers):
        user, _ = auth_user
        for _ in range(5):
            db.add(HeartbeatLog(
                user_id=user.id,
                response_token=str(uuid.uuid4()),
                status="responded",
            ))
        await db.flush()

        resp = await client.get("/api/heartbeat/history?limit=2", headers=auth_headers)
        assert len(resp.json()) == 2
