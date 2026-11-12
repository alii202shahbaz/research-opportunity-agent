from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Cookie, Depends, Header, HTTPException, Request, Response, status
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import User, UserSession

settings = get_settings()
password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)
SESSION_COOKIE = "__Host-session" if settings.cookie_secure else "session"
CSRF_COOKIE = "__Host-csrf" if settings.cookie_secure else "csrf_token"


def sha256_hex(value: str) -> str:
    return hmac.new(settings.app_secret_key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _encryption_key() -> bytes:
    configured = settings.token_encryption_key
    if not configured:
        raise RuntimeError("Set TOKEN_ENCRYPTION_KEY before connecting Notion")
    try:
        decoded = base64.urlsafe_b64decode(configured.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        decoded = b""
    if len(decoded) == 32:
        return decoded
    if len(configured) >= 32:
        # Render and other secret managers may generate opaque random strings
        # rather than base64. Derive a fixed AES-256 key with domain separation.
        return hashlib.sha256(b"research-opportunity-agent:notion-token:v1:" + configured.encode("utf-8")).digest()
    raise RuntimeError("TOKEN_ENCRYPTION_KEY must contain at least 32 random characters")


def encrypt_secret(plaintext: str, *, associated_data: str) -> str:
    key = _encryption_key()
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), associated_data.encode("utf-8"))
    return "v1." + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(envelope: str, *, associated_data: str) -> str:
    if not envelope.startswith("v1."):
        raise ValueError("Unsupported encrypted token format")
    key = _encryption_key()
    payload = base64.urlsafe_b64decode(envelope[3:].encode("ascii"))
    if len(payload) < 29:
        raise ValueError("Encrypted token payload is malformed")
    nonce, ciphertext = payload[:12], payload[12:]
    return AESGCM(key).decrypt(nonce, ciphertext, associated_data.encode("utf-8")).decode("utf-8")


def set_csrf_cookie(response: Response) -> str:
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=3600,
        secure=settings.cookie_secure,
        httponly=False,
        samesite="strict",
        path="/",
    )
    return token


def set_session_cookies(response: Response, session_token: str, csrf_token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=settings.session_days * 24 * 3600,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=settings.session_days * 24 * 3600,
        secure=settings.cookie_secure,
        httponly=False,
        samesite="strict",
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=settings.cookie_secure, httponly=False, samesite="strict")


def issue_session(db: Session, user: User, response: Response) -> None:
    session_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=sha256_hex(session_token),
            csrf_hash=sha256_hex(csrf_token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.session_days),
        )
    )
    db.commit()
    set_session_cookies(response, session_token, csrf_token)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_context(
    request: Request,
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
    db: Session = Depends(get_db),
) -> tuple[User, UserSession]:
    if not session_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
    session = db.scalar(select(UserSession).where(UserSession.token_hash == sha256_hex(session_token)))
    if session and session.expires_at.tzinfo is None:
        session.expires_at = session.expires_at.replace(tzinfo=timezone.utc)
    if not session or session.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    user = db.get(User, session.user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account unavailable")
    return user, session


def get_current_user(context: tuple[User, UserSession] = Depends(get_current_context)) -> User:
    return context[0]


def require_csrf(
    request: Request,
    csrf_header: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
    csrf_cookie: Optional[str] = Cookie(default=None, alias=CSRF_COOKIE),
) -> None:
    # Pre-authentication double-submit CSRF protection. Authenticated routes use
    # require_auth_csrf, which also binds the token to the stored session.
    if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def require_auth_csrf(
    context: tuple[User, UserSession] = Depends(get_current_context),
    csrf_header: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
    csrf_cookie: Optional[str] = Cookie(default=None, alias=CSRF_COOKIE),
) -> tuple[User, UserSession]:
    if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    if not hmac.compare_digest(sha256_hex(csrf_cookie), context[1].csrf_hash):
        raise HTTPException(status_code=403, detail="CSRF token is not bound to this session")
    return context
