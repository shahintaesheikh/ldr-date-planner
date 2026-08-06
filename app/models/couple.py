from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from pgvector.sqlalchemy import Vector


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class CalendarProvider(PyEnum):
    google = "google"
    caldav = "caldav"


class ConnectionStatus(PyEnum):
    active = "active"
    disconnected = "disconnected"
    expired = "expired"


class TraitSource(PyEnum):
    implicit = "implicit"
    explicit = "explicit"


class ActivitySource(PyEnum):
    seed = "seed"
    llm = "llm"
    user = "user"


class ProposalStatus(PyEnum):
    pending = "pending"
    confirmed = "confirmed"
    rejected = "rejected"
    expired = "expired"


class FeedbackSignal(PyEnum):
    accept = "accept"
    reject = "reject"
    rerun = "rerun"
    mute = "mute"


class EncryptedText(TypeDecorator):
    """Placeholder for encrypted-at-rest token storage.

    Phase 0 stores tokens as TEXT. Encryption (using the platform's KMS or a
    project master key) is intentionally deferred; the column type is isolated
    here so the encryption layer can be dropped in without touching models.
    """

    impl = Text
    cache_ok = True


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_number: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True
    )  # E.164, for Twilio
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="UTC"
    )  # IANA tz name
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    calendar_connections: Mapped[list["CalendarConnection"]] = relationship(
        back_populates="user"
    )


class Couple(Base):
    __tablename__ = "couples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    partner_a_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    partner_b_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    suggestions_muted: Mapped[bool] = mapped_column(server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    traits: Mapped[list["Trait"]] = relationship(back_populates="couple")
    proposals: Mapped[list["Proposal"]] = relationship(back_populates="couple")


class CalendarConnection(Base):
    __tablename__ = "calendar_connections"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    provider: Mapped[CalendarProvider] = mapped_column(
        Enum(CalendarProvider, name="calendar_provider"), nullable=False
    )
    # Either oauth_token (google) or caldav_credentials (caldav) is populated
    # depending on provider. Both stored encrypted at rest (see EncryptedText).
    oauth_token: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    caldav_credentials: Mapped[str | None] = mapped_column(
        EncryptedText, nullable=True
    )
    refresh_token: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    status: Mapped[ConnectionStatus] = mapped_column(
        Enum(ConnectionStatus, name="connection_status"),
        nullable=False,
        server_default=ConnectionStatus.active.name,
    )

    user: Mapped["User"] = relationship(back_populates="calendar_connections")


class Trait(Base):
    """EAV trait store, keyed to couple_id (not per-user).

    Open-ended trait set: `ideate_activity` can add new keys over time. Each
    row carries its own weight/source/updated_at.
    """

    __tablename__ = "traits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    couple_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("couples.id"), nullable=False
    )
    trait_key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    source: Mapped[TraitSource] = mapped_column(
        Enum(TraitSource, name="trait_source"),
        nullable=False,
        server_default=TraitSource.explicit.name,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    couple: Mapped["Couple"] = relationship(back_populates="traits")


class DateActivity(Base):
    """Activity catalog. Lookups are semantic (RAG via pgvector), not flat tags."""

    __tablename__ = "date_activities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    est_duration_min: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="60"
    )
    cost_tag: Mapped[str | None] = mapped_column(String(32), nullable=True)  # unused v1
    source: Mapped[ActivitySource] = mapped_column(
        Enum(ActivitySource, name="activity_source"),
        nullable=False,
        server_default=ActivitySource.seed.name,
    )
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), server_default="{}")
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(1536), nullable=True
    )  # pgvector; OpenAI embedding dim
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    couple_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("couples.id"), nullable=False
    )
    activity_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("date_activities.id"), nullable=False
    )
    proposed_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    proposed_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[ProposalStatus] = mapped_column(
        Enum(ProposalStatus, name="proposal_status"),
        nullable=False,
        server_default=ProposalStatus.pending.name,
    )
    confirmed_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    couple: Mapped["Couple"] = relationship(back_populates="proposals")
    feedback: Mapped[list["Feedback"]] = relationship(back_populates="proposal")
    sms_threads: Mapped[list["SMSThread"]] = relationship(back_populates="proposal")


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("proposals.id"), nullable=False
    )
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)  # explicit
    implicit_signal: Mapped[FeedbackSignal | None] = mapped_column(
        Enum(FeedbackSignal, name="feedback_signal"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    proposal: Mapped["Proposal"] = relationship(back_populates="feedback")


class SMSThread(Base):
    """Inbound SMS reply thread, scoped per pending proposal.

    Lets `classify_intent` resolve which proposal a reply amends, even after a
    proposal has expired.
    """

    __tablename__ = "sms_thread"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("proposals.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    raw_body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    proposal: Mapped["Proposal"] = relationship(back_populates="sms_threads")


# Export all models for Alembic and import convenience
__all__ = [
    "Base",
    "User",
    "Couple",
    "CalendarConnection",
    "Trait",
    "DateActivity",
    "Proposal",
    "Feedback",
    "SMSThread",
]