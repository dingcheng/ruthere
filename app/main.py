"""RUThere - Dead Man's Switch / Heartbeat System."""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.config import get_settings
from app.database import init_db
from app.services.scheduler import start_scheduler, stop_scheduler
from app.services.notify import close_http_client
from app.api import auth, secrets, recipients, heartbeat, web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    logger.info("Starting RUThere...")
    await init_db()
    await start_scheduler()
    logger.info("RUThere is running.")
    yield
    stop_scheduler()
    await close_http_client()
    logger.info("RUThere stopped.")


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
)

# Mount static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# API routes
app.include_router(auth.router)
app.include_router(secrets.router)
app.include_router(recipients.router)
app.include_router(heartbeat.router)

# Web UI routes
app.include_router(web.router)


@app.get("/health")
async def health():
    return {"status": "ok", "app": settings.app_name}


# Root-level icon routes (browsers and iOS request these automatically)
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse("app/static/favicon.ico", media_type="image/x-icon")


@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
async def apple_touch_icon():
    return FileResponse("app/static/apple-touch-icon.png", media_type="image/png")
