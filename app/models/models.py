import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, Integer, DateTime, ForeignKey, Text, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=True)

    # Language preference
    language: Mapped[str] = mapped_column(String(10), default="en")

    # ntfy.sh push notification topic (unique per user)
    ntfy_topic: Mapped[str] = mapped_column(String(100), unique=True, nullable=True)

    # iMessage (phone number or Apple ID email for iMessage delivery)
    imessage_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Heartbeat configuration
    heartbeat_interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    response_window_hours: Mapped[int] = mapped_column(Integer, default=4)
    missed_threshold: Mapped[int] = mapped_column(Integer, default=3)
    consecutive_misses: Mapped[int] = mapped_column(Integer, default=0)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # Timezone and active hours (heartbeats only sent during these hours in user's timezone)
    timezone: Mapped[str] = mapped_column(String(50), default="America/Los_Angeles")
    active_hours_start: Mapped[int] = mapped_column(Integer, default=8)   # 0-23, e.g. 8 = 8:00 AM
    active_hours_end: Mapped[int] = mapped_column(Integer, default=22)    # 0-23, e.g. 22 = 10:00 PM

    # Next scheduled heartbeat (persisted so restarts don't reset timing)
    next_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Relationships
    secrets: Mapped[list["Secret"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    recipients: Mapped[list["Recipient"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    heartbeat_logs: Mapped[list["HeartbeatLog"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    trigger_logs: Mapped[list["TriggerLog"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Secret(Base):
    __tablename__ = "secrets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_content: Mapped[bytes] = mapped_column(Text, nullable=False)  # AES-256-GCM ciphertext
    encryption_nonce: Mapped[bytes] = mapped_column(Text, nullable=False)  # GCM nonce
    encryption_tag: Mapped[bytes] = mapped_column(Text, nullable=False)  # GCM auth tag

    # E2E encryption fields
    encryption_type: Mapped[str] = mapped_column(String(10), default="server")  # "server" or "e2e"
    encryption_salt: Mapped[str | None] = mapped_column(Text, nullable=True)  # PBKDF2 salt (base64), only for e2e

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="secrets")
    recipients: Mapped[list["Recipient"]] = relationship(back_populates="secret")


class Recipient(Base):
    __tablename__ = "recipients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    secret_id: Mapped[str] = mapped_column(String(36), ForeignKey("secrets.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="recipients")
    secret: Mapped["Secret"] = relationship(back_populates="recipients")


class HeartbeatLog(Base):
    __tablename__ = "heartbeat_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    response_token: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="sent", index=True)  # sent | responded | missed | escalated

    # Composite index for the escalation checker's hot query path
    __table_args__ = (
        Index("ix_heartbeat_user_status_sent", "user_id", "status", "sent_at"),
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="heartbeat_logs")


class TriggerLog(Base):
    __tablename__ = "trigger_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    recipient_id: Mapped[str] = mapped_column(String(36), ForeignKey("recipients.id"), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    action_taken: Mapped[str] = mapped_column(String(255), nullable=False)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="trigger_logs")
    recipient: Mapped["Recipient"] = relationship()


class RevealToken(Base):
    """One-time tokens for recipients to access E2E encrypted secrets after trigger fires."""
    __tablename__ = "reveal_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    secret_id: Mapped[str] = mapped_column(String(36), ForeignKey("secrets.id"), nullable=False)
    recipient_id: Mapped[str] = mapped_column(String(36), ForeignKey("recipients.id"), nullable=False)
    token: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    secret: Mapped["Secret"] = relationship()
    recipient: Mapped["Recipient"] = relationship()
