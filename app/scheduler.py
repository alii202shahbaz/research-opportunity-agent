from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.discovery import discover_for_profile, persist_candidates
from app.emailer import refresh_application_statuses, send_deadline_digest
from app.models import NotionConnection, UserOpportunity, UserProfile
from app.notion import NotionAPIError, upsert_opportunity_page

logger = logging.getLogger(__name__)
settings = get_settings()


def next_scheduled_run(profile: UserProfile, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    local_zone = ZoneInfo(profile.timezone_name or "UTC")
    local_now = now.astimezone(local_zone)
    candidate = local_now.replace(hour=profile.update_hour, minute=0, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def process_user(user_id: str) -> None:
    db = SessionLocal()
    try:
        profile = db.get(UserProfile, user_id)
        if not profile:
            return
        now = datetime.now(timezone.utc)
        scheduled_due = bool(profile.next_run_at and (
            profile.next_run_at.replace(tzinfo=timezone.utc) if profile.next_run_at.tzinfo is None else profile.next_run_at
        ) <= now)
        manual_requested = profile.manual_run_requested
        candidates = discover_for_profile(profile) if (profile.automation_enabled or manual_requested) else []
        inserted, updated = persist_candidates(db, user_id, candidates) if candidates else (0, 0)
        local_today = now.astimezone(ZoneInfo(profile.timezone_name or "UTC")).date()
        expired_count = refresh_application_statuses(db, user_id, local_today)
        notion = db.get(NotionConnection, user_id)
        synced = 0
        sync_errors = 0
        if notion and notion.data_source_id:
            rows = db.scalars(
                select(UserOpportunity).where(UserOpportunity.user_id == user_id)
                .order_by(UserOpportunity.notion_page_id.is_(None).desc(),
                          UserOpportunity.deadline.is_(None), UserOpportunity.deadline.asc(),
                          UserOpportunity.relevance_score.desc())
                .limit(settings.max_pages_per_run)
            ).all()
            for row in rows:
                try:
                    upsert_opportunity_page(db, user_id, row)
                    synced += 1
                    time.sleep(0.35)
                except NotionAPIError as exc:
                    sync_errors += 1
                    logger.warning("Notion sync failed for user %s (%s)", user_id[:8], str(exc))
        email_result = "not_due"
        if profile.deadline_email_enabled and scheduled_due:
            email_result = send_deadline_digest(db, profile, now=now)
        profile.last_run_at = now
        profile.last_run_status = "partial" if sync_errors else "success"
        profile.last_run_summary = (
            f"{len(candidates)} candidates; {inserted} new; {updated} updated; "
            f"{synced} Notion rows synced; {sync_errors} sync errors; "
            f"{expired_count} deadlines passed; email digest: {email_result}"
        )[:500]
        profile.next_run_at = next_scheduled_run(profile) if (profile.automation_enabled or profile.deadline_email_enabled) else None
        profile.manual_run_requested = False
        profile.lease_until = None
        db.commit()
        logger.info("Completed daily job for user %s: %s", user_id[:8], profile.last_run_summary)
    except Exception as exc:
        db.rollback()
        logger.exception("Daily job failed for user %s (%s)", user_id[:8], type(exc).__name__)
        profile = db.get(UserProfile, user_id)
        if profile:
            profile.last_run_at = datetime.now(timezone.utc)
            profile.last_run_status = "error"
            profile.last_run_summary = f"Job failed: {type(exc).__name__}"[:500]
            profile.next_run_at = (datetime.now(timezone.utc) + timedelta(minutes=30)) if (profile.automation_enabled or profile.deadline_email_enabled) else None
            profile.manual_run_requested = False
            profile.lease_until = None
            db.commit()
    finally:
        db.close()


def claim_due_users(limit: int = 1) -> list[str]:
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        stmt = (
            select(UserProfile)
            .where(
                or_(
                    UserProfile.manual_run_requested.is_(True),
                    ((UserProfile.automation_enabled.is_(True) | UserProfile.deadline_email_enabled.is_(True)) & UserProfile.next_run_at.is_not(None) & (UserProfile.next_run_at <= now)),
                ),
                or_(UserProfile.lease_until.is_(None), UserProfile.lease_until < now),
            )
            .order_by(UserProfile.next_run_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        profiles = db.scalars(stmt).all()
        user_ids = []
        for profile in profiles:
            profile.lease_until = now + timedelta(minutes=settings.job_lease_minutes)
            # Keep manual_run_requested set until process_user finishes. If a
            # worker dies mid-run, the lease expires and the request is retried.
            user_ids.append(profile.user_id)
        db.commit()
        return user_ids
    finally:
        db.close()


def worker_loop() -> None:
    logger.info("Research agent worker started; polling every %s seconds", settings.worker_poll_seconds)
    while True:
        user_ids = claim_due_users()
        if not user_ids:
            time.sleep(settings.worker_poll_seconds)
            continue
        for user_id in user_ids:
            process_user(user_id)


def enqueue_immediate_run(db: Session, profile: UserProfile) -> None:
    profile.manual_run_requested = True
    profile.lease_until = None
    db.commit()