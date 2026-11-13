from __future__ import annotations

import html
import json
import logging
import hashlib
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
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


def email_delivery_status() -> dict[str, str | bool]:
    provider = settings.active_email_provider
    sender = parseaddr(settings.email_from)[1] if settings.email_from else ""
    provider_label = "Gmail SMTP" if provider == "smtp" else "Resend" if provider == "resend" else "Not configured"
    if settings.email_configured:
        message = f"Sender credentials are configured for {provider_label}; live delivery is not verified until a test message succeeds."
    elif sender and provider == "smtp":
        message = "Email delivery is unavailable because the sender credentials are incomplete."
    elif sender and provider == "resend":
        message = "Add the Resend API key to enable delivery."
    else:
        message = "Email sender is not configured."
    return {
        "configured": settings.email_configured,
        "provider": provider_label,
        "sender": sender,
        "message": message,
    }


def _deliver_email(payload: dict, idempotency_key: str = "") -> tuple[str, str]:
    if not settings.email_configured:
        return "provider_not_configured", ""

    provider = settings.active_email_provider
    if provider == "smtp":
        recipients = list(dict.fromkeys(payload.get("to", []) + payload.get("bcc", [])))
        message = EmailMessage()
        message["From"] = payload["from"]
        message["To"] = ", ".join(payload.get("to", []))
        message["Subject"] = payload["subject"]
        message["Date"] = formatdate(localtime=False)
        if idempotency_key:
            stable_id = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
            message["Message-ID"] = f"<{stable_id}@gmail.com>"
        else:
            message["Message-ID"] = make_msgid(domain="gmail.com")
        message.set_content(payload.get("text", ""))
        message.add_alternative(payload.get("html", ""), subtype="html")

        host = settings.gmail_smtp_host.strip() or "smtp.gmail.com"
        configured_port = int(settings.gmail_smtp_port or 465)
        ports = list(dict.fromkeys([configured_port, 587 if configured_port == 465 else 465]))
        failures: list[tuple[int, str, str]] = []

        for port in ports:
            stage = "connect"
            try:
                if port == 465:
                    with smtplib.SMTP_SSL(
                        host, port, context=ssl.create_default_context(), timeout=10
                    ) as client:
                        stage = "ehlo"
                        client.ehlo()
                        stage = "authentication"
                        client.login(settings.gmail_user, settings.gmail_app_password.replace(" ", ""))
                        stage = "send"
                        refused = client.send_message(
                            message, from_addr=settings.gmail_user, to_addrs=recipients
                        )
                else:
                    with smtplib.SMTP(host, port, timeout=10) as client:
                        stage = "ehlo"
                        client.ehlo()
                        stage = "tls"
                        client.starttls(context=ssl.create_default_context())
                        client.ehlo()
                        stage = "authentication"
                        client.login(settings.gmail_user, settings.gmail_app_password.replace(" ", ""))
                        stage = "send"
                        refused = client.send_message(
                            message, from_addr=settings.gmail_user, to_addrs=recipients
                        )
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
                return "sent", f"gmail-smtp-{port}"
            except smtplib.SMTPAuthenticationError:
                raise
            except smtplib.SMTPRecipientsRefused:
                raise
            except (smtplib.SMTPException, OSError, ssl.SSLError, TimeoutError) as exc:
                failures.append((port, stage, type(exc).__name__))
                logger.warning(
                    "Gmail SMTP failed port=%s stage=%s error=%s",
                    port, stage, type(exc).__name__,
                )
        raise SMTPTransportUnavailable(failures)

    if provider == "resend":
        headers = {
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0), follow_redirects=False) as client:
            response = client.post(RESEND_ENDPOINT, headers=headers, json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"email_provider_http_{response.status_code}")
        try:
            provider_id = str(response.json().get("id", ""))[:120]
        except (ValueError, AttributeError):
            provider_id = ""
        return "sent", provider_id

    return "provider_not_configured", ""


class SMTPTransportUnavailable(smtplib.SMTPException):
    def __init__(self, failures: list[tuple[int, str, str]]):
        self.failures = failures
        summary = "; ".join(
            f"port {port}: {stage} ({error})" for port, stage, error in failures
        )
        super().__init__(summary)


def probe_email_transport() -> dict:
    """Check Gmail TCP/TLS/authentication stage without sending a message."""
    if not settings.email_configured or settings.active_email_provider != "smtp":
        return {"configured": False, "provider": settings.active_email_provider}
    host = settings.gmail_smtp_host.strip() or "smtp.gmail.com"
    configured_port = int(settings.gmail_smtp_port or 465)
    ports = list(dict.fromkeys([configured_port, 587 if configured_port == 465 else 465]))
    outcomes = []
    for port in ports:
        route = {"port": port, "tcp": False, "tls": False, "authenticated": False}
        stage = "connect"
        try:
            if port == 465:
                with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=8) as client:
                    route["tcp"] = True
                    stage = "ehlo"
                    client.ehlo()
                    route["tls"] = True
                    stage = "authentication"
                    client.login(settings.gmail_user, settings.gmail_app_password.replace(" ", ""))
                    route["authenticated"] = True
            else:
                with smtplib.SMTP(host, port, timeout=8) as client:
                    route["tcp"] = True
                    stage = "ehlo"
                    client.ehlo()
                    stage = "tls"
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                    route["tls"] = True
                    stage = "authentication"
                    client.login(settings.gmail_user, settings.gmail_app_password.replace(" ", ""))
                    route["authenticated"] = True
            outcomes.append(route)
            return {"configured": True, "host": host, "result": "authenticated", "routes": outcomes}
        except smtplib.SMTPAuthenticationError as exc:
            route["stage"] = "authentication"
            route["smtp_code"] = int(exc.smtp_code or 0)
            outcomes.append(route)
        except (smtplib.SMTPException, OSError, ssl.SSLError, TimeoutError) as exc:
            route["stage"] = stage
            route["error_type"] = type(exc).__name__
            outcomes.append(route)
    return {"configured": True, "host": host, "result": "failed", "routes": outcomes}

def send_test_email(recipient: str) -> tuple[str, str]:
    payload = {
        "from": settings.email_from,
        "to": [recipient],
        "subject": "OppFindAgent — test email",
        "text": "Your OppFindAgent email delivery is working.",
        "html": (
            '<div style="font-family:Arial,sans-serif;color:#172235">'
            '<h2>OppFindAgent</h2>'
            '<p>Your email sender is configured and working.</p>'
            '</div>'
        ),
    }
    try:
        status, _provider_id = _deliver_email(payload)
        return status, ""
    except smtplib.SMTPAuthenticationError as exc:
        detail = f"Gmail rejected authentication (SMTP {exc.smtp_code}). Use a Google App Password, not the normal account password."
        logger.warning("Test email authentication rejected by SMTP (code=%s)", exc.smtp_code)
        return "authentication_failed", detail
    except smtplib.SMTPRecipientsRefused:
        logger.warning("Test email recipient rejected by SMTP")
        return "recipient_rejected", "Gmail rejected the recipient address."
    except SMTPTransportUnavailable as exc:
        detail = str(exc)[:300]
        logger.warning("All Gmail SMTP routes failed (%s)", detail)
        return "connection_failed", detail
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, ssl.SSLError, TimeoutError, OSError) as exc:
        logger.warning("Test email SMTP connection failed (%s)", type(exc).__name__)
        return "connection_failed", f"{type(exc).__name__} while contacting Gmail."
    except (httpx.HTTPError, smtplib.SMTPException, ValueError, RuntimeError) as exc:
        logger.warning("Test email failed (%s)", type(exc).__name__)
        return "failed", f"Email provider error: {type(exc).__name__}."
