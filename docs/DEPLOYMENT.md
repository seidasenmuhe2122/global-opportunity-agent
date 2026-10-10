# Deployment

For Render or another production host use PostgreSQL through `DATABASE_URL`, Redis through `REDIS_URL`, and shared S3-compatible object storage through `AWS_STORAGE_BUCKET_NAME`. Configure `AWS_S3_REGION_NAME`, `AWS_S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` when required by the provider. The same storage configuration must be available to the web and Celery services so CVs and generated application forms are accessible to background workers. CV uploads are limited to valid PDF/DOCX files up to 10 MB. Keep the bucket private: block public access and do not expose its objects through a public media route; authorized CV downloads go through the authenticated application view. Set `DEBUG=0`, a strong secret, a stable `CREDENTIAL_ENCRYPTION_KEY`, `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`. `.env.example` contains variable names and safe placeholders only; set real values through an environment or secret manager.

## Configuration reference

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL DSN; required in production. |
| `DJANGO_SECRET_KEY` | Unique Django signing key; required in all environments. |
| `DEBUG`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `SECURE_SSL_REDIRECT` | Django environment and host/HTTPS security. |
| `REDIS_URL` | Redis endpoint for production cache, rate limits, and default Celery endpoints. Required in production; must be a valid `redis://` or `rediss://` URL. |
| `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` | Optional Redis-compatible Celery endpoint overrides; must use `redis://` or `rediss://` and otherwise default to `REDIS_URL`. |
| `AI_API_KEY`, `AI_BASE_URL`, `AI_MODEL`, `AI_PROVIDER`, `AI_TIMEOUT` | OpenAI-compatible AI provider. Provider-specific fallback keys are optional. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_CHAT_IDS`, `TELEGRAM_ADMIN_USER_IDS` | Telegram delivery token and admin allow-lists. |
| `SITE_URL`, `TIME_ZONE` | Canonical site address and scheduling/application timezone. |
| `PLAYWRIGHT_STATE_DIR` | Private root for persistent browser profiles. Keep outside source control and restrict filesystem access. |
| `BROWSER_HEADLESS`, `BROWSER_TIMEOUT_MS` | Default browser headless mode and navigation/action timeout. Provider-specific adapter values override these defaults. |
| `CREDENTIAL_ENCRYPTION_KEY` | Stable private encryption key for stored mailbox and website credentials; required in production. |
| `AWS_STORAGE_BUCKET_NAME`, `AWS_S3_REGION_NAME`, `AWS_S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Private shared object storage for uploads and worker-produced artifacts. |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION` | Optional authorized Telethon session for public Telegram-source collection. |

Celery defaults to JSON-only task/result serialization, one prefetched task per worker process, late acknowledgements, and requeue on worker loss. Beat intervals are defined in `global_opportunity_agent/settings.py`; run exactly one Beat instance per deployment to avoid duplicate scheduled jobs.
The production cache and sensitive-endpoint rate limits use `REDIS_URL`, shared by all web workers. Set it to the private/internal Redis connection URL provided by the hosting provider, including its `redis://` or `rediss://` scheme. The application validates the scheme and host at startup without printing the URL or its credentials. If Redis becomes unavailable after startup, rate-limited requests fail closed with HTTP 503 and the server logs the exception traceback; rate limiting is not bypassed. Local development uses an in-process cache.

Website visibility and registration mode can be changed independently from **Admin Dashboard → Website settings** by a superuser or another account granted `manage_security_settings`. Private visibility enforces access server-side and suppresses the sitemap, indexing directives, and public navigation. Private links are issued and revoked through **Django Admin → Opportunity agent → Private access tokens**; raw links are shown once and stored hashes are not displayed.

## Windows quick setup

Install Python 3.12+ and Docker Desktop, then run these from the project root in PowerShell:

```powershell
.\scripts\setup_windows.ps1
.\scripts\start_windows.ps1
```

Setup creates the virtual environment, installs requirements and Playwright Chromium, creates `.env` from `.env.example` if missing, generates local random signing/encryption keys only for blank settings, migrates the database, creates standard roles, and runs Django checks. Add `-CreateSuperuser` to setup to create the first admin account interactively. Start brings up the Compose Redis service and starts the web server, Celery worker, and Beat scheduler. If Redis is already available outside Docker, run `.\scripts\start_windows.ps1 -SkipRedisContainer`. AI and Telegram credentials are optional; set them in the ignored `.env` only when those integrations are wanted.

Run web:

```bash
gunicorn global_opportunity_agent.wsgi:application
```

Run worker:

```bash
celery -A global_opportunity_agent.celery worker -l info
```

Run scheduler:

```bash
celery -A global_opportunity_agent.celery beat -l info
```

The scheduler invokes the automation cycle every 15 minutes, expiration hourly, queue processing every 5 minutes and a daily health check.

`render.yaml` defines separate web, worker and Beat services. Configure the same `DATABASE_URL`, `REDIS_URL`, `DJANGO_SECRET_KEY`, `CREDENTIAL_ENCRYPTION_KEY`, and object-storage values for each. `build.sh` installs Chromium and its system dependencies for browser automation, applies migrations, creates role groups, and collects static files. Never put real credentials in the blueprint or `.env.example`.
