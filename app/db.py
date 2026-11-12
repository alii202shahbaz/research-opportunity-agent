from __future__ import annotations

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings

settings = get_settings()


def _database_url(url: str) -> str:
    # Render and several other hosts provide postgres:// or postgresql:// URLs.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("sqlite://"):
        return url
    if "sslmode=" not in url:
        url += "&sslmode=require" if "?" in url else "?sslmode=require"
    return url


class Base(DeclarativeBase):
    pass


engine = create_engine(
    _database_url(settings.database_url),
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args={"check_same_thread": False, "timeout": 15} if settings.database_url.startswith("sqlite") else {},
)
if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_connection_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    # Import models so SQLAlchemy registers all tables.
    from app import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    # Lightweight forward-compatible migration for databases created before
    # exact semester tracking was introduced. Existing profiles remain valid.
    inspector = inspect(engine)
    profile_columns = {column["name"] for column in inspector.get_columns("user_profiles")}
    opportunity_columns = {column["name"] for column in inspector.get_columns("user_opportunities")}
    with engine.begin() as connection:
        profile_additions = {
            "current_semester": "INTEGER",
            "deadline_email_enabled": "BOOLEAN NOT NULL DEFAULT FALSE",
            "deadline_emails_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, ddl in profile_additions.items():
            if name not in profile_columns:
                connection.execute(text(f"ALTER TABLE user_profiles ADD COLUMN {name} {ddl}"))
        if "application_status" not in opportunity_columns:
            connection.execute(text("ALTER TABLE user_opportunities ADD COLUMN application_status VARCHAR(40) NOT NULL DEFAULT 'Pending'"))