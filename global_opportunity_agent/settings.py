import os
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parent.parent
if load_dotenv is not None:
    load_dotenv(BASE_DIR / '.env')

DEBUG = os.environ.get('DEBUG', '0').strip().lower() in {'1', 'true', 'yes', 'on'}
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '').strip()
if not SECRET_KEY or SECRET_KEY.lower() in {
    'change-me',
    'dev-secret-key-not-for-production',
    'secret',
    'password',
}:
    raise ImproperlyConfigured(
        'Set DJANGO_SECRET_KEY to a unique, randomly generated value.'
    )
if not DEBUG and len(SECRET_KEY) < 50:
    raise ImproperlyConfigured(
        'DJANGO_SECRET_KEY must contain at least 50 characters when DEBUG=0.'
    )

default_hosts = '127.0.0.1,localhost' if DEBUG else ''
ALLOWED_HOSTS = [host.strip() for host in os.environ.get('ALLOWED_HOSTS', default_hosts).split(',') if host.strip()]
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured('Set ALLOWED_HOSTS to the hostnames served by this deployment.')
if not DEBUG and '*' in ALLOWED_HOSTS:
    raise ImproperlyConfigured('Wildcard ALLOWED_HOSTS is not permitted when DEBUG=0.')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'opportunity_agent',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'opportunity_agent.middleware.SecurityHeadersMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'opportunity_agent.middleware.SensitiveEndpointRateLimitMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'opportunity_agent.middleware.PrivateModeMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'global_opportunity_agent.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'autoescape': True,
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'opportunity_agent.context_processors.access_flags',
                'opportunity_agent.context_processors.admin_metrics',
            ],
        },
    },
]

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
if not DEBUG and not DATABASE_URL:
    raise ImproperlyConfigured('Set DATABASE_URL to a PostgreSQL database for production.')

if DATABASE_URL:
    import dj_database_url

    DATABASES = {'default': dj_database_url.parse(DATABASE_URL, conn_max_age=600, ssl_require=not DEBUG)}
else:
    DATABASES = {'default': {'ENGINE':'django.db.backends.sqlite3','NAME':BASE_DIR/'db.sqlite3'}}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'opportunity_agent.validators.NumericPasswordValidator', 'OPTIONS': {'min_numeric': 1}},
]
AUTH_LOCKOUT_ATTEMPTS = int(os.environ.get('AUTH_LOCKOUT_ATTEMPTS', '5'))
AUTH_LOCKOUT_WINDOW = int(os.environ.get('AUTH_LOCKOUT_WINDOW', '300'))
AUTH_LOCKOUT_DURATION = int(os.environ.get('AUTH_LOCKOUT_DURATION', '900'))

LANGUAGE_CODE = 'en-us'
TIME_ZONE = os.environ.get('TIME_ZONE', 'UTC')
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': (
            'whitenoise.storage.CompressedManifestStaticFilesStorage'
            if not DEBUG else 'whitenoise.storage.CompressedStaticFilesStorage'
        ),
    },
}
MEDIA_BUCKET = os.environ.get('AWS_STORAGE_BUCKET_NAME', '').strip()
if not DEBUG and not MEDIA_BUCKET:
    raise ImproperlyConfigured(
        'Set AWS_STORAGE_BUCKET_NAME to shared object storage for production uploads.'
    )
if not DEBUG and not os.environ.get('CREDENTIAL_ENCRYPTION_KEY', '').strip():
    raise ImproperlyConfigured(
        'Set CREDENTIAL_ENCRYPTION_KEY to a stable, private key for stored credentials.'
    )
if MEDIA_BUCKET:
    STORAGES['default'] = {
        'BACKEND': 'storages.backends.s3.S3Storage',
        'OPTIONS': {
            'bucket_name': MEDIA_BUCKET,
            'region_name': os.environ.get('AWS_S3_REGION_NAME') or None,
            'endpoint_url': os.environ.get('AWS_S3_ENDPOINT_URL') or None,
            'access_key': os.environ.get('AWS_ACCESS_KEY_ID') or None,
            'secret_key': os.environ.get('AWS_SECRET_ACCESS_KEY') or None,
            'default_acl': None,
            'querystring_auth': True,
            'file_overwrite': False,
        },
    }
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


def _validated_redis_url(name, value):
    supported_schemes = ('redis', 'rediss')
    try:
        parsed = urlsplit(value)
        valid_address = bool(parsed.hostname)
        if valid_address:
            parsed.port
    except ValueError:
        valid_address = False
        parsed = None
    if (
        parsed is None
        or parsed.scheme.lower() not in supported_schemes
        or not valid_address
    ):
        raise ImproperlyConfigured(
            f'{name} must be a valid Redis URL using redis:// or rediss://.'
        )
    return value


REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0').strip()
if not DEBUG and not REDIS_URL:
    raise ImproperlyConfigured(
        'Set REDIS_URL to a valid Redis URL using redis:// or rediss://.'
    )
if REDIS_URL:
    REDIS_URL = _validated_redis_url('REDIS_URL', REDIS_URL)

CACHES = {
    'default': {
        'BACKEND': (
            'django.core.cache.backends.redis.RedisCache'
            if not DEBUG
            else 'django.core.cache.backends.locmem.LocMemCache'
        ),
        'LOCATION': (
            REDIS_URL
            if not DEBUG
            else 'opportunity_hub_rate_limits'
        ),
    }
}
RATE_LIMIT_RULES = {
    'login': {'limit': 20, 'window': 300, 'methods': {'POST'}, 'paths': ('/accounts/login/', '/login/')},
    'signup': {'limit': 20, 'window': 900, 'methods': {'POST'}, 'paths': ('/accounts/signup/', '/signup/')},
    'profile': {'limit': 30, 'window': 300, 'methods': {'POST'}, 'paths': ('/profile/', '/credentials/', '/website-visibility/')},
    'search': {'limit': 60, 'window': 60, 'methods': {'GET'}, 'paths': ('/opportunities/',)},
    'assistant': {'limit': 30, 'window': 60, 'methods': {'POST'}, 'paths': ('/assistant/', '/assistant/new/', '/assistant/conversations/')},
    'private_access': {'limit': 15, 'window': 300, 'methods': {'GET', 'POST'}, 'paths': ('/private-access/',)},
}
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'request_errors': {
            'class': 'logging.StreamHandler',
            'level': 'ERROR',
        },
    },
    'loggers': {
        'django.request': {
            'handlers': ['request_errors'],
            'level': 'ERROR',
            'propagate': False,
        },
        'opportunity_agent.middleware': {
            'handlers': ['request_errors'],
            'level': 'ERROR',
            'propagate': False,
        },
    },
}
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_REFERRER_POLICY = 'same-origin'
SECURE_CROSS_ORIGIN_OPENER_POLICY = 'same-origin'
CONTENT_SECURITY_POLICY = {
    'default-src': ["'self'"],
    'script-src': ["'self'", "'unsafe-inline'", "'unsafe-eval'"],
    'style-src': ["'self'", "'unsafe-inline'"],
    'img-src': ["'self'", 'data:', 'blob:'],
    'font-src': ["'self'", 'data:'],
    'connect-src': ["'self'"],
    'object-src': ["'none'"],
    'base-uri': ["'self'"],
    'frame-ancestors': ["'none'"],
}
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/'
AUTHENTICATION_BACKENDS = [
    'opportunity_agent.authentication.EmailOrUsernameModelBackend',
    'django.contrib.auth.backends.ModelBackend',
]
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    CSRF_COOKIE_SECURE = True
    CSRF_COOKIE_SAMESITE = 'Lax'
    CSRF_COOKIE_HTTPONLY = False
    X_FRAME_OPTIONS = 'DENY'
    SECURE_SSL_REDIRECT = os.environ.get('SECURE_SSL_REDIRECT', '1').strip().lower() in {'1', 'true', 'yes', 'on'}
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

CREDENTIAL_ENCRYPTION_KEY = os.environ.get('CREDENTIAL_ENCRYPTION_KEY', '')
AI_API_KEY = os.environ.get('AI_API_KEY', '')
AI_BASE_URL = os.environ.get('AI_BASE_URL', 'https://api.openai.com/v1')
AI_MODEL = os.environ.get('AI_MODEL', 'gpt-4o-mini')
AI_PROVIDER = os.environ.get('AI_PROVIDER', 'openai-compatible')
AI_TIMEOUT = int(os.environ.get('AI_TIMEOUT', '30'))
AI_GOOGLE_API_KEY = os.environ.get('AI_GOOGLE_API_KEY', '')
AI_GROQ_API_KEY = os.environ.get('AI_GROQ_API_KEY', '')
AI_OPENROUTER_API_KEY = os.environ.get('AI_OPENROUTER_API_KEY', '')
AI_MISTRAL_API_KEY = os.environ.get('AI_MISTRAL_API_KEY', '')
AI_TOGETHER_API_KEY = os.environ.get('AI_TOGETHER_API_KEY', '')
AI_HUGGINGFACE_API_KEY = os.environ.get('AI_HUGGINGFACE_API_KEY', '')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_ADMIN_CHAT_IDS = os.environ.get('TELEGRAM_ADMIN_CHAT_IDS', '')
TELEGRAM_API_ID = os.environ.get('TELEGRAM_API_ID', '')
TELEGRAM_API_HASH = os.environ.get('TELEGRAM_API_HASH', '')
TELEGRAM_SESSION = os.environ.get('TELEGRAM_SESSION', 'opportunity_hub')
SITE_URL = os.environ.get('SITE_URL', 'http://localhost:8000')
CSRF_TRUSTED_ORIGINS = [x.strip() for x in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if x.strip()]
PLAYWRIGHT_STATE_DIR = os.environ.get('PLAYWRIGHT_STATE_DIR', str(BASE_DIR / 'playwright_state'))
BROWSER_HEADLESS = os.environ.get('BROWSER_HEADLESS', '1').strip().lower() not in {
    '0',
    'false',
    'no',
    'off',
}
BROWSER_TIMEOUT_MS = int(os.environ.get('BROWSER_TIMEOUT_MS', '30000'))
if BROWSER_TIMEOUT_MS < 1000:
    raise ImproperlyConfigured('BROWSER_TIMEOUT_MS must be at least 1000.')
try:
    SOURCE_CANDIDATE_BATCH_SIZE = int(os.environ.get('SOURCE_CANDIDATE_BATCH_SIZE', '50'))
except ValueError as exc:
    raise ImproperlyConfigured('SOURCE_CANDIDATE_BATCH_SIZE must be an integer between 1 and 200.') from exc
if not 1 <= SOURCE_CANDIDATE_BATCH_SIZE <= 200:
    raise ImproperlyConfigured('SOURCE_CANDIDATE_BATCH_SIZE must be an integer between 1 and 200.')

def _positive_int_setting(name, default, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ImproperlyConfigured(
            f'{name} must be an integer between 1 and {maximum}.'
        ) from exc
    if not 1 <= value <= maximum:
        raise ImproperlyConfigured(
            f'{name} must be an integer between 1 and {maximum}.'
        )
    return value

TELEGRAM_POST_RETRY_MAX_ATTEMPTS = _positive_int_setting(
    'TELEGRAM_POST_RETRY_MAX_ATTEMPTS', 5, 20,
)
TELEGRAM_POST_RETRY_BASE_SECONDS = _positive_int_setting(
    'TELEGRAM_POST_RETRY_BASE_SECONDS', 60, 86400,
)
TELEGRAM_POST_RETRY_MAX_SECONDS = _positive_int_setting(
    'TELEGRAM_POST_RETRY_MAX_SECONDS', 21600, 604800,
)
DISCOVERY_MAX_PER_CYCLE = _positive_int_setting(
    'DISCOVERY_MAX_PER_CYCLE', 20, 100,
)
DISCOVERY_PAGE_SIZE = _positive_int_setting(
    'DISCOVERY_PAGE_SIZE', 20, 100,
)
APPLICATION_WORKFLOW_BUDGET_SECONDS = _positive_int_setting(
    'APPLICATION_WORKFLOW_BUDGET_SECONDS', 900, 86400,
)

CELERY_BROKER_URL = (
    os.environ.get('CELERY_BROKER_URL', '').strip() or REDIS_URL
)
CELERY_RESULT_BACKEND = (
    os.environ.get('CELERY_RESULT_BACKEND', '').strip() or REDIS_URL
)
if CELERY_BROKER_URL:
    CELERY_BROKER_URL = _validated_redis_url(
        'CELERY_BROKER_URL',
        CELERY_BROKER_URL,
    )
if CELERY_RESULT_BACKEND:
    CELERY_RESULT_BACKEND = _validated_redis_url(
        'CELERY_RESULT_BACKEND',
        CELERY_RESULT_BACKEND,
    )
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_BEAT_SCHEDULE = {
    'scan-sources-every-15-minutes': {'task': 'opportunity_agent.tasks.automation_cycle_task', 'schedule': 900.0},
    'discover-public-sources-every-6-hours': {'task': 'opportunity_agent.tasks.discover_sources_task', 'schedule': 21600.0},
    'expire-deadlines-hourly': {'task': 'opportunity_agent.tasks.expire_deadlines_task', 'schedule': 3600.0},
    'process-application-queue-every-5-minutes': {'task': 'opportunity_agent.tasks.process_application_queue_task', 'schedule': 300.0},
    'health-check-daily': {'task': 'opportunity_agent.tasks.health_check_task', 'schedule': 86400.0},
}
