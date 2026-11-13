from __future__ import annotations

from datetime import date
import re
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CredentialsIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(min_length=5, max_length=320)
    password: str = Field(min_length=12, max_length=128)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        value = value.lower()
        if "@" not in value or value.startswith("@") or value.endswith("@") or "." not in value.rsplit("@", 1)[-1]:
            raise ValueError("Enter a valid email address")
        return value


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    degree: str = Field(default="", max_length=120)
    university: str = Field(default="", max_length=240)
    cgpa: float | None = Field(default=None, ge=0, le=100)
    cgpa_scale: Literal[4.0, 5.0, 10.0, 100.0] = 4.0
    current_year: str = Field(default="", max_length=80)
    current_semester: int | None = Field(default=None, ge=1, le=16)
    graduation_year: int | None = Field(default=None, ge=2000, le=2100)
    citizenship: str = Field(default="", max_length=100)
    gender: Literal["", "Woman", "Man", "Non-binary", "Prefer not to say"] = ""
    interests: list[str] = Field(default_factory=list, max_length=15)
    preferred_countries: list[str] = Field(default_factory=list, max_length=30)
    funding_preference: Literal["funded_only", "funded_and_partial", "any"] = "funded_only"
    intake_year: int = Field(default=2027, ge=2026, le=2035)
    intake_season: Literal["Summer", "Winter", "Spring", "Autumn", "Any"] = "Summer"
    timezone_name: str = Field(default="UTC", max_length=80)
    update_hour: int = Field(default=8, ge=0, le=23)
    automation_enabled: bool = True
    deadline_email_enabled: bool = False
    deadline_emails: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("deadline_emails")
    @classmethod
    def validate_deadline_emails(cls, values: list[str]) -> list[str]:
        cleaned = []
        seen = set()
        pattern = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$", re.I)
        for item in values:
            email = item.strip().lower()
            if not email:
                continue
            if not pattern.fullmatch(email):
                raise ValueError(f"Invalid reminder email address: {email}")
            if email not in seen:
                cleaned.append(email)
                seen.add(email)
        if len(cleaned) > 10:
            raise ValueError("You can add at most 10 reminder recipients")
        return cleaned

    @model_validator(mode="after")
    def email_reminders_need_recipients(self) -> "ProfileIn":
        if self.deadline_email_enabled and not self.deadline_emails:
            raise ValueError("Add at least one reminder email address or turn reminders off")
        return self

    @field_validator("interests", "preferred_countries")
    @classmethod
    def clean_list(cls, values: list[str]) -> list[str]:
        cleaned = []
        seen = set()
        for item in values:
            item = item.strip()
            if item and item.lower() not in seen:
                cleaned.append(item[:100])
                seen.add(item.lower())
        return cleaned

    @field_validator("timezone_name")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Use a valid IANA timezone, such as Asia/Karachi") from exc
        return value

    @model_validator(mode="after")
    def require_interests(self) -> "ProfileIn":
        if not self.interests:
            raise ValueError("Add at least one research interest")
        if self.cgpa is not None and self.cgpa > self.cgpa_scale:
            raise ValueError("CGPA cannot exceed the selected grading scale")
        return self


class CreateDatabaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_page_id: str = Field(min_length=32, max_length=80)


class OpportunityOut(BaseModel):
    id: str
    program_name: str
    institution: str
    country: str
    domains: list[str]
    funding_type: str
    funding_details: str
    stipend: str
    tuition_waiver: bool | None
    travel_grant: bool | None
    housing: str
    deadline: date | None
    deadline_note: str
    application_url: str
    official_source_url: str
    eligibility: str
    verification_status: str
    relevance_score: float
    intake_year: int
    intake_confirmed: bool
    opportunity_status: str
    application_status: str
    first_seen_at: str
    last_verified_at: str