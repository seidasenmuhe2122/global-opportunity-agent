#!/usr/bin/env bash
set -e
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
python manage.py migrate --noinput
python manage.py setup_roles
python manage.py create_initial_superuser
python manage.py collectstatic --noinput