# Security

- Keep `.env`, API keys, Telegram tokens and Telethon sessions out of Git.
- Copy `.env.example` only as a template; it contains no real secrets. Use a unique random `DJANGO_SECRET_KEY` and `CREDENTIAL_ENCRYPTION_KEY` in a secret manager. `DEBUG` defaults off, production rejects wildcard hosts and requires PostgreSQL, Redis, private shared object storage, and the separate credential key.
- Keep Celery broker credentials, AWS credentials, AI provider keys, and Telegram tokens in the deployment secret store. Do not put secrets in `SystemSetting`, provider-adapter JSON, source notes, or task arguments.
- CV uploads accept valid PDF/DOCX files up to 10 MB. CV files are only downloadable by their owner or a user with profile-view permission. User uploads are not served through Django's public media URL; production object storage must block public access.
- Use HTTPS in production.
- Restrict `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`.
- Keep admin accounts separate from normal users.
- Website visibility and registration mode are separate settings. Configure both from **Admin Dashboard → Website settings** with a superuser or an account granted `manage_security_settings`.
- Private mode enforces server-side access checks. It also sends `X-Robots-Tag: noindex` and a robots meta tag, disallows crawling in `robots.txt`, and disables the sitemap. These SEO directives supplement, but do not replace, access control.
- Issue and revoke private links from **Django Admin → Opportunity agent → Private access tokens**. Link tokens are stored as SHA-256 hashes, displayed only once at creation, and never grant staff status or Django permissions.
- Production rate limits use the Redis cache configured by `REDIS_URL`; the local-memory cache is for development only and is not shared between web processes.
- Run `python manage.py setup_roles` after migrations to synchronize role permissions; grant Staff status only to trusted admin, operator, reviewer, or super-admin accounts.
- Users' profile, application, match, mailbox, and website-credential records are scoped to their account in application views. Admin review queues are permission-gated and reviewer querysets exclude records outside the review statuses.
- Telegram admin commands are allow-listed by chat ID.
- Public-source fetching rejects local/private IP targets to reduce SSRF risk.
- Auto-apply never bypasses CAPTCHA, MFA or access restrictions.
- Persistent Playwright browser profiles can contain authentication state; keep `PLAYWRIGHT_STATE_DIR` private, persistent only where required, and excluded from source control and static/media serving.
