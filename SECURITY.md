# Security notes and responsible deployment

## Implemented controls

- Passwords are hashed with Argon2id (not reversible encryption).
- Browser sessions use opaque random tokens; only HMAC-peppered token hashes are stored in PostgreSQL.
- Session cookies are HttpOnly, SameSite=Lax, path-scoped, and Secure in production. Production cookie names use the `__Host-` prefix.
- State-changing browser requests require a double-submit CSRF token, additionally bound to the authenticated session.
- Notion OAuth state is random, hashed in storage, short-lived, and single-use.
- Notion access and refresh tokens are encrypted using AES-256-GCM with the user ID as associated authenticated data.
- OAuth callback and Notion API errors never log tokens or raw credential payloads.
- The Notion connection is keyed by user ID; profile and opportunity API queries are scoped to that user.
- Notion authorization asks users to choose an accessible page; the app creates a new database there and does not modify an existing database.
- The app includes host validation, a restrictive Content Security Policy, HSTS in production, no CORS allowlist, no-store API responses, streamed and size-capped HTML downloads, public-address checks to reduce SSRF risk, and conservative request timeouts.
- User can disconnect Notion and request account deletion.

## Before public production launch

1. **Get an independent security review.** This code has not undergone a penetration test or formal audit. Do not advertise it as audited or perfectly secure.
2. Add verified-email sign-up, password reset with short-lived single-use tokens, and optional/passkey MFA. At present, the MVP has no email ownership verification or recovery flow.
3. Replace the in-memory request throttle with Redis-backed or edge/WAF rate limiting before running multiple web replicas. The current limiter is per-process and is only a baseline guardrail.
4. Add SQL migrations (Alembic) rather than relying on `create_all` for schema evolution. Enable encrypted automated database backups and test restores.
5. Configure provider-side secret management, secret rotation, access reviews, and alerts for unusual OAuth or account activity. Keep the encryption key separate from database backups.
6. Define and publish a privacy policy, terms, retention schedule, data-processing disclosures for Notion and the search provider, and a security contact. Confirm local legal obligations before accepting users globally.
7. Add structured audit events that never contain tokens, passwords, full academic profiles, or private Notion page contents.
8. Add a shared opportunity cache only after a separate privacy review. The current design searches separately per user and does not expose one user's profile or stored records to another.
9. Consider requiring email verification before Notion authorization. Add CAPTCHA or additional abuse controls if public registration is targeted.
10. Pin and update dependencies through Dependabot/Renovate, run `pip-audit` in CI, and review Notion API version changes before upgrading.

## Incident response

If the encryption key may have leaked, immediately stop the worker, rotate/revoke the Notion integration credentials where possible, invalidate all app sessions, and treat every stored Notion token as compromised. Changing `TOKEN_ENCRYPTION_KEY` without re-encrypting stored tokens makes existing tokens unreadable; plan a controlled migration or require users to reconnect. If a Notion token may be exposed, revoke the connection through Notion and reconnect after resolving the cause.

## Scope boundaries

The app does not request workspace-admin, billing, or comment permissions. Do not add capabilities unless a specific feature needs them. Search queries may disclose the user's broad research interests, degree level, current academic year, graduation year, intake, and preferred countries to the search provider. Never include the user's name, email, password, CGPA, citizenship, or Notion tokens in provider queries. Users should be told this before enabling discovery.
