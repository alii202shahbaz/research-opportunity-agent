# Research Opportunity Agent

A multi-user web application that collects an individual's academic profile, discovers relevant funded research opportunities, validates extracted fields, and syncs matching records into that user's own Notion workspace on a daily schedule.

**This is a security-conscious deployable MVP, not a claim of a completed independent security audit.** Before opening registration to the public, complete the production checklist in [SECURITY.md](SECURITY.md), add email verification/MFA and shared rate limiting, and run an external security review.

## What it does

- User registration and sign-in with Argon2id password hashing.
- User-specific academic profiles: degree, university, CGPA with selectable grading scale (4, 5, 10 or 100), academic year and exact current semester, graduation year, citizenship, research interests, target intake, countries, funding preference, timezone, and daily run time.
- Research discovery through the Brave Search API; pages are fetched with bounded size/timeouts and parsed with BeautifulSoup.
- Extracts funding signals, stipend clues, tuition/travel/housing indicators, application/deadline clues, source URL, and research domain.
- Screens explicit degree, academic-year, semester, graduation-year, citizenship and same-scale GPA rules; uncertain or missing data stays unknown/possibly eligible rather than being guessed.
- Notion OAuth: users authorize a public integration, select an accessible parent page, and create a dedicated structured database under that page. No user is asked to paste an API token.
- AES-256-GCM encryption for Notion access/refresh tokens at rest; HMAC-peppered hashes for app session tokens and OAuth state.
- HTTP-only session cookie, CSRF protection, strict security headers, host validation, account deletion, and Notion disconnect/revocation.
- A separate database-backed worker schedules each user at their own local time and sorts saved opportunities by known deadline.
- A Render Blueprint provides a web service, worker, and managed PostgreSQL database. GitHub stores the source; Render runs the backend.

## Architecture

```text
Browser
  | HTTPS, secure session cookie + CSRF header
  v
FastAPI web service ------------------------------+
  |                                               |
  | profile / session / OAuth state               | Notion OAuth + API
  v                                               v
PostgreSQL (tenant-scoped records) <---------- Notion workspace per user
  ^
  | SELECT ... FOR UPDATE SKIP LOCKED (job lease)
  |
Background worker
  |-- Brave Search API (queries contain broad interests, degree level, academic year and semester number, graduation year, and preferred countries)
  |-- bounded HTML fetch + BeautifulSoup extraction
  |-- Pydantic validation and profile matching
  +-- Notion page insert/update
```

Each profile, session, Notion connection, and saved opportunity is keyed by an internal user ID. API queries always scope records to the authenticated user. The current MVP performs search per user rather than sharing a public opportunity cache across users; this is simpler to reason about and avoids cross-user profile leakage.

## Local setup

Requirements: Python 3.12+, a Notion public OAuth integration, and a Brave Search API key.

1. Create an environment file:

   ```bash
   cp .env.example .env
   ```

2. Generate two independent secrets. Put the output into `.env` as `APP_SECRET_KEY` and `TOKEN_ENCRYPTION_KEY` respectively:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
   ```

3. Set `NOTION_OAUTH_CLIENT_ID`, `NOTION_OAUTH_CLIENT_SECRET`, `BRAVE_SEARCH_API_KEY`, and the local redirect URI `http://localhost:8000/oauth/notion/callback`.

4. In the Notion Developer Portal, create a **public OAuth integration**. Add the exact redirect URI. Enable only the content capabilities required by this application: read content, insert content, and update content. Do not enable unrelated comment, user, or workspace-admin capabilities.

5. Install and run:

   ```bash
   python -m venv .venv
   # Windows PowerShell: .venv\\Scripts\\Activate.ps1
   # macOS/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   uvicorn app.main:app --reload
   ```

6. In a second terminal, using the same `.env` and database:

   ```bash
   python -m app.worker
   ```

7. Open `http://localhost:8000`, create an account, save a profile, connect Notion, search for an accessible parent page, and create the research database. The first daily run is scheduled using the user's selected timezone and time. Use **Run discovery now** to queue a manual run.

SQLite is only for local single-user development. Use PostgreSQL for deployment and multi-user operation.

## Deploy from GitHub to Render

GitHub is the source-code repository; it is not the runtime for this application. GitHub Pages cannot run the FastAPI backend or an always-on scheduler. Render runs the public web service, private background worker, and PostgreSQL database.

1. Create a **private GitHub repository** (recommended while developing), then push this project to its `main` branch. Do not upload `.env`, OAuth secrets, encryption keys, database dumps, or real user data.
2. In Render, choose **New → Blueprint**, connect the GitHub repository, and select `render.yaml`.
3. The Blueprint creates two services and a managed Postgres database. Its shared crypto environment group generates independent `APP_SECRET_KEY` and `TOKEN_ENCRYPTION_KEY` values for the web service and worker. Render prompts for provider credentials separately for each service because `sync: false` values cannot be placed in an environment group; enter the same Notion OAuth client ID/secret, Brave Search key, callback URL, and app URL for both.
4. After Render assigns the web service hostname, set these values in both services:
   - `APP_BASE_URL=https://YOUR-SERVICE.onrender.com`
   - `NOTION_REDIRECT_URI=https://YOUR-SERVICE.onrender.com/oauth/notion/callback`
   For the web service, set `ALLOWED_HOSTS=YOUR-SERVICE.onrender.com`. The worker does not receive public web traffic.
5. Confirm the web service and worker share the same generated crypto environment group and the same Postgres `DATABASE_URL`.
6. In the Notion Developer Portal, add that exact production callback URL. In the OAuth integration settings, keep only the required content capabilities enabled.
7. Redeploy after setting environment variables. Confirm `/healthz` returns `{"status":"ok"}`, create a test account, complete OAuth, create a test database, and run a manual discovery.

The Blueprint uses Render's current plan identifiers for a continuously running web service, worker, and PostgreSQL. Review current pricing and plan availability before provisioning. You can use another host, but it must support persistent PostgreSQL, HTTPS, secure environment variables, a continuously running worker, and reliable backups. Render only prompts for `sync: false` values during the initial Blueprint creation; add any missing values manually in the Dashboard afterward.

## Environment variables

| Variable | Required | Description |
|---|---:|---|
| `APP_ENV` | Yes | `development` or `production` |
| `APP_BASE_URL` | Yes | Public HTTPS app origin in production |
| `DATABASE_URL` | Yes | PostgreSQL URL in production; SQLite only for local development |
| `APP_SECRET_KEY` | Yes | Random app secret used as an HMAC pepper |
| `TOKEN_ENCRYPTION_KEY` | Yes | URL-safe base64 of 32 random bytes, or a secret-manager-generated random string of at least 32 characters (SHA-256-derived AES-256 key) |
| `NOTION_OAUTH_CLIENT_ID` | Yes | Application owner's Notion OAuth client ID |
| `NOTION_OAUTH_CLIENT_SECRET` | Yes | Application owner's Notion OAuth client secret |
| `NOTION_REDIRECT_URI` | Yes | Exact callback URI configured in Notion |
| `BRAVE_SEARCH_API_KEY` | Yes for discovery | Search provider key; queries may disclose broad research interests, degree level, current academic year, graduation year, intake and preferred countries. CGPA, citizenship, identity, account credentials and Notion tokens are never included. |
| `COOKIE_SECURE` | Yes in production | Must be `true` over HTTPS |
| `ALLOWED_HOSTS` | Yes in production | Exact public hostname(s), comma-separated |
| `NOTION_API_VERSION` | No | Pinned to `2026-03-11` by default; review before upgrading |
| `SEARCH_RESULTS_PER_QUERY` | No | Search results per query (default 8) |
| `MAX_SEARCH_QUERIES_PER_RUN` | No | Query cap per profile run (default 10) |
| `MAX_PAGES_PER_RUN` | No | Page/Notion row cap per run (default 30) |
| `WORKER_POLL_SECONDS` | No | Worker polling cadence (default 30) |
| `JOB_LEASE_MINUTES` | No | Stuck-job lease timeout (default 60); each worker claims one user job at a time |
| `SESSION_DAYS` | No | Session lifetime (default 14) |

## Data quality and limitations

This version uses heuristic extraction, not a language model or a manually curated global research database. It can miss hidden deadlines, funding conditions in PDF documents, country-specific eligibility clauses, supervisor-nominated opportunities, and dynamic application portals. Search engine snippets and university pages may be stale. `Official Source` means the URL appears to be an institutional domain; it is not a guarantee that every extracted field has been independently confirmed. Every result retains its official source link, and unknown fields remain marked unknown. A person must review program terms before applying.

The `intake_confirmed` field distinguishes pages that explicitly mention the requested intake year from pages found through a targeted search but not independently confirming the cycle. The agent excludes clearly unpaid results only when the profile is set to funded-only; funding not disclosed remains visible for review instead of being mislabeled unpaid. New discoveries with deadlines already in the past are not inserted as active opportunities; previously tracked records are retained and marked expired.

## API routes

- `POST /api/register`, `POST /api/login`, `POST /api/logout`
- `GET /api/me`, `PUT /api/profile`
- `POST /api/notion/connect`, `GET /oauth/notion/callback`
- `GET /api/notion/pages`, `POST /api/notion/database`, `POST /api/notion/disconnect`
- `POST /api/run-now`, `GET /api/opportunities`, `DELETE /api/account`
- `GET /healthz`

## Tests

```bash
python -m pytest -q
```

GitHub Actions runs the test suite and a `pip-audit` dependency vulnerability check on pushes and pull requests to `main`. Dependabot checks Python and GitHub Actions dependencies weekly.