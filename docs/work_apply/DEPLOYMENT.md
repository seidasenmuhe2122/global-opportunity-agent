# Deployment and local operation

This document records the Work Apply deployment capabilities now integrated into the merged
project. Use the root [README](../../README.md) and [deployment guide](../DEPLOYMENT.md) for the
current shared configuration and email-backed credential workflows.

## Local Windows use

From the project root, run `.\scripts\setup_windows.ps1`, create an administrator, and start the
services with `.\scripts\start_windows.ps1`. The script starts Django, a Celery worker, Celery Beat
and the authorized Telegram admin bot.

## Background tasks

The merged scanner discovers and extracts public opportunity listings, then calculates user
matches. The queue is prepared separately from browser submission. Before submission, the
credential-aware provider checks the user profile, match threshold, daily limit and application
state; unsupported domains and CAPTCHA/MFA/security challenges go to Needs Review. The
15-minute Celery Beat cycle scans, matches and processes the application queue.

## Production

Use PostgreSQL through `DATABASE_URL`, Redis through `REDIS_URL`, a stable Django and credential
encryption key, and shared S3-compatible storage for CVs and generated application forms. The
Render blueprint starts separate web, Celery worker and Beat services. Never commit `.env`,
Playwright browser state, mailbox app passwords, or real API credentials.
