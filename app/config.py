from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from typing import List
from urllib.parse import urlparse

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    app_env: str = "development"
    app_name: str = "Research Opportunity Agent"
    app_base_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./research_agent.db"

    # Generate both keys independently. Never commit either key.
    app_secret_key: str = "development-only-change-me-32-characters"
    token_encryption_key: str = ""

    notion_oauth_client_id: str = ""
    notion_oauth_client_secret: str = ""
    notion_redirect_uri: str = "http://localhost:8000/oauth/notion/callback"
    notion_api_version: str = "2026-03-11"

    tavily_api_key: str = ""
    resend_api_key: str = ""
    email_from: str = ""
    search_results_per_query: int = 8
    max_search_queries_per_run: int = 5
    max_pages_per_run: int = 30
    worker_poll_seconds: int = 30
    job_lease_minutes: int = 60

    session_days: int = 14
    cookie_secure: bool = False
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    log_level: str = "INFO"

    @field_validator("app_secret_key")
    @classmethod
    def validate_secret(cls, value: str) -> str:
        if value.lower().startswith(("replace-with-", "changeme", "change-me", "your-")):
            raise ValueError("Replace the APP_SECRET_KEY placeholder with a randomly generated secret")
        if len(value) < 32 and value != "development-only-change-me-32-characters":
            raise ValueError("APP_SECRET_KEY must contain at least 32 characters")
        return value

    @field_validator("token_encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: str) -> str:
        if not value:
            return value
        if value.lower().startswith(("replace-with-", "changeme", "change-me", "your-")):
            raise ValueError("Replace the TOKEN_ENCRYPTION_KEY placeholder with a randomly generated secret")
        # Accept a canonical URL-safe base64 32-byte key, or a long randomly
        # generated secret from hosting platforms. security.py deterministically
        # derives an AES-256 key from the latter using SHA-256.
        try:
            decoded = base64.urlsafe_b64decode(value.encode("ascii"))
        except (binascii.Error, UnicodeEncodeError):
            decoded = b""
        if len(decoded) == 32 or len(value) >= 32:
            return value
        raise ValueError("TOKEN_ENCRYPTION_KEY must be a base64 32-byte key or a random secret of at least 32 characters")

    @property
    def host_list(self) -> List[str]:
        configured = [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]
        # The configured public origin is always an allowed host. This keeps
        # the non-serving worker's configuration valid without weakening the
        # web process's exact-host validation.
        app_host = urlparse(self.app_base_url).hostname
        if app_host and app_host not in configured:
            configured.append(app_host)
        return configured

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @model_validator(mode="after")
    def validate_production(self) -> "Settings":
        if self.is_production:
            if self.app_secret_key == "development-only-change-me-32-characters":
                raise ValueError("Set a unique APP_SECRET_KEY in production")
            if not self.token_encryption_key:
                raise ValueError("TOKEN_ENCRYPTION_KEY is required in production")
            if self.token_encryption_key == self.app_secret_key:
                raise ValueError("APP_SECRET_KEY and TOKEN_ENCRYPTION_KEY must be independent secrets")
            if not self.notion_oauth_client_id or not self.notion_oauth_client_secret:
                raise ValueError("Notion OAuth credentials are required in production")
            if not self.tavily_api_key:
                raise ValueError("TAVILY_API_KEY is required to enable discovery")
            if bool(self.resend_api_key) != bool(self.email_from):
                raise ValueError("Configure both RESEND_API_KEY and EMAIL_FROM, or leave both empty")
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be true in production")
            if not self.app_base_url.startswith("https://"):
                raise ValueError("APP_BASE_URL must use HTTPS in production")
            if not self.notion_redirect_uri.startswith("https://"):
                raise ValueError("NOTION_REDIRECT_URI must use HTTPS in production")
            base = urlparse(self.app_base_url)
            callback = urlparse(self.notion_redirect_uri)
            if (
                base.scheme != "https" or callback.scheme != "https"
                or base.hostname != callback.hostname or base.port != callback.port
                or base.path not in {"", "/"}
                or callback.path != "/oauth/notion/callback"
                or callback.query or callback.fragment
            ):
                raise ValueError("NOTION_REDIRECT_URI must be the HTTPS app origin plus /oauth/notion/callback")
            if "*" in self.host_list:
                raise ValueError("Wildcard ALLOWED_HOSTS is not allowed in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()