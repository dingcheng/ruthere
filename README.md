# RUThere

A dead man's switch / heartbeat system. Store encrypted secrets in a vault, receive periodic check-in prompts, and if you fail to respond, your designated recipients are automatically notified.

## How It Works

1. **You store secrets** — encrypted messages, passwords, instructions, or any sensitive information
2. **The system sends you heartbeat check-ins** — via push notification (ntfy.sh) or email, on a configurable schedule
3. **You tap "I'm here"** — a one-click link confirms you're okay and resets the counter
4. **If you miss multiple check-ins** — the system triggers: secrets are delivered to your designated recipients

### Encryption Options

**Server-encrypted** — secrets are encrypted with AES-256-GCM on the server. The server can decrypt them to email plaintext to recipients when the switch triggers. Simpler setup, suitable when you trust the server operator.

**End-to-end encrypted** — secrets are encrypted in your browser using a passphrase (PBKDF2 + AES-256-GCM via the Web Crypto API). The server stores only ciphertext and can never read the secret. When the switch triggers, recipients receive a link to a browser-based decryption page where they enter the passphrase you shared with them out-of-band. The passphrase and plaintext never touch the server.

You can verify E2E encryption by opening your browser's Network tab — no plaintext or passphrase is ever sent to the server.

### Heartbeat Schedule

- Configurable interval (e.g., every 4 hours, daily, weekly)
- Active hours window — heartbeats only sent during your specified hours in your timezone (e.g., 8 AM - 10 PM)
- Heartbeats that land outside your active window are pushed to the next window, not counted as missed
- Notification tier: ntfy push first, email fallback
- Escalation: if you miss a push notification, the system escalates to email before counting it as a miss
- Configurable miss threshold (default: 3 consecutive misses before trigger)
- All scheduling is persisted to the database and survives server restarts

### Trigger Simulation

A built-in simulation wizard lets you test the entire trigger sequence step by step without affecting your real heartbeat state:

1. **Send Heartbeat** — sends a real ntfy push + email so you can test the notification flow
2. **Escalate** — simulates the response window expiring and sends an escalation email
3. **Miss** — marks the heartbeat as missed (simulation-only counter, not your real miss count)
4. **Fire Trigger** — delivers secrets to a test email you specify (never to real recipients)

Simulation data is fully isolated: it doesn't appear on the dashboard, doesn't affect your consecutive miss counter, and doesn't deactivate your heartbeat. All simulation emails are clearly labeled `[SIMULATION]`.

## Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.12+, FastAPI, Uvicorn |
| Database | SQLite (WAL mode) via SQLAlchemy async |
| Server encryption | AES-256-GCM (`cryptography` library) |
| E2E encryption | PBKDF2 (600k iterations, SHA-256) + AES-256-GCM (Web Crypto API, zero dependencies) |
| Auth | bcrypt + JWT (72-hour tokens) |
| Push notifications | ntfy.sh (free, no account required) |
| Email | Resend API |
| Scheduling | APScheduler (async, in-process) |
| HTTP client | httpx with shared connection pooling |
| Deployment | Docker Compose + Cloudflare Tunnel |
| Tests | pytest + pytest-asyncio (122 tests) |

## Self-Hosting Guide

### Prerequisites

- Docker and Docker Compose
- A domain name (for Cloudflare Tunnel)
- A [Resend](https://resend.com) account (free tier: 3,000 emails/month)
- The [ntfy](https://ntfy.sh) app on your phone (free, no account needed)

### 1. Clone the repo

```bash
git clone https://github.com/dingcheng/ruthere.git
cd ruthere
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env` and fill in the values:

```bash
# Generate a secret key for JWT signing
python3 -c "import secrets; print(secrets.token_hex(32))"

# Generate a vault encryption key for server-side secrets
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

| Variable | What to set |
|----------|-------------|
| `SECRET_KEY` | Random 64-char hex string (generated above) |
| `VAULT_KEY` | Base64-encoded 32-byte key (generated above) |
| `BASE_URL` | Your public URL, e.g., `https://ruthere.yourdomain.com` |
| `RESEND_API_KEY` | Your Resend API key |
| `EMAIL_FROM` | Sender address (must match a verified domain in Resend) |
| `TUNNEL_TOKEN` | Your Cloudflare Tunnel token (see step 4) |

### 3. Create the data directory

```bash
mkdir -p data
```

### 4. Set up Cloudflare Tunnel

1. Add your domain to [Cloudflare](https://dash.cloudflare.com) (free plan)
2. Update your domain's nameservers to Cloudflare's (at your registrar)
3. Go to **Cloudflare Zero Trust** > **Networks** > **Tunnels** > **Create a tunnel**
4. Copy the tunnel token into your `.env` as `TUNNEL_TOKEN`
5. Add a **Public Hostname** in the tunnel config:
   - Subdomain: `ruthere` (or whatever you prefer)
   - Domain: `yourdomain.com`
   - Type: `HTTP`
   - URL: `localhost:8000`

### 5. Start the application

```bash
docker compose up -d --build
```

Verify it's running:

```bash
# Check container status
docker compose ps

# Check logs
docker compose logs -f app

# Test health endpoint
curl https://ruthere.yourdomain.com/health
```

### 6. Create your account

1. Open `https://ruthere.yourdomain.com` in your browser
2. Click **Register** and create an account
3. Go to **Settings** to configure your heartbeat interval, timezone, and active hours

### 7. Set up ntfy notifications

1. Install the **ntfy** app on your phone ([iOS](https://apps.apple.com/us/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy))
2. Go to **Settings** in the RUThere web UI
3. Copy your **ntfy Topic** value
4. In the ntfy app, subscribe to that topic

### 8. Add secrets and recipients

1. Go to **Secrets** > **Add New Secret**
2. Choose encryption type (server or E2E) and save
3. Go to **Recipients** > **Add Recipient** and link them to a secret
4. For E2E secrets: share the passphrase with your recipient in person or by phone

### 9. Test the trigger flow

1. Go to **Simulate** in the nav bar
2. Walk through the 4-step wizard to verify notifications, escalation, and secret delivery
3. Use your own email as the test target — real recipients are never contacted during simulation
4. Click **Reset** when done

## Common Commands

```bash
# Start in background
docker compose up -d

# Stop
docker compose down

# View logs
docker compose logs -f app

# Rebuild after code changes
docker compose up -d --build

# Restart (e.g., after editing .env)
docker compose restart app

# Run tests
python -m pytest tests/ -v
```

## Running Without Docker (Development)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your values

uvicorn app.main:app --reload
# Open http://localhost:8000
```

## Project Structure

```
ruthere/
├── app/
│   ├── main.py              # FastAPI app entry point
│   ├── config.py             # Settings from .env
│   ├── database.py           # SQLAlchemy async engine (SQLite, WAL mode)
│   ├── models/
│   │   └── models.py         # User, Secret, Recipient, HeartbeatLog, TriggerLog, RevealToken
│   ├── api/
│   │   ├── auth.py           # Register, login, profile
│   │   ├── secrets.py        # CRUD + reveal endpoint for E2E secrets
│   │   ├── recipients.py     # CRUD + invite emails
│   │   ├── heartbeat.py      # Respond, status, settings, history, test
│   │   ├── simulate.py       # Step-by-step trigger simulation (fully isolated)
│   │   └── web.py            # Server-rendered HTML pages
│   ├── services/
│   │   ├── auth.py           # bcrypt + JWT
│   │   ├── vault.py          # AES-256-GCM server-side encryption
│   │   ├── notify.py         # ntfy.sh push + Resend email (shared httpx client)
│   │   ├── scheduler.py      # Heartbeat dispatcher, escalation checker, log cleanup
│   │   └── trigger.py        # Dead man's switch execution (server + E2E paths)
│   └── static/
│       └── js/e2e.js         # Client-side PBKDF2 + AES-256-GCM (Web Crypto API)
├── tests/                    # 122 tests across 8 files
│   ├── conftest.py           # Fixtures: async client, in-memory DB, test user
│   ├── test_vault.py         # Encryption round-trip, tamper detection
│   ├── test_auth.py          # Register, login, JWT, password hashing
│   ├── test_secrets.py       # Server + E2E CRUD, cross-user isolation
│   ├── test_recipients.py    # CRUD, linked secret validation
│   ├── test_heartbeat.py     # Respond, settings, history, scheduling
│   ├── test_scheduler.py     # Active hours, next heartbeat computation
│   ├── test_trigger.py       # Server + E2E trigger, reveal tokens
│   ├── test_simulate.py      # Simulation isolation, flow, dashboard exclusion
│   └── test_web.py           # Page rendering, auth redirects
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml            # pytest config
├── requirements.txt
└── .env.example
```

## License

MIT
