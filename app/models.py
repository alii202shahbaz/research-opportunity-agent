from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    profile: Mapped["UserProfile"] = relationship(back_populates="user", cascade="all, delete-orphan", uselist=False)
    sessions: Mapped[list["UserSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    notion: Mapped["NotionConnection"] = relationship(back_populates="user", cascade="all, delete-orphan", uselist=False)
    opportunities: Mapped[list["UserOpportunity"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    user: Mapped[User] = relationship(back_populates="sessions")


class UserProfile(Base):
    __tablename__ = "user_profiles"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    degree: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    university: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    cgpa: Mapped[float | None] = mapped_column(Float, nullable=True)
    cgpa_scale: Mapped[float] = mapped_column(Float, default=4.0, nullable=False)
    current_year: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    current_semester: Mapped[int | None] = mapped_column(Integer, nullable=True)
    graduation_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    citizenship: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    interests_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    preferred_countries_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    funding_preference: Mapped[str] = mapped_column(String(30), default="funded_only", nullable=False)
    intake_year: Mapped[int] = mapped_column(Integer, default=2027, nullable=False)
    intake_season: Mapped[str] = mapped_column(String(30), default="Summer", nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(80), default="UTC", nullable=False)
    update_hour: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    automation_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    deadline_email_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deadline_emails_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    manual_run_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_status: Mapped[str] = mapped_column(String(40), default="not_run", nullable=False)
    last_run_summary: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    user: Mapped[User] = relationship(back_populates="profile")


class OAuthState(Base):
    __tablename__ = "oauth_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotionConnection(Base):
    __tablename__ = "notion_connections"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    workspace_id: Mapped[str] = mapped_column(String(100), nullable=False)
    workspace_name: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    bot_id: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    database_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    data_source_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    database_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    user: Mapped[User] = relationship(back_populates="notion")


class UserOpportunity(Base):
    __tablename__ = "user_opportunities"
    __table_args__ = (
        UniqueConstraint("user_id", "canonical_url", name="uq_user_opportunity_url"),
        Index("ix_user_opportunity_deadline", "user_id", "deadline"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    program_name: Mapped[str] = mapped_column(String(400), nullable=False)
    institution: Mapped[str] = mapped_column(String(240), default="Not identified", nullable=False)
    country: Mapped[str] = mapped_column(String(160), default="Not specified", nullable=False)
    domains_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    funding_type: Mapped[str] = mapped_column(String(40), default="Not disclosed", nullable=False)
    funding_details: Mapped[str] = mapped_column(Text, default="", nullable=False)
    stipend: Mapped[str] = mapped_column(String(300), default="Not specified", nullable=False)
    tuition_waiver: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    travel_grant: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    housing: Mapped[str] = mapped_column(String(300), default="Not specified", nullable=False)
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    deadline_note: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    application_url: Mapped[str] = mapped_column(Text, nullable=False)
    official_source_url: Mapped[str] = mapped_column(Text, nullable=False)
    eligibility: Mapped[str] = mapped_column(String(40), default="Unknown", nullable=False)
    verification_status: Mapped[str] = mapped_column(String(40), default="Needs review", nullable=False)
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    intake_year: Mapped[int] = mapped_column(Integer, default=2027, nullable=False)
    intake_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    opportunity_status: Mapped[str] = mapped_column(String(40), default="Unknown", nullable=False)
    application_status: Mapped[str] = mapped_column(String(40), default="Pending", nullable=False)
    notion_page_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    user: Mapped[User] = relationship(back_populates="opportunities")


class EmailDigestLog(Base):
    __tablename__ = "email_digest_logs"
    __table_args__ = (UniqueConstraint("user_id", "local_date", name="uq_email_digest_user_date"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="sending", nullable=False)
    recipient_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    error_code: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)