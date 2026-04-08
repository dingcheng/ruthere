"""Notification service: iMessage + ntfy.sh push + Resend email."""
import asyncio
import logging
import subprocess
import httpx
from app.config import get_settings

logger = logging.getLogger(__name__)

# Shared HTTP client with connection pooling — reused across all notification calls.
# Eliminates per-request TLS handshake overhead (~100ms saved per call).
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
    """Send an iMessage via macOS Messages app using osascript.
    
    Only works when the server is running on a Mac with Messages signed in.
    
    Args:
        phone_number: The recipient's phone number or Apple ID email.
        message: The message text to send.
    
    Returns:
        True if sent successfully, False otherwise.
    """
    # Escape double quotes and backslashes in the message
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
    """Send a heartbeat check-in via iMessage as two messages:
    1. The check-in prompt
    2. Just the link (for easy tapping)
    """
    msg_sent = await send_imessage(phone_number, "RUThere? Heartbeat check-in. Tap the link below to confirm you're OK.")
    if msg_sent:
        await send_imessage(phone_number, response_url)
    return msg_sent


async def send_ntfy_push(topic: str, title: str, message: str, click_url: str | None = None) -> bool:
    """Send a push notification via ntfy.sh.
    
    Args:
        topic: The user's unique ntfy topic.
        title: Notification title.
        message: Notification body.
        click_url: URL to open when user taps the notification.
    
    Returns:
        True if sent successfully, False otherwise.
    """
    settings = get_settings()
    url = f"{settings.ntfy_base_url}/{topic}"

    headers = {
        "Title": title,
        "Priority": "high",
        "Tags": "heartbeat",
    }
    if click_url:
        headers["Click"] = click_url
        headers["Actions"] = f"http, I'm here, {click_url}, clear=true"

    try:
        client = get_http_client()
        resp = await client.post(url, content=message, headers=headers)
        resp.raise_for_status()
        logger.info(f"ntfy push sent to topic '{topic}'")
        return True
    except Exception as e:
        logger.error(f"Failed to send ntfy push to topic '{topic}': {e}")
        return False


async def send_email(to: str, subject: str, body_html: str) -> bool:
    """Send an email via Resend API.
    
    Args:
        to: Recipient email address.
        subject: Email subject.
        body_html: HTML body content.
    
    Returns:
        True if sent successfully, False otherwise.
    """
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


async def send_heartbeat_email(to: str, response_url: str) -> bool:
    """Send heartbeat check-in email with one-click response button."""
    subject = "RUThere? - Heartbeat Check-in"
    body_html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #333;">Heartbeat Check-in</h2>
        <p style="color: #555; font-size: 16px;">This is your periodic check-in from RUThere. Please confirm you're okay by clicking the button below.</p>
        <a href="{response_url}" 
           style="display: inline-block; background: #22c55e; color: white; padding: 14px 28px; 
                  text-decoration: none; border-radius: 8px; font-size: 18px; font-weight: 600; margin: 16px 0;">
            I'm Here
        </a>
        <p style="color: #999; font-size: 13px; margin-top: 24px;">
            If you don't respond, your designated contacts may be notified according to your settings.
        </p>
    </div>
    """
    return await send_email(to, subject, body_html)


async def send_secret_email(to: str, recipient_name: str, sender_name: str, secret_title: str, secret_content: str) -> bool:
    """Send a triggered secret to a recipient."""
    subject = f"RUThere - Message from {sender_name}"
    body_html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #333;">A Message For You</h2>
        <p style="color: #555; font-size: 16px;">
            Dear {recipient_name},
        </p>
        <p style="color: #555; font-size: 16px;">
            {sender_name} set up a heartbeat check-in system and designated you as a recipient 
            of the following message. This message has been automatically delivered because 
            they did not respond to multiple check-in requests.
        </p>
        <div style="background: #f8f9fa; border: 1px solid #e9ecef; border-radius: 8px; padding: 20px; margin: 20px 0;">
            <h3 style="color: #333; margin-top: 0;">{secret_title}</h3>
            <div style="color: #333; font-size: 15px; white-space: pre-wrap;">{secret_content}</div>
        </div>
        <p style="color: #999; font-size: 13px; margin-top: 24px;">
            This is an automated message from the RUThere heartbeat system.
        </p>
    </div>
    """
    return await send_email(to, subject, body_html)


async def send_recipient_invite_email(
    to: str,
    recipient_name: str,
    sender_name: str,
) -> bool:
    """Send an invite/notification email to a newly added recipient.
    
    Explains what RUThere is, that they've been designated as a recipient,
    and what to expect if the switch triggers. Does not reveal any details
    about the secret content or title.
    """
    subject = f"{sender_name} has designated you as a trusted contact on RUThere"
    body_html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #333;">You've Been Designated as a Trusted Contact</h2>
        <p style="color: #555; font-size: 16px;">
            Dear {recipient_name},
        </p>
        <p style="color: #555; font-size: 16px;">
            <strong>{sender_name}</strong> has added you as a trusted contact on
            <strong>RUThere</strong>, a personal heartbeat check-in system.
        </p>

        <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; padding: 20px; margin: 20px 0;">
            <h3 style="color: #166534; margin-top: 0;">What does this mean?</h3>
            <p style="color: #555; font-size: 15px; margin-bottom: 0;">
                RUThere periodically sends {sender_name} a check-in prompt. If they respond,
                nothing happens. If they fail to respond to multiple consecutive check-ins,
                the system will automatically deliver an important message to you via email.
            </p>
        </div>

        <div style="background: #f8f9fa; border: 1px solid #e9ecef; border-radius: 8px; padding: 20px; margin: 20px 0;">
            <h3 style="color: #333; margin-top: 0;">What you need to do</h3>
            <ul style="color: #555; font-size: 15px; padding-left: 20px;">
                <li style="margin-bottom: 8px;"><strong>Nothing right now.</strong> There is no action required on your part.</li>
                <li style="margin-bottom: 8px;">Make sure emails from this address don't go to your spam folder.</li>
                <li style="margin-bottom: 8px;">If you ever receive a message from RUThere, it means {sender_name} has not
                    responded to several check-in attempts and wanted you to have that information.</li>
            </ul>
        </div>

        <p style="color: #555; font-size: 15px;">
            If you have questions about this, please reach out to {sender_name} directly.
        </p>

        <p style="color: #999; font-size: 13px; margin-top: 24px;">
            This is an automated message from the RUThere heartbeat system.
        </p>
    </div>
    """
    return await send_email(to, subject, body_html)
