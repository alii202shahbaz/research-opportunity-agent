from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.emailer import refresh_application_statuses
from app.models import User, UserOpportunity
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
