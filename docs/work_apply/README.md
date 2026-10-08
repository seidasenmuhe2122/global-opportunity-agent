# Global Opportunity Agent

This project is a standalone Django-based system for discovering, matching, and managing global career, study, and opportunity applications.

## Quick setup

Windows:

```powershell
.\scripts\setup_windows.ps1
.\.venv\Scripts\Activate.ps1
python manage.py createsuperuser
.\scripts\start_windows.ps1
```

Then open <http://127.0.0.1:8000/> and sign in to the admin at <http://127.0.0.1:8000/admin/>.
The setup script creates the virtual environment, installs dependencies, initializes the database,
and creates the standard role groups. Add opportunities and their application links in the admin.
AI and Telegram features need their corresponding keys in `.env`; background tasks need Redis and
a Celery worker (see [Deployment](./docs/DEPLOYMENT.md)).

Docker Compose (development):

```powershell
Copy-Item .env.example .env
docker compose up
```

The Compose setup starts the development web server, Redis, and a Celery worker. Create an admin
account from another terminal with `docker compose exec web python manage.py createsuperuser`.

## Features

- Multi-user profiles with role-based access control
- Source management and source discovery
- Opportunity extraction, deduplication, and matching
- Application queue and automatic application safety checks
- Telegram notification support
- Audit logs and dashboards
- Celery-ready background tasks

## Configuration

The Windows setup script copies `.env.example` to `.env`. Fill in provider keys only for services
you use. The local default uses SQLite and Django's development server; do not use those defaults
for an internet-facing production deployment.

## Validation

```bash
python manage.py check
python manage.py makemigrations --check
python manage.py test
```
