from __future__ import annotations

import json
import logging
import re
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import (
    Application,
    ApplicationAttempt,
    AuditLog,
    AutomationRun,
    Match,
    Opportunity,
    Source,
    UserProfile,
)
from .audit import record_audit_event
from .matching import compute_match_score

logger = logging.getLogger(__name__)

OPPORTUNITY_TYPES = {value for value, _ in Opportunity.OPPORTUNITY_TYPES}
WORK_MODES = {value for value, _ in Opportunity.WORK_MODE_CHOICES}
MAX_DAILY_APPLICATION_LIMIT = 100
MAX_BULK_APPLICATIONS = 20
CONFIRMATION_ACTIONS = {
    'retry_application',
    'cancel_application',
    'set_auto_apply',
    'submit_application',
    'bulk_apply',
    'update_profile',
}


class ToolInputError(ValueError):
    pass


def _result(
    action: str,
    success: bool,
    status: str,
    message: str,
    **data: Any,
) -> dict[str, Any]:
    return {
        'success': success,
        'action': action,
        'status': status,
        'message': message,
        'data': data,
    }


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolInputError(f'{name} must be an integer.')
    if not minimum <= value <= maximum:
        raise ToolInputError(f'{name} must be between {minimum} and {maximum}.')
    return value


def _string(value: Any, name: str, maximum: int = 255) -> str:
    if not isinstance(value, str):
        raise ToolInputError(f'{name} must be text.')
    value = value.strip()
    if not value or len(value) > maximum:
        raise ToolInputError(f'{name} must contain 1 to {maximum} characters.')
    return value


def _require_permission(user, permission: str) -> None:
    if not user.has_perm(permission):
        raise PermissionDenied(f'Permission required: {permission}.')


def _profile(user, permission: str = 'opportunity_agent.change_userprofile'):
    _require_permission(user, 'opportunity_agent.view_userprofile')
    _require_permission(user, permission)
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


def _get_application(user, value: Any) -> Application:
    application_id = _integer(value, 'application_id', 1, 2**63 - 1)
    _require_permission(user, 'opportunity_agent.view_application')
    applications = Application.objects.select_related(
            'user',
            'opportunity',
        )
    application = applications.filter(pk=application_id, user=user).first()
    if application is not None:
        return application
    if user.is_staff and user.has_perm('opportunity_agent.change_application'):
        application = applications.filter(pk=application_id).first()
        if application is not None:
            return application
    raise ToolInputError('Application not found.')


def _opportunity(value: Any) -> Opportunity:
    opportunity_id = _integer(value, 'opportunity_id', 1, 2**63 - 1)
    try:
        return Opportunity.objects.get(pk=opportunity_id)
    except Opportunity.DoesNotExist as exc:
        raise ToolInputError('Opportunity not found.') from exc


def _profile_data(profile: UserProfile) -> dict[str, Any]:
    return {
        'skills': profile.skills,
        'current_country': profile.current_country,
        'target_countries': profile.target_countries,
        'worldwide_preference': profile.worldwide_preference,
        'preferred_opportunity_types': profile.preferred_opportunity_types,
        'preferred_work_modes': profile.preferred_work_modes,
        'visa_sponsorship_preference': profile.visa_sponsorship_preference,
        'salary_stipend_preference': profile.salary_stipend_preference,
        'minimum_ai_match_score': profile.minimum_ai_match_score,
        'auto_apply': profile.auto_apply,
        'education': profile.education,
        'degree': profile.degree,
        'certifications': profile.certifications,
        'work_experience': profile.work_experience,
        'languages': profile.languages,
    }


def _opportunity_data(opportunity: Opportunity) -> dict[str, Any]:
    return {
        'skills': opportunity.skills,
        'country': opportunity.country,
        'work_mode': opportunity.work_mode,
        'remote_worldwide': opportunity.remote_worldwide,
        'opportunity_type': opportunity.opportunity_type,
        'visa_sponsorship': opportunity.visa_sponsorship,
        'education_requirements': opportunity.education_requirements,
        'experience_requirements': opportunity.experience_requirements,
        'qualifications': opportunity.qualifications,
        'languages': opportunity.languages,
        'salary_stipend': opportunity.salary_stipend,
    }


def _save_match(user, opportunity: Opportunity, data: dict[str, Any]) -> Match:
    match, _ = Match.objects.update_or_create(
        user=user,
        opportunity=opportunity,
        defaults={
            'score': data['score'],
            'eligible': data['eligible'],
            'reasons': data['reasons'],
            'strong_matches': data.get('strong_matches', []),
            'missing_requirements': data['missing'],
            'risk_factors': data.get('risks', []),
            'recommended_action': data.get('recommended_action', ''),
        },
    )
    return match


def _search_opportunities(user, args: dict[str, Any]) -> dict[str, Any]:
    _require_permission(user, 'opportunity_agent.view_opportunity')
    allowed = {'query', 'country', 'opportunity_type', 'work_mode', 'remote_only', 'minimum_score', 'limit'}
    if set(args) - allowed:
        raise ToolInputError('Unsupported search filter.')
    query = args.get('query', '')
    country = args.get('country', '')
    kind = args.get('opportunity_type', '')
    mode = args.get('work_mode', '')
    if not isinstance(query, str) or len(query) > 200:
        raise ToolInputError('query must be text no longer than 200 characters.')
    if not isinstance(country, str) or len(country) > 120:
        raise ToolInputError('country must be text no longer than 120 characters.')
    if kind and kind not in OPPORTUNITY_TYPES:
        raise ToolInputError('Unknown opportunity type.')
    if mode and mode not in WORK_MODES:
        raise ToolInputError('Unknown work mode.')
    remote_only = args.get('remote_only', False)
    if not isinstance(remote_only, bool):
        raise ToolInputError('remote_only must be true or false.')
    limit = _integer(args.get('limit', 20), 'limit', 1, 50)
    minimum_score = args.get('minimum_score')
    if minimum_score is not None:
        minimum_score = _integer(minimum_score, 'minimum_score', 0, 100)

    opportunities = Opportunity.objects.filter(status='active')
    if country:
        opportunities = opportunities.filter(country__icontains=country)
    if kind:
        opportunities = opportunities.filter(opportunity_type=kind)
    if mode:
        if mode == 'remote':
            opportunities = opportunities.filter(
                Q(work_mode='remote') | Q(remote_worldwide=True)
            )
        else:
            opportunities = opportunities.filter(work_mode=mode)
    if remote_only:
        opportunities = opportunities.filter(
            Q(work_mode='remote') | Q(remote_worldwide=True)
        )
    if query:
        terms = [term for term in re.split(r'\s+', query) if term]
        for term in terms:
            opportunities = opportunities.filter(
                Q(title__icontains=term)
                | Q(organization__icontains=term)
                | Q(description__icontains=term)
                | Q(skills__icontains=term)
            )
    profile = _profile(user, 'opportunity_agent.view_userprofile')
    results = []
    for opportunity in opportunities.select_related('source').order_by('-created_at')[:200]:
        match = compute_match_score(
            _profile_data(profile),
            _opportunity_data(opportunity),
        )
        if minimum_score is not None and match['score'] < minimum_score:
            continue
        results.append({
            'id': opportunity.pk,
            'title': opportunity.title,
            'organization': opportunity.organization,
            'country': opportunity.country,
            'opportunity_type': opportunity.opportunity_type,
            'work_mode': opportunity.work_mode,
            'remote_worldwide': opportunity.remote_worldwide,
            'deadline_status': opportunity.deadline_status,
            'match_score': match['score'],
            'eligible': match['eligible'],
        })
        if len(results) == limit:
            break
    return _result(
        'search_opportunities',
        True,
        'success',
        f'Found {len(results)} matching opportunity record(s).',
        opportunities=results,
    )


def _calculate_match(user, args: dict[str, Any]) -> dict[str, Any]:
    _require_permission(user, 'opportunity_agent.view_opportunity')
    _require_permission(user, 'opportunity_agent.add_match')
    if set(args) != {'opportunity_id'}:
        raise ToolInputError('opportunity_id is required.')
    profile = _profile(user, 'opportunity_agent.view_userprofile')
    opportunity = _opportunity(args['opportunity_id'])
    match_data = compute_match_score(
        _profile_data(profile),
        _opportunity_data(opportunity),
    )
    match = _save_match(user, opportunity, match_data)
    return _result(
        'calculate_match',
        True,
        'success',
        f'Match score: {match.score}%.',
        opportunity_id=opportunity.pk,
        score=match.score,
        eligible=match.eligible,
        reasons=match.reasons,
        missing_requirements=match.missing_requirements,
        risk_factors=match.risk_factors,
    )


def _prepare_application(user, args: dict[str, Any]) -> dict[str, Any]:
    _require_permission(user, 'opportunity_agent.add_application')
    _require_permission(user, 'opportunity_agent.add_match')
    if set(args) != {'opportunity_id'}:
        raise ToolInputError('opportunity_id is required.')
    opportunity = _opportunity(args['opportunity_id'])
    profile = _profile(user, 'opportunity_agent.view_userprofile')
    match_data = compute_match_score(
        _profile_data(profile),
        _opportunity_data(opportunity),
    )
    _save_match(user, opportunity, match_data)
    if opportunity.status != 'active' or opportunity.is_expired():
        return _result(
            'prepare_application',
            False,
            'needs_review',
            'This opportunity is inactive or past its deadline; it was not queued.',
            opportunity_id=opportunity.pk,
        )
    application, created = Application.objects.get_or_create(
        user=user,
        opportunity=opportunity,
        defaults={
            'match_score': match_data['score'],
            'status': 'queued' if match_data['eligible'] else 'needs_review',
            'error_message': '' if match_data['eligible'] else (
                f"Match score {match_data['score']} is below the "
                f"configured threshold of {match_data['minimum_score']}."
            ),
        },
    )
    if not created and application.status not in {'failed', 'rejected', 'cancelled'}:
        return _result(
            'prepare_application',
            False,
            application.status,
            'An application already exists and was not changed.',
            application_id=application.pk,
        )
    if not created:
        application.status = 'queued' if match_data['eligible'] else 'needs_review'
        application.match_score = match_data['score']
        application.error_message = '' if match_data['eligible'] else (
            f"Match score {match_data['score']} is below the "
            f"configured threshold of {match_data['minimum_score']}."
        )
        application.save(
            update_fields=('status', 'match_score', 'error_message', 'updated_at')
        )
    return _result(
        'prepare_application',
        bool(match_data['eligible']),
        application.status,
        (
            'Application added to the safe processing queue.'
            if match_data['eligible']
            else 'The application was recorded for review because the match is not eligible.'
        ),
        application_id=application.pk,
        match_score=match_data['score'],
        created=created,
    )


def _preflight_application(application: Application) -> tuple[bool, str]:
    from .application_runner import safety_check

    return safety_check(application)


def _set_needs_review(application: Application, reason: str) -> dict[str, Any]:
    application.status = 'needs_review'
    application.error_message = reason[:2000]
    application.save(update_fields=('status', 'error_message', 'updated_at'))
    return _result(
        'submit_application',
        False,
        'needs_review',
        reason,
        application_id=application.pk,
        required_action='Manual review required.',
    )


def _retry_application(user, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) != {'application_id'}:
        raise ToolInputError('application_id is required.')
    _require_permission(user, 'opportunity_agent.change_application')
    application = _get_application(user, args['application_id'])
    if application.status not in {'failed', 'rejected'}:
        raise ToolInputError(
            f'Only failed or rejected applications can be retried; '
            f'this application is {application.status}.'
        )
    allowed, reason = _preflight_application(application)
    if not allowed:
        return _set_needs_review(application, reason)
    application.status = 'queued'
    application.error_message = ''
    application.rejection_reason = ''
    application.save(
        update_fields=('status', 'error_message', 'rejection_reason', 'updated_at')
    )
    from ..tasks import process_application_queue_task

    process_application_queue_task.delay(1)
    return _result(
        'retry_application',
        True,
        'queued',
        'Application queued for safety-checked processing.',
        application_id=application.pk,
    )


def _cancel_application(user, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) != {'application_id'}:
        raise ToolInputError('application_id is required.')
    _require_permission(user, 'opportunity_agent.change_application')
    application = _get_application(user, args['application_id'])
    if application.status in {'submitted', 'cancelled'}:
        raise ToolInputError(
            f'Cannot cancel an application with status {application.status}.'
        )
    application.status = 'cancelled'
    application.save(update_fields=('status', 'updated_at'))
    return _result(
        'cancel_application',
        True,
        'cancelled',
        'Application cancelled.',
        application_id=application.pk,
    )


def _submit_application(user, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) != {'application_id'}:
        raise ToolInputError('application_id is required.')
    _require_permission(user, 'opportunity_agent.change_application')
    application = _get_application(user, args['application_id'])
    if application.status not in {'queued', 'prepared', 'pending'}:
        raise ToolInputError(
            f'Application cannot be submitted from status {application.status}; '
            'retry failed or rejected applications first.'
        )
    allowed, reason = _preflight_application(application)
    if not allowed:
        return _set_needs_review(application, reason)
    application.status = 'queued'
    application.error_message = ''
    application.save(update_fields=('status', 'error_message', 'updated_at'))
    from ..tasks import process_application_queue_task

    process_application_queue_task.delay(1)
    return _result(
        'submit_application',
        True,
        'queued',
        'Application passed preflight and was queued; submission is not yet confirmed.',
        application_id=application.pk,
    )


def _update_preferences(user, args: dict[str, Any]) -> dict[str, Any]:
    allowed_fields = {
        'target_countries',
        'preferred_opportunity_types',
        'preferred_work_modes',
        'worldwide_preference',
        'minimum_ai_match_score',
        'daily_application_limit',
    }
    if not args or set(args) - allowed_fields:
        raise ToolInputError('Only supported profile preference fields may be updated.')
    profile = _profile(user)
    updates = {}
    if 'target_countries' in args:
        countries = args['target_countries']
        if not isinstance(countries, list) or len(countries) > 30:
            raise ToolInputError('target_countries must be a list of at most 30 countries.')
        if any(not isinstance(item, str) or not item.strip() or len(item) > 120 for item in countries):
            raise ToolInputError('Each country must contain 1 to 120 characters.')
        updates['target_countries'] = list(dict.fromkeys(item.strip() for item in countries))
    if 'preferred_opportunity_types' in args:
        values = args['preferred_opportunity_types']
        if not isinstance(values, list) or any(value not in OPPORTUNITY_TYPES for value in values):
            raise ToolInputError('preferred_opportunity_types contains an unsupported type.')
        updates['preferred_opportunity_types'] = list(dict.fromkeys(values))
    if 'preferred_work_modes' in args:
        values = args['preferred_work_modes']
        if not isinstance(values, list) or any(value not in WORK_MODES for value in values):
            raise ToolInputError('preferred_work_modes contains an unsupported mode.')
        updates['preferred_work_modes'] = list(dict.fromkeys(values))
    if 'worldwide_preference' in args:
        if not isinstance(args['worldwide_preference'], bool):
            raise ToolInputError('worldwide_preference must be true or false.')
        updates['worldwide_preference'] = args['worldwide_preference']
    if 'minimum_ai_match_score' in args:
        updates['minimum_ai_match_score'] = _integer(
            args['minimum_ai_match_score'], 'minimum_ai_match_score', 0, 100
        )
    if 'daily_application_limit' in args:
        updates['daily_application_limit'] = _integer(
            args['daily_application_limit'],
            'daily_application_limit',
            1,
            MAX_DAILY_APPLICATION_LIMIT,
        )
    for field, value in updates.items():
        setattr(profile, field, value)
    profile.full_clean()
    profile.save(update_fields=(*updates.keys(), 'updated_at'))
    return _result(
        'update_preferences',
        True,
        'success',
        'Your opportunity preferences were updated.',
        changed_fields=sorted(updates),
    )


def _update_profile(user, args: dict[str, Any]) -> dict[str, Any]:
    fields = {'full_name', 'phone', 'education', 'degree', 'work_experience', 'skills', 'languages', 'certifications'}
    if not args or set(args) - fields:
        raise ToolInputError('Only supported profile fields may be updated.')
    profile = _profile(user)
    for name in ('full_name', 'phone', 'education', 'degree', 'work_experience'):
        if name in args:
            value = args[name]
            if not isinstance(value, str) or len(value) > 5000:
                raise ToolInputError(f'{name} must be text no longer than 5000 characters.')
            setattr(profile, name, value.strip())
    for name in ('skills', 'languages', 'certifications'):
        if name in args:
            value = args[name]
            if not isinstance(value, list) or len(value) > 100 or any(
                not isinstance(item, str) or not item.strip() or len(item) > 120
                for item in value
            ):
                raise ToolInputError(f'{name} must be a list of at most 100 text values.')
            setattr(profile, name, list(dict.fromkeys(item.strip() for item in value)))
    profile.full_clean()
    profile.save(update_fields=(*args.keys(), 'updated_at'))
    return _result(
        'update_profile',
        True,
        'success',
        'Your profile was updated with the information you supplied.',
        changed_fields=sorted(args),
    )


def _set_auto_apply(user, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) != {'enabled'} or not isinstance(args['enabled'], bool):
        raise ToolInputError('enabled must be true or false.')
    profile = _profile(user)
    profile.auto_apply = args['enabled']
    profile.save(update_fields=('auto_apply', 'updated_at'))
    return _result(
        'set_auto_apply',
        True,
        'success',
        f'Auto-apply {"enabled" if args["enabled"] else "disabled"}.',
        enabled=profile.auto_apply,
    )


def _set_opportunity_type_filter(user, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) != {'types'}:
        raise ToolInputError('types must be a list of opportunity types.')
    return _update_preferences(
        user,
        {'preferred_opportunity_types': args['types']},
    )


def _set_remote_only(user, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) != {'enabled'} or not isinstance(args['enabled'], bool):
        raise ToolInputError('enabled must be true or false.')
    return _update_preferences(
        user,
        {'preferred_work_modes': ['remote'] if args['enabled'] else []},
    )


def _set_match_threshold(user, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) != {'score'}:
        raise ToolInputError('score is required.')
    return _update_preferences(
        user,
        {'minimum_ai_match_score': args['score']},
    )


def _set_daily_application_limit(user, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) != {'limit'}:
        raise ToolInputError('limit is required.')
    return _update_preferences(
        user,
        {'daily_application_limit': args['limit']},
    )


def _change_target_country(user, args: dict[str, Any], *, add: bool) -> dict[str, Any]:
    if set(args) != {'country'}:
        raise ToolInputError('country is required.')
    country = _string(args['country'], 'country', 120)
    profile = _profile(user)
    countries = list(profile.target_countries or [])
    existing = next(
        (item for item in countries if item.casefold() == country.casefold()),
        None,
    )
    if add and existing is None:
        countries.append(country)
    elif not add and existing is not None:
        countries.remove(existing)
    profile.target_countries = countries
    profile.save(update_fields=('target_countries', 'updated_at'))
    action = 'add_target_country' if add else 'remove_target_country'
    verb = 'added' if add else 'removed'
    return _result(
        action,
        True,
        'success',
        f'{country} {verb} {"to" if add else "from"} your target countries.',
        target_countries=countries,
    )


def _add_target_country(user, args: dict[str, Any]) -> dict[str, Any]:
    return _change_target_country(user, args, add=True)


def _remove_target_country(user, args: dict[str, Any]) -> dict[str, Any]:
    return _change_target_country(user, args, add=False)


def _generate_cover_letter(user, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) != {'application_id'}:
        raise ToolInputError('application_id is required.')
    _require_permission(user, 'opportunity_agent.change_application')
    application = _get_application(user, args['application_id'])
    from .ai_engine import AIClient, AIProviderError

    client = AIClient()
    if not client._providers():
        raise ToolInputError('Configure an AI provider before generating a cover letter.')
    profile = application.user.profile
    cover_letter = client.generate_cover_letter(
        {
            'name': profile.full_name or user.get_full_name(),
            'skills': profile.skills,
            'education': profile.education,
            'degree': profile.degree,
            'work_experience': profile.work_experience,
            'certifications': profile.certifications,
            'languages': profile.languages,
        },
        {
            'title': application.opportunity.title,
            'organization': application.opportunity.organization,
            'description': application.opportunity.description,
            'requirements': application.opportunity.requirements,
            'skills': application.opportunity.skills,
        },
    )
    if not cover_letter.strip():
        raise AIProviderError('AI provider returned an empty cover letter.')
    application.cover_letter = cover_letter
    application.save(update_fields=('cover_letter', 'updated_at'))
    return _result(
        'generate_cover_letter',
        True,
        'success',
        'A cover letter was generated from the saved profile and opportunity facts.',
        application_id=application.pk,
        cover_letter=cover_letter,
    )


def _analyze_rejection(user, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) != {'application_id'}:
        raise ToolInputError('application_id is required.')
    application = _get_application(user, args['application_id'])
    if application.status != 'rejected':
        raise ToolInputError('Rejection analysis is available only for rejected applications.')
    profile = _profile(user, 'opportunity_agent.view_userprofile')
    match = Match.objects.filter(
        user=user,
        opportunity=application.opportunity,
    ).first()
    attempts = list(
        ApplicationAttempt.objects.filter(application=application)
        .order_by('-attempt_number')
        .values('attempt_number', 'status', 'error_message', 'created_at')[:10]
    )
    audit = list(
        AuditLog.objects.filter(
            Q(target=str(application.pk))
            | Q(details__application_id=application.pk)
        ).order_by('-timestamp').values('action', 'details', 'timestamp')[:20]
    )
    actual_reason = (
        application.rejection_reason
        or application.error_message
        or next((item['error_message'] for item in attempts if item['error_message']), '')
    )
    analysis = {
        'application_id': application.pk,
        'rejection_reason': actual_reason or None,
        'reason_source': (
            'application.rejection_reason'
            if application.rejection_reason
            else 'application.error_message'
            if application.error_message
            else 'application_attempt'
            if any(item['error_message'] for item in attempts)
            else None
        ),
        'opportunity_requirements': application.opportunity.requirements,
        'experience_requirements': application.opportunity.experience_requirements,
        'education_requirements': application.opportunity.education_requirements,
        'profile_experience': profile.work_experience,
        'profile_education': profile.education,
        'profile_degree': profile.degree,
        'cv_filename': profile.cv.name if profile.cv else None,
        'match_score': match.score if match else application.match_score,
        'match_gaps': match.missing_requirements if match else [],
        'application_answers': application.generated_answers,
        'attempts': attempts,
        'audit_history': application.audit_history,
        'audit_events': audit,
        'explanation': (
            actual_reason
            if actual_reason
            else 'No rejection reason was recorded; the cause cannot be determined from stored evidence.'
        ),
        'fixable': False,
        'recommended_action': 'Review the recorded evidence and provide missing truthful information if available.',
    }
    return _result(
        'analyze_rejection',
        True,
        'success',
        analysis['explanation'],
        analysis=analysis,
    )


def _fix_and_retry(user, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) != {'application_id'}:
        raise ToolInputError('application_id is required.')
    analysis_result = _analyze_rejection(user, args)
    application = _get_application(user, args['application_id'])
    reason = analysis_result['data']['analysis']['rejection_reason']
    if not reason:
        return _set_needs_review(
            application,
            'No rejection reason is recorded. Review the application manually; no profile facts were changed.',
        )
    match = Match.objects.filter(user=user, opportunity=application.opportunity).first()
    if match and match.missing_requirements:
        return _set_needs_review(
            application,
            'Stored match evidence shows unresolved requirements: '
            + '; '.join(str(item) for item in match.missing_requirements[:5])
            + '. No profile facts were changed.',
        )
    allowed, safety_reason = _preflight_application(application)
    if not allowed:
        return _set_needs_review(application, safety_reason)
    application.status = 'queued'
    application.error_message = ''
    application.rejection_reason = ''
    application.save(
        update_fields=('status', 'error_message', 'rejection_reason', 'updated_at')
    )
    from ..tasks import process_application_queue_task

    process_application_queue_task.delay(1)
    return _result(
        'fix_and_retry',
        True,
        'queued',
        'No profile facts were invented or changed. The application passed the safety preflight and was queued.',
        application_id=application.pk,
    )


def _bulk_apply(user, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) - {'minimum_score', 'country', 'opportunity_type', 'work_mode'}:
        raise ToolInputError('Unsupported bulk application filter.')
    minimum_score = _integer(args.get('minimum_score', 80), 'minimum_score', 0, 100)
    profile = _profile(user)
    if not profile.auto_apply:
        raise ToolInputError('Enable Auto Apply in your profile before bulk application.')
    opportunities = Opportunity.objects.filter(status='active')
    if args.get('country'):
        opportunities = opportunities.filter(country__icontains=_string(args['country'], 'country', 120))
    if args.get('opportunity_type'):
        kind = args['opportunity_type']
        if kind not in OPPORTUNITY_TYPES:
            raise ToolInputError('Unknown opportunity type.')
        opportunities = opportunities.filter(opportunity_type=kind)
    if args.get('work_mode'):
        mode = args['work_mode']
        if mode not in WORK_MODES:
            raise ToolInputError('Unknown work mode.')
        opportunities = opportunities.filter(work_mode=mode)
    today = timezone.localdate()
    submitted_today = ApplicationAttempt.objects.filter(
        application__user=user,
        created_at__date=today,
    ).values('application_id').distinct().count()
    remaining = max(0, profile.daily_application_limit - submitted_today)
    if not remaining:
        raise ToolInputError('Your daily application limit has already been reached.')
    queued = []
    needs_review = []
    for opportunity in opportunities.order_by('-created_at')[:200]:
        if len(queued) >= min(remaining, MAX_BULK_APPLICATIONS):
            break
        if opportunity.is_expired():
            continue
        if Application.objects.filter(user=user, opportunity=opportunity).exists():
            continue
        match_data = compute_match_score(
            _profile_data(profile),
            _opportunity_data(opportunity),
        )
        if not match_data['eligible'] or match_data['score'] < minimum_score:
            continue
        application = Application.objects.create(
            user=user,
            opportunity=opportunity,
            match_score=match_data['score'],
            status='queued',
        )
        allowed, reason = _preflight_application(application)
        if not allowed:
            application.status = 'needs_review'
            application.error_message = reason[:2000]
            application.save(update_fields=('status', 'error_message', 'updated_at'))
            needs_review.append({'application_id': application.pk, 'reason': reason})
        else:
            queued.append(application.pk)
    if queued:
        from ..tasks import process_application_queue_task

        process_application_queue_task.delay(min(len(queued), MAX_BULK_APPLICATIONS))
    return _result(
        'bulk_apply',
        True,
        'queued' if queued else 'needs_review' if needs_review else 'no_matches',
        f'{len(queued)} application(s) queued; {len(needs_review)} need manual review.',
        queued_application_ids=queued,
        needs_review=needs_review,
        daily_limit_remaining=remaining - len(queued),
    )


def _automation_status(user, args: dict[str, Any]) -> dict[str, Any]:
    if args:
        raise ToolInputError('automation_status takes no arguments.')
    _require_permission(user, 'opportunity_agent.view_automationrun')
    latest = AutomationRun.objects.order_by('-started_at').first()
    today = timezone.localdate()
    status = {
        'active_sources': Source.objects.filter(enabled=True).count(),
        'disabled_sources': Source.objects.filter(enabled=False).count(),
        'successful_scans_today': Source.objects.filter(last_successful_scan__date=today).count(),
        'failed_sources': Source.objects.filter(status='error').count(),
        'opportunities_discovered_today': Opportunity.objects.filter(created_at__date=today).count(),
        'applications_processed_today': Application.objects.filter(updated_at__date=today).count(),
        'last_automation_run': {
            'status': latest.status,
            'started_at': latest.started_at.isoformat(),
            'finished_at': latest.finished_at.isoformat() if latest.finished_at else None,
        } if latest else None,
    }
    return _result(
        'automation_status',
        True,
        'success',
        'Current stored automation status.',
        automation=status,
    )


def _scan_source(user, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) != {'source_id'}:
        raise ToolInputError('source_id is required.')
    _require_permission(user, 'opportunity_agent.scan_sources')
    source_id = _integer(args['source_id'], 'source_id', 1, 2**63 - 1)
    try:
        source = Source.objects.get(pk=source_id, enabled=True)
    except Source.DoesNotExist as exc:
        raise ToolInputError('Enabled web source not found.') from exc
    from ..tasks import scan_one_source_task

    task = scan_one_source_task.delay(source.pk)
    return _result(
        'scan_source',
        True,
        'queued',
        'Source scan queued; results will be available after the worker completes.',
        source_id=source.pk,
        task_id=task.id,
    )


def _scan_now(user, args: dict[str, Any]) -> dict[str, Any]:
    if args:
        raise ToolInputError('scan_now takes no arguments.')
    _require_permission(user, 'opportunity_agent.scan_sources')
    from ..tasks import scan_sources_task

    task = scan_sources_task.delay(30)
    return _result(
        'scan_now',
        True,
        'queued',
        'Source scan batch queued; completion is not yet confirmed.',
        task_id=task.id,
    )


def _discover_sources(user, args: dict[str, Any]) -> dict[str, Any]:
    if args:
        raise ToolInputError('discover_sources takes no arguments.')
    _require_permission(user, 'opportunity_agent.manage_automation')
    from ..tasks import discover_sources_task

    task = discover_sources_task.delay()
    return _result(
        'discover_sources',
        True,
        'queued',
        'Public source discovery queued; completion is not yet confirmed.',
        task_id=task.id,
    )


def _scan_telegram_source(user, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) != {'source_id'}:
        raise ToolInputError('source_id is required.')
    _require_permission(user, 'opportunity_agent.scan_telegram_sources')
    source_id = _integer(args['source_id'], 'source_id', 1, 2**63 - 1)
    from ..models import TelegramSource

    try:
        source = TelegramSource.objects.get(pk=source_id, enabled=True)
    except TelegramSource.DoesNotExist as exc:
        raise ToolInputError('Enabled Telegram source not found.') from exc
    from ..tasks import scan_telegram_sources_task

    task = scan_telegram_sources_task.delay(
        1,
        telegram_source_ids=[source.pk],
    )
    return _result(
        'scan_telegram_source',
        True,
        'queued',
        'Telegram source scan queued; results are not yet confirmed.',
        source_id=source.pk,
        task_id=task.id,
    )


def _run_automation_cycle(user, args: dict[str, Any]) -> dict[str, Any]:
    if args:
        raise ToolInputError('run_automation_cycle takes no arguments.')
    _require_permission(user, 'opportunity_agent.manage_automation')
    from ..tasks import automation_cycle_task

    task = automation_cycle_task.delay()
    return _result(
        'run_automation_cycle',
        True,
        'queued',
        'Automation cycle queued; completion is not yet confirmed.',
        task_id=task.id,
    )


def _disable_source(user, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) != {'source_id'}:
        raise ToolInputError('source_id is required.')
    _require_permission(user, 'opportunity_agent.change_source')
    source_id = _integer(args['source_id'], 'source_id', 1, 2**63 - 1)
    try:
        source = Source.objects.get(pk=source_id)
    except Source.DoesNotExist as exc:
        raise ToolInputError('Web source not found.') from exc
    source.enabled = False
    source.status = 'disabled'
    source.save(update_fields=('enabled', 'status', 'updated_at'))
    return _result(
        'disable_source',
        True,
        'disabled',
        'Web source disabled.',
        source_id=source.pk,
    )


TOOL_HANDLERS = {
    'search_opportunities': _search_opportunities,
    'calculate_match': _calculate_match,
    'prepare_application': _prepare_application,
    'retry_application': _retry_application,
    'cancel_application': _cancel_application,
    'generate_cover_letter': _generate_cover_letter,
    'analyze_rejection': _analyze_rejection,
    'fix_and_retry': _fix_and_retry,
    'update_profile': _update_profile,
    'update_preferences': _update_preferences,
    'set_opportunity_type_filter': _set_opportunity_type_filter,
    'set_remote_only': _set_remote_only,
    'set_auto_apply': _set_auto_apply,
    'set_match_threshold': _set_match_threshold,
    'set_daily_application_limit': _set_daily_application_limit,
    'add_target_country': _add_target_country,
    'remove_target_country': _remove_target_country,
    'submit_application': _submit_application,
    'bulk_apply': _bulk_apply,
    'automation_status': _automation_status,
    'scan_source': _scan_source,
    'scan_now': _scan_now,
    'discover_sources': _discover_sources,
    'scan_telegram_source': _scan_telegram_source,
    'run_automation_cycle': _run_automation_cycle,
    'disable_source': _disable_source,
}

_CONFIRMATION_TOOL_NAMES = CONFIRMATION_ACTIONS | {'fix_and_retry'}
_CONFIRMATION_TOOL_NAMES.update({
    'disable_source',
    'scan_now',
    'scan_source',
    'scan_telegram_source',
    'discover_sources',
    'run_automation_cycle',
})

TOOL_ARGUMENT_KEYS = {
    'search_opportunities': {'query', 'country', 'opportunity_type', 'work_mode', 'remote_only', 'minimum_score', 'limit'},
    'calculate_match': {'opportunity_id'},
    'prepare_application': {'opportunity_id'},
    'retry_application': {'application_id'},
    'cancel_application': {'application_id'},
    'generate_cover_letter': {'application_id'},
    'analyze_rejection': {'application_id'},
    'fix_and_retry': {'application_id'},
    'update_profile': {'full_name', 'phone', 'education', 'degree', 'work_experience', 'skills', 'languages', 'certifications'},
    'update_preferences': {'target_countries', 'preferred_opportunity_types', 'preferred_work_modes', 'worldwide_preference', 'minimum_ai_match_score', 'daily_application_limit'},
    'set_opportunity_type_filter': {'types'},
    'set_remote_only': {'enabled'},
    'set_auto_apply': {'enabled'},
    'set_match_threshold': {'score'},
    'set_daily_application_limit': {'limit'},
    'add_target_country': {'country'},
    'remove_target_country': {'country'},
    'submit_application': {'application_id'},
    'bulk_apply': {'minimum_score', 'country', 'opportunity_type', 'work_mode'},
    'automation_status': set(),
    'scan_source': {'source_id'},
    'scan_now': set(),
    'discover_sources': set(),
    'scan_telegram_source': {'source_id'},
    'run_automation_cycle': set(),
    'disable_source': {'source_id'},
}

TOOL_PERMISSIONS = {
    'search_opportunities': ('opportunity_agent.view_opportunity', 'opportunity_agent.view_userprofile'),
    'calculate_match': ('opportunity_agent.view_opportunity', 'opportunity_agent.view_userprofile', 'opportunity_agent.add_match'),
    'prepare_application': ('opportunity_agent.view_userprofile', 'opportunity_agent.add_application', 'opportunity_agent.add_match'),
    'retry_application': ('opportunity_agent.view_application', 'opportunity_agent.change_application'),
    'cancel_application': ('opportunity_agent.view_application', 'opportunity_agent.change_application'),
    'generate_cover_letter': ('opportunity_agent.view_application', 'opportunity_agent.change_application'),
    'analyze_rejection': ('opportunity_agent.view_application', 'opportunity_agent.view_userprofile'),
    'fix_and_retry': ('opportunity_agent.view_application', 'opportunity_agent.change_application', 'opportunity_agent.view_userprofile'),
    'update_profile': ('opportunity_agent.view_userprofile', 'opportunity_agent.change_userprofile'),
    'update_preferences': ('opportunity_agent.view_userprofile', 'opportunity_agent.change_userprofile'),
    'set_opportunity_type_filter': ('opportunity_agent.view_userprofile', 'opportunity_agent.change_userprofile'),
    'set_remote_only': ('opportunity_agent.view_userprofile', 'opportunity_agent.change_userprofile'),
    'set_auto_apply': ('opportunity_agent.view_userprofile', 'opportunity_agent.change_userprofile'),
    'set_match_threshold': ('opportunity_agent.view_userprofile', 'opportunity_agent.change_userprofile'),
    'set_daily_application_limit': ('opportunity_agent.view_userprofile', 'opportunity_agent.change_userprofile'),
    'add_target_country': ('opportunity_agent.view_userprofile', 'opportunity_agent.change_userprofile'),
    'remove_target_country': ('opportunity_agent.view_userprofile', 'opportunity_agent.change_userprofile'),
    'submit_application': ('opportunity_agent.view_application', 'opportunity_agent.change_application'),
    'bulk_apply': ('opportunity_agent.view_userprofile', 'opportunity_agent.change_application', 'opportunity_agent.add_application'),
    'automation_status': ('opportunity_agent.view_automationrun',),
    'scan_source': ('opportunity_agent.scan_sources',),
    'scan_now': ('opportunity_agent.scan_sources',),
    'discover_sources': ('opportunity_agent.manage_automation',),
    'scan_telegram_source': ('opportunity_agent.scan_telegram_sources',),
    'run_automation_cycle': ('opportunity_agent.manage_automation',),
    'disable_source': ('opportunity_agent.change_source',),
}


def _validate_argument_shape(action: str, arguments: dict[str, Any]) -> None:
    allowed_keys = TOOL_ARGUMENT_KEYS[action]
    if set(arguments) - allowed_keys:
        raise ToolInputError('Arguments contain fields not supported by this action.')
    forbidden_names = {'password', 'secret', 'token', 'api_key', 'credential'}

    def inspect(value: Any, depth: int = 0) -> None:
        if depth > 5:
            raise ToolInputError('Arguments are nested too deeply.')
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > 80:
                    raise ToolInputError('Argument field names are invalid.')
                if any(part in key.casefold() for part in forbidden_names):
                    raise ToolInputError('Credentials and secret values cannot be passed to tools.')
                inspect(child, depth + 1)
        elif isinstance(value, list):
            if len(value) > 100:
                raise ToolInputError('An argument list is too large.')
            for child in value:
                inspect(child, depth + 1)
        elif isinstance(value, str):
            if len(value) > 5000:
                raise ToolInputError('An argument value is too long.')
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise ToolInputError('Arguments must contain JSON values only.')

    inspect(arguments)
    try:
        encoded = json.dumps(arguments, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ToolInputError('Arguments must be JSON-serializable.') from exc
    if len(encoded) > 12000:
        raise ToolInputError('Arguments are too large.')


def _preauthorize(actor, action: str, arguments: dict[str, Any]) -> None:
    for permission in TOOL_PERMISSIONS[action]:
        _require_permission(actor, permission)
    if action in {
        'retry_application',
        'cancel_application',
        'generate_cover_letter',
        'analyze_rejection',
        'fix_and_retry',
        'submit_application',
    }:
        _get_application(actor, arguments.get('application_id'))


def tool_requires_confirmation(action: str, arguments: dict[str, Any]) -> bool:
    if action in _CONFIRMATION_TOOL_NAMES:
        return True
    if action == 'update_preferences' and (
        {'minimum_ai_match_score', 'daily_application_limit'} & set(arguments)
    ):
        return True
    if action == 'set_opportunity_type_filter':
        return True
    if action == 'set_remote_only':
        return True
    if action == 'set_auto_apply':
        return bool(arguments.get('enabled'))
    return False


def execute_tool(
    actor,
    action: str,
    arguments: dict[str, Any],
    *,
    confirmed: bool = False,
    confirmation_channel: str = 'web',
) -> dict[str, Any]:
    if not getattr(actor, 'is_authenticated', False) or not actor.is_active:
        return _result(action, False, 'unauthorized', 'Authentication is required.')
    if not isinstance(action, str) or action not in TOOL_HANDLERS:
        return _result('unknown', False, 'invalid_tool', 'This action is not available.')
    if not isinstance(arguments, dict):
        return _result(action, False, 'invalid_arguments', 'Tool arguments must be an object.')

    try:
        _validate_argument_shape(action, arguments)
        _preauthorize(actor, action, arguments)
        if tool_requires_confirmation(action, arguments) and not confirmed:
            pending_result = _result(
                action,
                False,
                'confirmation_required',
                'Confirm this action before it can run.',
                confirmation_required=True,
            )
            record_audit_event(
                f'ai_tool_{action}_confirmation_requested',
                '',
                {
                    'channel': confirmation_channel,
                    'arguments': arguments,
                },
                actor=actor,
            )
            return pending_result
        with transaction.atomic():
            result = TOOL_HANDLERS[action](actor, arguments)
            record_audit_event(
                f'ai_tool_{action}',
                result.get('data', {}).get('application_id')
                or result.get('data', {}).get('opportunity_id')
                or result.get('data', {}).get('source_id')
                or '',
                {
                    'channel': confirmation_channel,
                    'success': result['success'],
                    'status': result['status'],
                    'arguments': arguments,
                    'result': result,
                },
                actor=actor,
            )
            return result
    except (ToolInputError, ValidationError, PermissionDenied) as exc:
        logger.info(
            'Conversational action %s rejected for user %s: %s',
            action,
            actor.pk,
            exc,
        )
        result = _result(
            action,
            False,
            'unauthorized' if isinstance(exc, PermissionDenied) else 'invalid_request',
            str(exc),
        )
    except Exception:
        logger.exception(
            'Conversational action %s failed for user %s.',
            action,
            actor.pk,
        )
        result = _result(
            action,
            False,
            'error',
            'The action failed. Check the application logs for details.',
        )
    try:
        record_audit_event(
            f'ai_tool_{action}_failed',
            '',
            {
                'channel': confirmation_channel,
                'status': result['status'],
                'arguments': arguments,
                'error': result['message'],
            },
            actor=actor,
        )
    except Exception:
        logger.exception(
            'Could not record failed conversational action %s for user %s.',
            action,
            actor.pk,
        )
    return result


def rejected_application_evidence(user, application_id: int) -> dict[str, Any]:
    return _analyze_rejection(user, {'application_id': application_id})


def daily_application_count(user) -> int:
    return ApplicationAttempt.objects.filter(
        application__user=user,
        created_at__date=timezone.localdate(),
    ).values('application_id').distinct().count()
