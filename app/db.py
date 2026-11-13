from __future__ import annotations

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

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


_is_sqlite = settings.database_url.startswith("sqlite")
_engine_options = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}
if _is_sqlite:
    _engine_options["connect_args"] = {"check_same_thread": False, "timeout": 15}
else:
    # Vercel Functions and the GitHub Actions worker connect through Supabase's
    # transaction pooler. Avoid a process-local connection pool and disable
    # psycopg prepared statements, which the shared transaction pooler lacks.
    _engine_options["poolclass"] = NullPool
    _engine_options["connect_args"] = {"prepare_threshold": None}

engine = create_engine(_database_url(settings.database_url), **_engine_options)
if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_connection_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Create/update the small schema safely across concurrent serverless cold starts.

    Vercel can start several FastAPI instances at once. Calling metadata.create_all()
    without a cross-process lock lets two instances both observe a missing table
    and race to create its PostgreSQL composite type, producing pg_type duplicate
    key errors. A transaction-scoped advisory lock serializes schema setup without
    requiring a persistent database connection between function invocations.
    """
    # Import models so SQLAlchemy registers all tables.
    from app import models  # noqa: F401

    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            # Transaction-level lock works with Supabase's transaction pooler.
            # All app versions use this fixed key to serialize startup migrations.
            connection.execute(text("SELECT pg_advisory_xact_lock(734821930147::bigint)"))

        Base.metadata.create_all(bind=connection)

        inspector = inspect(connection)
        profile_columns = {column["name"] for column in inspector.get_columns("user_profiles")}
        opportunity_columns = {column["name"] for column in inspector.get_columns("user_opportunities")}

        # Lightweight forward-compatible migration for databases created before
        # exact semester and deadline-email tracking were introduced.
        profile_additions = {
            "current_semester": "INTEGER",
            "gender": "VARCHAR(40) NOT NULL DEFAULT ''",
            "deadline_email_enabled": "BOOLEAN NOT NULL DEFAULT FALSE",
            "deadline_emails_json": "TEXT NOT NULL DEFAULT '[]'",
            "manual_cooldown_until": "TIMESTAMP WITH TIME ZONE" if connection.dialect.name == "postgresql" else "DATETIME",
        }
        for name, ddl in profile_additions.items():
            if name not in profile_columns:
                connection.execute(text(f"ALTER TABLE user_profiles ADD COLUMN {name} {ddl}"))

        if "eligibility_details_json" not in opportunity_columns:
            connection.execute(text("ALTER TABLE user_opportunities ADD COLUMN eligibility_details_json TEXT NOT NULL DEFAULT '{}'"))

        if "application_status" not in opportunity_columns:
            connection.execute(text(
                "ALTER TABLE user_opportunities "
                "ADD COLUMN application_status VARCHAR(40) NOT NULL DEFAULT 'Pending'"
            ))
