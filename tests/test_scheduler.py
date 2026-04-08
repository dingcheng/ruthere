"""Tests for the scheduler: compute_next_heartbeat, active hours, dispatcher logic."""
import pytest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from app.services.scheduler import compute_next_heartbeat, _hour_in_active_window


class MockUser:
    """Minimal mock of User for scheduler testing."""
    def __init__(self, tz="America/Los_Angeles", interval=4, start=8, end=22):
        self.timezone = tz
        self.heartbeat_interval_hours = interval
        self.active_hours_start = start
        self.active_hours_end = end


class TestHourInActiveWindow:
    def test_normal_range_inside(self):
        assert _hour_in_active_window(12, 8, 22) is True
        assert _hour_in_active_window(8, 8, 22) is True
        assert _hour_in_active_window(21, 8, 22) is True

    def test_normal_range_outside(self):
        assert _hour_in_active_window(7, 8, 22) is False
        assert _hour_in_active_window(22, 8, 22) is False
        assert _hour_in_active_window(3, 8, 22) is False

    def test_midnight_wrap_inside(self):
        """Active 22:00 - 06:00 (overnight)."""
        assert _hour_in_active_window(23, 22, 6) is True
        assert _hour_in_active_window(0, 22, 6) is True
        assert _hour_in_active_window(5, 22, 6) is True
        assert _hour_in_active_window(22, 22, 6) is True

    def test_midnight_wrap_outside(self):
        assert _hour_in_active_window(6, 22, 6) is False
        assert _hour_in_active_window(12, 22, 6) is False
        assert _hour_in_active_window(21, 22, 6) is False

    def test_same_start_end(self):
        """start == end means 0-hour window — always false."""
        assert _hour_in_active_window(12, 12, 12) is False

    def test_full_day(self):
        """0 to 0 wraps: should be active all day."""
        assert _hour_in_active_window(0, 0, 0) is False  # 0 == 0, same as empty
        # 0 to 24 isn't possible, but 0 to 23 covers almost all day
        for h in range(24):
            assert _hour_in_active_window(h, 0, 23) is True or h == 23


class TestComputeNextHeartbeat:
    def test_within_active_hours(self):
        """If candidate lands inside active window, keep it."""
        user = MockUser(interval=4, start=8, end=22)
        la = ZoneInfo("America/Los_Angeles")
        # 11:00 local + 4h = 15:00 local — inside window
        now = datetime(2026, 4, 7, 18, 0, 0, tzinfo=timezone.utc)  # 11:00 LA
        nxt = compute_next_heartbeat(user, after=now)
        nxt_local = nxt.astimezone(la)
        assert 8 <= nxt_local.hour < 22

    def test_outside_active_hours_pushed_forward(self):
        """If candidate lands outside active window, push to next window start."""
        user = MockUser(interval=4, start=8, end=22)
        la = ZoneInfo("America/Los_Angeles")
        # 19:00 local + 4h = 23:00 local — outside window
        now = datetime(2026, 4, 8, 2, 0, 0, tzinfo=timezone.utc)  # 19:00 LA
        nxt = compute_next_heartbeat(user, after=now)
        nxt_local = nxt.astimezone(la)
        assert nxt_local.hour == 8  # pushed to next day 8:00 AM

    def test_late_night_pushed_to_morning(self):
        """Late night heartbeat should push to next morning."""
        user = MockUser(interval=4, start=8, end=22)
        la = ZoneInfo("America/Los_Angeles")
        # 3:00 AM local + 4h = 7:00 AM — still before window
        now = datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc)  # 3:00 LA
        nxt = compute_next_heartbeat(user, after=now)
        nxt_local = nxt.astimezone(la)
        assert nxt_local.hour == 8

    def test_different_timezone(self):
        """Timezone should be respected in scheduling."""
        user = MockUser(tz="Asia/Tokyo", interval=4, start=9, end=21)
        tokyo = ZoneInfo("Asia/Tokyo")
        now = datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc)  # 19:00 Tokyo
        nxt = compute_next_heartbeat(user, after=now)
        nxt_local = nxt.astimezone(tokyo)
        assert 9 <= nxt_local.hour < 21

    def test_short_interval_stays_in_window(self):
        """1-hour interval during active hours should stay in window."""
        user = MockUser(interval=1, start=8, end=22)
        la = ZoneInfo("America/Los_Angeles")
        # 12:00 local + 1h = 13:00 — inside
        now = datetime(2026, 4, 7, 19, 0, 0, tzinfo=timezone.utc)  # 12:00 LA
        nxt = compute_next_heartbeat(user, after=now)
        nxt_local = nxt.astimezone(la)
        assert 8 <= nxt_local.hour < 22

    def test_24h_interval(self):
        """24h interval from mid-day should land at same time next day."""
        user = MockUser(interval=24, start=8, end=22)
        la = ZoneInfo("America/Los_Angeles")
        now = datetime(2026, 4, 7, 22, 0, 0, tzinfo=timezone.utc)  # 15:00 LA
        nxt = compute_next_heartbeat(user, after=now)
        nxt_local = nxt.astimezone(la)
        # 15:00 + 24h = 15:00 next day — inside window
        assert nxt_local.day == 8
        assert 8 <= nxt_local.hour < 22

    def test_midnight_wrap_active_hours(self):
        """Overnight active window (e.g., night shift worker: 22:00 - 06:00)."""
        user = MockUser(interval=4, start=22, end=6)
        la = ZoneInfo("America/Los_Angeles")
        # 23:00 local + 4h = 03:00 — inside overnight window
        now = datetime(2026, 4, 8, 6, 0, 0, tzinfo=timezone.utc)  # 23:00 LA
        nxt = compute_next_heartbeat(user, after=now)
        nxt_local = nxt.astimezone(la)
        assert nxt_local.hour >= 22 or nxt_local.hour < 6

    def test_chained_computation(self):
        """Computing next from previous next should always land in window."""
        user = MockUser(interval=4, start=8, end=22)
        la = ZoneInfo("America/Los_Angeles")
        now = datetime(2026, 4, 7, 15, 0, 0, tzinfo=timezone.utc)  # 8:00 LA

        times = []
        cursor = now
        for _ in range(10):
            cursor = compute_next_heartbeat(user, after=cursor)
            local = cursor.astimezone(la)
            times.append(local)
            assert 8 <= local.hour < 22, f"Time {local} outside active window"

        # All times should be ascending
        for i in range(1, len(times)):
            assert times[i] > times[i-1]
