from __future__ import annotations

import base64
import secrets
import time
import logging
import json
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import NotionConnection, OAuthState, User, UserProfile
from app.security import decrypt_secret, encrypt_secret, sha256_hex

settings = get_settings()
logger = logging.getLogger(__name__)
NOTION_API = "https://api.notion.com/v1"


class NotionAPIError(RuntimeError):
    pass


def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(25.0, connect=10.0), follow_redirects=False)


def _basic_auth() -> str:
    raw = f"{settings.notion_oauth_client_id}:{settings.notion_oauth_client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _oauth_headers() -> dict[str, str]:
    return {
        "Authorization": _basic_auth(),
        "Content-Type": "application/json",
        "Notion-Version": settings.notion_api_version,
    }


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": settings.notion_api_version,
    }


def _request(method: str, path: str, *, token: str, json: dict | None = None) -> dict:
    with _client() as client:
        response = None
        for attempt in range(4):
            response = client.request(method, f"{NOTION_API}{path}", headers=_api_headers(token), json=json)
            if response.status_code not in {429, 500, 502, 503, 504, 529} or attempt == 3:
                break
            retry_after = response.headers.get("Retry-After", "")
            try:
                delay = min(8.0, max(0.5, float(retry_after)))
            except ValueError:
                delay = min(8.0, 0.75 * (2 ** attempt))
            time.sleep(delay)
    if response is None or response.status_code >= 400:
        # Never log access tokens or full API payloads. Notion's error message is
        # safe enough to surface only as a generic integration error.
        try:
            body = response.json() if response is not None else {}
            code = body.get("code", "notion_error")
        except ValueError:
            code = "notion_error"
        status = response.status_code if response is not None else 502
        raise NotionAPIError(f"Notion API request failed ({status}; {code})")
    return response.json()


def begin_oauth(db: Session, user: User) -> str:
    if not settings.notion_oauth_client_id or not settings.notion_oauth_client_secret:
        raise HTTPException(status_code=503, detail="Notion OAuth has not been configured by the application owner")
    state = secrets.token_urlsafe(32)
    db.add(
        OAuthState(
            user_id=user.id,
            state_hash=sha256_hex(state),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    )
    db.commit()
    params = {
        "client_id": settings.notion_oauth_client_id,
        "response_type": "code",
        "owner": "user",
        "redirect_uri": settings.notion_redirect_uri,
        "state": state,
    }
    return "https://api.notion.com/v1/oauth/authorize?" + urlencode(params)


def consume_oauth_state(db: Session, state: str) -> User:
    state_row = db.scalar(select(OAuthState).where(OAuthState.state_hash == sha256_hex(state)).with_for_update())
    now = datetime.now(timezone.utc)
    if state_row and state_row.expires_at.tzinfo is None:
        state_row.expires_at = state_row.expires_at.replace(tzinfo=timezone.utc)
    if not state_row or state_row.consumed_at is not None or state_row.expires_at <= now:
        raise HTTPException(status_code=400, detail="Invalid or expired Notion authorization state")
    state_row.consumed_at = now
    user = db.get(User, state_row.user_id)
    if not user:
        raise HTTPException(status_code=400, detail="Account no longer exists")
    db.commit()
    return user


def exchange_code(db: Session, user: User, code: str) -> NotionConnection:
    payload = {"grant_type": "authorization_code", "code": code, "redirect_uri": settings.notion_redirect_uri}
    with _client() as client:
        response = client.post(f"{NOTION_API}/oauth/token", headers=_oauth_headers(), json=payload)
    if response.status_code >= 400:
        # Notion rejects invalid/mismatched OAuth clients with HTTP 401.
        # Log only the provider error code; never log credentials or token bodies.
        try:
            provider_error = str(response.json().get("error", "unknown"))[:80]
        except (ValueError, AttributeError):
            provider_error = "unparseable_response"
        logger.warning(
            "Notion OAuth token exchange rejected (HTTP %s, provider_error=%s)",
            response.status_code,
            provider_error,
        )
        if response.status_code == 401:
            raise NotionAPIError(
                "Notion rejected the OAuth client credentials (401). "
                "Confirm that NOTION_OAUTH_CLIENT_ID and NOTION_OAUTH_CLIENT_SECRET "
                "belong to the same public connection, and that its redirect URI "
                "exactly matches NOTION_REDIRECT_URI."
            )
        raise NotionAPIError(
            f"Notion authorization exchange failed (HTTP {response.status_code}, {provider_error}). "
            "Check the OAuth redirect URI and integration settings."
        )
    data = response.json()
    access = data.get("access_token")
    workspace_id = data.get("workspace_id")
    if not access or not workspace_id:
        raise NotionAPIError("Notion did not return the required workspace authorization data")

    expires_at = None
    if data.get("expires_in"):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(data["expires_in"]))

    row = db.get(NotionConnection, user.id)
    if row is None:
        row = NotionConnection(user_id=user.id, encrypted_access_token="", workspace_id=workspace_id)
        db.add(row)
    row.encrypted_access_token = encrypt_secret(access, associated_data=user.id)
    refresh = data.get("refresh_token")
    row.encrypted_refresh_token = encrypt_secret(refresh, associated_data=user.id) if refresh else None
    row.access_token_expires_at = expires_at
    row.workspace_id = workspace_id
    row.workspace_name = (data.get("workspace_name") or "")[:240]
    row.bot_id = data.get("bot_id") or ""
    row.database_id = None
    row.data_source_id = None
    row.database_url = None
    row.connected_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return row


def _refresh_if_needed(db: Session, connection: NotionConnection) -> str:
    token = decrypt_secret(connection.encrypted_access_token, associated_data=connection.user_id)
    exp = connection.access_token_expires_at
    now = datetime.now(timezone.utc)
    if exp and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if not exp or exp > now + timedelta(minutes=3):
        return token
    if not connection.encrypted_refresh_token:
        return token
    refresh = decrypt_secret(connection.encrypted_refresh_token, associated_data=connection.user_id)
    payload = {"grant_type": "refresh_token", "refresh_token": refresh}
    with _client() as client:
        response = client.post(f"{NOTION_API}/oauth/token", headers=_oauth_headers(), json=payload)
    if response.status_code >= 400:
        raise NotionAPIError("Notion access token refresh failed. Reconnect your workspace.")
    data = response.json()
    new_access = data.get("access_token")
    if not new_access:
        raise NotionAPIError("Notion did not return a refreshed access token")
    connection.encrypted_access_token = encrypt_secret(new_access, associated_data=connection.user_id)
    new_refresh = data.get("refresh_token")
    if new_refresh:
        connection.encrypted_refresh_token = encrypt_secret(new_refresh, associated_data=connection.user_id)
    connection.access_token_expires_at = now + timedelta(seconds=int(data.get("expires_in", 3600)))
    db.commit()
    return new_access


def api_for_user(db: Session, user_id: str, method: str, path: str, *, json: dict | None = None) -> dict:
    connection = db.get(NotionConnection, user_id)
    if not connection:
        raise NotionAPIError("Connect a Notion workspace first")
    token = _refresh_if_needed(db, connection)
    try:
        return _request(method, path, token=token, json=json)
    except NotionAPIError as exc:
        if "401" in str(exc):
            raise NotionAPIError("Notion authorization is no longer valid. Reconnect your workspace.") from exc
        raise


def search_accessible_pages(db: Session, user_id: str, query: str = "") -> list[dict[str, str]]:
    payload: dict = {"page_size": 50, "filter": {"property": "object", "value": "page"}}
    if query.strip():
        payload["query"] = query.strip()[:100]
    data = api_for_user(db, user_id, "POST", "/search", json=payload)
    pages = []
    for item in data.get("results", []):
        title = "Untitled page"
        props = item.get("properties", {})
        for prop in props.values():
            if prop.get("type") == "title":
                title = "".join(part.get("plain_text", "") for part in prop.get("title", [])) or title
                break
        if title == "Untitled page":
            title = "".join(part.get("plain_text", "") for part in item.get("title", [])) or title
        pages.append({"id": item["id"], "title": title, "url": item.get("url", "")})
    return pages


def _notion_schema() -> dict:
    return {
        "Program Name": {"title": {}},
        "Institution": {"rich_text": {}},
        "Country / Location": {"rich_text": {}},
        "Academic Domain": {
            "multi_select": {"options": [
                {"name": "Computer Vision"},
                {"name": "Computer Architecture"},
                {"name": "Deepfake & AI/ML"},
                {"name": "Other"},
            ]}
        },
        "Funding Type": {"select": {"options": [
            {"name": "Fully Funded"}, {"name": "Partially Funded"},
            {"name": "Unpaid"}, {"name": "Not Disclosed"},
        ]}},
        "Funding Details": {"rich_text": {}},
        "Stipend": {"rich_text": {}},
        "Tuition Waiver": {"rich_text": {}},
        "Travel Grant": {"rich_text": {}},
        "Housing": {"rich_text": {}},
        "Application Deadline": {"date": {}},
        "Deadline Notes": {"rich_text": {}},
        "Application URL": {"url": {}},
        "Official Source URL": {"url": {}},
        "Eligibility": {"select": {"options": [
            {"name": "Eligible"}, {"name": "Possibly Eligible"},
            {"name": "Ineligible"}, {"name": "Unknown"},
        ]}},
        "Verification Status": {"select": {"options": [
            {"name": "Official Source"}, {"name": "Third-party Source"}, {"name": "Needs Review"},
        ]}},
        "Relevance Score": {"number": {"format": "number"}},
        "Intake Year": {"number": {"format": "number"}},
        "Intake Confirmed": {"checkbox": {}},
        "Opportunity Status": {"select": {"options": [
            {"name": "Open / Upcoming"}, {"name": "Expired"}, {"name": "Unknown"},
        ]}},
        "Program ID": {"rich_text": {}},
        "Last Verified": {"date": {}},
        "Application Status": {"select": {"options": [
            {"name": "Pending"}, {"name": "Waiting"}, {"name": "Done"},
            {"name": "Deadline Passed"}, {"name": "Accepted"}, {"name": "Rejected"},
        ]}},
    }


def create_opportunity_database(db: Session, user_id: str, parent_page_id: str) -> dict[str, str]:
    connection = db.get(NotionConnection, user_id)
    if connection and connection.database_id and connection.data_source_id:
        return {"database_id": connection.database_id, "data_source_id": connection.data_source_id, "url": connection.database_url or ""}
    # First prove the selected parent is accessible to this exact OAuth connection.
    api_for_user(db, user_id, "GET", f"/pages/{parent_page_id}")
    profile = db.get(UserProfile, user_id)
    intake_year = profile.intake_year if profile else 2027
    intake_season = profile.intake_season if profile else "Summer"
    database_title = f"{intake_year} {intake_season} Research Opportunities"
    data = api_for_user(
        db,
        user_id,
        "POST",
        "/databases",
        json={
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": database_title}}],
            "description": [{"type": "text", "text": {"content": "Automatically discovered research internships, fellowships and funded opportunities matched to your academic profile."}}],
            "initial_data_source": {"properties": _notion_schema()},
        },
    )
    data_sources = data.get("data_sources", [])
    if not data.get("id") or not data_sources or not data_sources[0].get("id"):
        raise NotionAPIError("Notion created the database but did not return a data source ID")
    connection = db.get(NotionConnection, user_id)
    connection.database_id = data["id"]
    connection.data_source_id = data_sources[0]["id"]
    connection.database_url = data.get("url")
    db.commit()
    # Create a deadline-ascending table view. The main database remains usable
    # even if the view endpoint is temporarily unavailable.
    try:
        api_for_user(db, user_id, "POST", "/views", json={
            "database_id": connection.database_id,
            "data_source_id": connection.data_source_id,
            "name": "Upcoming Deadlines",
            "type": "table",
            "sorts": [{"property": "Application Deadline", "direction": "ascending"}],
        })
    except NotionAPIError as exc:
        logger.warning("Could not create optional deadline view (%s)", str(exc))
    return {"database_id": connection.database_id, "data_source_id": connection.data_source_id, "url": connection.database_url or ""}


def _rt(value: str, limit: int = 1900) -> list[dict]:
    return [{"type": "text", "text": {"content": (value or "")[:limit]}}] if value else []


def _property_payload(opp) -> dict:
    domains = __import__("json").loads(opp.domains_json or "[]")
    funding_map = {
        "Fully funded": "Fully Funded",
        "Partially funded": "Partially Funded",
        "Unpaid": "Unpaid",
        "Not disclosed": "Not Disclosed",
    }
    verification = "Official Source" if opp.verification_status == "Official Source" else (
        "Third-party Source" if opp.verification_status == "Third-party Source" else "Needs Review"
    )
    status = "Expired" if opp.opportunity_status == "Expired" else (
        "Open / Upcoming" if opp.opportunity_status == "Open / Upcoming" else "Unknown"
    )
    properties = {
        "Program Name": {"title": _rt(opp.program_name)},
        "Institution": {"rich_text": _rt(opp.institution)},
        "Country / Location": {"rich_text": _rt(opp.country)},
        "Academic Domain": {"multi_select": [{"name": d} for d in domains if d in {"Computer Vision", "Computer Architecture", "Deepfake & AI/ML", "Other"}]},
        "Funding Type": {"select": {"name": funding_map.get(opp.funding_type, "Not Disclosed")}},
        "Funding Details": {"rich_text": _rt(opp.funding_details)},
        "Stipend": {"rich_text": _rt(opp.stipend)},
        "Tuition Waiver": {"rich_text": _rt("Provided" if opp.tuition_waiver is True else "Not provided" if opp.tuition_waiver is False else "Not disclosed")},
        "Travel Grant": {"rich_text": _rt("Provided" if opp.travel_grant is True else "Not provided" if opp.travel_grant is False else "Not disclosed")},
        "Housing": {"rich_text": _rt(opp.housing)},
        "Application URL": {"url": opp.application_url or None},
        "Official Source URL": {"url": opp.official_source_url or None},
        "Eligibility": {"select": {"name": opp.eligibility if opp.eligibility in {"Eligible", "Possibly Eligible", "Ineligible", "Unknown"} else "Unknown"}},
        "Verification Status": {"select": {"name": verification}},
        "Relevance Score": {"number": round(float(opp.relevance_score or 0), 3)},
        "Intake Year": {"number": int(opp.intake_year)},
        "Intake Confirmed": {"checkbox": bool(opp.intake_confirmed)},
        "Opportunity Status": {"select": {"name": status}},
        "Program ID": {"rich_text": _rt(opp.id)},
        "Application Status": {"select": {"name": opp.application_status if opp.application_status in {"Pending", "Waiting", "Done", "Deadline Passed", "Accepted", "Rejected"} else "Pending"}},
        "Last Verified": {"date": {"start": opp.last_verified_at.date().isoformat()}},
    }
    if opp.deadline:
        properties["Application Deadline"] = {"date": {"start": opp.deadline.isoformat()}}
    else:
        properties["Application Deadline"] = {"date": None}
    properties["Deadline Notes"] = {"rich_text": _rt(opp.deadline_note)}
    return properties


def upsert_opportunity_page(db: Session, user_id: str, opp) -> str:
    connection = db.get(NotionConnection, user_id)
    if not connection or not connection.data_source_id:
        raise NotionAPIError("Create a Notion opportunities database before syncing")
    properties = _property_payload(opp)
    if opp.notion_page_id:
        try:
            api_for_user(db, user_id, "PATCH", f"/pages/{opp.notion_page_id}", json={"properties": properties})
            return opp.notion_page_id
        except NotionAPIError as exc:
            if "404" not in str(exc):
                raise
            # User may have deleted the Notion row; recreate it below.
    created = api_for_user(
        db,
        user_id,
        "POST",
        "/pages",
        json={
            "parent": {"type": "data_source_id", "data_source_id": connection.data_source_id},
            "properties": properties,
        },
    )
    page_id = created.get("id")
    if not page_id:
        raise NotionAPIError("Notion did not return the new page ID")
    opp.notion_page_id = page_id
    db.commit()
    return page_id



@dataclass(frozen=True)
class NotionSyncContext:
    """A short-lived token snapshot used to sync while DB writes are unavailable."""

    user_id: str
    data_source_id: str
    access_token: str


def prepare_notion_sync(db: Session, user_id: str) -> NotionSyncContext | None:
    connection = db.get(NotionConnection, user_id)
    if not connection or not connection.data_source_id:
        return None
    # Resolve/refresh once while the database is available. Subsequent page
    # requests use this in-memory token and do not need another DB lookup.
    token = _refresh_if_needed(db, connection)
    return NotionSyncContext(
        user_id=user_id,
        data_source_id=connection.data_source_id,
        access_token=token,
    )


def sync_discovered_candidate(context: NotionSyncContext, candidate) -> str | None:
    """Upsert a fresh discovery directly to Notion before attempting DB writes.

    The official source URL is used as the deduplication key, so a DB write
    outage does not cause duplicate Notion rows on the next run.
    """
    source_url = str(candidate.official_source_url)
    queried = _request(
        "POST",
        f"/data_sources/{context.data_source_id}/query",
        token=context.access_token,
        json={
            "filter": {
                "property": "Official Source URL",
                "url": {"equals": source_url},
            },
            "page_size": 1,
        },
    )
    existing = (queried.get("results") or [None])[0]
    if existing is None and candidate.opportunity_status == "Expired":
        return None

    existing_properties = (existing or {}).get("properties", {})
    existing_status = (
        (existing_properties.get("Application Status") or {})
        .get("select", {}).get("name", "Pending")
    )
    stable_program_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"{context.user_id}:{source_url}"
    ))
    payload_object = SimpleNamespace(
        id=stable_program_id,
        program_name=candidate.program_name,
        institution=candidate.institution,
        country=candidate.country,
        domains_json=json.dumps(candidate.domains),
        funding_type=candidate.funding_type,
        funding_details=candidate.funding_details,
        stipend=candidate.stipend,
        tuition_waiver=candidate.tuition_waiver,
        travel_grant=candidate.travel_grant,
        housing=candidate.housing,
        deadline=candidate.deadline,
        deadline_note=candidate.deadline_note,
        application_url=str(candidate.application_url),
        official_source_url=source_url,
        eligibility=candidate.eligibility,
        verification_status=candidate.verification_status,
        relevance_score=candidate.relevance_score,
        intake_year=candidate.intake_year,
        intake_confirmed=candidate.intake_confirmed,
        opportunity_status=candidate.opportunity_status,
        application_status=existing_status,
        last_verified_at=datetime.now(timezone.utc),
    )
    properties = _property_payload(payload_object)
    if existing:
        # Application status is user-maintained. Never overwrite it during a
        # recovery sync, and keep the existing Program ID link intact.
        properties.pop("Application Status", None)
        properties.pop("Program ID", None)
        page_id = existing.get("id")
        if not page_id:
            raise NotionAPIError("Notion returned a row without a page ID")
        _request(
            "PATCH", f"/pages/{page_id}", token=context.access_token,
            json={"properties": properties},
        )
        return page_id

    created = _request(
        "POST",
        "/pages",
        token=context.access_token,
        json={
            "parent": {"type": "data_source_id", "data_source_id": context.data_source_id},
            "properties": properties,
        },
    )
    page_id = created.get("id")
    if not page_id:
        raise NotionAPIError("Notion did not return the new page ID")
    return page_id


def trash_notion_page(context: NotionSyncContext, page_id: str) -> None:
    """Move a retained Notion page to Trash using the current API field."""
    _request(
        "PATCH", f"/pages/{page_id}", token=context.access_token,
        json={"in_trash": True},
    )

def revoke_and_disconnect(db: Session, user_id: str) -> None:
    connection = db.get(NotionConnection, user_id)
    if not connection:
        return
    token = decrypt_secret(connection.encrypted_access_token, associated_data=user_id)
    with _client() as client:
        response = client.post(
            f"{NOTION_API}/oauth/revoke",
            headers=_oauth_headers(),
            json={"token": token},
        )
    if response.status_code >= 500:
        raise NotionAPIError("Notion could not revoke the token right now. Try again.")
    db.delete(connection)
    db.commit()