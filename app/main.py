from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import NotionConnection, OAuthState, User, UserOpportunity, UserProfile, UserSession
from app.notion import (
    NotionAPIError,
    begin_oauth,
    consume_oauth_state,
    create_opportunity_database,
    exchange_code,
    revoke_and_disconnect,
    search_accessible_pages,
)
from app.scheduler import enqueue_immediate_run, next_scheduled_run
from app.schemas import CreateDatabaseIn, CredentialsIn, OpportunityOut, ProfileIn
from app.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    clear_session_cookies,
    get_current_context,
    get_current_user,
    get_db,
    hash_password,
    issue_session,
    require_auth_csrf,
    require_csrf,
    set_csrf_cookie,
    set_session_cookies,
    sha256_hex,
    verify_password,
)

settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title=settings.app_name,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.host_list)

# Small per-process guardrail. For a horizontally scaled public SaaS, move this
# limiter to Redis or enforce equivalent limits at the edge/WAF.
_rate_lock = threading.Lock()
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


def enforce_rate_limit(request: Request, bucket: str, limit: int, window_seconds: int = 60) -> None:
    ip = request.client.host if request.client else "unknown"
    key = f"{bucket}:{ip}"
    now = time.monotonic()
    with _rate_lock:
        q = _rate_buckets[key]
        while q and q[0] <= now - window_seconds:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(status_code=429, detail="Too many requests. Try again shortly.")
        q.append(now)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'"
    )
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.on_event("startup")
def startup() -> None:
    init_db()
    logger.info("Application started in %s mode", settings.app_env)


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/api/csrf")
def csrf_token(request: Request, response: Response):
    existing = request.cookies.get(CSRF_COOKIE)
    if existing:
        return {"csrf_token": existing}
    return {"csrf_token": set_csrf_cookie(response)}


def _profile_payload(profile: UserProfile | None) -> dict | None:
    if not profile:
        return None
    return {
        "degree": profile.degree,
        "university": profile.university,
        "cgpa": profile.cgpa,
        "cgpa_scale": profile.cgpa_scale,
        "current_year": profile.current_year,
        "current_semester": profile.current_semester,
        "graduation_year": profile.graduation_year,
        "citizenship": profile.citizenship,
        "interests": json.loads(profile.interests_json or "[]"),
        "preferred_countries": json.loads(profile.preferred_countries_json or "[]"),
        "funding_preference": profile.funding_preference,
        "intake_year": profile.intake_year,
        "intake_season": profile.intake_season,
        "timezone_name": profile.timezone_name,
        "update_hour": profile.update_hour,
        "automation_enabled": profile.automation_enabled,
        "last_run_at": profile.last_run_at.isoformat() if profile.last_run_at else None,
        "last_run_status": profile.last_run_status,
        "last_run_summary": profile.last_run_summary,
        "next_run_at": profile.next_run_at.isoformat() if profile.next_run_at else None,
    }


@app.post("/api/register")
def register(
    payload: CredentialsIn,
    request: Request,
    response: Response,
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "register", 5)
    if not re.search(r"[a-z]", payload.password) or not re.search(r"[A-Z]", payload.password) or not re.search(r"\d", payload.password) or not re.search(r"[^A-Za-z0-9]", payload.password):
        raise HTTPException(status_code=422, detail="Password must include upper-case, lower-case and numeric characters")
    if db.scalar(select(User.id).where(User.email == payload.email)):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    user = User(email=payload.email, password_hash=hash_password(payload.password))
    user.profile = UserProfile()
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    issue_session(db, user, response)
    return {"ok": True, "email": user.email}


@app.post("/api/login")
def login(
    payload: CredentialsIn,
    request: Request,
    response: Response,
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "login", 10)
    user = db.scalar(select(User).where(User.email == payload.email))
    # Keep the outward error identical to avoid account enumeration.
    valid = bool(user and user.is_active and verify_password(payload.password, user.password_hash))
    if not valid:
        raise HTTPException(status_code=401, detail="Email or password is incorrect")
    db.query(UserSession).filter(UserSession.user_id == user.id).delete(synchronize_session=False)
    db.commit()
    issue_session(db, user, response)
    return {"ok": True, "email": user.email}


@app.post("/api/logout")
def logout(
    response: Response,
    context=Depends(require_auth_csrf),
    db: Session = Depends(get_db),
):
    user, session = context
    db.delete(session)
    db.commit()
    clear_session_cookies(response)
    return {"ok": True}


@app.get("/api/me")
def me(context=Depends(get_current_context)):
    user, _session = context
    db = SessionLocal()
    try:
        profile = db.get(UserProfile, user.id)
        notion = db.get(NotionConnection, user.id)
        return {
            "email": user.email,
            "profile": _profile_payload(profile),
            "notion": None if not notion else {
                "connected": True,
                "workspace_name": notion.workspace_name,
                "database_id": notion.database_id,
                "database_url": notion.database_url,
                "database_ready": bool(notion.database_id and notion.data_source_id),
            },
        }
    finally:
        db.close()


@app.put("/api/profile")
def save_profile(
    payload: ProfileIn,
    context=Depends(require_auth_csrf),
    db: Session = Depends(get_db),
):
    user, _session = context
    profile = db.get(UserProfile, user.id)
    if not profile:
        profile = UserProfile(user_id=user.id)
        db.add(profile)
    for field in ["degree", "university", "cgpa", "cgpa_scale", "current_year", "current_semester", "graduation_year", "citizenship",
                  "funding_preference", "intake_year", "intake_season", "timezone_name", "update_hour", "automation_enabled"]:
        setattr(profile, field, getattr(payload, field))
    profile.interests_json = json.dumps(payload.interests)
    profile.preferred_countries_json = json.dumps(payload.preferred_countries)
    profile.next_run_at = next_scheduled_run(profile) if payload.automation_enabled else None
    profile.lease_until = None
    db.commit()
    return {"ok": True, "profile": _profile_payload(profile)}


@app.post("/api/notion/connect")
def notion_connect(
    request: Request,
    context=Depends(require_auth_csrf),
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "notion_connect", 6)
    user, _session = context
    return {"authorization_url": begin_oauth(db, user)}


@app.get("/oauth/notion/callback", include_in_schema=False)
def notion_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return RedirectResponse(url="/?notion_error=authorization_cancelled", status_code=303)
    if not code or not state:
        return RedirectResponse(url="/?notion_error=missing_oauth_parameters", status_code=303)
    db = SessionLocal()
    try:
        user = consume_oauth_state(db, state)
        exchange_code(db, user, code)
        return RedirectResponse(url="/?notion=connected", status_code=303)
    except Exception as exc:
        logger.warning("Notion OAuth callback failed (%s)", type(exc).__name__)
        return RedirectResponse(url="/?notion_error=connection_failed", status_code=303)
    finally:
        db.close()


@app.get("/api/notion/pages")
def notion_pages(request: Request, q: str = "", context=Depends(get_current_context)):
    enforce_rate_limit(request, "notion_pages", 20)
    user, _session = context
    db = SessionLocal()
    try:
        notion = db.get(NotionConnection, user.id)
        if not notion:
            raise HTTPException(status_code=409, detail="Connect Notion first")
        return {"pages": search_accessible_pages(db, user.id, q)}
    except NotionAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    finally:
        db.close()


@app.post("/api/notion/database")
def notion_create_database(
    payload: CreateDatabaseIn,
    context=Depends(require_auth_csrf),
    db: Session = Depends(get_db),
):
    user, _session = context
    try:
        return {"ok": True, **create_opportunity_database(db, user.id, payload.parent_page_id)}
    except NotionAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/notion/disconnect")
def notion_disconnect(
    context=Depends(require_auth_csrf),
    db: Session = Depends(get_db),
):
    user, _session = context
    try:
        revoke_and_disconnect(db, user.id)
    except NotionAPIError as exc:
        # Local credentials are still removed, but the user should also revoke
        # the integration from Notion's own Connections settings.
        connection = db.get(NotionConnection, user.id)
        if connection:
            db.delete(connection)
            db.commit()
        return {"ok": True, "warning": str(exc)}
    return {"ok": True}


@app.post("/api/run-now")
def run_now(
    request: Request,
    context=Depends(require_auth_csrf),
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "run_now", 4, 3600)
    user, _session = context
    profile = db.get(UserProfile, user.id)
    if not profile or not profile.interests_json or profile.interests_json == "[]":
        raise HTTPException(status_code=409, detail="Complete your academic profile before starting discovery")
    enqueue_immediate_run(db, profile)
    return {"ok": True, "message": "Discovery queued. The worker will process it shortly."}


@app.get("/api/opportunities", response_model=list[OpportunityOut])
def opportunities(context=Depends(get_current_context)):
    user, _session = context
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(UserOpportunity).where(UserOpportunity.user_id == user.id)
            .order_by(UserOpportunity.deadline.is_(None), UserOpportunity.deadline.asc(), UserOpportunity.relevance_score.desc())
            .limit(500)
        ).all()
        return [
            OpportunityOut(
                id=row.id, program_name=row.program_name, institution=row.institution, country=row.country,
                domains=json.loads(row.domains_json or "[]"), funding_type=row.funding_type,
                funding_details=row.funding_details, stipend=row.stipend, tuition_waiver=row.tuition_waiver,
                travel_grant=row.travel_grant, housing=row.housing, deadline=row.deadline,
                deadline_note=row.deadline_note, application_url=row.application_url,
                official_source_url=row.official_source_url, eligibility=row.eligibility,
                verification_status=row.verification_status, relevance_score=row.relevance_score,
                intake_year=row.intake_year, intake_confirmed=row.intake_confirmed, opportunity_status=row.opportunity_status,
                first_seen_at=row.first_seen_at.isoformat(), last_verified_at=row.last_verified_at.isoformat(),
            ) for row in rows
        ]
    finally:
        db.close()


@app.delete("/api/account")
def delete_account(
    response: Response,
    context=Depends(require_auth_csrf),
    db: Session = Depends(get_db),
):
    user, _session = context
    revoke_warning = None
    if db.get(NotionConnection, user.id):
        try:
            revoke_and_disconnect(db, user.id)
        except NotionAPIError as exc:
            revoke_warning = str(exc)
            connection = db.get(NotionConnection, user.id)
            if connection:
                db.delete(connection)
                db.commit()
    db.delete(user)
    db.commit()
    clear_session_cookies(response)
    return {"ok": True, "warning": revoke_warning}


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s (%s)", request.url.path, type(exc).__name__)
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=500, content={"detail": "An internal error occurred"})
