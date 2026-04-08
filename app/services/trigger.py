"""Trigger service: executes dead man's switch actions when heartbeat threshold is exceeded.

Handles both encryption types:
- Server-encrypted: decrypts on server, emails plaintext to recipient
- E2E encrypted: generates a time-limited reveal link, emails link to recipient
  (server CANNOT decrypt E2E secrets — recipient must enter the passphrase in-browser)
"""
import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.models import User, Recipient, Secret, TriggerLog, RevealToken
from app.services.vault import decrypt, decode_from_storage
from app.services.notify import send_secret_email, send_email
from app.config import get_settings
from app.i18n import t

logger = logging.getLogger(__name__)


async def execute_trigger(user: User, db: AsyncSession) -> int:
    """Execute the dead man's switch for a user.

    For server-encrypted secrets: decrypts and emails plaintext.
    For E2E encrypted secrets: creates a reveal token and emails a link.

    Returns:
        Number of secrets successfully sent/linked.
    """
    logger.warning(f"TRIGGER ACTIVATED for user {user.email} (ID: {user.id})")

    result = await db.execute(
        select(Recipient).where(Recipient.user_id == user.id)
    )
    recipients = result.scalars().all()

    if not recipients:
        logger.warning(f"No recipients configured for user {user.email}. Nothing to trigger.")
        return 0

    sent_count = 0
    settings = get_settings()

    for recipient in recipients:
        secret_result = await db.execute(
            select(Secret).where(Secret.id == recipient.secret_id)
        )
        secret = secret_result.scalar_one_or_none()

        if not secret:
            logger.error(f"Secret {recipient.secret_id} not found for recipient {recipient.name}")
            continue

        try:
            if secret.encryption_type == "e2e":
                # E2E: cannot decrypt on server — create a reveal link
                success = await _trigger_e2e_secret(
                    user, recipient, secret, db, settings
                )
            else:
                # Server-encrypted: decrypt and email plaintext
                success = await _trigger_server_secret(
                    user, recipient, secret
                )

            if success:
                sent_count += 1
                trigger_log = TriggerLog(
                    user_id=user.id,
                    recipient_id=recipient.id,
                    action_taken=(
                        f"Emailed reveal link for E2E secret '{secret.title}' to {recipient.email}"
                        if secret.encryption_type == "e2e"
                        else f"Emailed secret '{secret.title}' to {recipient.email}"
                    ),
                )
                db.add(trigger_log)
                logger.info(f"Secret '{secret.title}' delivered to {recipient.email}")
            else:
                logger.error(f"Failed to deliver secret '{secret.title}' to {recipient.email}")

        except Exception as e:
            logger.error(f"Error processing secret '{secret.title}' for {recipient.email}: {e}")

    await db.commit()

    # Deactivate heartbeat after trigger
    user.is_active = False
    user.next_heartbeat_at = None
    await db.commit()

    logger.warning(f"Trigger complete for {user.email}: {sent_count}/{len(recipients)} secrets delivered")
    return sent_count


async def _trigger_server_secret(user: User, recipient: Recipient, secret: Secret) -> bool:
    """Decrypt a server-encrypted secret and email the plaintext."""
    lang = user.language or "en"
    plaintext = decrypt(
        decode_from_storage(secret.encrypted_content),
        decode_from_storage(secret.encryption_nonce),
        decode_from_storage(secret.encryption_tag),
    )
    return await send_secret_email(
        to=recipient.email,
        recipient_name=recipient.name,
        sender_name=user.display_name or user.email,
        secret_title=secret.title,
        secret_content=plaintext,
        lang=lang,
    )


async def _trigger_e2e_secret(
    user: User, recipient: Recipient, secret: Secret,
    db: AsyncSession, settings,
) -> bool:
    """Create a reveal token for an E2E secret and email the link to the recipient."""
    lang = user.language or "en"
    token = str(uuid.uuid4())

    reveal = RevealToken(
        secret_id=secret.id,
        recipient_id=recipient.id,
        token=token,
    )
    db.add(reveal)
    await db.flush()

    reveal_url = f"{settings.base_url}/reveal/{token}?lang={lang}"
    sender_name = user.display_name or user.email

    subject = t("email.e2e_subject", lang).replace("{sender_name}", sender_name)
    greeting = t("email.e2e_greeting", lang).replace("{recipient_name}", recipient.name)
    body_text = t("email.e2e_body", lang).replace("{sender_name}", sender_name)
    encrypted_body = t("email.e2e_encrypted_body", lang).replace("{sender_name}", sender_name)
    footer = t("email.e2e_footer", lang)

    body_html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #333;">{t("email.e2e_heading", lang)}</h2>
        <p style="color: #555; font-size: 16px;">{greeting}</p>
        <p style="color: #555; font-size: 16px;">{body_text}</p>

        <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; padding: 20px; margin: 20px 0;">
            <h3 style="color: #166534; margin-top: 0;">{t("email.e2e_encrypted_title", lang)}</h3>
            <p style="color: #555; font-size: 15px;">{encrypted_body}</p>
        </div>

        <a href="{reveal_url}"
           style="display: inline-block; background: #22c55e; color: white; padding: 14px 28px;
                  text-decoration: none; border-radius: 8px; font-size: 18px; font-weight: 600; margin: 16px 0;">
            {t("email.e2e_btn", lang)}
        </a>

        <p style="color: #999; font-size: 13px; margin-top: 24px;">{footer}</p>
    </div>
    """

    return await send_email(recipient.email, subject, body_html)
