from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.db.models import Avg
from django.utils import timezone


def access_flags(request):
    user = request.user
    is_platform_admin = user.is_authenticated and (
        user.is_staff or user.is_superuser or user.groups.filter(name__in=['SUPER ADMIN', 'ADMIN']).exists()
    )
    visibility = getattr(request, 'website_visibility', None)
    if visibility is None:
        from .models import get_website_visibility

        visibility = get_website_visibility()
    session_grant_active = False
    session = getattr(request, 'session', None)
    if session is not None:
        try:
            expires_at = datetime.fromisoformat(
                session.get('private_access_granted_until', '')
            )
            session_grant_active = timezone.now() < expires_at
        except (TypeError, ValueError):
            session_grant_active = False
    can_browse_website = visibility != 'private' or (
        user.is_authenticated and user.is_active
    ) or session_grant_active
    return {
        'is_platform_admin': is_platform_admin,
        'website_is_private': visibility == 'private',
        'can_browse_website': can_browse_website,
    }


def admin_metrics(request):
    """Read-only operational statistics for the Django admin index."""
    if (
        not getattr(request, 'user', None)
        or not request.user.is_authenticated
        or not request.user.is_staff
        or getattr(getattr(request, 'resolver_match', None), 'url_name', None) != 'index'
    ):
        return {}

    from .models import (
        Application,
        AutomationRun,
        Match,
        Opportunity,
        Source,
        TelegramSource,
    )

    User = get_user_model()
    today = timezone.localdate()
    week_start = today - timedelta(days=6)
    applications = Application.objects.all()
    opportunities = Opportunity.objects.all()
    all_sources = Source.objects.all()
    telegram_sources = TelegramSource.objects.all()

    latest_run = AutomationRun.objects.order_by('-started_at', '-pk').first()
    last_successful_run = AutomationRun.objects.filter(
        status='success',
    ).order_by('-started_at', '-pk').first()
    running = AutomationRun.objects.filter(status='running').exists()
    if running:
        automation_status = 'Running'
    elif latest_run is None:
        automation_status = 'Not run'
    else:
        automation_status = latest_run.get_status_display()

    source_errors = (
        all_sources.filter(status='error').count()
        + telegram_sources.filter(status='error').count()
    )
    return {
        'admin_metrics': {
            'users': User.objects.count(),
            'active_users': User.objects.filter(is_active=True).count(),
            'sources': all_sources.count() + telegram_sources.count(),
            'active_sources': (
                all_sources.filter(enabled=True, status='active').count()
                + telegram_sources.filter(enabled=True, status='active').count()
            ),
            'web_sources': all_sources.count(),
            'active_web_sources': all_sources.filter(
                enabled=True,
                status='active',
            ).count(),
            'telegram_sources': telegram_sources.count(),
            'active_telegram_sources': telegram_sources.filter(
                enabled=True,
                status='active',
            ).count(),
            'opportunities_today': opportunities.filter(created_at__date=today).count(),
            'opportunities_week': opportunities.filter(
                created_at__date__gte=week_start,
                created_at__date__lte=today,
            ).count(),
            'applications_today': applications.filter(created_at__date=today).count(),
            'submitted': applications.filter(status='submitted').count(),
            'rejected': applications.filter(status='rejected').count(),
            'failed': applications.filter(status='failed').count(),
            'needs_review': applications.filter(status='needs_review').count(),
            'automation_status': automation_status,
            'latest_run': latest_run,
            'last_successful_run': last_successful_run,
            'source_errors': source_errors,
            'avg_match': round(Match.objects.aggregate(v=Avg('score')).get('v') or 0, 1),
        }
    }
