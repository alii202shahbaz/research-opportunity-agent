from __future__ import annotations

import html
import json
import logging
import hashlib
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import EmailDigestLog, User, UserOpportunity, UserProfile

settings = get_settings()
logger = logging.getLogger(__name__)
RESEND_ENDPOINT = "https://api.resend.com/emails"


def tomorrow_deadlines(db: Session, user_id: str, tomorrow: date) -> list[UserOpportunity]:
    return db.scalars(
        select(UserOpportunity)
        .where(
            UserOpportunity.user_id == user_id,
            UserOpportunity.deadline == tomorrow,
            UserOpportunity.application_status == "Pending",
        )
        .order_by(UserOpportunity.deadline.asc(), UserOpportunity.relevance_score.desc())
    ).all()


def _render_digest(rows: list[UserOpportunity], deadline_date: date) -> tuple[str, str, str]:
    subject = f"Research deadlines tomorrow — {len(rows)} {'opportunity' if len(rows) == 1 else 'opportunities'}"
    text_lines = [f"These research opportunity deadlines fall on {deadline_date.isoformat()}:", ""]
    cards = []
    for row in rows:
        name = row.program_name
        text_lines.extend([
            f"{name} — {row.institution} ({row.country})",
            f"Funding: {row.funding_type}; deadline: {deadline_date.isoformat()}",
            f"Application: {row.application_url}",
            f"Source: {row.official_source_url}",
            "",
        ])
        safe_name = html.escape(name)
        safe_meta = html.escape(f"{row.institution} · {row.country} · {row.funding_type}")
        safe_url = html.escape(row.application_url, quote=True)
        safe_source = html.escape(row.official_source_url, quote=True)
        cards.append(
            '<tr><td style="padding:18px 0;border-bottom:1px solid #e5eaf1">'
            f'<div style="font-size:16px;font-weight:700;color:#172235">{safe_name}</div>'
            f'<div style="font-size:13px;color:#657286;margin:5px 0 12px">{safe_meta}</div>'
            f'<a href="{safe_url}" style="color:#3157d5;text-decoration:none;font-weight:600">Application page</a>'
            f' &nbsp;·&nbsp; <a href="{safe_source}" style="color:#657286;text-decoration:none">Source</a>'
            '</td></tr>'
        )
    html_body = (
        '<div style="font-family:Arial,sans-serif;max-width:680px;margin:auto;color:#172235">'
        '<h2 style="font-size:22px">Deadlines coming up tomorrow</h2>'
        f'<p style="color:#657286">You have {len(rows)} pending research opportunity deadline(s) on '
        f'<strong>{deadline_date.isoformat()}</strong>.</p>'
        '<table style="width:100%;border-collapse:collapse">' + "".join(cards) + '</table>'
        '<p style="font-size:12px;color:#8792a3;margin-top:24px">This is your single daily research deadline digest. '
        'Update each application status in your Research Agent dashboard. Completed items are excluded.</p>'
        '</div>'
    )
    return subject, html_body, "\n".join(text_lines)


def send_deadline_digest(db: Session, profile: UserProfile, *, now: datetime | None = None) -> str:
    """Send one idempotent digest per user and local calendar day, if needed."""
    if not profile.deadline_email_enabled:
        return "disabled"
    recipients = json.loads(profile.deadline_emails_json or "[]")
    if not recipients:
        return "no_recipients"
    if not settings.resend_api_key or not settings.email_from:
        return "provider_not_configured"

    zone = ZoneInfo(profile.timezone_name or "UTC")
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    local_date = local_now.date()
    tomorrow = local_date + timedelta(days=1)
    rows = tomorrow_deadlines(db, profile.user_id, tomorrow)
    if not rows:
        return "no_deadlines"

    log = db.scalar(select(EmailDigestLog).where(
        EmailDigestLog.user_id == profile.user_id,
        EmailDigestLog.local_date == local_date,
    ))
    if log and log.status == "sent":
        return "already_sent"
    if log and log.status == "sending":
        attempted = log.attempted_at
        if attempted.tzinfo is None:
            attempted = attempted.replace(tzinfo=timezone.utc)
        if attempted > datetime.now(timezone.utc) - timedelta(minutes=30):
            return "already_in_progress"
    if log is None:
        log = EmailDigestLog(
            user_id=profile.user_id,
            local_date=local_date,
            status="sending",
            recipient_count=len(recipients),
            attempted_at=datetime.now(timezone.utc),
        )
        db.add(log)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return "already_in_progress"
    else:
        log.status = "sending"
        log.recipient_count = len(recipients)
        log.error_code = ""
        log.attempted_at = datetime.now(timezone.utc)
        db.commit()

    subject, body, plain_text = _render_digest(rows, tomorrow)
    payload = {
        "from": settings.email_from,
        # BCC all extra addresses so a user's recipients do not expose each
        # other's email addresses to one another.
        "to": [recipients[0]],
        "subject": subject,
        "html": body,
        "text": plain_text,
    }
    if len(recipients) > 1:
        payload["bcc"] = recipients[1:]
    idempotency_key = f"deadline-digest/{profile.user_id}/{local_date.isoformat()}"
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0), follow_redirects=False) as client:
            response = client.post(
                RESEND_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotency_key,
                },
                json=payload,
            )
        if response.status_code >= 400:
            log.status = "failed"
            log.error_code = f"http_{response.status_code}"
            db.commit()
            logger.warning("Deadline email failed for user %s (HTTP %s)", profile.user_id[:8], response.status_code)
            return "failed"
        data = response.json()
        log.status = "sent"
        log.provider_message_id = str(data.get("id", ""))[:120]
        log.sent_at = datetime.now(timezone.utc)
        log.error_code = ""
        db.commit()
        return "sent"
    except (httpx.HTTPError, ValueError) as exc:
        log.status = "failed"
        log.error_code = type(exc).__name__[:80]
        db.commit()
        logger.warning("Deadline email transport failed for user %s (%s)", profile.user_id[:8], type(exc).__name__)
        return "failed"


def refresh_application_statuses(db: Session, user_id: str, today: date) -> int:
    """Automatically mark a pending application as missed after its deadline.

    Submitted/waiting and completed records are never overwritten.
    """
    rows = db.scalars(select(UserOpportunity).where(
        UserOpportunity.user_id == user_id,
        UserOpportunity.application_status == "Pending",
        UserOpportunity.deadline.is_not(None),
        UserOpportunity.deadline < today,
    )).all()
    for row in rows:
        row.application_status = "Deadline Passed"
    db.commit()
    return len(rows)


def set_application_status(db: Session, user_id: str, opportunity: UserOpportunity, new_status: str) -> None:
    allowed = {"Pending", "Waiting", "Done", "Deadline Passed", "Accepted", "Rejected"}
    if new_status not in allowed:
        raise ValueError("Unsupported application status")
    if opportunity.user_id != user_id:
        raise LookupError("Opportunity not found")
    opportunity.application_status = new_status
    db.commit()

def send_recovery_digest(
    profile_snapshot: dict,
    opportunities: list[dict],
    *,
    reason: str,
    run_id: str,
) -> str:
    """Send an opt-in operational recovery email without a DB write.

    The profile snapshot must have been loaded before the DB became
    unavailable. This does not replace the normal deadline digest.
    """
    if not profile_snapshot.get("deadline_email_enabled"):
        return "disabled"
    recipients = profile_snapshot.get("deadline_emails") or []
    if not recipients or not settings.resend_api_key or not settings.email_from:
        return "not_configured"

    clean_rows = []
    for item in opportunities:
        clean_rows.append({
            "program_name": str(item.get("program_name", "")),
            "institution": str(item.get("institution", "")),
            "country": str(item.get("country", "")),
            "funding_type": str(item.get("funding_type", "Not disclosed")),
            "deadline": item.get("deadline"),
            "application_url": str(item.get("application_url", "")),
            "official_source_url": str(item.get("official_source_url", "")),
        })
    subject = f"Research agent recovery report — {len(clean_rows)} opportunities"
    text_lines = [
        "Your Research Opportunity Agent could not save this run to its database.",
        "New findings below were sent directly to your connected Notion workspace where possible.",
        "The agent will retry database persistence on a later run.",
        "",
    ]
    cards = []
    for item in clean_rows:
        deadline = item["deadline"] or "Not reliably identified"
        text_lines.extend([
            f'{item["program_name"]} — {item["institution"]} ({item["country"]})',
            f'Funding: {item["funding_type"]}; deadline: {deadline}',
            f'Application: {item["application_url"]}',
            f'Source: {item["official_source_url"]}',
            "",
        ])
        name = html.escape(item["program_name"])
        meta = html.escape(f'{item["institution"]} · {item["country"]} · {item["funding_type"]}')
        app_url = html.escape(item["application_url"], quote=True)
        source_url = html.escape(item["official_source_url"], quote=True)
        cards.append(
            '<tr><td style="padding:16px 0;border-bottom:1px solid #e5eaf1">'
            f'<strong>{name}</strong><div style="color:#657286">{meta}</div>'
            f'<a href="{app_url}">Application</a> · <a href="{source_url}">Official source</a>'
            f'<div>Deadline: {html.escape(str(deadline))}</div></td></tr>'
        )
    body = (
        '<div style="font-family:Arial,sans-serif;max-width:680px;margin:auto">'
        '<h2>Research agent recovery report</h2>'
        '<p>The database could not be updated during this run. This is a recovery copy, '
        'not confirmation that records were saved in your dashboard.</p>'
        '<p>Notion sync was attempted independently. The agent will retry persistence later.</p>'
        + ('<table style="width:100%;border-collapse:collapse">' + ''.join(cards) + '</table>'
           if cards else '<p>No new opportunity passed validation in this run.</p>')
        + '</div>'
    )
    # Stable key deduplicates the same user's same set of findings on the
    # same local day, including a worker retry after its lease expires.
    zone = ZoneInfo(profile_snapshot.get("timezone_name") or "UTC")
    local_date = datetime.now(timezone.utc).astimezone(zone).date().isoformat()
    source_fingerprint = hashlib.sha256(
        "\\n".join(sorted(item["official_source_url"] for item in clean_rows)).encode("utf-8")
    ).hexdigest()[:32]
    user_key = str(profile_snapshot.get("user_id", "unknown"))
    idempotency_key = f"recovery/{user_key}/{local_date}/{source_fingerprint}"
    payload = {
        "from": settings.email_from,
        "to": [recipients[0]],
        "subject": subject,
        "html": body,
        "text": "\\n".join(text_lines),
    }
    if len(recipients) > 1:
        payload["bcc"] = recipients[1:]
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0), follow_redirects=False) as client:
            response = client.post(
                RESEND_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotency_key,
                },
                json=payload,
            )
        if response.status_code >= 400:
            logger.warning("Recovery email failed (HTTP %s)", response.status_code)
            return "failed"
        return "sent"
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Recovery email transport failed (%s)", type(exc).__name__)
        return "failed"
