"""Auth API routes: register, login, logout, profile."""
import uuid
from fastapi import APIRouter, Depends, HTTPException, Response, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.models import User
from app.services.auth import hash_password, verify_password, create_access_token, get_current_user
from app.services.scheduler import compute_next_heartbeat
from app.config import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    access_token: str
    user_id: str
    email: str


@router.post("/register", response_model=AuthResponse)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    # Check if email already exists
    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    settings = get_settings()

    # Create user with a unique ntfy topic
    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        ntfy_topic=f"ruthere-{uuid.uuid4().hex[:12]}",
        heartbeat_interval_hours=settings.default_heartbeat_interval_hours,
        response_window_hours=settings.default_response_window_hours,
        missed_threshold=settings.default_missed_threshold,
    )
    db.add(user)
    await db.flush()

    # Schedule first heartbeat
    user.next_heartbeat_at = compute_next_heartbeat(user)

    token = create_access_token(user.id)
    return AuthResponse(access_token=token, user_id=user.id, email=user.email)


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token(user.id)

    # Set cookie for web UI
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=72 * 3600,
    )

    return AuthResponse(access_token=token, user_id=user.id, email=user.email)


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("access_token")
    return {"message": "Logged out"}


class ProfileUpdate(BaseModel):
    display_name: str | None = None


class ProfileResponse(BaseModel):
    user_id: str
    email: str
    display_name: str | None


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(user: User = Depends(get_current_user)):
    return ProfileResponse(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
    )


@router.put("/profile", response_model=ProfileResponse)
async def update_profile(
    body: ProfileUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.display_name is not None:
        user.display_name = body.display_name.strip() or None

    await db.flush()

    return ProfileResponse(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
    )
