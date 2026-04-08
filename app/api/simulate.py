"""Simulation API: step-by-step trigger sequence testing.

FULLY ISOLATED: does not modify user.consecutive_misses, does not affect
dashboard stats. Simulation heartbeat logs use 'sim-' prefixed tokens and
trigger logs use '[SIMULATION]' prefix so they can be filtered out everywhere.
The simulation tracks its own miss count by counting sim- heartbeats with status='missed'.
"""
import uuid
import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.models import User, Secret, Recipient, HeartbeatLog, TriggerLog, RevealToken
from app.services.auth import get_current_user
from app.services.notify import send_ntfy_push, send_heartbeat_email, send_email
from app.services.vault import decrypt, decode_from_storage
from app.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/simulate", tags=["simulation"])

SIM_PREFIX = "sim-"


class SimulationStatus(BaseModel):
    step: int
    step_name: str
    description: str
    sim_misses: int  # simulation-only miss count (not the real user counter)
    missed_threshold: int
    heartbeat_id: str | None = None
    heartbeat_status: str | None = None
    triggered: bool = False
    details: str | None = None


class SimulateStartRequest(BaseModel):
    test_email: EmailStr | None = None


async def _count_sim_misses(user_id: str, db: AsyncSession) -> int:
    """Count simulation heartbeat logs with status='missed' for this user."""
    result = await db.execute(
        select(func.count()).select_from(HeartbeatLog).where(
            and_(
                HeartbeatLog.user_id == user_id,
                HeartbeatLog.response_token.like(f"{SIM_PREFIX}%"),
                HeartbeatLog.status == "missed",
            )
        )
    )
    return result.scalar() or 0


@router.get("/status", response_model=SimulationStatus)
async def get_simulation_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the current state of the simulation."""
    sim_misses = await _count_sim_misses(user.id, db)

    result = await db.execute(
        select(HeartbeatLog)
        .where(
            and_(
                HeartbeatLog.user_id == user.id,
                HeartbeatLog.response_token.like(f"{SIM_PREFIX}%"),
            )
        )
        .order_by(HeartbeatLog.sent_at.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()

    if not latest:
        return SimulationStatus(
            step=0,
            step_name="Not started",
            description="Start the simulation to walk through the full trigger sequence.",
            sim_misses=0,
            missed_threshold=user.missed_threshold,
        )

    return SimulationStatus(
        step=sim_misses + (1 if latest.status in ("sent", "escalated") else 0),
        step_name=f"Heartbeat {latest.status}",
        description=f"Last heartbeat status: {latest.status}. Simulation misses: {sim_misses}/{user.missed_threshold}.",
        sim_misses=sim_misses,
        missed_threshold=user.missed_threshold,
        heartbeat_id=latest.id,
        heartbeat_status=latest.status,
        triggered=sim_misses >= user.missed_threshold,
    )


@router.post("/step1-send-heartbeat", response_model=SimulationStatus)
async def sim_step1_send_heartbeat(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Step 1: Send a heartbeat check-in (via ntfy + email). Does NOT affect real user state."""
    token = f"{SIM_PREFIX}{uuid.uuid4()}"
    hb = HeartbeatLog(user_id=user.id, response_token=token, status="sent")
    db.add(hb)
    await db.flush()

    settings = get_settings()
    response_url = f"{settings.base_url}/heartbeat/respond/{token}"

    sent_via = "email"
    if user.ntfy_topic:
        ok = await send_ntfy_push(
            topic=user.ntfy_topic,
            title="[SIM] RUThere? Heartbeat Check-in",
            message="Simulated heartbeat. Tap to respond or proceed to next step.",
            click_url=response_url,
        )
        if ok:
            sent_via = "ntfy"

    await send_heartbeat_email(user.email, response_url)

    sim_misses = await _count_sim_misses(user.id, db)

    return SimulationStatus(
        step=1,
        step_name="Heartbeat Sent",
        description=f"Heartbeat sent via {sent_via} + email. Respond to test the response flow, or proceed to Step 2.",
        sim_misses=sim_misses,
        missed_threshold=user.missed_threshold,
        heartbeat_id=hb.id,
        heartbeat_status="sent",
        details=f"Response URL sent via {sent_via} + email",
    )


@router.post("/step2-escalate", response_model=SimulationStatus)
async def sim_step2_escalate(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Step 2: Simulate response window expiring — escalate to email. Does NOT affect real user state."""
    result = await db.execute(
        select(HeartbeatLog)
        .where(
            and_(
                HeartbeatLog.user_id == user.id,
                HeartbeatLog.response_token.like(f"{SIM_PREFIX}%"),
                HeartbeatLog.status == "sent",
            )
        )
        .order_by(HeartbeatLog.sent_at.desc())
        .limit(1)
    )
    hb = result.scalar_one_or_none()
    if not hb:
        raise HTTPException(status_code=400, detail="No pending heartbeat to escalate. Run Step 1 first.")

    hb.status = "escalated"
    hb.escalated_at = datetime.now(timezone.utc)

    settings = get_settings()
    response_url = f"{settings.base_url}/heartbeat/respond/{hb.response_token}"
    await send_heartbeat_email(user.email, response_url)

    sim_misses = await _count_sim_misses(user.id, db)

    return SimulationStatus(
        step=2,
        step_name="Escalated to Email",
        description="Escalation email sent. Respond to it, or proceed to Step 3 to mark as missed.",
        sim_misses=sim_misses,
        missed_threshold=user.missed_threshold,
        heartbeat_id=hb.id,
        heartbeat_status="escalated",
    )


@router.post("/step3-miss", response_model=SimulationStatus)
async def sim_step3_miss(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Step 3: Mark heartbeat as missed. Increments simulation miss count only, NOT user.consecutive_misses."""
    result = await db.execute(
        select(HeartbeatLog)
        .where(
            and_(
                HeartbeatLog.user_id == user.id,
                HeartbeatLog.response_token.like(f"{SIM_PREFIX}%"),
                HeartbeatLog.status.in_(["sent", "escalated"]),
            )
        )
        .order_by(HeartbeatLog.sent_at.desc())
        .limit(1)
    )
    hb = result.scalar_one_or_none()
    if not hb:
        raise HTTPException(status_code=400, detail="No pending heartbeat to mark as missed. Run Step 1 first.")

    hb.status = "missed"
    await db.flush()

    sim_misses = await _count_sim_misses(user.id, db)
    at_threshold = sim_misses >= user.missed_threshold

    return SimulationStatus(
        step=3,
        step_name="Heartbeat Missed",
        description=(
            f"Simulation misses: {sim_misses}/{user.missed_threshold}. "
            + ("THRESHOLD REACHED — proceed to Step 4 to fire the trigger!" if at_threshold
               else "Not at threshold yet. Go back to Step 1 to send another heartbeat.")
        ),
        sim_misses=sim_misses,
        missed_threshold=user.missed_threshold,
        heartbeat_id=hb.id,
        heartbeat_status="missed",
    )


@router.post("/step4-trigger")
async def sim_step4_trigger(
    body: SimulateStartRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Step 4: Fire the trigger (simulation). Sends to test email, NOT real recipients. Does NOT deactivate user."""
    test_email = body.test_email if body else None

    result = await db.execute(
        select(Recipient).where(Recipient.user_id == user.id)
    )
    recipients = result.scalars().all()

    if not recipients:
        raise HTTPException(status_code=400, detail="No recipients configured. Add recipients first.")

    settings = get_settings()
    deliveries = []

    for recipient in recipients:
        secret_result = await db.execute(
            select(Secret).where(Secret.id == recipient.secret_id)
        )
        secret = secret_result.scalar_one_or_none()
        if not secret:
            continue

        target_email = test_email or user.email  # NEVER send to real recipient in simulation

        if secret.encryption_type == "e2e":
            token = str(uuid.uuid4())
            reveal = RevealToken(
                secret_id=secret.id,
                recipient_id=recipient.id,
                token=token,
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            )
            db.add(reveal)
            await db.flush()

            reveal_url = f"{settings.base_url}/reveal/{token}"
            sender_name = user.display_name or user.email

            subject = f"[SIMULATION] RUThere - Message from {sender_name}"
            body_html = f"""
            <div style="font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
                <div style="background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:12px;margin-bottom:16px;color:#92400e;font-size:14px;">
                    This is a <strong>simulation</strong>. In a real trigger, this would go to <strong>{recipient.email}</strong>.
                </div>
                <h2 style="color: #333;">A Message For You</h2>
                <p style="color: #555;">Dear {recipient.name},</p>
                <p style="color: #555;">{sender_name} has an end-to-end encrypted message for you.</p>
                <a href="{reveal_url}" style="display:inline-block;background:#22c55e;color:white;padding:14px 28px;text-decoration:none;border-radius:8px;font-size:18px;font-weight:600;margin:16px 0;">
                    View Message
                </a>
                <p style="color:#999;font-size:13px;">This link expires in 7 days. You'll need the passphrase to decrypt.</p>
            </div>
            """
            success = await send_email(target_email, subject, body_html)
            deliveries.append({
                "recipient": recipient.name,
                "email": target_email,
                "actual_recipient_email": recipient.email,
                "secret": secret.title,
                "type": "e2e",
                "reveal_url": reveal_url,
                "sent": success,
            })
        else:
            plaintext = decrypt(
                decode_from_storage(secret.encrypted_content),
                decode_from_storage(secret.encryption_nonce),
                decode_from_storage(secret.encryption_tag),
            )

            subject = f"[SIMULATION] RUThere - Message from {user.display_name or user.email}"
            body_html = f"""
            <div style="font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
                <div style="background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:12px;margin-bottom:16px;color:#92400e;font-size:14px;">
                    This is a <strong>simulation</strong>. In a real trigger, this would go to <strong>{recipient.email}</strong>.
                </div>
                <h2 style="color: #333;">A Message For You</h2>
                <p style="color: #555;">Dear {recipient.name},</p>
                <p style="color: #555;">{user.display_name or user.email} wanted you to have this message.</p>
                <div style="background:#f8f9fa;border:1px solid #e9ecef;border-radius:8px;padding:20px;margin:20px 0;">
                    <h3 style="color:#333;margin-top:0;">{secret.title}</h3>
                    <div style="color:#333;font-size:15px;white-space:pre-wrap;">{plaintext}</div>
                </div>
            </div>
            """
            success = await send_email(target_email, subject, body_html)
            deliveries.append({
                "recipient": recipient.name,
                "email": target_email,
                "actual_recipient_email": recipient.email,
                "secret": secret.title,
                "type": "server",
                "sent": success,
            })

        # Log with [SIMULATION] prefix so dashboard can filter it out
        trigger_log = TriggerLog(
            user_id=user.id,
            recipient_id=recipient.id,
            action_taken=f"[SIMULATION] Delivered '{secret.title}' to {target_email}",
        )
        db.add(trigger_log)

    await db.flush()

    sim_misses = await _count_sim_misses(user.id, db)

    return {
        "step": 4,
        "step_name": "Trigger Fired (Simulation)",
        "description": "Secrets delivered to your test email. Real recipients were NOT contacted.",
        "sim_misses": sim_misses,
        "deliveries": deliveries,
        "note": "This was a simulation. Your real heartbeat, miss counter, and active status are unaffected.",
    }


@router.post("/reset", response_model=SimulationStatus)
async def sim_reset(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset the simulation: remove all sim- heartbeat logs and [SIMULATION] trigger logs."""
    # Delete simulation heartbeat logs
    result = await db.execute(
        select(HeartbeatLog).where(
            and_(
                HeartbeatLog.user_id == user.id,
                HeartbeatLog.response_token.like(f"{SIM_PREFIX}%"),
            )
        )
    )
    for hb in result.scalars().all():
        await db.delete(hb)

    # Delete simulation trigger logs
    result = await db.execute(
        select(TriggerLog).where(
            and_(
                TriggerLog.user_id == user.id,
                TriggerLog.action_taken.like("[SIMULATION]%"),
            )
        )
    )
    for tl in result.scalars().all():
        await db.delete(tl)

    await db.flush()

    return SimulationStatus(
        step=0,
        step_name="Reset",
        description="Simulation reset. All simulation data cleared.",
        sim_misses=0,
        missed_threshold=user.missed_threshold,
    )
