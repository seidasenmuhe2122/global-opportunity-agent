# Global Opportunity Agent — Complete

A standalone Django platform for multi-user global opportunity discovery, AI extraction, matching, safe application preparation, analytics and Telegram administration. It is independent of AFRIJOB.

## Documentation

- [Architecture and data model](docs/ARCHITECTURE.md)
- [Auto-apply and safety gates](docs/AUTO_APPLY.md)
- [Telegram sources, destinations, and admin bot](docs/TELEGRAM.md)
- [Source management and extraction](docs/SOURCES.md)
- [Deployment and environment configuration](docs/DEPLOYMENT.md)
- [Security controls](docs/SECURITY.md)
- [Provider adapter configuration](docs/PROVIDER_ADAPTERS.md)
- [Credentialed forms and uploads](docs/CREDENTIALS_AND_FORMS.md)

## Included
- Jobs, scholarships, internships, fellowships, grants, training, study, exchange, volunteer, research and competitions.
- Multi-user profiles, private PDF/DOCX CV upload (10 MB maximum), target countries/worldwide/remote preferences, languages, education, experience and certifications.
- Profile completeness is reported by personal information, education, skills, experience, CV/resume, languages, and preferences, with missing fields explained; incomplete required profile data blocks automatic submission.
- Country targeting accepts one or multiple comma-separated countries; worldwide, opportunity types, and on-site/hybrid/remote work-mode preferences can be combined. Matching and the public opportunity filters use the recorded location and work mode.
- Separate website/RSS/API source management and public Telegram channel sources, each with independent health, trust score, scan history and admin controls.
- AI provider fallback chain (configured provider, Google, Groq, OpenRouter, Mistral, Together, Hugging Face).
- Strict extraction: application URL is never replaced with a source URL; explicit email/phone/Telegram/address are preserved when no application URL exists.
- Deadlines are parsed from explicit deadline labels, date-only deadlines remain valid through the end of that date, and opportunities are labeled past/today/upcoming/missing. The scheduled expiry task marks expired open opportunities inactive, and application guards independently block expired submissions.
- Deduplication, deadline expiration, explainable multi-factor 0–100 match scores and Match records.
- Application queue, per-user daily submission limits (failed attempts count), audit history, attempts, cover letters and safe provider-adapter architecture.
- Applications store generated answers separately from audit history, enforce one application per user/opportunity in the database, and audit every saved status transition.
- CAPTCHA/MFA/access-control bypass is never attempted. Unsupported providers become Needs Review.
- Telegram notifications, multiple destinations and an authorized admin bot.
- Neon-style responsive admin command center and analytics: activity trends, application funnel, source health, country/type mix, match quality and automation runs.
- Celery background cycle every 15 minutes when Celery worker + beat are running; scheduled jobs also discover public sources, expire deadlines, process queued applications, and check source health.
- Audit log records account/profile changes, discovered and matched opportunities, application lifecycle transitions, source/admin changes, authorized Telegram commands, and notification outcomes.
- Transient public-source and Telegram HTTP failures receive up to three attempts with exponential delays; source, application, notification-destination, and automation-cycle failures are isolated and logged so later work can continue. Uncertain application submissions are routed to Needs Review rather than blindly resubmitted.
- Email-backed registration and login (username login remains supported for existing accounts).
- Per-user encrypted website credentials and mailbox app passwords, with optional email-verified website registration.
- Public opportunity browsing, application safety checks, source extraction, browser automation, PDF/DOCX form filling, and CAPTCHA/MFA manual-review fallbacks.

## Windows one-command setup

Prerequisites: Python 3.12+ and Docker Desktop (for local Redis and background workers). From the project root, run:

```powershell
.\scripts\setup_windows.ps1
.\scripts\start_windows.ps1
```

Setup creates `.venv`, installs Python requirements and Playwright Chromium, creates `.env` from `.env.example` when absent, generates local Django and credential-encryption keys if those entries are blank, applies migrations, creates standard roles, and runs Django checks. Existing non-empty `.env` secrets are preserved. Start launches Redis with Docker Compose when Docker is available, then opens Celery worker/Beat windows and runs the site at <http://127.0.0.1:8000/>. If Redis is already running elsewhere or you do not want Docker started, use `.\scripts\start_windows.ps1 -SkipRedisContainer`. The optional Telegram bot starts only when a bot token and admin chat allow-list are configured.

Create the first admin account during setup by running `.\scripts\setup_windows.ps1 -CreateSuperuser`, or afterward with `.\.venv\Scripts\python.exe manage.py createsuperuser`. Configure optional AI and Telegram integrations by adding their credentials to the ignored `.env` file; the site and core workflows can be started without them.

For advanced/manual Windows startup:
```powershell
.\.venv\Scripts\python.exe manage.py runserver
```
For repeatable local runs, keep `.env` private and never commit or share its secrets. `DEBUG` defaults to off, and production startup requires a strong `DJANGO_SECRET_KEY`, explicit `ALLOWED_HOSTS`, PostgreSQL `DATABASE_URL`, a private `AWS_STORAGE_BUCKET_NAME`, `CREDENTIAL_ENCRYPTION_KEY`, and `REDIS_URL`. Keep the object-storage bucket private; CV downloads go through the authenticated application endpoint, not public media URLs.

Credentials stored for supported website accounts and mailboxes are encrypted at rest using `CREDENTIAL_ENCRYPTION_KEY` (or the Django key in local development). Use a separate, stable production encryption key, store it only in the deployment secret manager, and back it up securely: changing or losing it makes existing encrypted credentials unreadable. API and Telegram tokens remain environment-provided and are not written to application settings or database records.
For automation install Redis, then run in separate terminals:
```powershell
celery -A global_opportunity_agent.celery worker -l info
celery -A global_opportunity_agent.celery beat -l info
```
Celery Beat must run as one scheduler process alongside at least one worker. `docker compose up` starts the web app, worker, Redis, and Beat. Source scanning performs extraction and deduplication in the background; matching, application preparation/submission, manual retry requests, deadline expiration, Telegram notifications, and health checks are dispatched as Celery work. Application retries require an explicit authorized request; uncertain submissions are not automatically retried.
Optional Telegram admin bot:
```powershell
python manage.py telegram_bot
```

## Managing users and permissions

Sign in to `/admin/` with a Django superuser to manage accounts under **Authentication and Authorization → Users**, profiles under **Opportunity agent → User profiles**, and role permissions under **Groups**. The `SUPER ADMIN` group receives permission to create, view, edit, and delete users, groups, and profiles when you run `python manage.py setup_roles`. Add trusted staff accounts to that group and enable **Staff status** to let them access the admin site. To grant permissions directly to one account, open that account from **Users → User permissions**, select the permissions, and save; group permissions remain available separately under **Groups**. Use **Active** on a user account to suspend or restore that account's access.

Run `python manage.py migrate` followed by `python manage.py setup_roles` after deployment or when role definitions change. Role setup synchronizes least-privilege permissions and assigns existing non-staff accounts to `USER`; new registrations are assigned that role automatically. Assign `ADMIN`, `OPERATOR`, and `REVIEWER` only to trusted accounts and enable **Staff status** for Django admin access.

- `SUPER ADMIN`: all platform, Django user/group/permission, configuration, automation, and log permissions.
- `ADMIN`: user/profile and core opportunity operations, applications/reviews, Telegram and automation configuration, and log viewing; cannot change role permissions or promote protected staff/superuser accounts.
- `OPERATOR`: source and opportunity management, source scanning, application queues, and failed-application retries.
- `REVIEWER`: access only to pending, Needs Review, failed, and rejected applications; approve/reject actions are audited and application records are read-only.
- `USER`: profile, CV, preferences, credentials, matched opportunities, and applications through owner-scoped account views; has no access to other users' private records.

Sensitive views and admin actions enforce permissions on the server. Reviewers only see the review queue. Operators can retry failed applications and scan sources; users can only manage their own data through the account views.

## Managing opportunity sources

In `/admin/`, trusted staff with source permissions can add and edit web, RSS, and API sources under **Opportunity agent → Sources**. Telegram collection channels have a separate model and admin page under **Opportunity agent → Telegram sources**. Their public channel URL, country, opportunity types, enabled state, trust, scan frequency, health, notes, and scan history are independent of normal sources and notification destinations. Telegram destinations under **Telegram destinations** are output-only notification groups and are never treated as source channels.

Use source actions to scan selected enabled web sources, discover candidate public sources, or enable/disable sources in bulk. Telegram sources have their own scan action. Automatic scans run only for enabled sources whose frequency is due; `Manual only` sources are scanned only when explicitly selected. Hourly, 6-hourly, daily, and weekly frequencies are supported. Celery Beat runs the automation cycle every 15 minutes (including due public Telegram channel scans) and public web-source discovery every 6 hours; discovered web sources are conservatively checked for relevant public content, assigned a cautious trust score, and enabled for weekly scanning. Web search is enabled by default; set `ENABLE_WEB_SOURCE_DISCOVERY=0` to limit discovery to the curated candidate list. Discovery skips inaccessible, redirected, oversized, irrelevant, or access-barrier pages. It does not attempt authentication or bypass CAPTCHA, MFA, anti-bot protection, or other access controls. Scheduled work requires the Celery worker, Redis, and Celery Beat described above. Web scans reject non-public/local source hosts and redirects; configure the final public HTTP(S) URL directly.

The **Import sources from CSV** button accepts UTF-8 CSV files up to 5 MB and 5,000 rows. Required headers are `name,url`. Optional headers are `source_type,country,opportunity_types,enabled,trust_score,scan_frequency,notes`; put multiple opportunity-type values in one field separated by `|` (for example `job|internship`). Supported frequency values are `manual`, `hourly`, `every_6_hours`, `daily`, and `weekly`. Existing URLs and repeated URLs in the file are skipped. The upload is validated before any rows are created, so an invalid row prevents the entire import.

## Opportunity extraction

Collected records retain the source content and extract structured opportunity details including organization, type, location, remote scope, responsibilities and qualifications, skills and languages, compensation and benefits, sponsorship, deadline, application and source URLs, and contact destinations (email, phone, Telegram, address and organization website). Public API fields and explicit contact details are extracted directly; AI-assisted contact destinations must themselves occur in the source text, in addition to having a supporting verbatim quote. Unknown facts remain blank (or null for yes/no facts such as worldwide remote and visa sponsorship). The source URL is preserved separately and is never substituted for an application URL. When no application URL was identified, the opportunity page clearly labels available contact details as “Contact to apply”; it does not infer missing contacts.

## AI provider and capabilities

Set `AI_API_KEY`, `AI_BASE_URL`, and `AI_MODEL` for any OpenAI-compatible chat-completions provider. `AI_PROVIDER=openai-compatible` selects that endpoint first; the optional provider-specific API keys enable fallback providers. `AI_TIMEOUT` sets the request timeout in seconds. The client exposes structured classification, information and requirement extraction, evidence-backed profile matching, matching-gap explanations, opportunity ranking, and application document/cover-letter generation. Structured extraction and generated personalization are checked against verbatim supplied text/profile facts; missing facts are not filled with guesses. Rule-based match scores remain authoritative for eligibility.

## Opportunity deduplication

All website/API and RSS/Telegram ingestion paths share the same duplicate detector. It canonicalizes application URLs by removing fragments and common tracking parameters, stores indexed URL/content fingerprints, and compares normalized titles, organization names, content tokens, and deadlines. Fuzzy matching uses conservative title/organization thresholds and a small synonym vocabulary; deadlines more than 14 days apart keep otherwise similar postings separate. When a duplicate is found, the existing opportunity is retained and only missing structured fields are filled from the new record. This is deterministic lexical semantic matching, not an external embedding service.

## Matching and threshold overrides

Match scores combine listed skills, education, experience, location, opportunity type, languages, work mode, sponsorship, qualifications, and compensation preferences. Each result includes the score, reasons, strong matches, missing or unconfirmed requirements, risks, eligibility, and a recommended action. Unknown opportunity facts are called out instead of being treated as matches. Automatic queueing requires the user's minimum score and matching location/work-mode preferences. A signed-in user can explicitly check the threshold-override option on an opportunity page to queue a below-threshold match; the action is recorded on the application and in the audit log. The override never bypasses location or work-mode restrictions, daily limits, CV/profile requirements, auto-apply settings, or provider safety checks.

Opportunity records are shared across users, while `Match` records and saved-state are unique to each user/opportunity pair. Matching uses each user's own skills and career-interest text, education, experience, location and preferences against structured fields and the opportunity's title and source description. The dashboard refreshes stale or missing matches for the signed-in user, including existing opportunities for newly registered users; source ingestion independently matches newly collected records to active users. Authenticated opportunity search ranks results by that user's score and supports country, type, work mode, qualification, deadline and minimum-score filters. An incomplete profile with no qualifying recommendations sees general active opportunities and is prompted to improve the profile. Saving an opportunity is private to the signed-in user and is stored on their match record; apply migration `0026_match_is_saved` before deploying this version.

Application-status notifications are sent through configured Telegram destinations. Although profiles store `notification_preferences`, per-user notifications for new opportunity matches are not currently implemented. The application does not claim those notifications are sent.

## Production
Use PostgreSQL via `DATABASE_URL`, Redis via `REDIS_URL`, and an S3-compatible shared media bucket via `AWS_STORAGE_BUCKET_NAME` (plus region/endpoint and access credentials as needed). Set `DEBUG=0`, a strong `DJANGO_SECRET_KEY`, stable `CREDENTIAL_ENCRYPTION_KEY`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, AI keys and Telegram configuration. The Render blueprint defines separate web, Celery worker and Celery Beat services; all three require the same database, Redis, Django secret, credential-encryption key and media-storage settings. Shared object storage is required so uploaded CVs and generated forms are available to Celery workers.

## Auto-apply safety
Auto-apply is intentionally provider-specific. A generic URL is not treated as permission to submit blindly. Provider adapters may be configured for known domains and selectors; CAPTCHA, MFA, login challenges, anti-bot checks and access restrictions are never bypassed. Unknown sites are routed to Needs Review with an explanation.

## Validation
```powershell
python manage.py check
python manage.py makemigrations --check
python manage.py test
```

## Auditing legacy application routes
The read-only-by-default `audit_application_routes` command checks saved source
URLs, reports listing-page candidates and fetches public detail pages to identify
explicit application routes. It does not use an AI provider. Review its output
before making any changes:

```powershell
python manage.py audit_application_routes
```

For a detailed read-only CSV review (including current/proposed route fields,
classification, and source evidence), choose a new report path:

```powershell
python manage.py audit_application_routes --report-csv .\application-route-review.csv
```

The report mode fetches saved public source pages for verification but does not
write to the database. Existing reports are never overwritten; choose another
path to rerun it. Historical or potentially expired routes are separated from
currently verified routes and are not proposed as new application routes.

Migration `0025` adds `application_methods` (a JSON list defaulting to `[]`) and
`application_instructions` (blank text); it does not infer or backfill either
field for existing opportunities. Older records can therefore have an empty
route list and instructions even when their legacy `application_method` or
application URL is populated. The audit command rechecks the saved source page
and proposes only explicit, verified evidence; it does not change records unless
`--apply` is provided.

Before applying repairs, back up the database using the deployment's normal
database backup procedure. For a portable Django data backup, create the target
directory first and run:

```powershell
New-Item -ItemType Directory -Force .\backups
python manage.py dumpdata opportunity_agent.Opportunity --indent 2 --output .\backups\opportunities-before-routes.json
python manage.py audit_application_routes --apply --backup-file .\backups\route-audit-changes.json --confirm "APPLY VERIFIED ROUTE REPAIRS"
```

`--apply` is explicit, requires a new command snapshot file and the exact
confirmation phrase. The command preserves source URLs and existing saved
application URLs, marks confirmed listing pages and historical or uncertain
routes for review, and never deletes records. A verified opportunity detail
page is not marked for review solely because its application method could not
be verified. Unreachable and ambiguous pages remain reported for manual review.
The command snapshot contains the pre-repair opportunity fields for records it
will update.

## Credentialed applications and custom forms
See `docs/CREDENTIALS_AND_FORMS.md`. Website credentials are encrypted at rest. Provider adapters can perform authorized login, form filling, CV upload and submission when selectors are explicitly configured. Fillable PDF application forms are supported; scanned/non-fillable forms are routed to Needs Review. CAPTCHA/MFA/anti-bot controls are never bypassed.


Authorized credentialed site workflows now support encrypted SiteCredential records, configurable login selectors, custom application field maps, fillable PDF application forms, generated application artifacts, CV uploads, confirmation checks, and Needs Review fallback for security challenges.

## Email-backed website registration

Each user can configure an authorized mailbox with a dedicated app password. Website credentials can reference that mailbox and enable automatic registration. The system can detect common registration fields, generate and encrypt a strong website password, submit registration, and follow a same-domain email activation link when the site uses ordinary email verification. Sign in at `/accounts/login/` with either the account email address or the legacy username; `/login/` remains an alias.

The mailbox app password is used only for the user's mailbox; it is never treated as the website password. Email MFA/2FA codes, CAPTCHA, anti-bot challenges, and other security controls are not bypassed. Such flows are moved to Needs Review.

Work Apply-only services and documentation are retained in the same project, including its browser application runner, source scanner, Telegram administration bot and persistent Playwright profile configuration. The email-aware application adapter remains the scheduled submission path because it enforces per-user credentials, adapter domain authorization and manual-review fallbacks.

The original standalone Work Apply `apply/` subproject is also preserved at the project root. The deployable merged project is the root Django project (`manage.py`); the Render configuration does not start the nested standalone project.
