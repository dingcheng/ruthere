"""Notification service: iMessage + ntfy.sh push + Resend email.

All user-facing strings use the i18n module for translation.
"""
import asyncio
import logging
import subprocess
import httpx
from app.config import get_settings
from app.i18n import t

logger = logging.getLogger(__name__)

# Shared HTTP client with connection pooling — reused across all notification calls.
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Get or create the shared HTTP client."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=10,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=120,
            ),
        )
    return _http_client


async def close_http_client():
    """Close the shared HTTP client. Call on app shutdown."""
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


async def send_imessage(phone_number: str, message: str) -> bool:
    """Send an iMessage via macOS Messages app using osascript."""
    escaped_message = message.replace("\\", "\\\\").replace('"', '\\"')
    escaped_recipient = phone_number.replace("\\", "\\\\").replace('"', '\\"')

    applescript = f'''
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant "{escaped_recipient}" of targetService
        send "{escaped_message}" to targetBuddy
    end tell
    '''

    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", applescript,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode == 0:
            logger.info(f"iMessage sent to {phone_number}")
            return True
        else:
            logger.error(f"iMessage failed (rc={proc.returncode}): {stderr.decode().strip()}")
            return False
    except Exception as e:
        logger.error(f"Failed to send iMessage to {phone_number}: {e}")
        return False


async def send_heartbeat_imessage(phone_number: str, response_url: str) -> bool:
    """Send a heartbeat check-in via iMessage as two messages."""
    msg_sent = await send_imessage(phone_number, "RUThere? Heartbeat check-in. Tap the link below to confirm you're OK.")
    if msg_sent:
        await send_imessage(phone_number, response_url)
    return msg_sent


async def send_ntfy_push(topic: str, title: str, message: str, click_url: str | None = None, lang: str = "en") -> bool:
    """Send a push notification via ntfy.sh using the JSON API (supports UTF-8)."""
    settings = get_settings()
    url = f"{settings.ntfy_base_url}"

    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": 4,  # high
        "tags": ["heartbeat"],
    }
    if click_url:
        payload["click"] = click_url
        action_label = t("ntfy.action_label", lang)
        payload["actions"] = [
            {"action": "http", "label": action_label, "url": click_url, "clear": True}
        ]

    try:
        client = get_http_client()
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        logger.info(f"ntfy push sent to topic '{topic}'")
        return True
    except Exception as e:
        logger.error(f"Failed to send ntfy push to topic '{topic}': {e}")
        return False


async def send_email(to: str, subject: str, body_html: str) -> bool:
    """Send an email via Resend API."""
    settings = get_settings()

    if not settings.resend_api_key or settings.resend_api_key.startswith("re_your"):
        logger.warning(f"Resend API key not configured. Would send email to {to}: {subject}")
        return False

    try:
        client = get_http_client()
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": settings.email_from,
                "to": [to],
                "subject": subject,
                "html": body_html,
            },
        )
        if not resp.is_success:
            logger.error(f"Resend API error ({resp.status_code}): {resp.text}")
        resp.raise_for_status()
        logger.info(f"Email sent to {to}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email to {to}: {e}")
        return False


async def send_heartbeat_email(to: str, response_url: str, lang: str = "en") -> bool:
    """Send heartbeat check-in email with one-click response button."""
    subject = t("email.heartbeat_subject", lang)
    body_html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #333;">{t("email.heartbeat_heading", lang)}</h2>
        <p style="color: #555; font-size: 16px;">{t("email.heartbeat_body", lang)}</p>
        <a href="{response_url}" 
           style="display: inline-block; background: #22c55e; color: white; padding: 14px 28px; 
                  text-decoration: none; border-radius: 8px; font-size: 18px; font-weight: 600; margin: 16px 0;">
            {t("email.heartbeat_btn", lang)}
        </a>
        <p style="color: #999; font-size: 13px; margin-top: 24px;">
            {t("email.heartbeat_footer", lang)}
        </p>
    </div>
    """
    return await send_email(to, subject, body_html)


async def send_secret_email(to: str, recipient_name: str, sender_name: str, secret_title: str, secret_content: str, lang: str = "en") -> bool:
    """Send a triggered secret to a recipient."""
    subject = t("email.secret_subject", lang).replace("{sender_name}", sender_name)
    greeting = t("email.secret_greeting", lang).replace("{recipient_name}", recipient_name)
    body_text = t("email.secret_body", lang).replace("{sender_name}", sender_name)
    footer = t("app.automated_message", lang)

    body_html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #333;">{t("email.secret_heading", lang)}</h2>
        <p style="color: #555; font-size: 16px;">{greeting}</p>
        <p style="color: #555; font-size: 16px;">{body_text}</p>
        <div style="background: #f8f9fa; border: 1px solid #e9ecef; border-radius: 8px; padding: 20px; margin: 20px 0;">
            <h3 style="color: #333; margin-top: 0;">{secret_title}</h3>
            <div style="color: #333; font-size: 15px; white-space: pre-wrap;">{secret_content}</div>
        </div>
        <p style="color: #999; font-size: 13px; margin-top: 24px;">{footer}</p>
    </div>
    """
    return await send_email(to, subject, body_html)


async def send_recipient_invite_email(
    to: str,
    recipient_name: str,
    sender_name: str,
    lang: str = "en",
) -> bool:
    """Send an invite/notification email to a newly added recipient."""
    subject = t("email.invite_subject", lang).replace("{sender_name}", sender_name)
    intro = t("email.invite_intro", lang).replace("{sender_name}", sender_name)
    what_body = t("email.invite_what_body", lang).replace("{sender_name}", sender_name)
    action_meaning = t("email.invite_action_meaning", lang).replace("{sender_name}", sender_name)
    questions = t("email.invite_questions", lang).replace("{sender_name}", sender_name)
    greeting = t("email.invite_greeting", lang).replace("{recipient_name}", recipient_name)
    footer = t("app.automated_message", lang)

    body_html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #333;">{t("email.invite_heading", lang)}</h2>
        <p style="color: #555; font-size: 16px;">{greeting}</p>
        <p style="color: #555; font-size: 16px;">{intro}</p>

        <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; padding: 20px; margin: 20px 0;">
            <h3 style="color: #166534; margin-top: 0;">{t("email.invite_what_title", lang)}</h3>
            <p style="color: #555; font-size: 15px; margin-bottom: 0;">{what_body}</p>
        </div>

        <div style="background: #f8f9fa; border: 1px solid #e9ecef; border-radius: 8px; padding: 20px; margin: 20px 0;">
            <h3 style="color: #333; margin-top: 0;">{t("email.invite_action_title", lang)}</h3>
            <ul style="color: #555; font-size: 15px; padding-left: 20px;">
                <li style="margin-bottom: 8px;">{t("email.invite_action_nothing", lang)}</li>
                <li style="margin-bottom: 8px;">{t("email.invite_action_spam", lang)}</li>
                <li style="margin-bottom: 8px;">{action_meaning}</li>
            </ul>
        </div>

        <p style="color: #555; font-size: 15px;">{questions}</p>
        <p style="color: #999; font-size: 13px; margin-top: 24px;">{footer}</p>
    </div>
    """
    return await send_email(to, subject, body_html)
