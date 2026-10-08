from __future__ import annotations

import html

from django.conf import settings
from django.core.cache import cache

try:
    import bleach
except ImportError:  # pragma: no cover
    bleach = None


def sanitize_text(value, allowed_tags=None, allowed_attributes=None, strip_comments=True, strip=True):
    if value is None:
        return value
    if not isinstance(value, str):
        value = str(value)
    if bleach is not None:
        return bleach.clean(
            value,
            tags=allowed_tags or [],
            attributes=allowed_attributes or {},
            strip_comments=strip_comments,
            strip=strip,
        )
    return html.escape(value, quote=True)


def _client_ip(request):
    forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip() or request.META.get('REMOTE_ADDR', 'unknown')
    return request.META.get('REMOTE_ADDR', 'unknown')


def _normalized_username(value):
    return str(value or '').strip().lower()


def _lockout_keys(request, username=None):
    username_key = _normalized_username(username)
    ip_key = f'auth_lockout:ip:{_client_ip(request)}'
    user_key = f'auth_lockout:user:{username_key}' if username_key else None
    return ip_key, user_key


def is_auth_locked(request, username=None):
    ip_key, user_key = _lockout_keys(request, username)
    if cache.get(f'{ip_key}:locked'):
        return True
    if user_key and cache.get(f'{user_key}:locked'):
        return True
    return False


def clear_auth_failures(request, username=None):
    ip_key, user_key = _lockout_keys(request, username)
    cache.delete(f'{ip_key}:count')
    cache.delete(f'{ip_key}:locked')
    if user_key:
        cache.delete(f'{user_key}:count')
        cache.delete(f'{user_key}:locked')


def record_failed_auth_attempt(request, username=None):
    if request is None:
        return False

    attempts = getattr(settings, 'AUTH_LOCKOUT_ATTEMPTS', 5)
    window = getattr(settings, 'AUTH_LOCKOUT_WINDOW', 300)
    duration = getattr(settings, 'AUTH_LOCKOUT_DURATION', 900)
    ip_key, user_key = _lockout_keys(request, username)

    ip_count = int(cache.get(f'{ip_key}:count', 0)) + 1
    cache.set(f'{ip_key}:count', ip_count, timeout=window)
    if ip_count >= attempts:
        cache.set(f'{ip_key}:locked', True, timeout=duration)

    if user_key:
        user_count = int(cache.get(f'{user_key}:count', 0)) + 1
        cache.set(f'{user_key}:count', user_count, timeout=window)
        if user_count >= attempts:
            cache.set(f'{user_key}:locked', True, timeout=duration)
            return True

    return ip_count >= attempts
