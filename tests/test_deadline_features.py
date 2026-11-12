from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.emailer import refresh_application_statuses
from app.models import User, UserOpportunity, UserProfile
from app.schemas import ProfileIn


def test_deadline_recipient_list_is_validated_and_deduplicated():
    profile = ProfileIn(
        interests=["Computer Vision"],
        deadline_email_enabled=True,
        deadline_emails=["one@example.com", "TWO@example.org", "one@example.com"],
    )
    assert profile.deadline_emails == ["one@example.com", "two@example.org"]


def test_email_reminders_require_at_least_one_recipient():
    import pytest

    with pytest.raises(Exception):
        ProfileIn(interests=["Computer Vision"], deadline_email_enabled=True, deadline_emails=[])


def test_deadline_status_only_changes_pending_opportunities():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    try:
        user = User(email="status@example.com", password_hash="test")
        db.add(user)
        db.flush()
        rows = [
            UserOpportunity(user_id=user.id, canonical_url="https://a.edu/", program_name="Pending", application_url="https://a.edu/apply", official_source_url="https://a.edu/", deadline=date(2026, 11, 11), application_status="Pending"),
            UserOpportunity(user_id=user.id, canonical_url="https://b.edu/", program_name="Waiting", application_url="https://b.edu/apply", official_source_url="https://b.edu/", deadline=date(2026, 11, 11), application_status="Waiting"),
            UserOpportunity(user_id=user.id, canonical_url="https://c.edu/", program_name="Done", application_url="https://c.edu/apply", official_source_url="https://c.edu/", deadline=date(2026, 11, 11), application_status="Done"),
        ]
        db.add_all(rows)
        db.commit()
        changed = refresh_application_statuses(db, user.id, date(2026, 11, 12))
        assert changed == 1
        statuses = {row.program_name: row.application_status for row in db.scalars(select(UserOpportunity)).all()}
        assert statuses == {"Pending": "Deadline Passed", "Waiting": "Waiting", "Done": "Done"}
    finally:
        db.close()
        engine.dispose()



def test_one_combined_digest_per_user_per_local_day(monkeypatch):
    import app.emailer as emailer
    from app.models import EmailDigestLog
    from datetime import datetime, timezone

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    requests = []

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"id": "email_test_123"}

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, url, *, headers, json):
            requests.append((headers, json))
            return FakeResponse()

    monkeypatch.setattr(emailer.httpx, "Client", FakeClient)
    monkeypatch.setattr(emailer.settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(emailer.settings, "email_from", "Research Agent <deadlines@example.com>")
    try:
        user = User(email="digest@example.com", password_hash="test")
        user.profile = UserProfile(timezone_name="Asia/Karachi", deadline_email_enabled=True,
                                   deadline_emails_json='["one@example.com", "two@example.org"]')
        db.add(user)
        db.flush()
        db.add(UserOpportunity(user_id=user.id, canonical_url="https://example.edu/", program_name="Summer Lab",
                               institution="Example University", country="Switzerland", funding_type="Fully funded",
                               application_url="https://example.edu/apply", official_source_url="https://example.edu/",
                               deadline=date(2026, 11, 13), application_status="Pending"))
        db.commit()
        now = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
        assert emailer.send_deadline_digest(db, user.profile, now=now) == "sent"
        assert emailer.send_deadline_digest(db, user.profile, now=now) == "already_sent"
        assert len(requests) == 1
        headers, payload = requests[0]
        assert headers["Idempotency-Key"] == f"deadline-digest/{user.id}/2026-11-12"
        assert payload["to"] == ["one@example.com"]
        assert payload["bcc"] == ["two@example.org"]
        assert "Summer Lab" in payload["text"]
        assert db.query(EmailDigestLog).filter_by(user_id=user.id, local_date=date(2026, 11, 12), status="sent").count() == 1
    finally:
        db.close()
        engine.dispose()
