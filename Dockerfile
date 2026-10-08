FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium
COPY . .
RUN python manage.py collectstatic --noinput
CMD ["gunicorn","global_opportunity_agent.wsgi:application","--bind","0.0.0.0:8000"]
