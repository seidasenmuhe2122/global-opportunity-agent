from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone

from .models import get_website_visibility


DEFAULT_RATE_LIMIT_RULES = {
    'login': {'limit': 20, 'window': 300, 'methods': {'POST'}, 'paths': ('/accounts/login/', '/login/')},
    'signup': {'limit': 20, 'window': 900, 'methods': {'POST'}, 'paths': ('/accounts/signup/', '/signup/')},
    'profile': {'limit': 30, 'window': 300, 'methods': {'POST'}, 'paths': ('/profile/', '/credentials/', '/website-visibility/')},
    'search': {'limit': 60, 'window': 60, 'methods': {'GET'}, 'paths': ('/opportunities/',)},
    'assistant': {'limit': 30, 'window': 60, 'methods': {'POST'}, 'paths': ('/assistant/', '/assistant/new/', '/assistant/conversations/')},
    'private_access': {'limit': 15, 'window': 300, 'methods': {'GET', 'POST'}, 'paths': ('/private-access/',)},
}


class SensitiveEndpointRateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _client_ip(request):
        forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
        if forwarded_for:
            return forwarded_for.split(',')[0].strip() or request.META.get('REMOTE_ADDR', 'unknown')
        return request.META.get('REMOTE_ADDR', 'unknown')

    @staticmethod
    def _request_matches_path(request, paths):
        path = request.path_info or request.path or '/'
        return any(path == candidate or path.startswith(candidate) for candidate in paths)

    def _rate_limit_key(self, rule_name, request, scope='ip'):
        if scope == 'user' and request.user.is_authenticated:
            return f'ratelimit:{rule_name}:user:{request.user.pk}'
        return f'ratelimit:{rule_name}:ip:{self._client_ip(request)}'

    def _rate_limit_response(self, request, window_seconds):
        message = 'Too many requests. Please wait a moment and retry.'
        if request.path.startswith('/assistant/') or request.headers.get('x-requested-with') == 'XMLHttpRequest':
            response = JsonResponse({'success': False, 'message': message}, status=429)
        else:
            response = HttpResponse(message, status=429)
        response['Retry-After'] = str(window_seconds)
        return response

    def __call__(self, request):
        rules = getattr(settings, 'RATE_LIMIT_RULES', DEFAULT_RATE_LIMIT_RULES)
        path = request.path_info or request.path or '/'
        for rule_name, rule in rules.items():
            methods = rule.get('methods', {'GET'})
            if request.method not in methods:
                continue
            if not self._request_matches_path(request, rule.get('paths', ())):
                continue
            if path.startswith('/admin/'):
                continue
            limit = int(rule.get('limit', 20))
            window = int(rule.get('window', 60))
            scope = rule.get('scope', 'ip')
            bucket = self._rate_limit_key(rule_name, request, scope)
            if cache.add(bucket, 1, timeout=window):
                count = 1
            else:
                try:
                    count = cache.incr(bucket)
                except ValueError:
                    if cache.add(bucket, 1, timeout=window):
                        count = 1
                    else:
                        count = cache.incr(bucket)
            if count > limit:
                return self._rate_limit_response(request, window)
        return self.get_response(request)


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault('X-Frame-Options', 'DENY')
        response.setdefault('X-Content-Type-Options', 'nosniff')
        response.setdefault('X-XSS-Protection', '1; mode=block')
        response.setdefault('Referrer-Policy', 'same-origin')
        if getattr(request, 'website_visibility', None) == 'private':
            response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
            response['Cache-Control'] = 'private, no-store'
        csp = getattr(settings, 'CONTENT_SECURITY_POLICY', {})
        if csp:
            directives = []
            for key, values in csp.items():
                if isinstance(values, str):
                    directives.append(f"{key} {values}")
                else:
                    directives.append(f"{key} {' '.join(values)}")
            response.setdefault('Content-Security-Policy', '; '.join(directives))
        return response


class PrivateModeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        visibility = get_website_visibility()
        request.website_visibility = visibility
        if visibility != 'private':
            return self.get_response(request)

        user = getattr(request, 'user', None)
        session = getattr(request, 'session', None)
        path = request.path_info or request.path or '/'
        allowed_prefixes = ('/admin/', '/static/', '/private-access/')

        if path in {'/robots.txt', '/favicon.ico', '/sitemap.xml'}:
            return self.get_response(request)
        if path.startswith(allowed_prefixes):
            return self.get_response(request)

        if user is not None and user.is_authenticated and user.is_active:
            return self.get_response(request)

        session_access_granted = False
        if session is not None and session.get('private_access_granted_until'):
            try:
                expires_at = datetime.fromisoformat(session['private_access_granted_until'])
                if timezone.now() < expires_at:
                    session_access_granted = True
            except (TypeError, ValueError):
                pass
            if not session_access_granted:
                session.pop('private_access_granted_until', None)
                session.pop('private_access_granted', None)
                session.pop('private_access_registration_allowed', None)

        if (
            (path.startswith('/accounts/signup/') or path == '/signup/')
            and session is not None
            and session_access_granted
            and not session.get('private_access_registration_allowed', False)
        ):
            return HttpResponse('Registration is not available through this private access link.', status=403)

        if session_access_granted:
            return self.get_response(request)

        return redirect(f'/private-access/?next={quote(request.get_full_path())}')
