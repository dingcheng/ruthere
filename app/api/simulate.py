"""Simulation API: step-by-step trigger sequence testing.

FULLY ISOLATED: does not modify user.consecutive_misses, does not affect
dashboard stats. Simulation heartbeat logs use 'sim-' prefixed tokens and
trigger logs use '[SIMULATION]' prefix so they can be filtered out everywhere.
The simulation tracks its own miss count by counting sim- heartbeats with status='missed'.
"""
import uuid
import logging
from datetime import datetime, timezone
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
from app.i18n import t

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/simulate", tags=["simulation"])

SIM_PREFIX = "sim-"


class SimulationStatus(BaseModel):
    step: int
    step_name: str
    description: str
    sim_misses: int
    missed_threshold: int
    heartbeat_id: str | None = None
    heartbeat_status: str | None = None
    triggered: bool = False
    details: str | None = None


class SimulateStartRequest(BaseModel):
    test_email: EmailStr | None = None


async def _count_sim_misses(user_id: str, db: AsyncSession) -> int:
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
    lang = user.language or "en"
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
            step_name=t("simulate.api_not_started", lang),
            description=t("simulate.api_not_started_desc", lang),
            sim_misses=0,
            missed_threshold=user.missed_threshold,
        )

    return SimulationStatus(
        step=sim_misses + (1 if latest.status in ("sent", "escalated") else 0),
        step_name=f"{t('simulate.api_step1_name', lang)} ({latest.status})",
        description=f"{t('simulate.sim_misses', lang)} {sim_misses}/{user.missed_threshold}",
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
    lang = user.language or "en"
    token = f"{SIM_PREFIX}{uuid.uuid4()}"
    hb = HeartbeatLog(user_id=user.id, response_token=token, status="sent")
    db.add(hb)
    await db.flush()

    settings = get_settings()
    response_url = f"{settings.base_url}/heartbeat/respond/{token}"

    # Send ntfy first, email as fallback (same as real heartbeat flow)
    sent_ntfy = False
    if user.ntfy_topic:
        sent_ntfy = await send_ntfy_push(
            topic=user.ntfy_topic,
            title=t("ntfy.sim_title", lang),
            message=t("ntfy.sim_msg", lang),
            click_url=response_url,
            lang=lang,
        )

    if not sent_ntfy:
        await send_heartbeat_email(user.email, response_url, lang=lang)

    channel = "ntfy + email" if sent_ntfy else "email"
    # Always also send email in simulation so user can see it
    if sent_ntfy:
        await send_heartbeat_email(user.email, response_url, lang=lang)

    sim_misses = await _count_sim_misses(user.id, db)

    return SimulationStatus(
        step=1,
        step_name=t("simulate.api_step1_name", lang),
        description=t("simulate.api_step1_desc", lang).replace("{channel}", channel),
        sim_misses=sim_misses,
        missed_threshold=user.missed_threshold,
        heartbeat_id=hb.id,
        heartbeat_status="sent",
        details=t("simulate.api_step1_details", lang).replace("{channel}", channel),
    )


@router.post("/step2-escalate", response_model=SimulationStatus)
async def sim_step2_escalate(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Step 2: Simulate response window expiring — escalate to email."""
    lang = user.language or "en"
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
        raise HTTPException(status_code=400, detail=t("simulate.api_no_heartbeat", lang))

    hb.status = "escalated"
    hb.escalated_at = datetime.now(timezone.utc)

    settings = get_settings()
    response_url = f"{settings.base_url}/heartbeat/respond/{hb.response_token}"
    await send_heartbeat_email(user.email, response_url, lang=lang)

    sim_misses = await _count_sim_misses(user.id, db)

    return SimulationStatus(
        step=2,
        step_name=t("simulate.api_step2_name", lang),
        description=t("simulate.api_step2_desc", lang),
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
    """Step 3: Mark heartbeat as missed. Simulation-only counter."""
    lang = user.language or "en"
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
        raise HTTPException(status_code=400, detail=t("simulate.api_no_heartbeat", lang))

    hb.status = "missed"
    await db.flush()

    sim_misses = await _count_sim_misses(user.id, db)
    at_threshold = sim_misses >= user.missed_threshold

    return SimulationStatus(
        step=3,
        step_name=t("simulate.api_step3_name", lang),
        description=(
            f"{t('simulate.sim_misses', lang)} {sim_misses}/{user.missed_threshold} "
            + (t("simulate.api_step3_threshold", lang) if at_threshold
               else t("simulate.api_step3_not_yet", lang))
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
    """Step 4: Fire the trigger (simulation)."""
    lang = user.language or "en"
    test_email = body.test_email if body else None

    result = await db.execute(
        select(Recipient).where(Recipient.user_id == user.id)
    )
    recipients = result.scalars().all()

    if not recipients:
        raise HTTPException(status_code=400, detail=t("simulate.api_no_recipients", lang))

    settings = get_settings()
    deliveries = []

    for recipient in recipients:
        secret_result = await db.execute(
            select(Secret).where(Secret.id == recipient.secret_id)
        )
        secret = secret_result.scalar_one_or_none()
        if not secret:
            continue

        target_email = test_email or user.email
        sender_name = user.display_name or user.email

        if secret.encryption_type == "e2e":
            token = str(uuid.uuid4())
            reveal = RevealToken(
                secret_id=secret.id,
                recipient_id=recipient.id,
                token=token,
            )
            db.add(reveal)
            await db.flush()

            reveal_url = f"{settings.base_url}/reveal/{token}?lang={lang}"

            subject = f"[SIMULATION] {t('email.e2e_subject', lang).replace('{sender_name}', sender_name)}"
            greeting = t("email.e2e_greeting", lang).replace("{recipient_name}", recipient.name)
            body_text = t("email.e2e_body", lang).replace("{sender_name}", sender_name)
            encrypted_body = t("email.e2e_encrypted_body", lang).replace("{sender_name}", sender_name)
            footer = t("email.e2e_footer", lang)

            body_html = f"""
            <div style="font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
                <div style="background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:12px;margin-bottom:16px;color:#92400e;font-size:14px;">
                    {t("simulate.api_sim_banner", lang)} <strong>{recipient.email}</strong>
                </div>
                <h2 style="color: #333;">{t("email.e2e_heading", lang)}</h2>
                <p style="color: #555;">{greeting}</p>
                <p style="color: #555;">{body_text}</p>
                <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:20px;margin:20px 0;">
                    <h3 style="color:#166534;margin-top:0;">{t("email.e2e_encrypted_title", lang)}</h3>
                    <p style="color:#555;font-size:15px;">{encrypted_body}</p>
                </div>
                <a href="{reveal_url}" style="display:inline-block;background:#22c55e;color:white;padding:14px 28px;text-decoration:none;border-radius:8px;font-size:18px;font-weight:600;margin:16px 0;">
                    {t("email.e2e_btn", lang)}
                </a>
                <p style="color:#999;font-size:13px;">{footer}</p>
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

            subject = f"[SIMULATION] {t('email.secret_subject', lang).replace('{sender_name}', sender_name)}"
            greeting = t("email.secret_greeting", lang).replace("{recipient_name}", recipient.name)
            body_text = t("email.secret_body", lang).replace("{sender_name}", sender_name)
            footer = t("app.automated_message", lang)

            body_html = f"""
            <div style="font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
                <div style="background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:12px;margin-bottom:16px;color:#92400e;font-size:14px;">
                    {t("simulate.api_sim_banner", lang)} <strong>{recipient.email}</strong>
                </div>
                <h2 style="color: #333;">{t("email.secret_heading", lang)}</h2>
                <p style="color: #555;">{greeting}</p>
                <p style="color: #555;">{body_text}</p>
                <div style="background:#f8f9fa;border:1px solid #e9ecef;border-radius:8px;padding:20px;margin:20px 0;">
                    <h3 style="color:#333;margin-top:0;">{secret.title}</h3>
                    <div style="color:#333;font-size:15px;white-space:pre-wrap;">{plaintext}</div>
                </div>
                <p style="color:#999;font-size:13px;">{footer}</p>
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
        "step_name": t("simulate.api_step4_name", lang),
        "description": t("simulate.api_step4_desc", lang),
        "sim_misses": sim_misses,
        "deliveries": deliveries,
        "note": t("simulate.api_step4_note", lang),
    }


@router.post("/reset", response_model=SimulationStatus)
async def sim_reset(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset the simulation."""
    lang = user.language or "en"

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
        step_name=t("simulate.api_reset_name", lang),
        description=t("simulate.api_reset_desc", lang),
        sim_misses=0,
        missed_threshold=user.missed_threshold,
    )
