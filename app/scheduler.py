from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.discovery import discover_for_profile, persist_candidates
from app.emailer import refresh_application_statuses, send_deadline_digest, send_recovery_digest
from app.models import NotionConnection, UserOpportunity, UserProfile
from app.notion import (NotionAPIError, prepare_notion_sync, sync_discovered_candidate, trash_notion_page, upsert_opportunity_page)

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


def _profile_snapshot(profile: UserProfile) -> dict:
    return {
        "user_id": profile.user_id,
        "timezone_name": profile.timezone_name or "UTC",
        "deadline_email_enabled": bool(profile.deadline_email_enabled),
        "deadline_emails": json.loads(profile.deadline_emails_json or "[]"),
        "automation_enabled": bool(profile.automation_enabled),
        "next_run_at": profile.next_run_at,
    }


def _candidate_snapshot(candidate) -> dict:
    return {
        "program_name": candidate.program_name,
        "institution": candidate.institution,
        "country": candidate.country,
        "funding_type": candidate.funding_type,
        "deadline": candidate.deadline.isoformat() if candidate.deadline else None,
        "application_url": str(candidate.application_url),
        "official_source_url": str(candidate.official_source_url),
    }


def archive_stale_opportunities(db: Session, user_id: str, today: date, notion_context) -> int:
    """Archive unreviewed opportunities 30 days after their deadline.

    User-managed statuses and no-deadline opportunities are preserved.
    Notion rows are moved to Trash before their local mirror is deleted.
    """
    cutoff = today - timedelta(days=30)
    stale = db.scalars(
        select(UserOpportunity).where(
            UserOpportunity.user_id == user_id,
            UserOpportunity.deadline.is_not(None),
            UserOpportunity.deadline < cutoff,
            UserOpportunity.application_status.in_(["Pending", "Deadline Passed"]),
        )
    ).all()
    archived = 0
    for row in stale:
        if row.notion_page_id and notion_context:
            try:
                trash_notion_page(notion_context, row.notion_page_id)
            except NotionAPIError as exc:
                logger.warning("Retention archive deferred for user %s (%s)", user_id[:8], str(exc))
                continue
        db.delete(row)
        archived += 1
    if archived:
        db.commit()
    return archived


def process_user(user_id: str) -> None:
    db = SessionLocal()
    profile_snapshot = None
    notion_context = None
    run_id = f"{user_id}/{datetime.now(timezone.utc).isoformat()}"
    try:
        profile = db.get(UserProfile, user_id)
        if not profile:
            return
        profile_snapshot = _profile_snapshot(profile)
        now = datetime.now(timezone.utc)
        scheduled_due = bool(profile.next_run_at and (
            profile.next_run_at.replace(tzinfo=timezone.utc) if profile.next_run_at.tzinfo is None else profile.next_run_at
        ) <= now)
        manual_requested = profile.manual_run_requested
        try:
            notion_context = prepare_notion_sync(db, user_id)
        except NotionAPIError as exc:
            logger.warning("Could not prepare Notion recovery sync for user %s (%s)", user_id[:8], str(exc))

        candidates = discover_for_profile(profile) if (profile.automation_enabled or manual_requested) else []
        local_today = now.astimezone(ZoneInfo(profile.timezone_name or "UTC")).date()
        notion_direct_synced = 0
        notion_direct_errors = 0
        direct_page_ids: dict[str, str] = {}

        # Send fresh discoveries to Notion before attempting the DB write.
        if notion_context:
            for candidate in candidates:
                try:
                    page_id = sync_discovered_candidate(notion_context, candidate)
                    if page_id:
                        direct_page_ids[str(candidate.official_source_url)] = page_id
                    notion_direct_synced += 1
                    time.sleep(0.35)
                except NotionAPIError as exc:
                    notion_direct_errors += 1
                    logger.warning("Direct Notion recovery sync failed for user %s (%s)", user_id[:8], str(exc))

        try:
            inserted, updated = persist_candidates(db, user_id, candidates) if candidates else (0, 0)
            # Link direct Notion writes to their local rows so the regular sync
            # path updates the same pages rather than creating duplicates.
            if direct_page_ids:
                for row in db.scalars(select(UserOpportunity).where(UserOpportunity.user_id == user_id)).all():
                    page_id = direct_page_ids.get(row.canonical_url)
                    if page_id and not row.notion_page_id:
                        row.notion_page_id = page_id
                db.commit()
        except Exception as exc:
            db.rollback()
            logger.exception("Database persistence failed for user %s (%s)", user_id[:8], type(exc).__name__)
            email_result = send_recovery_digest(
                profile_snapshot,
                [_candidate_snapshot(item) for item in candidates],
                reason=type(exc).__name__,
                run_id=run_id,
            )
            logger.warning(
                "Recovery run user=%s candidates=%s notion_synced=%s notion_errors=%s email=%s",
                user_id[:8], len(candidates), notion_direct_synced, notion_direct_errors, email_result,
            )
            # Keep the lease and schedule untouched. The lease expires and the
            # next worker run retries persistence; direct Notion upsert is safe.
            return

        expired_count = refresh_application_statuses(db, user_id, local_today)
        notion = db.get(NotionConnection, user_id)
        synced = 0
        sync_errors = notion_direct_errors
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
        archived_count = archive_stale_opportunities(db, user_id, local_today, notion_context)
        profile.last_run_at = now
        profile.last_run_status = "partial" if sync_errors else "success"
        profile.last_run_summary = (
            f"{len(candidates)} candidates; {inserted} new; {updated} updated; "
            f"{synced + notion_direct_synced} Notion rows synced; {sync_errors} sync errors; "
            f"{expired_count} deadlines passed; {archived_count} old opportunities archived; "
            f"email digest: {email_result}"
        )[:500]
        profile.next_run_at = next_scheduled_run(profile) if (profile.automation_enabled or profile.deadline_email_enabled) else None
        profile.manual_run_requested = False
        profile.lease_until = None
        db.commit()
        logger.info("Completed daily job for user %s: %s", user_id[:8], profile.last_run_summary)
    except Exception as exc:
        db.rollback()
        logger.exception("Daily job failed for user %s (%s)", user_id[:8], type(exc).__name__)
        # On DB outages, leave the lease to expire so the job can retry.
        try:
            profile = db.get(UserProfile, user_id)
            if profile:
                profile.last_run_at = datetime.now(timezone.utc)
                profile.last_run_status = "error"
                profile.last_run_summary = f"Job failed: {type(exc).__name__}"[:500]
                profile.lease_until = None
                db.commit()
        except Exception:
            db.rollback()
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