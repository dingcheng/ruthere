"""Heartbeat API routes: respond to heartbeat, get status, update settings."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.models import User, HeartbeatLog
from app.services.auth import get_current_user
from app.i18n import t
from app.services.scheduler import compute_next_heartbeat

router = APIRouter(tags=["heartbeat"])


class HeartbeatSettingsUpdate(BaseModel):
    heartbeat_interval_hours: int | None = None
    response_window_hours: int | None = None
    missed_threshold: int | None = None
    is_active: bool | None = None
    ntfy_topic: str | None = None
    imessage_id: str | None = None
    timezone: str | None = None
    active_hours_start: int | None = None
    active_hours_end: int | None = None


class HeartbeatStatus(BaseModel):
    is_active: bool
    heartbeat_interval_hours: int
    response_window_hours: int
    missed_threshold: int
    consecutive_misses: int
    last_heartbeat_at: str | None
    next_heartbeat_at: str | None
    ntfy_topic: str | None
    imessage_id: str | None
    timezone: str
    active_hours_start: int
    active_hours_end: int


class HeartbeatLogResponse(BaseModel):
    id: str
    sent_at: str
    responded_at: str | None
    escalated_at: str | None
    status: str


# --- Public endpoint: one-click heartbeat response ---

@router.api_route("/heartbeat/respond/{token}", methods=["GET", "POST"], response_class=HTMLResponse)
async def respond_to_heartbeat(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """One-click heartbeat response. User taps a link to confirm they're alive."""
    result = await db.execute(
        select(HeartbeatLog).where(HeartbeatLog.response_token == token)
    )
    heartbeat = result.scalar_one_or_none()

    # Detect language from user preference or query param
    lang = request.query_params.get("lang", "en")
    if heartbeat:
        user_result = await db.execute(select(User).where(User.id == heartbeat.user_id))
        hb_user = user_result.scalar_one_or_none()
        if hb_user:
            lang = hb_user.language or "en"

    if not heartbeat:
        return HTMLResponse(
            content=_response_page(t("heartbeat_response.invalid_title", lang), t("heartbeat_response.invalid_msg", lang), success=False),
            status_code=404,
        )

    if heartbeat.status == "responded":
        return HTMLResponse(
            content=_response_page(t("heartbeat_response.already_title", lang), t("heartbeat_response.already_msg", lang), success=True),
        )

    if heartbeat.status == "missed":
        return HTMLResponse(
            content=_response_page(t("heartbeat_response.expired_title", lang), t("heartbeat_response.expired_msg", lang), success=False),
        )

    # Mark as responded
    heartbeat.status = "responded"
    heartbeat.responded_at = datetime.now(timezone.utc)

    # Reset consecutive misses
    user_result2 = await db.execute(select(User).where(User.id == heartbeat.user_id))
    user = user_result2.scalar_one_or_none()
    if user:
        user.consecutive_misses = 0
        user.last_heartbeat_at = datetime.now(timezone.utc)

    await db.commit()

    return HTMLResponse(
        content=_response_page(t("heartbeat_response.confirmed_title", lang), t("heartbeat_response.confirmed_msg", lang), success=True),
    )


# --- Authenticated API endpoints ---

@router.get("/api/heartbeat/status", response_model=HeartbeatStatus)
async def get_heartbeat_status(user: User = Depends(get_current_user)):
    return HeartbeatStatus(
        is_active=user.is_active,
        heartbeat_interval_hours=user.heartbeat_interval_hours,
        response_window_hours=user.response_window_hours,
        missed_threshold=user.missed_threshold,
        consecutive_misses=user.consecutive_misses,
        last_heartbeat_at=user.last_heartbeat_at.isoformat() if user.last_heartbeat_at else None,
        next_heartbeat_at=user.next_heartbeat_at.isoformat() if user.next_heartbeat_at else None,
        ntfy_topic=user.ntfy_topic,
        imessage_id=user.imessage_id,
        timezone=user.timezone,
        active_hours_start=user.active_hours_start,
        active_hours_end=user.active_hours_end,
    )


@router.put("/api/heartbeat/settings", response_model=HeartbeatStatus)
async def update_heartbeat_settings(
    body: HeartbeatSettingsUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.heartbeat_interval_hours is not None:
        if body.heartbeat_interval_hours < 1:
            raise HTTPException(status_code=400, detail="Interval must be at least 1 hour")
        user.heartbeat_interval_hours = body.heartbeat_interval_hours

    if body.response_window_hours is not None:
        if body.response_window_hours < 1:
            raise HTTPException(status_code=400, detail="Response window must be at least 1 hour")
        user.response_window_hours = body.response_window_hours

    if body.missed_threshold is not None:
        if body.missed_threshold < 1:
            raise HTTPException(status_code=400, detail="Threshold must be at least 1")
        user.missed_threshold = body.missed_threshold

    if body.ntfy_topic is not None:
        user.ntfy_topic = body.ntfy_topic

    if body.imessage_id is not None:
        user.imessage_id = body.imessage_id.strip() or None

    if body.timezone is not None:
        # Validate timezone
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(body.timezone)
            user.timezone = body.timezone
        except (ZoneInfoNotFoundError, KeyError):
            raise HTTPException(status_code=400, detail=f"Invalid timezone: {body.timezone}")

    if body.active_hours_start is not None:
        if not 0 <= body.active_hours_start <= 23:
            raise HTTPException(status_code=400, detail="Active hours start must be 0-23")
        user.active_hours_start = body.active_hours_start

    if body.active_hours_end is not None:
        if not 0 <= body.active_hours_end <= 23:
            raise HTTPException(status_code=400, detail="Active hours end must be 0-23")
        user.active_hours_end = body.active_hours_end

    if body.is_active is not None:
        user.is_active = body.is_active
        if body.is_active:
            user.consecutive_misses = 0
        else:
            user.next_heartbeat_at = None

    # Recompute next_heartbeat_at if any scheduling-related field changed
    needs_reschedule = any([
        body.heartbeat_interval_hours is not None,
        body.timezone is not None,
        body.active_hours_start is not None,
        body.active_hours_end is not None,
        body.is_active is True,
    ])

    if needs_reschedule and user.is_active:
        user.next_heartbeat_at = compute_next_heartbeat(user)

    await db.flush()

    return HeartbeatStatus(
        is_active=user.is_active,
        heartbeat_interval_hours=user.heartbeat_interval_hours,
        response_window_hours=user.response_window_hours,
        missed_threshold=user.missed_threshold,
        consecutive_misses=user.consecutive_misses,
        last_heartbeat_at=user.last_heartbeat_at.isoformat() if user.last_heartbeat_at else None,
        next_heartbeat_at=user.next_heartbeat_at.isoformat() if user.next_heartbeat_at else None,
        ntfy_topic=user.ntfy_topic,
        imessage_id=user.imessage_id,
        timezone=user.timezone,
        active_hours_start=user.active_hours_start,
        active_hours_end=user.active_hours_end,
    )


@router.get("/api/heartbeat/history", response_model=list[HeartbeatLogResponse])
async def get_heartbeat_history(
    limit: int = 20,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(HeartbeatLog)
        .where(
            HeartbeatLog.user_id == user.id,
            ~HeartbeatLog.response_token.like("sim-%"),
            ~HeartbeatLog.response_token.like("test-%"),
        )
        .order_by(HeartbeatLog.sent_at.desc())
        .limit(limit)
    )
    logs = result.scalars().all()
    return [
        HeartbeatLogResponse(
            id=log.id,
            sent_at=log.sent_at.isoformat(),
            responded_at=log.responded_at.isoformat() if log.responded_at else None,
            escalated_at=log.escalated_at.isoformat() if log.escalated_at else None,
            status=log.status,
        )
        for log in logs
    ]


@router.post("/api/heartbeat/test")
async def test_heartbeat(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger a test heartbeat. Uses 'test-' prefixed token so it won't
    be escalated, marked as missed, or shown on the dashboard."""
    import uuid
    from app.models.models import HeartbeatLog
    from app.services.notify import send_ntfy_push, send_heartbeat_email
    from app.config import get_settings

    token = f"test-{uuid.uuid4()}"
    hb = HeartbeatLog(user_id=user.id, response_token=token, status="sent")
    db.add(hb)
    await db.flush()

    settings = get_settings()
    response_url = f"{settings.base_url}/heartbeat/respond/{token}"

    sent = False
    if user.ntfy_topic:
        sent = await send_ntfy_push(
            topic=user.ntfy_topic,
            title="[TEST] RUThere? Heartbeat Check-in",
            message="This is a test heartbeat. Tap to test the response flow.",
            click_url=response_url,
        )

    if not sent:
        await send_heartbeat_email(user.email, response_url)

    return {"message": "Test heartbeat sent. It won't count as a miss if unresponded."}


def _response_page(title: str, message: str, success: bool = True) -> str:
    """Generate a simple HTML response page for heartbeat confirmations."""
    color = "#22c55e" if success else "#ef4444"
    icon = "&#10003;" if success else "&#10007;"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RUThere - {title}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            display: flex; align-items: center; justify-content: center;
            min-height: 100vh; background: #f8f9fa;
        }}
        .card {{
            text-align: center; padding: 48px 32px; background: white;
            border-radius: 16px; box-shadow: 0 4px 24px rgba(0,0,0,0.08);
            max-width: 400px; width: 90%;
        }}
        .icon {{
            width: 80px; height: 80px; border-radius: 50%;
            background: {color}; color: white; font-size: 40px;
            display: flex; align-items: center; justify-content: center;
            margin: 0 auto 24px;
        }}
        h1 {{ font-size: 24px; color: #333; margin-bottom: 12px; }}
        p {{ font-size: 16px; color: #666; line-height: 1.5; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">{icon}</div>
        <h1>{title}</h1>
        <p>{message}</p>
    </div>
</body>
</html>"""
