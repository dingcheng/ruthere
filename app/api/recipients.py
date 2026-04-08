"""Recipients CRUD API routes."""
import logging
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.models import User, Recipient, Secret
from app.services.auth import get_current_user
from app.services.notify import send_recipient_invite_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recipients", tags=["recipients"])


class RecipientCreate(BaseModel):
    name: str
    email: EmailStr
    secret_id: str


class RecipientUpdate(BaseModel):
    name: str | None = None
    email: EmailStr | None = None
    secret_id: str | None = None


class RecipientResponse(BaseModel):
    id: str
    name: str
    email: str
    secret_id: str
    secret_title: str | None = None
    created_at: str


@router.post("", response_model=RecipientResponse, status_code=201)
async def create_recipient(
    body: RecipientCreate,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Verify the secret belongs to this user
    result = await db.execute(
        select(Secret).where(Secret.id == body.secret_id, Secret.user_id == user.id)
    )
    secret = result.scalar_one_or_none()
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")

    recipient = Recipient(
        user_id=user.id,
        secret_id=body.secret_id,
        name=body.name,
        email=body.email,
    )
    db.add(recipient)
    await db.flush()

    # Send invite email in the background (non-blocking)
    sender_name = user.display_name or user.email
    background_tasks.add_task(
        send_recipient_invite_email,
        to=body.email,
        recipient_name=body.name,
        sender_name=sender_name,
    )
    logger.info(f"Recipient invite email queued for {body.email}")

    return RecipientResponse(
        id=recipient.id,
        name=recipient.name,
        email=recipient.email,
        secret_id=recipient.secret_id,
        secret_title=secret.title,
        created_at=recipient.created_at.isoformat(),
    )


@router.get("", response_model=list[RecipientResponse])
async def list_recipients(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Recipient).where(Recipient.user_id == user.id).order_by(Recipient.created_at.desc())
    )
    recipients = result.scalars().all()

    responses = []
    for r in recipients:
        secret_result = await db.execute(select(Secret.title).where(Secret.id == r.secret_id))
        secret_title = secret_result.scalar_one_or_none()
        responses.append(
            RecipientResponse(
                id=r.id,
                name=r.name,
                email=r.email,
                secret_id=r.secret_id,
                secret_title=secret_title,
                created_at=r.created_at.isoformat(),
            )
        )
    return responses


@router.put("/{recipient_id}", response_model=RecipientResponse)
async def update_recipient(
    recipient_id: str,
    body: RecipientUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Recipient).where(Recipient.id == recipient_id, Recipient.user_id == user.id)
    )
    recipient = result.scalar_one_or_none()
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found")

    if body.name is not None:
        recipient.name = body.name
    if body.email is not None:
        recipient.email = body.email
    if body.secret_id is not None:
        # Verify new secret belongs to user
        secret_result = await db.execute(
            select(Secret).where(Secret.id == body.secret_id, Secret.user_id == user.id)
        )
        if not secret_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Secret not found")
        recipient.secret_id = body.secret_id

    await db.flush()

    secret_result = await db.execute(select(Secret.title).where(Secret.id == recipient.secret_id))
    secret_title = secret_result.scalar_one_or_none()

    return RecipientResponse(
        id=recipient.id,
        name=recipient.name,
        email=recipient.email,
        secret_id=recipient.secret_id,
        secret_title=secret_title,
        created_at=recipient.created_at.isoformat(),
    )


@router.delete("/{recipient_id}", status_code=204)
async def delete_recipient(
    recipient_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Recipient).where(Recipient.id == recipient_id, Recipient.user_id == user.id)
    )
    recipient = result.scalar_one_or_none()
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found")

    await db.delete(recipient)
