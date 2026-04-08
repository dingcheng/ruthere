"""Heartbeat scheduler: dispatch-based scheduling with persistent next-heartbeat times.

Instead of one APScheduler job per user, a single dispatcher runs every 5 minutes
and checks which users are due for a heartbeat. Each user's next_heartbeat_at is
persisted in the DB so timing survives restarts.

After sending a heartbeat, the next one is computed by adding interval_hours to now,
then pushed forward into the next active window if it lands during quiet hours.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, and_
from app.database import async_session
from app.models.models import User, HeartbeatLog
from app.services.notify import send_ntfy_push, send_heartbeat_email
from app.services.trigger import execute_trigger
from app.config import get_settings

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

DISPATCHER_INTERVAL_MINUTES = 5

# Max concurrent notification sends (prevents flooding external APIs)
NOTIFICATION_CONCURRENCY = 20
_notification_semaphore = asyncio.Semaphore(NOTIFICATION_CONCURRENCY)


def _get_user_tz(user: User) -> ZoneInfo:
    """Get a user's timezone, falling back to UTC on invalid values."""
    try:
        return ZoneInfo(user.timezone)
    except Exception:
        return ZoneInfo("UTC")


def compute_next_heartbeat(user: User, after: datetime | None = None) -> datetime:
    """Compute the next heartbeat time for a user.
    
    1. Start from `after` (or now) + interval_hours
    2. If that lands outside active hours, push forward to the next active window start
    
    All computation is done in the user's local timezone, then converted back to UTC.
    """
    tz = _get_user_tz(user)
    now_utc = after or datetime.now(timezone.utc)
    candidate_utc = now_utc + timedelta(hours=user.heartbeat_interval_hours)
    candidate_local = candidate_utc.astimezone(tz)

    start = user.active_hours_start
    end = user.active_hours_end

    if _hour_in_active_window(candidate_local.hour, start, end):
        # Already within active hours
        return candidate_utc

    # Push forward to the next active window start
    # Find the next occurrence of start hour
    next_start = candidate_local.replace(hour=start, minute=0, second=0, microsecond=0)

    if start <= end:
        # Normal range (e.g., 8-22)
        if candidate_local.hour >= end:
            # Past today's window, push to tomorrow
            next_start += timedelta(days=1)
        # else: before today's window start, next_start is today
    else:
        # Wraps midnight (e.g., 22-6)
        if end <= candidate_local.hour < start:
            # In the gap between end and start, next_start is today at start
            pass
        # else: already handled by _hour_in_active_window above

    # Convert back to UTC
    return next_start.astimezone(timezone.utc)


def _hour_in_active_window(hour: int, start: int, end: int) -> bool:
    """Check if an hour falls within the active window."""
    if start <= end:
        return start <= hour < end
    else:
        # Wraps midnight
        return hour >= start or hour < end


async def heartbeat_dispatcher():
    """Main dispatcher: runs every few minutes, sends heartbeats for users who are due.
    
    Strategy: Do all DB work first (create heartbeat logs, schedule next times),
    commit once, then fire all notifications concurrently with a semaphore.
    This keeps the DB transaction short and parallelizes the slow network I/O.
    """
    now_utc = datetime.now(timezone.utc)

    # Phase 1: DB work — figure out who needs a heartbeat, create log entries
    pending_notifications = []  # list of (ntfy_topic, email, response_url) tuples

    async with async_session() as db:
        # Find active users whose next_heartbeat_at is in the past
        result = await db.execute(
            select(User).where(
                and_(
                    User.is_active == True,  # noqa: E712
                    User.next_heartbeat_at != None,  # noqa: E711
                    User.next_heartbeat_at <= now_utc,
                )
            )
        )
        due_users = result.scalars().all()

        for user in due_users:
            try:
                notification = await _prepare_heartbeat(user, db)
                if notification:
                    pending_notifications.append(notification)
            except Exception as e:
                logger.error(f"Error preparing heartbeat for {user.email}: {e}")

        # Handle users with no next_heartbeat_at (newly registered or reset)
        result = await db.execute(
            select(User).where(
                and_(
                    User.is_active == True,  # noqa: E712
                    User.next_heartbeat_at == None,  # noqa: E711
                )
            )
        )
        unscheduled_users = result.scalars().all()

        for user in unscheduled_users:
            try:
                user.next_heartbeat_at = compute_next_heartbeat(user)
                logger.info(
                    f"Initialized next heartbeat for {user.email}: "
                    f"{user.next_heartbeat_at.isoformat()}"
                )
            except Exception as e:
                logger.error(f"Error initializing heartbeat for {user.email}: {e}")

        await db.commit()

    # Phase 2: Fire all notifications concurrently (no DB lock held)
    if pending_notifications:
        logger.info(f"Sending {len(pending_notifications)} heartbeat notification(s) concurrently")
        tasks = [_send_notification(n) for n in pending_notifications]
        await asyncio.gather(*tasks, return_exceptions=True)


async def _prepare_heartbeat(user: User, db) -> tuple | None:
    """Do all DB work for a heartbeat: check outstanding, create log entry, schedule next.
    
    Returns a notification tuple (ntfy_topic, email, response_url) or None if skipped.
    """
    # Double-check active hours
    tz = _get_user_tz(user)
    now_local = datetime.now(tz)
    if not _hour_in_active_window(now_local.hour, user.active_hours_start, user.active_hours_end):
        user.next_heartbeat_at = compute_next_heartbeat(user)
        logger.info(
            f"Heartbeat for {user.email} landed outside active hours, "
            f"rescheduled to {user.next_heartbeat_at.isoformat()}"
        )
        return None

    # Check outstanding heartbeats (escalation/trigger)
    await _check_outstanding_heartbeats(user, db)

    if not user.is_active:
        return None

    # Create heartbeat log entry
    token = str(uuid.uuid4())
    heartbeat = HeartbeatLog(
        user_id=user.id,
        response_token=token,
        status="sent",
    )
    db.add(heartbeat)
    await db.flush()

    settings = get_settings()
    response_url = f"{settings.base_url}/heartbeat/respond/{token}"

    # Schedule next heartbeat
    user.next_heartbeat_at = compute_next_heartbeat(user)
    logger.info(f"Next heartbeat for {user.email}: {user.next_heartbeat_at.isoformat()}")

    return (user.ntfy_topic, user.email, response_url)


async def _send_notification(notification: tuple):
    """Send a single heartbeat notification with semaphore-limited concurrency."""
    ntfy_topic, email, response_url = notification
    async with _notification_semaphore:
        sent = False
        if ntfy_topic:
            sent = await send_ntfy_push(
                topic=ntfy_topic,
                title="RUThere? Heartbeat Check-in",
                message="Tap to confirm you're okay.",
                click_url=response_url,
            )
            if sent:
                logger.info(f"Heartbeat sent to {email} via ntfy")

        if not sent:
            await send_heartbeat_email(email, response_url)
            logger.info(f"Heartbeat sent to {email} via email")


async def _check_outstanding_heartbeats(user: User, db):
    """Check for outstanding (unresponded) heartbeats and handle escalation/triggers."""
    window = timedelta(hours=user.response_window_hours)
    cutoff = datetime.now(timezone.utc) - window

    # Find heartbeats that were sent but never responded to and are past the response window
    result = await db.execute(
        select(HeartbeatLog).where(
            and_(
                HeartbeatLog.user_id == user.id,
                HeartbeatLog.status.in_(["sent", "escalated"]),
                HeartbeatLog.sent_at < cutoff,
            )
        )
    )
    expired_heartbeats = result.scalars().all()

    for hb in expired_heartbeats:
        # Try email escalation first if not already escalated
        if hb.status == "sent" and not hb.escalated_at:
            hb.status = "escalated"
            hb.escalated_at = datetime.now(timezone.utc)
            settings = get_settings()
            response_url = f"{settings.base_url}/heartbeat/respond/{hb.response_token}"
            await send_heartbeat_email(user.email, response_url)
            logger.info(f"Escalated heartbeat to email for {user.email}")
            await db.flush()
            continue

        # If already escalated and still no response past the window, mark as missed
        if hb.status == "escalated" and hb.escalated_at:
            escalation_cutoff = hb.escalated_at + window
            if datetime.now(timezone.utc) > escalation_cutoff:
                hb.status = "missed"
                user.consecutive_misses += 1
                logger.warning(
                    f"Heartbeat MISSED for {user.email}. "
                    f"Consecutive misses: {user.consecutive_misses}/{user.missed_threshold}"
                )
                await db.flush()

                if user.consecutive_misses >= user.missed_threshold:
                    logger.warning(f"THRESHOLD REACHED for {user.email}. Executing trigger.")
                    await execute_trigger(user, db)


async def check_escalations():
    """Check for heartbeats that need escalation or marking as missed.
    
    Optimized: queries only users that actually have outstanding heartbeats
    instead of loading all active users and checking each one.
    """
    async with async_session() as db:
        window_hours = 4  # default, we'll check per-user below
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)  # broad cutoff

        # Single query: find users who have outstanding heartbeats (sent or escalated)
        # within the last 24h. This avoids loading ALL active users.
        result = await db.execute(
            select(User).where(
                User.id.in_(
                    select(HeartbeatLog.user_id).where(
                        and_(
                            HeartbeatLog.status.in_(["sent", "escalated"]),
                            HeartbeatLog.sent_at > cutoff,
                        )
                    ).distinct()
                )
            )
        )
        users_with_outstanding = result.scalars().all()

        if users_with_outstanding:
            logger.info(f"Escalation check: {len(users_with_outstanding)} user(s) with outstanding heartbeats")

        for user in users_with_outstanding:
            try:
                await _check_outstanding_heartbeats(user, db)
            except Exception as e:
                logger.error(f"Error checking escalations for {user.email}: {e}")

        await db.commit()


async def cleanup_old_logs():
    """Remove heartbeat logs older than 90 days to prevent unbounded growth."""
    async with async_session() as db:
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)

        # Delete old responded/missed heartbeat logs
        from sqlalchemy import delete
        result = await db.execute(
            delete(HeartbeatLog).where(
                and_(
                    HeartbeatLog.sent_at < cutoff,
                    HeartbeatLog.status.in_(["responded", "missed"]),
                )
            )
        )
        deleted = result.rowcount
        await db.commit()

        if deleted:
            logger.info(f"Cleaned up {deleted} heartbeat log(s) older than 90 days")


def unschedule_user_heartbeat(user_id: str):
    """Clear a user's next heartbeat time (pausing their schedule).
    
    Note: In the current architecture, this is only needed as a fallback.
    The heartbeat settings API handles next_heartbeat_at directly.
    """
    import asyncio

    async def _clear_next():
        async with async_session() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                return
            user.next_heartbeat_at = None
            await db.commit()
            logger.info(f"Unscheduled heartbeat for {user.email}")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_clear_next())
    except RuntimeError:
        asyncio.run(_clear_next())


async def start_scheduler():
    """Start the scheduler with global jobs: dispatcher, escalation checker, and log cleanup."""
    # Heartbeat dispatcher — checks every 5 minutes which users are due
    scheduler.add_job(
        heartbeat_dispatcher,
        trigger=IntervalTrigger(minutes=DISPATCHER_INTERVAL_MINUTES),
        id="heartbeat_dispatcher",
        replace_existing=True,
        max_instances=1,
    )

    # Escalation checker — checks every 15 minutes for missed/escalated heartbeats
    scheduler.add_job(
        check_escalations,
        trigger=IntervalTrigger(minutes=15),
        id="escalation_checker",
        replace_existing=True,
        max_instances=1,
    )

    # Log cleanup — runs once daily, removes logs older than 90 days
    scheduler.add_job(
        cleanup_old_logs,
        trigger=IntervalTrigger(hours=24),
        id="log_cleanup",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.start()
    logger.info("Scheduler started (dispatcher every 5min, escalation checker every 15min)")

    # Initialize next_heartbeat_at for any active users that don't have one
    async with async_session() as db:
        result = await db.execute(
            select(User).where(
                and_(
                    User.is_active == True,  # noqa: E712
                    User.next_heartbeat_at == None,  # noqa: E711
                )
            )
        )
        users = result.scalars().all()
        for user in users:
            user.next_heartbeat_at = compute_next_heartbeat(user)
            logger.info(f"Initialized heartbeat for {user.email}: {user.next_heartbeat_at.isoformat()}")
        await db.commit()

    # Log all scheduled users
    async with async_session() as db:
        result = await db.execute(
            select(User).where(User.is_active == True)  # noqa: E712
        )
        all_active = result.scalars().all()
        logger.info(f"Loaded {len(all_active)} active user(s)")
        for u in all_active:
            if u.next_heartbeat_at:
                logger.info(f"  {u.email}: next heartbeat at {u.next_heartbeat_at.isoformat()}")


def stop_scheduler():
    """Shutdown the scheduler gracefully."""
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
