"""Secrets CRUD API routes — supports both server-side and E2E encryption."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.models import User, Secret, Recipient
from app.services.auth import get_current_user
from app.services.vault import encrypt, decrypt, encode_for_storage, decode_from_storage

router = APIRouter(prefix="/api/secrets", tags=["secrets"])


class SecretCreate(BaseModel):
    title: str
    content: str | None = None  # plaintext (server encryption only)
    encryption_type: str = "server"  # "server" or "e2e"
    # E2E fields — client sends pre-encrypted data:
    encrypted_content: str | None = None
    encryption_nonce: str | None = None
    encryption_tag: str | None = None
    encryption_salt: str | None = None


class SecretUpdate(BaseModel):
    title: str | None = None
    content: str | None = None  # server-encrypted only
    # E2E re-encryption fields:
    encrypted_content: str | None = None
    encryption_nonce: str | None = None
    encryption_tag: str | None = None
    encryption_salt: str | None = None


class SecretResponse(BaseModel):
    id: str
    title: str
    encryption_type: str
    created_at: str
    updated_at: str


class SecretDetailResponse(SecretResponse):
    content: str | None = None  # plaintext (server-encrypted secrets only)
    # E2E encrypted payload — returned as-is for client-side decryption:
    encrypted_content: str | None = None
    encryption_nonce: str | None = None
    encryption_tag: str | None = None
    encryption_salt: str | None = None


@router.post("", response_model=SecretResponse, status_code=201)
async def create_secret(
    body: SecretCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.encryption_type == "e2e":
        # E2E: client already encrypted — store ciphertext as-is
        if not all([body.encrypted_content, body.encryption_nonce, body.encryption_tag, body.encryption_salt]):
            raise HTTPException(status_code=400, detail="E2E secrets require encrypted_content, encryption_nonce, encryption_tag, and encryption_salt")

        secret = Secret(
            user_id=user.id,
            title=body.title,
            encrypted_content=body.encrypted_content,
            encryption_nonce=body.encryption_nonce,
            encryption_tag=body.encryption_tag,
            encryption_type="e2e",
            encryption_salt=body.encryption_salt,
        )
    else:
        # Server-side encryption (legacy/default)
        if not body.content:
            raise HTTPException(status_code=400, detail="Server-encrypted secrets require content")

        ciphertext, nonce, tag = encrypt(body.content)
        secret = Secret(
            user_id=user.id,
            title=body.title,
            encrypted_content=encode_for_storage(ciphertext),
            encryption_nonce=encode_for_storage(nonce),
            encryption_tag=encode_for_storage(tag),
            encryption_type="server",
        )

    db.add(secret)
    await db.flush()

    return SecretResponse(
        id=secret.id,
        title=secret.title,
        encryption_type=secret.encryption_type,
        created_at=secret.created_at.isoformat(),
        updated_at=secret.updated_at.isoformat(),
    )


@router.get("", response_model=list[SecretResponse])
async def list_secrets(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Secret).where(Secret.user_id == user.id).order_by(Secret.created_at.desc())
    )
    secrets = result.scalars().all()
    return [
        SecretResponse(
            id=s.id,
            title=s.title,
            encryption_type=s.encryption_type or "server",
            created_at=s.created_at.isoformat(),
            updated_at=s.updated_at.isoformat(),
        )
        for s in secrets
    ]


@router.get("/{secret_id}", response_model=SecretDetailResponse)
async def get_secret(
    secret_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Secret).where(Secret.id == secret_id, Secret.user_id == user.id)
    )
    secret = result.scalar_one_or_none()
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")

    if secret.encryption_type == "e2e":
        # Return encrypted blob — client will decrypt
        return SecretDetailResponse(
            id=secret.id,
            title=secret.title,
            encryption_type="e2e",
            encrypted_content=secret.encrypted_content,
            encryption_nonce=secret.encryption_nonce,
            encryption_tag=secret.encryption_tag,
            encryption_salt=secret.encryption_salt,
            created_at=secret.created_at.isoformat(),
            updated_at=secret.updated_at.isoformat(),
        )
    else:
        # Server-side decryption
        plaintext = decrypt(
            decode_from_storage(secret.encrypted_content),
            decode_from_storage(secret.encryption_nonce),
            decode_from_storage(secret.encryption_tag),
        )
        return SecretDetailResponse(
            id=secret.id,
            title=secret.title,
            encryption_type="server",
            content=plaintext,
            created_at=secret.created_at.isoformat(),
            updated_at=secret.updated_at.isoformat(),
        )


@router.put("/{secret_id}", response_model=SecretResponse)
async def update_secret(
    secret_id: str,
    body: SecretUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Secret).where(Secret.id == secret_id, Secret.user_id == user.id)
    )
    secret = result.scalar_one_or_none()
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")

    if body.title is not None:
        secret.title = body.title

    if secret.encryption_type == "e2e":
        # E2E: accept re-encrypted data from client
        if body.encrypted_content is not None:
            secret.encrypted_content = body.encrypted_content
            secret.encryption_nonce = body.encryption_nonce
            secret.encryption_tag = body.encryption_tag
            secret.encryption_salt = body.encryption_salt
    else:
        # Server-side: re-encrypt on server
        if body.content is not None:
            ciphertext, nonce, tag = encrypt(body.content)
            secret.encrypted_content = encode_for_storage(ciphertext)
            secret.encryption_nonce = encode_for_storage(nonce)
            secret.encryption_tag = encode_for_storage(tag)

    await db.flush()

    return SecretResponse(
        id=secret.id,
        title=secret.title,
        encryption_type=secret.encryption_type or "server",
        created_at=secret.created_at.isoformat(),
        updated_at=secret.updated_at.isoformat(),
    )


@router.delete("/{secret_id}", status_code=204)
async def delete_secret(
    secret_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Secret).where(Secret.id == secret_id, Secret.user_id == user.id)
    )
    secret = result.scalar_one_or_none()
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")

    # Check if any recipients are linked to this secret
    recipient_result = await db.execute(
        select(func.count()).select_from(Recipient).where(
            Recipient.secret_id == secret_id
        )
    )
    recipient_count = recipient_result.scalar()
    if recipient_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete this secret — it is assigned to {recipient_count} "
                   f"recipient{'s' if recipient_count != 1 else ''}. "
                   f"Remove the recipient{'s' if recipient_count != 1 else ''} first.",
        )

    await db.delete(secret)


# --- Public endpoint: reveal E2E encrypted secret ---

@router.get("/reveal/{token}")
async def get_reveal_data(token: str, db: AsyncSession = Depends(get_db)):
    """Return the encrypted payload for a reveal token. Client decrypts in browser."""
    from datetime import datetime, timezone
    from app.models.models import RevealToken

    result = await db.execute(
        select(RevealToken).where(RevealToken.token == token)
    )
    reveal = result.scalar_one_or_none()
    if not reveal:
        raise HTTPException(status_code=404, detail="Invalid or expired reveal link")

    now = datetime.now(timezone.utc)
    if reveal.expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=410, detail="This reveal link has expired")

    # Load the secret
    secret_result = await db.execute(
        select(Secret).where(Secret.id == reveal.secret_id)
    )
    secret = secret_result.scalar_one_or_none()
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")

    # Mark as accessed
    if not reveal.accessed_at:
        reveal.accessed_at = now

    # Load sender info for display
    from app.models.models import User
    user_result = await db.execute(select(User).where(User.id == secret.user_id))
    sender = user_result.scalar_one_or_none()

    return {
        "title": secret.title,
        "sender_name": sender.display_name or sender.email if sender else "Unknown",
        "encrypted_content": secret.encrypted_content,
        "encryption_nonce": secret.encryption_nonce,
        "encryption_tag": secret.encryption_tag,
        "encryption_salt": secret.encryption_salt,
    }
