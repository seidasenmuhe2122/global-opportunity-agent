# Architecture

This project follows a modular Django architecture.

## Components

- `opportunity_agent` app: models, services, views, and tests
- Celery-ready tasks for scanning and matching
- SQLite default database for local development
- Telegram integration for admin and notifications
- Provider adapters for application automation
