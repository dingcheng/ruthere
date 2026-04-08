"""Web UI routes: server-rendered pages for dashboard and management."""
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.models import User, Secret, Recipient, HeartbeatLog, TriggerLog
from app.services.auth import decode_token

router = APIRouter(tags=["web"])


async def _get_web_user(request: Request, db: AsyncSession) -> User | None:
    """Try to get the current user from cookie. Returns None if not authenticated."""
    token = request.cookies.get("access_token")
    if not token:
        return None
    user_id = decode_token(token)
    if not user_id:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_web_user(request, db)
    if not user:
        return _login_page()
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_web_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return _login_page()


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_web_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return _register_page()


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_web_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    # Get counts
    secrets_result = await db.execute(
        select(func.count()).select_from(Secret).where(Secret.user_id == user.id)
    )
    secrets_count = secrets_result.scalar()

    recipients_result = await db.execute(
        select(func.count()).select_from(Recipient).where(Recipient.user_id == user.id)
    )
    recipients_count = recipients_result.scalar()

    # Recent heartbeat logs
    logs_result = await db.execute(
        select(HeartbeatLog)
        .where(HeartbeatLog.user_id == user.id)
        .order_by(HeartbeatLog.sent_at.desc())
        .limit(10)
    )
    recent_logs = logs_result.scalars().all()

    # Trigger logs
    triggers_result = await db.execute(
        select(func.count()).select_from(TriggerLog).where(TriggerLog.user_id == user.id)
    )
    triggers_count = triggers_result.scalar()

    return _dashboard_page(user, secrets_count, recipients_count, recent_logs, triggers_count)


@router.get("/manage/secrets", response_class=HTMLResponse)
async def manage_secrets(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_web_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    result = await db.execute(
        select(Secret).where(Secret.user_id == user.id).order_by(Secret.created_at.desc())
    )
    secrets = result.scalars().all()
    return _secrets_page(user, secrets)


@router.get("/manage/recipients", response_class=HTMLResponse)
async def manage_recipients(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_web_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    result = await db.execute(
        select(Recipient).where(Recipient.user_id == user.id).order_by(Recipient.created_at.desc())
    )
    recipients = result.scalars().all()

    # Also get secrets for the dropdown
    secrets_result = await db.execute(
        select(Secret).where(Secret.user_id == user.id).order_by(Secret.title)
    )
    secrets = secrets_result.scalars().all()

    return _recipients_page(user, recipients, secrets)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_web_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return _settings_page(user)


@router.get("/reveal/{token}", response_class=HTMLResponse)
async def reveal_page(token: str, db: AsyncSession = Depends(get_db)):
    """Render the E2E secret reveal page. Decryption happens client-side."""
    from datetime import datetime, timezone
    from app.models.models import RevealToken

    result = await db.execute(
        select(RevealToken).where(RevealToken.token == token)
    )
    reveal = result.scalar_one_or_none()

    if not reveal:
        return HTMLResponse(content=_reveal_error_page("Invalid Link", "This reveal link is not valid."), status_code=404)

    now = datetime.now(timezone.utc)
    expires = reveal.expires_at.replace(tzinfo=timezone.utc) if reveal.expires_at.tzinfo is None else reveal.expires_at
    if expires < now:
        return HTMLResponse(content=_reveal_error_page("Expired", "This reveal link has expired."), status_code=410)

    # Load secret and sender info
    secret_result = await db.execute(select(Secret).where(Secret.id == reveal.secret_id))
    secret = secret_result.scalar_one_or_none()
    if not secret:
        return HTMLResponse(content=_reveal_error_page("Not Found", "The secret could not be found."), status_code=404)

    from app.models.models import User as UserModel
    user_result = await db.execute(select(UserModel).where(UserModel.id == secret.user_id))
    sender = user_result.scalar_one_or_none()
    sender_name = sender.display_name or sender.email if sender else "Unknown"

    # Mark as accessed
    if not reveal.accessed_at:
        reveal.accessed_at = now

    return HTMLResponse(content=_reveal_page(
        sender_name=sender_name,
        secret_title=secret.title,
        encrypted_content=secret.encrypted_content,
        encryption_nonce=secret.encryption_nonce,
        encryption_tag=secret.encryption_tag,
        encryption_salt=secret.encryption_salt,
    ))


# ==================== HTML Templates ====================

def _base_html(title: str, content: str, user: User | None = None) -> str:
    nav = ""
    if user:
        nav = f"""
        <nav>
            <div class="nav-left">
                <a href="/dashboard" class="logo">RUThere</a>
            </div>
            <button class="nav-toggle" onclick="document.querySelector('.nav-links').classList.toggle('open')" aria-label="Menu">
                <span></span><span></span><span></span>
            </button>
            <div class="nav-links">
                <a href="/dashboard">Dashboard</a>
                <a href="/manage/secrets">Secrets</a>
                <a href="/manage/recipients">Recipients</a>
                <a href="/settings">Settings</a>
                <a href="#" onclick="logout()">Logout</a>
            </div>
        </nav>"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} - RUThere</title>
    <link rel="icon" href="/static/favicon.ico" sizes="any">
    <link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
    <link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; -webkit-text-size-adjust: 100%; }}

        /* Navigation */
        nav {{ background: #1e293b; padding: 12px 20px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid #334155; position: sticky; top: 0; z-index: 50; flex-wrap: wrap; }}
        .nav-left {{ display: flex; align-items: center; }}
        .logo {{ font-size: 20px; font-weight: 700; color: #22c55e; text-decoration: none; }}
        .nav-toggle {{ display: none; background: none; border: none; cursor: pointer; padding: 8px; }}
        .nav-toggle span {{ display: block; width: 22px; height: 2px; background: #94a3b8; margin: 5px 0; border-radius: 1px; transition: 0.2s; }}
        .nav-links {{ display: flex; gap: 20px; align-items: center; }}
        .nav-links a {{ color: #94a3b8; text-decoration: none; font-size: 14px; font-weight: 500; padding: 4px 0; }}
        .nav-links a:hover {{ color: #e2e8f0; }}

        /* Layout */
        .container {{ max-width: 960px; margin: 0 auto; padding: 24px 16px; }}
        h1 {{ font-size: 24px; margin-bottom: 6px; color: #f1f5f9; }}
        h2 {{ font-size: 18px; margin-bottom: 12px; color: #f1f5f9; }}
        .subtitle {{ color: #94a3b8; margin-bottom: 24px; font-size: 14px; }}

        /* Cards */
        .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 20px; margin-bottom: 16px; overflow-x: auto; }}

        /* Stats grid */
        .grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-bottom: 24px; }}
        .stat {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 16px; text-align: center; }}
        .stat-value {{ font-size: 24px; font-weight: 700; color: #22c55e; }}
        .stat-label {{ font-size: 12px; color: #94a3b8; margin-top: 4px; }}
        .stat.warning .stat-value {{ color: #f59e0b; }}
        .stat.danger .stat-value {{ color: #ef4444; }}

        /* Badges */
        .badge {{ display: inline-block; padding: 3px 10px; border-radius: 9999px; font-size: 12px; font-weight: 600; white-space: nowrap; }}
        .badge-green {{ background: #064e3b; color: #34d399; }}
        .badge-yellow {{ background: #451a03; color: #fbbf24; }}
        .badge-red {{ background: #450a0a; color: #f87171; }}
        .badge-gray {{ background: #1e293b; color: #94a3b8; border: 1px solid #334155; }}

        /* Tables */
        table {{ width: 100%; border-collapse: collapse; }}
        th {{ text-align: left; padding: 8px 10px; font-size: 11px; text-transform: uppercase; color: #64748b; border-bottom: 1px solid #334155; white-space: nowrap; }}
        td {{ padding: 10px; border-bottom: 1px solid #1e293b; font-size: 13px; }}

        /* Forms */
        input, select, textarea {{ width: 100%; padding: 12px 14px; background: #0f172a; border: 1px solid #334155; border-radius: 8px; color: #e2e8f0; font-size: 16px; margin-bottom: 12px; -webkit-appearance: none; }}
        input:focus, select:focus, textarea:focus {{ outline: none; border-color: #22c55e; }}
        textarea {{ min-height: 100px; resize: vertical; font-family: inherit; }}
        label {{ display: block; font-size: 13px; color: #94a3b8; margin-bottom: 4px; font-weight: 500; }}

        /* Buttons */
        .btn {{ display: inline-block; padding: 12px 20px; border-radius: 8px; font-size: 14px; font-weight: 600; border: none; cursor: pointer; text-decoration: none; text-align: center; touch-action: manipulation; -webkit-tap-highlight-color: transparent; }}
        .btn-primary {{ background: #22c55e; color: #0f172a; }}
        .btn-primary:hover {{ background: #16a34a; }}
        .btn-danger {{ background: #ef4444; color: white; }}
        .btn-danger:hover {{ background: #dc2626; }}
        .btn-secondary {{ background: #334155; color: #e2e8f0; }}
        .btn-secondary:hover {{ background: #475569; }}
        .btn-sm {{ padding: 8px 12px; font-size: 12px; }}
        .form-row {{ margin-bottom: 16px; }}

        /* Alerts */
        .alert {{ padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; display: none; }}
        .alert-success {{ background: #064e3b; color: #34d399; border: 1px solid #065f46; }}
        .alert-error {{ background: #450a0a; color: #f87171; border: 1px solid #991b1b; }}

        /* Utility */
        .flex {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
        .flex-between {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }}
        .mt-4 {{ margin-top: 16px; }}
        .mb-4 {{ margin-bottom: 16px; }}
        small {{ display: block; color: #64748b; margin-top: 2px; font-size: 12px; line-height: 1.4; }}

        /* Responsive flex row (side by side on desktop, stacked on mobile) */
        .flex-row-responsive {{ display: flex; gap: 16px; }}
        .flex-row-responsive > * {{ flex: 1; }}

        /* Auth pages */
        .auth-container {{ display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 16px; }}
        .auth-card {{ background: #1e293b; border: 1px solid #334155; border-radius: 16px; padding: 32px 24px; width: 100%; max-width: 400px; }}
        .auth-card h1 {{ text-align: center; margin-bottom: 4px; }}
        .auth-card .subtitle {{ text-align: center; }}
        .auth-card .btn {{ width: 100%; text-align: center; }}
        .auth-link {{ text-align: center; margin-top: 16px; font-size: 14px; color: #94a3b8; }}
        .auth-link a {{ color: #22c55e; text-decoration: none; }}

        /* ===== Mobile (< 640px) ===== */
        @media (max-width: 640px) {{
            .nav-toggle {{ display: block; }}
            .nav-links {{
                display: none; width: 100%; flex-direction: column; gap: 0;
                padding-top: 12px; margin-top: 12px; border-top: 1px solid #334155;
            }}
            .nav-links.open {{ display: flex; }}
            .nav-links a {{ padding: 10px 0; font-size: 15px; border-bottom: 1px solid #334155; }}
            .nav-links a:last-child {{ border-bottom: none; }}

            .container {{ padding: 16px 12px; }}
            h1 {{ font-size: 22px; }}
            .card {{ padding: 16px; border-radius: 10px; }}
            .grid {{ grid-template-columns: repeat(2, 1fr); gap: 10px; }}
            .stat {{ padding: 12px 8px; }}
            .stat-value {{ font-size: 20px; }}
            .stat-label {{ font-size: 11px; }}

            /* Make tables scroll horizontally */
            .card {{ -webkit-overflow-scrolling: touch; }}
            td, th {{ padding: 8px 6px; font-size: 12px; }}

            .flex-between {{ flex-direction: column; align-items: flex-start; gap: 8px; }}
            .flex-row-responsive {{ flex-direction: column; gap: 8px; }}
            .btn {{ width: 100%; text-align: center; }}
            .btn-sm {{ width: auto; }}
        }}

        /* ===== Tablet (641px - 768px) ===== */
        @media (min-width: 641px) and (max-width: 768px) {{
            .grid {{ grid-template-columns: repeat(2, 1fr); }}
        }}

        /* ===== Desktop (> 768px) ===== */
        @media (min-width: 769px) {{
            .grid {{ grid-template-columns: repeat(4, 1fr); }}
            .stat-value {{ font-size: 32px; }}
            .container {{ padding: 32px 24px; }}
            h1 {{ font-size: 28px; }}
            h2 {{ font-size: 20px; }}
        }}
    </style>
</head>
<body>
    {nav}
    {content}
    <script>
        async function apiCall(method, url, body) {{
            const opts = {{
                method,
                headers: {{ 'Content-Type': 'application/json' }},
                credentials: 'same-origin',
            }};
            if (body) opts.body = JSON.stringify(body);
            const res = await fetch(url, opts);
            if (!res.ok) {{
                const err = await res.json().catch(() => ({{ detail: 'Request failed' }}));
                throw new Error(err.detail || 'Request failed');
            }}
            if (res.status === 204) return null;
            return res.json();
        }}

        function showAlert(id, msg, isError) {{
            const el = document.getElementById(id);
            if (!el) return;
            el.textContent = msg;
            el.className = 'alert ' + (isError ? 'alert-error' : 'alert-success');
            el.style.display = 'block';
            setTimeout(() => el.style.display = 'none', 5000);
        }}

        async function logout() {{
            await apiCall('POST', '/api/auth/logout');
            window.location.href = '/login';
        }}
    </script>
</body>
</html>"""


def _login_page() -> str:
    content = """
    <div class="auth-container">
        <div class="auth-card">
            <h1>RUThere</h1>
            <p class="subtitle">Dead man's switch / heartbeat system</p>
            <div id="alert" class="alert"></div>
            <form onsubmit="handleLogin(event)">
                <div class="form-row">
                    <label for="email">Email</label>
                    <input type="email" id="email" required placeholder="you@example.com">
                </div>
                <div class="form-row">
                    <label for="password">Password</label>
                    <input type="password" id="password" required placeholder="Your password">
                </div>
                <button type="submit" class="btn btn-primary">Sign In</button>
            </form>
            <p class="auth-link">Don't have an account? <a href="/register">Register</a></p>
        </div>
    </div>
    <script>
        async function handleLogin(e) {
            e.preventDefault();
            try {
                const data = await apiCall('POST', '/api/auth/login', {
                    email: document.getElementById('email').value,
                    password: document.getElementById('password').value,
                });
                window.location.href = '/dashboard';
            } catch (err) {
                showAlert('alert', err.message, true);
            }
        }
    </script>"""
    return _base_html("Login", content)


def _register_page() -> str:
    content = """
    <div class="auth-container">
        <div class="auth-card">
            <h1>Create Account</h1>
            <p class="subtitle">Set up your heartbeat system</p>
            <div id="alert" class="alert"></div>
            <form onsubmit="handleRegister(event)">
                <div class="form-row">
                    <label for="display_name">Display Name</label>
                    <input type="text" id="display_name" placeholder="Your name">
                </div>
                <div class="form-row">
                    <label for="email">Email</label>
                    <input type="email" id="email" required placeholder="you@example.com">
                </div>
                <div class="form-row">
                    <label for="password">Password</label>
                    <input type="password" id="password" required minlength="8" placeholder="Min 8 characters">
                </div>
                <button type="submit" class="btn btn-primary">Create Account</button>
            </form>
            <p class="auth-link">Already have an account? <a href="/login">Sign in</a></p>
        </div>
    </div>
    <script>
        async function handleRegister(e) {
            e.preventDefault();
            try {
                const data = await apiCall('POST', '/api/auth/register', {
                    email: document.getElementById('email').value,
                    password: document.getElementById('password').value,
                    display_name: document.getElementById('display_name').value || null,
                });
                // Set cookie manually since register doesn't set it via response
                document.cookie = `access_token=${data.access_token}; path=/; max-age=${72*3600}; samesite=lax`;
                window.location.href = '/dashboard';
            } catch (err) {
                showAlert('alert', err.message, true);
            }
        }
    </script>"""
    return _base_html("Register", content)


def _dashboard_page(user: User, secrets_count: int, recipients_count: int, recent_logs: list, triggers_count: int) -> str:
    status_class = ""
    if user.consecutive_misses > 0:
        status_class = "warning" if user.consecutive_misses < user.missed_threshold else "danger"

    # Convert times to both UTC and user's local timezone
    from datetime import timezone as dt_timezone
    from zoneinfo import ZoneInfo
    from app.services.scheduler import compute_next_heartbeat
    try:
        user_tz = ZoneInfo(user.timezone)
    except Exception:
        user_tz = ZoneInfo("UTC")

    tz_abbrev = user.timezone.split("/")[-1].replace("_", " ")

    def to_utc_aware(dt):
        """Ensure a datetime is UTC-aware (naive datetimes from SQLite are assumed UTC)."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=dt_timezone.utc)
        return dt

    last_hb = "Never"
    last_hb_local = "Never"
    if user.last_heartbeat_at:
        aware = to_utc_aware(user.last_heartbeat_at)
        last_hb = aware.strftime("%Y-%m-%d %H:%M")
        last_hb_local = aware.astimezone(user_tz).strftime("%Y-%m-%d %H:%M")

    log_rows = ""
    for log in recent_logs:
        badge_cls = {
            "responded": "badge-green",
            "sent": "badge-yellow",
            "escalated": "badge-yellow",
            "missed": "badge-red",
        }.get(log.status, "badge-gray")

        sent_aware = to_utc_aware(log.sent_at)
        sent_utc = sent_aware.strftime("%Y-%m-%d %H:%M")
        sent_local = sent_aware.astimezone(user_tz).strftime("%Y-%m-%d %H:%M")

        if log.responded_at:
            resp_aware = to_utc_aware(log.responded_at)
            resp_utc = resp_aware.strftime("%H:%M")
            resp_local = resp_aware.astimezone(user_tz).strftime("%H:%M")
        else:
            resp_utc = "-"
            resp_local = "-"

        log_rows += f"""<tr>
            <td><span class="tz-time" data-utc="{sent_utc}" data-local="{sent_local}">{sent_local}</span></td>
            <td><span class="tz-time" data-utc="{resp_utc}" data-local="{resp_local}">{resp_local}</span></td>
            <td><span class="badge {badge_cls}">{log.status}</span></td>
        </tr>"""

    if not log_rows:
        log_rows = '<tr><td colspan="3" style="text-align:center;color:#64748b;">No heartbeats sent yet</td></tr>'

    # Compute next 3 upcoming heartbeat times
    upcoming_rows = ""
    if user.is_active:
        from datetime import datetime as dt_cls
        next_time = user.next_heartbeat_at
        if next_time:
            if next_time.tzinfo is None:
                next_time = next_time.replace(tzinfo=dt_timezone.utc)
            upcoming_times = [next_time]
            # Compute 2 more by chaining compute_next_heartbeat
            cursor = next_time
            for _ in range(2):
                cursor = compute_next_heartbeat(user, after=cursor)
                upcoming_times.append(cursor)

            items = ""
            for i, t in enumerate(upcoming_times):
                t_utc = t.strftime("%Y-%m-%d %H:%M")
                t_local = t.astimezone(user_tz).strftime("%Y-%m-%d %H:%M")
                label = "Next" if i == 0 else f"#{i + 1}"
                badge = 'style="background:#064e3b;color:#34d399;"' if i == 0 else 'style="background:#1e293b;color:#94a3b8;border:1px solid #334155;"'
                items += f"""
                <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;{'border-bottom:1px solid #334155;' if i < 2 else ''}">
                    <span class="badge" {badge}>{label}</span>
                    <span class="tz-time" data-utc="{t_utc}" data-local="{t_local}" style="font-size:15px;font-weight:500;">{t_local}</span>
                </div>"""
            upcoming_rows = items
        else:
            upcoming_rows = '<p style="color:#64748b;text-align:center;">Scheduling first heartbeat...</p>'
    else:
        upcoming_rows = '<p style="color:#64748b;text-align:center;">Heartbeat is paused</p>'

    content = f"""
    <div class="container">
        <h1>Dashboard</h1>
        <p class="subtitle">Welcome back, {user.display_name or user.email}</p>

        <div class="grid">
            <div class="stat {'warning' if not user.is_active else ''}">
                <div class="stat-value">{'Active' if user.is_active else 'Paused'}</div>
                <div class="stat-label">Heartbeat Status</div>
            </div>
            <div class="stat {status_class}">
                <div class="stat-value">{user.consecutive_misses} / {user.missed_threshold}</div>
                <div class="stat-label">Consecutive Misses</div>
            </div>
            <div class="stat">
                <div class="stat-value">{secrets_count}</div>
                <div class="stat-label">Secrets Stored</div>
            </div>
            <div class="stat">
                <div class="stat-value">{recipients_count}</div>
                <div class="stat-label">Recipients</div>
            </div>
        </div>

        <div class="flex-between mb-4">
            <div class="flex">
                <h2>Recent Heartbeats</h2>
                <span id="tz-toggle" onclick="toggleTimezone()" 
                      style="cursor:pointer;background:#334155;padding:3px 10px;border-radius:9999px;font-size:12px;font-weight:600;color:#22c55e;user-select:none;"
                      title="Click to switch between local and UTC">{tz_abbrev}</span>
            </div>
            <button class="btn btn-secondary btn-sm" onclick="testHeartbeat()">Send Test Heartbeat</button>
        </div>
        <div id="hb-alert" class="alert"></div>

        <div class="card">
            <table>
                <thead>
                    <tr><th>Sent</th><th>Responded</th><th>Status</th></tr>
                </thead>
                <tbody>{log_rows}</tbody>
            </table>
        </div>

        <h2 style="margin-bottom:12px;">Upcoming Heartbeats</h2>
        <div class="card">
            {upcoming_rows}
        </div>

        <div class="card">
            <div class="flex-between">
                <div>
                    <strong>Last confirmed heartbeat:</strong>
                    <span class="tz-time" data-utc="{last_hb}" data-local="{last_hb_local}">{last_hb_local}</span>
                </div>
                <div>
                    <strong>Interval:</strong> every {user.heartbeat_interval_hours}h
                    &nbsp;|&nbsp;
                    <strong>Window:</strong> {user.response_window_hours}h
                </div>
            </div>
        </div>

        <div class="card" style="margin-top: 12px;">
            <div class="flex-between">
                <div>
                    <strong>ntfy topic:</strong> <code>{user.ntfy_topic or 'Not configured'}</code>
                </div>
                <div>
                    <strong>Triggers fired:</strong> {triggers_count}
                </div>
            </div>
        </div>
    </div>
    <script>
        let showingLocal = true;
        const localLabel = "{tz_abbrev}";

        function toggleTimezone() {{
            showingLocal = !showingLocal;
            const label = showingLocal ? localLabel : "UTC";
            document.getElementById('tz-toggle').textContent = label;
            document.querySelectorAll('.tz-time').forEach(el => {{
                el.textContent = showingLocal ? el.dataset.local : el.dataset.utc;
            }});
        }}

        async function testHeartbeat() {{
            try {{
                await apiCall('POST', '/api/heartbeat/test');
                showAlert('hb-alert', 'Test heartbeat sent! Check your ntfy app or email.', false);
            }} catch (err) {{
                showAlert('hb-alert', err.message, true);
            }}
        }}
    </script>"""
    return _base_html("Dashboard", content, user)


def _secrets_page(user: User, secrets: list) -> str:
    rows = ""
    for s in secrets:
        enc_type = getattr(s, 'encryption_type', 'server') or 'server'
        badge = '<span class="badge badge-green" style="font-size:11px;" title="End-to-end encrypted — server cannot read this">E2E</span>' if enc_type == 'e2e' else '<span class="badge badge-gray" style="font-size:11px;" title="Encrypted on server">Server</span>'
        view_onclick = f"viewE2ESecret('{s.id}')" if enc_type == 'e2e' else f"viewSecret('{s.id}')"
        rows += f"""<tr id="secret-{s.id}">
            <td><strong>{s.title}</strong> {badge}</td>
            <td>{s.created_at.strftime("%Y-%m-%d")}</td>
            <td>
                <button class="btn btn-secondary btn-sm" onclick="{view_onclick}">View</button>
                <button class="btn btn-danger btn-sm" onclick="deleteSecret('{s.id}')">Delete</button>
            </td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="3" style="text-align:center;color:#64748b;">No secrets stored yet. Add your first one below.</td></tr>'

    content = f"""
    <div class="container">
        <h1>Vault Secrets</h1>
        <p class="subtitle">Encrypted messages that will be sent to your designated recipients</p>

        <div id="alert" class="alert"></div>

        <div class="card">
            <table>
                <thead><tr><th>Title</th><th>Created</th><th>Actions</th></tr></thead>
                <tbody id="secrets-table">{rows}</tbody>
            </table>
        </div>

        <div class="card">
            <h2>Add New Secret</h2>
            <form onsubmit="addSecret(event)">
                <div class="form-row">
                    <label>Encryption Type</label>
                    <div style="display:flex;gap:12px;margin-bottom:12px;">
                        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;padding:10px 16px;background:#0f172a;border:2px solid #334155;border-radius:8px;flex:1;font-size:14px;" id="enc-server-label">
                            <input type="radio" name="enc_type" value="server" checked onchange="toggleEncType()" style="width:auto;margin:0;">
                            Server encrypted
                        </label>
                        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;padding:10px 16px;background:#0f172a;border:2px solid #334155;border-radius:8px;flex:1;font-size:14px;" id="enc-e2e-label">
                            <input type="radio" name="enc_type" value="e2e" onchange="toggleEncType()" style="width:auto;margin:0;">
                            End-to-end encrypted
                        </label>
                    </div>
                </div>
                <div id="e2e-info" style="display:none;background:#064e3b;border:1px solid #065f46;border-radius:8px;padding:14px;margin-bottom:16px;font-size:13px;color:#34d399;line-height:1.5;">
                    <strong>Zero-knowledge encryption.</strong> Your secret is encrypted in your browser before it reaches the server.
                    The server never sees the plaintext or the passphrase. Only someone with the passphrase can decrypt it.
                    <br><br>
                    <strong>Verify:</strong> Open your browser's Network tab and confirm — no plaintext or passphrase is sent.
                </div>
                <div class="form-row">
                    <label for="title">Title</label>
                    <input type="text" id="title" required placeholder="e.g., Password Manager Master Key">
                </div>
                <div class="form-row">
                    <label for="content">Secret Content</label>
                    <textarea id="content" required placeholder="The secret message or information..."></textarea>
                </div>
                <div class="form-row" id="passphrase-row" style="display:none;">
                    <label for="passphrase">Passphrase</label>
                    <input type="password" id="passphrase" placeholder="A passphrase you'll share with the recipient">
                    <small style="color:#64748b;">You must share this passphrase with your recipient out-of-band (in person, phone call, etc). Without it, nobody can decrypt this secret — not even the server.</small>
                </div>
                <div class="form-row" id="passphrase-confirm-row" style="display:none;">
                    <label for="passphrase_confirm">Confirm Passphrase</label>
                    <input type="password" id="passphrase_confirm" placeholder="Type the passphrase again">
                </div>
                <button type="submit" class="btn btn-primary" id="save-btn">Encrypt & Save</button>
            </form>
        </div>

        <!-- View modal (server-encrypted) -->
        <div id="view-modal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.7); z-index:100;">
            <div class="card" style="max-width:500px; width:90%; margin:auto; position:relative; top:50%; transform:translateY(-50%);">
                <div class="flex-between mb-4">
                    <h2 id="modal-title"></h2>
                    <button class="btn btn-secondary btn-sm" onclick="closeModal()">Close</button>
                </div>
                <pre id="modal-content" style="background:#0f172a;padding:16px;border-radius:8px;white-space:pre-wrap;font-size:14px;max-height:400px;overflow-y:auto;"></pre>
            </div>
        </div>

        <!-- View modal (E2E — needs passphrase) -->
        <div id="e2e-modal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.7); z-index:100;">
            <div class="card" style="max-width:500px; width:90%; margin:auto; position:relative; top:50%; transform:translateY(-50%);">
                <div class="flex-between mb-4">
                    <h2 id="e2e-modal-title"></h2>
                    <button class="btn btn-secondary btn-sm" onclick="closeE2EModal()">Close</button>
                </div>
                <div id="e2e-modal-form">
                    <div style="background:#064e3b;border:1px solid #065f46;border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:12px;color:#34d399;display:flex;align-items:center;gap:8px;">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                        End-to-end encrypted — decrypts in your browser
                    </div>
                    <label for="e2e-view-passphrase">Passphrase</label>
                    <input type="password" id="e2e-view-passphrase" placeholder="Enter passphrase to decrypt"
                           onkeydown="if(event.key==='Enter')decryptE2EView()">
                    <div id="e2e-view-error" style="display:none;background:#450a0a;color:#f87171;border:1px solid #991b1b;padding:10px;border-radius:8px;margin-bottom:12px;font-size:13px;"></div>
                    <button class="btn btn-primary" onclick="decryptE2EView()" id="e2e-decrypt-btn">Decrypt</button>
                </div>
                <pre id="e2e-modal-content" style="display:none;background:#0f172a;padding:16px;border-radius:8px;white-space:pre-wrap;font-size:14px;max-height:400px;overflow-y:auto;"></pre>
            </div>
        </div>
    </div>
    <script src="/static/js/e2e.js"></script>
    <script>
        let currentE2EData = null;

        function toggleEncType() {{
            const isE2E = document.querySelector('input[name="enc_type"]:checked').value === 'e2e';
            document.getElementById('passphrase-row').style.display = isE2E ? 'block' : 'none';
            document.getElementById('passphrase-confirm-row').style.display = isE2E ? 'block' : 'none';
            document.getElementById('e2e-info').style.display = isE2E ? 'block' : 'none';
            document.getElementById('passphrase').required = isE2E;
            document.getElementById('passphrase_confirm').required = isE2E;

            // Visual highlight
            document.getElementById('enc-server-label').style.borderColor = isE2E ? '#334155' : '#22c55e';
            document.getElementById('enc-e2e-label').style.borderColor = isE2E ? '#22c55e' : '#334155';
        }}
        toggleEncType(); // init

        async function addSecret(e) {{
            e.preventDefault();
            const encType = document.querySelector('input[name="enc_type"]:checked').value;
            const title = document.getElementById('title').value;
            const content = document.getElementById('content').value;

            try {{
                if (encType === 'e2e') {{
                    const passphrase = document.getElementById('passphrase').value;
                    const confirm = document.getElementById('passphrase_confirm').value;
                    if (passphrase !== confirm) {{
                        showAlert('alert', 'Passphrases do not match.', true);
                        return;
                    }}
                    if (passphrase.length < 6) {{
                        showAlert('alert', 'Passphrase must be at least 6 characters.', true);
                        return;
                    }}

                    document.getElementById('save-btn').textContent = 'Encrypting...';
                    document.getElementById('save-btn').disabled = true;

                    // Encrypt client-side
                    const encrypted = await E2E.encrypt(content, passphrase);

                    await apiCall('POST', '/api/secrets', {{
                        title: title,
                        encryption_type: 'e2e',
                        encrypted_content: encrypted.encrypted_content,
                        encryption_nonce: encrypted.encryption_nonce,
                        encryption_tag: encrypted.encryption_tag,
                        encryption_salt: encrypted.encryption_salt,
                    }});
                    showAlert('alert', 'Secret end-to-end encrypted and saved! The server cannot read it.', false);
                }} else {{
                    await apiCall('POST', '/api/secrets', {{
                        title: title,
                        content: content,
                    }});
                    showAlert('alert', 'Secret encrypted and saved!', false);
                }}
                setTimeout(() => location.reload(), 1000);
            }} catch (err) {{
                showAlert('alert', err.message, true);
                document.getElementById('save-btn').textContent = 'Encrypt & Save';
                document.getElementById('save-btn').disabled = false;
            }}
        }}

        async function viewSecret(id) {{
            try {{
                const data = await apiCall('GET', `/api/secrets/${{id}}`);
                document.getElementById('modal-title').textContent = data.title;
                document.getElementById('modal-content').textContent = data.content;
                document.getElementById('view-modal').style.display = 'block';
            }} catch (err) {{
                showAlert('alert', err.message, true);
            }}
        }}

        async function viewE2ESecret(id) {{
            try {{
                const data = await apiCall('GET', `/api/secrets/${{id}}`);
                currentE2EData = data;
                document.getElementById('e2e-modal-title').textContent = data.title;
                document.getElementById('e2e-modal-form').style.display = 'block';
                document.getElementById('e2e-modal-content').style.display = 'none';
                document.getElementById('e2e-view-passphrase').value = '';
                document.getElementById('e2e-view-error').style.display = 'none';
                document.getElementById('e2e-modal').style.display = 'block';
                document.getElementById('e2e-view-passphrase').focus();
            }} catch (err) {{
                showAlert('alert', err.message, true);
            }}
        }}

        async function decryptE2EView() {{
            const passphrase = document.getElementById('e2e-view-passphrase').value;
            if (!passphrase) return;
            const btn = document.getElementById('e2e-decrypt-btn');
            btn.disabled = true;
            btn.textContent = 'Decrypting...';
            try {{
                const plaintext = await E2E.decrypt(
                    currentE2EData.encrypted_content,
                    currentE2EData.encryption_nonce,
                    currentE2EData.encryption_tag,
                    currentE2EData.encryption_salt,
                    passphrase
                );
                document.getElementById('e2e-modal-form').style.display = 'none';
                document.getElementById('e2e-modal-content').style.display = 'block';
                document.getElementById('e2e-modal-content').textContent = plaintext;
            }} catch (err) {{
                document.getElementById('e2e-view-error').textContent = 'Incorrect passphrase.';
                document.getElementById('e2e-view-error').style.display = 'block';
            }}
            btn.disabled = false;
            btn.textContent = 'Decrypt';
        }}

        function closeModal() {{
            document.getElementById('view-modal').style.display = 'none';
        }}

        function closeE2EModal() {{
            document.getElementById('e2e-modal').style.display = 'none';
            currentE2EData = null;
        }}

        async function deleteSecret(id) {{
            if (!confirm('Delete this secret? This cannot be undone.')) return;
            try {{
                await apiCall('DELETE', `/api/secrets/${{id}}`);
                document.getElementById(`secret-${{id}}`).remove();
                showAlert('alert', 'Secret deleted.', false);
            }} catch (err) {{
                showAlert('alert', err.message, true);
            }}
        }}
    </script>"""
    return _base_html("Secrets", content, user)


def _recipients_page(user: User, recipients: list, secrets: list) -> str:
    secret_options = "".join(
        f'<option value="{s.id}">{s.title}</option>' for s in secrets
    )
    if not secret_options:
        secret_options = '<option value="" disabled>No secrets yet — create one first</option>'

    rows = ""
    for r in recipients:
        # Find secret title
        secret_title = next((s.title for s in secrets if s.id == r.secret_id), "Unknown")
        rows += f"""<tr id="recipient-{r.id}">
            <td><strong>{r.name}</strong></td>
            <td>{r.email}</td>
            <td>{secret_title}</td>
            <td>
                <button class="btn btn-danger btn-sm" onclick="deleteRecipient('{r.id}')">Delete</button>
            </td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="4" style="text-align:center;color:#64748b;">No recipients configured yet.</td></tr>'

    content = f"""
    <div class="container">
        <h1>Recipients</h1>
        <p class="subtitle">People who will receive your secrets if the heartbeat triggers</p>

        <div id="alert" class="alert"></div>

        <div class="card">
            <table>
                <thead><tr><th>Name</th><th>Email</th><th>Secret</th><th>Actions</th></tr></thead>
                <tbody id="recipients-table">{rows}</tbody>
            </table>
        </div>

        <div class="card">
            <h2>Add Recipient</h2>
            <form onsubmit="addRecipient(event)">
                <div class="form-row">
                    <label for="name">Recipient Name</label>
                    <input type="text" id="name" required placeholder="e.g., John Doe">
                </div>
                <div class="form-row">
                    <label for="email">Recipient Email</label>
                    <input type="email" id="rec-email" required placeholder="recipient@example.com">
                </div>
                <div class="form-row">
                    <label for="secret_id">Secret to Send</label>
                    <select id="secret_id" required>{secret_options}</select>
                </div>
                <button type="submit" class="btn btn-primary">Add Recipient</button>
            </form>
        </div>
    </div>
    <script>
        async function addRecipient(e) {{
            e.preventDefault();
            try {{
                await apiCall('POST', '/api/recipients', {{
                    name: document.getElementById('name').value,
                    email: document.getElementById('rec-email').value,
                    secret_id: document.getElementById('secret_id').value,
                }});
                showAlert('alert', 'Recipient added!', false);
                setTimeout(() => location.reload(), 1000);
            }} catch (err) {{
                showAlert('alert', err.message, true);
            }}
        }}

        async function deleteRecipient(id) {{
            if (!confirm('Remove this recipient?')) return;
            try {{
                await apiCall('DELETE', `/api/recipients/${{id}}`);
                document.getElementById(`recipient-${{id}}`).remove();
                showAlert('alert', 'Recipient removed.', false);
            }} catch (err) {{
                showAlert('alert', err.message, true);
            }}
        }}
    </script>"""
    return _base_html("Recipients", content, user)


def _settings_page(user: User) -> str:
    active_checked = "checked" if user.is_active else ""
    display_name_val = user.display_name or ""

    # Common timezone options
    common_timezones = [
        "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
        "America/Anchorage", "Pacific/Honolulu", "America/Phoenix",
        "America/Toronto", "America/Vancouver",
        "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Amsterdam",
        "Europe/Moscow", "Europe/Istanbul",
        "Asia/Dubai", "Asia/Kolkata", "Asia/Shanghai", "Asia/Tokyo",
        "Asia/Seoul", "Asia/Singapore", "Asia/Hong_Kong",
        "Australia/Sydney", "Australia/Melbourne", "Australia/Perth",
        "Pacific/Auckland",
        "UTC",
    ]
    tz_options = ""
    for tz in common_timezones:
        selected = "selected" if tz == user.timezone else ""
        tz_options += f'<option value="{tz}" {selected}>{tz}</option>'

    # Hour options for active hours
    def hour_options(selected_hour):
        opts = ""
        for h in range(24):
            sel = "selected" if h == selected_hour else ""
            label = f"{h:02d}:00"
            if h == 0:
                label = "12:00 AM"
            elif h < 12:
                label = f"{h}:00 AM"
            elif h == 12:
                label = "12:00 PM"
            else:
                label = f"{h - 12}:00 PM"
            opts += f'<option value="{h}" {sel}>{label}</option>'
        return opts

    start_options = hour_options(user.active_hours_start)
    end_options = hour_options(user.active_hours_end)
    content = f"""
    <div class="container">
        <h1>Settings</h1>
        <p class="subtitle">Configure your profile, heartbeat schedule, and notification preferences</p>

        <div id="alert" class="alert"></div>

        <div class="card">
            <h2>Profile</h2>
            <form onsubmit="saveProfile(event)">
                <div class="form-row">
                    <label for="display_name">Display Name</label>
                    <input type="text" id="display_name" value="{display_name_val}" placeholder="Your name">
                    <small style="color:#64748b;">This is how you'll appear to recipients in notification emails</small>
                </div>
                <div class="form-row">
                    <label>Email</label>
                    <div style="background:#0f172a;padding:12px;border-radius:8px;font-size:14px;color:#94a3b8;">
                        {user.email}
                    </div>
                </div>
                <button type="submit" class="btn btn-primary mt-4">Save Profile</button>
            </form>
        </div>

        <div class="card">
            <h2>Heartbeat Schedule</h2>
            <form onsubmit="saveSettings(event)">
                <div class="form-row">
                    <label for="timezone">Timezone</label>
                    <select id="timezone">{tz_options}</select>
                </div>
                <div class="form-row">
                    <label for="interval">Heartbeat Interval (hours)</label>
                    <input type="number" id="interval" min="1" max="720" value="{user.heartbeat_interval_hours}">
                    <small style="color:#64748b;">How often you'll receive a check-in prompt</small>
                </div>
                <div class="flex-row-responsive">
                    <div class="form-row" style="flex:1;">
                        <label for="active_start">Active Hours Start</label>
                        <select id="active_start">{start_options}</select>
                    </div>
                    <div class="form-row" style="flex:1;">
                        <label for="active_end">Active Hours End</label>
                        <select id="active_end">{end_options}</select>
                    </div>
                </div>
                <small style="color:#64748b;display:block;margin-bottom:12px;">Heartbeats will only be sent during these hours in your timezone. Outside this window, they are silently skipped (not counted as missed).</small>
                <div class="form-row">
                    <label for="window">Response Window (hours)</label>
                    <input type="number" id="window" min="1" max="72" value="{user.response_window_hours}">
                    <small style="color:#64748b;">How long you have to respond before it counts as a miss</small>
                </div>
                <div class="form-row">
                    <label for="threshold">Miss Threshold</label>
                    <input type="number" id="threshold" min="1" max="10" value="{user.missed_threshold}">
                    <small style="color:#64748b;">Number of consecutive misses before triggering the dead man's switch</small>
                </div>
                <div class="form-row">
                    <label for="active" style="display:inline;">
                        <input type="checkbox" id="active" {active_checked} style="width:auto;margin-right:8px;">
                        Heartbeat Active
                    </label>
                </div>
                <button type="submit" class="btn btn-primary mt-4">Save Settings</button>
            </form>
        </div>

        <div class="card">
            <h2>Notification Channels</h2>
            <p style="color:#94a3b8;margin-bottom:16px;font-size:14px;">
                Heartbeat prompts are sent via ntfy push notification first, then email as fallback.
            </p>
            <div class="form-row">
                <label>ntfy Topic (primary)</label>
                <div style="background:#0f172a;padding:12px;border-radius:8px;font-family:monospace;font-size:14px;">
                    {user.ntfy_topic or 'Not configured'}
                </div>
                <small style="color:#64748b;">
                    Install the <a href="https://ntfy.sh" style="color:#22c55e;" target="_blank">ntfy app</a>
                    on your phone and subscribe to this topic to receive push notifications.
                </small>
            </div>
            <div class="form-row" style="margin-top:8px;">
                <label>Email (fallback)</label>
                <div style="background:#0f172a;padding:12px;border-radius:8px;font-size:14px;color:#94a3b8;">
                    {user.email}
                </div>
                <small style="color:#64748b;">Used if ntfy fails to deliver.</small>
            </div>
        </div>

        <div class="card" style="border-color: #ef4444;">
            <h2 style="color:#ef4444;">Danger Zone</h2>
            <p style="color:#94a3b8;margin-bottom:16px;">Reset your consecutive miss counter back to 0.</p>
            <button class="btn btn-danger" onclick="resetMisses()">Reset Miss Counter</button>
        </div>
    </div>
    <script>
        async function saveProfile(e) {{
            e.preventDefault();
            try {{
                await apiCall('PUT', '/api/auth/profile', {{
                    display_name: document.getElementById('display_name').value || null,
                }});
                showAlert('alert', 'Profile updated!', false);
            }} catch (err) {{
                showAlert('alert', err.message, true);
            }}
        }}

        async function saveSettings(e) {{
            e.preventDefault();
            try {{
                await apiCall('PUT', '/api/heartbeat/settings', {{
                    heartbeat_interval_hours: parseInt(document.getElementById('interval').value),
                    response_window_hours: parseInt(document.getElementById('window').value),
                    missed_threshold: parseInt(document.getElementById('threshold').value),
                    is_active: document.getElementById('active').checked,
                    timezone: document.getElementById('timezone').value,
                    active_hours_start: parseInt(document.getElementById('active_start').value),
                    active_hours_end: parseInt(document.getElementById('active_end').value),
                }});
                showAlert('alert', 'Settings saved!', false);
            }} catch (err) {{
                showAlert('alert', err.message, true);
            }}
        }}

        async function resetMisses() {{
            if (!confirm('Reset consecutive miss counter to 0?')) return;
            try {{
                await apiCall('PUT', '/api/heartbeat/settings', {{
                    is_active: true,
                }});
                showAlert('alert', 'Miss counter reset and heartbeat reactivated.', false);
                setTimeout(() => location.reload(), 1000);
            }} catch (err) {{
                showAlert('alert', err.message, true);
            }}
        }}
    </script>"""
    return _base_html("Settings", content, user)


def _reveal_page(sender_name: str, secret_title: str,
                 encrypted_content: str, encryption_nonce: str,
                 encryption_tag: str, encryption_salt: str) -> str:
    """Render the E2E secret reveal page with client-side decryption."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reveal Secret - RUThere</title>
    <link rel="icon" href="/static/favicon.ico" sizes="any">
    <link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
               background: #0f172a; color: #e2e8f0; min-height: 100vh;
               display: flex; align-items: center; justify-content: center; padding: 16px; }}
        .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 16px;
                padding: 32px 24px; max-width: 500px; width: 100%; }}
        h1 {{ font-size: 22px; margin-bottom: 4px; color: #f1f5f9; }}
        .subtitle {{ color: #94a3b8; font-size: 14px; margin-bottom: 24px; }}
        .e2e-badge {{ display: inline-flex; align-items: center; gap: 6px; background: #064e3b;
                     color: #34d399; padding: 6px 12px; border-radius: 8px; font-size: 13px;
                     font-weight: 600; margin-bottom: 20px; }}
        .e2e-badge svg {{ width: 16px; height: 16px; }}
        label {{ display: block; font-size: 13px; color: #94a3b8; margin-bottom: 4px; font-weight: 500; }}
        input {{ width: 100%; padding: 14px; background: #0f172a; border: 1px solid #334155;
                border-radius: 8px; color: #e2e8f0; font-size: 16px; margin-bottom: 16px; }}
        input:focus {{ outline: none; border-color: #22c55e; }}
        .btn {{ display: block; width: 100%; padding: 14px; border-radius: 8px; font-size: 16px;
               font-weight: 600; border: none; cursor: pointer; text-align: center; }}
        .btn-primary {{ background: #22c55e; color: #0f172a; }}
        .btn-primary:hover {{ background: #16a34a; }}
        .btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
        .error {{ background: #450a0a; color: #f87171; border: 1px solid #991b1b;
                 padding: 12px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; display: none; }}
        .secret-box {{ background: #0f172a; border: 1px solid #334155; border-radius: 8px;
                      padding: 20px; margin-top: 20px; white-space: pre-wrap; font-size: 15px;
                      line-height: 1.6; display: none; }}
        .secret-title {{ font-size: 16px; font-weight: 600; color: #f1f5f9; margin-bottom: 8px; }}
        .info {{ color: #64748b; font-size: 12px; margin-top: 16px; line-height: 1.5; }}
        .info a {{ color: #22c55e; }}
        .spinner {{ display: none; }}
        .spinner.active {{ display: inline-block; width: 16px; height: 16px;
                          border: 2px solid #0f172a; border-top-color: transparent;
                          border-radius: 50%; animation: spin 0.6s linear infinite;
                          vertical-align: middle; margin-right: 8px; }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    </style>
</head>
<body>
    <div class="card">
        <h1>A Message For You</h1>
        <p class="subtitle">From {sender_name}</p>

        <div class="e2e-badge">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>
            </svg>
            End-to-end encrypted
        </div>

        <div id="passphrase-form">
            <label for="passphrase">Enter the passphrase that {sender_name} shared with you</label>
            <input type="password" id="passphrase" placeholder="Passphrase" autofocus
                   onkeydown="if(event.key==='Enter')decryptSecret()">
            <div id="error" class="error"></div>
            <button class="btn btn-primary" onclick="decryptSecret()" id="decrypt-btn">
                <span class="spinner" id="spinner"></span>
                Decrypt Message
            </button>
        </div>

        <div id="secret-result" style="display:none;">
            <div class="secret-title">{secret_title}</div>
            <div class="secret-box" id="secret-content"></div>
        </div>

        <p class="info">
            This message is decrypted entirely in your browser. The server cannot read it.
            <br>To verify: open your browser's Developer Tools &gt; Network tab and observe
            that no plaintext or passphrase is sent to the server.
        </p>
    </div>

    <script src="/static/js/e2e.js"></script>
    <script>
        const encData = {{
            encrypted_content: "{encrypted_content}",
            encryption_nonce: "{encryption_nonce}",
            encryption_tag: "{encryption_tag}",
            encryption_salt: "{encryption_salt}",
        }};

        async function decryptSecret() {{
            const passphrase = document.getElementById('passphrase').value;
            if (!passphrase) return;

            const btn = document.getElementById('decrypt-btn');
            const spinner = document.getElementById('spinner');
            const error = document.getElementById('error');
            btn.disabled = true;
            spinner.className = 'spinner active';
            error.style.display = 'none';

            try {{
                const plaintext = await E2E.decrypt(
                    encData.encrypted_content,
                    encData.encryption_nonce,
                    encData.encryption_tag,
                    encData.encryption_salt,
                    passphrase
                );

                document.getElementById('passphrase-form').style.display = 'none';
                document.getElementById('secret-result').style.display = 'block';
                document.getElementById('secret-content').style.display = 'block';
                document.getElementById('secret-content').textContent = plaintext;
            }} catch (e) {{
                error.textContent = 'Incorrect passphrase. Please try again.';
                error.style.display = 'block';
                btn.disabled = false;
                spinner.className = 'spinner';
            }}
        }}
    </script>
</body>
</html>"""


def _reveal_error_page(title: str, message: str) -> str:
    """Render an error page for invalid/expired reveal links."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} - RUThere</title>
    <link rel="icon" href="/static/favicon.ico" sizes="any">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
               background: #0f172a; color: #e2e8f0; min-height: 100vh;
               display: flex; align-items: center; justify-content: center; padding: 16px; }}
        .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 16px;
                padding: 48px 32px; text-align: center; max-width: 400px; width: 100%; }}
        .icon {{ width: 80px; height: 80px; border-radius: 50%; background: #ef4444;
                color: white; font-size: 40px; display: flex; align-items: center;
                justify-content: center; margin: 0 auto 24px; }}
        h1 {{ font-size: 24px; margin-bottom: 12px; }}
        p {{ color: #94a3b8; font-size: 16px; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">&#10007;</div>
        <h1>{title}</h1>
        <p>{message}</p>
    </div>
</body>
</html>"""
