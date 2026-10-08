import logging

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Q
from django.utils import timezone

from ..models import Application, ApplicationAttempt
from .matching import compute_match_score

logger = logging.getLogger(__name__)


def _profile_data(profile):
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


def _opportunity_data(opportunity):
    return {
        'skills': opportunity.skills,
        'country': opportunity.country,
        'work_mode': opportunity.work_mode,
        'remote_worldwide': opportunity.remote_worldwide,
        'opportunity_type': opportunity.opportunity_type,
        'visa_sponsorship': opportunity.visa_sponsorship,
        'education_requirements': opportunity.education_requirements,
        'experience_requirements': opportunity.experience_requirements,
        'requirements': opportunity.requirements,
        'qualifications': opportunity.qualifications,
        'languages': opportunity.languages,
        'salary_stipend': opportunity.salary_stipend,
    }


class ApplicationGuard:
    REQUIRED_PROFILE_FIELDS = {
        'full_name': lambda profile, user, app: profile.full_name or user.get_full_name(),
        'email': lambda profile, user, app: user.email,
        'phone': lambda profile, user, app: profile.phone,
        'current_country': lambda profile, user, app: profile.current_country,
        'degree': lambda profile, user, app: profile.degree,
        'education': lambda profile, user, app: profile.education,
        'work_experience': lambda profile, user, app: profile.work_experience,
        'skills': lambda profile, user, app: profile.skills,
        'languages': lambda profile, user, app: profile.languages,
        'certifications': lambda profile, user, app: profile.certifications,
        'cover_letter': lambda profile, user, app: app.cover_letter,
    }

    @staticmethod
    def _cv_is_available(profile):
        if not profile.cv or not profile.cv.name:
            return False, 'A CV is required for automatic application.'
        if not profile.cv.name.lower().endswith(('.pdf', '.docx')):
            return False, 'The saved CV must be a PDF or DOCX file.'
        try:
            if not profile.cv.storage.exists(profile.cv.name):
                return False, 'The saved CV file is missing; upload a valid CV before applying.'
            if profile.cv.size <= 0:
                return False, 'The saved CV file is empty; upload a valid CV before applying.'
        except Exception as exc:
            logger.exception('Could not validate CV storage for user %s.', profile.user_id)
            return False, f'The saved CV file could not be validated: {exc}'
        return True, ''

    @staticmethod
    def _adapter_preflight(adapter, application):
        if adapter is None:
            return False, 'Provider adapter unavailable: no adapter is configured for this application.'
        preflight = getattr(adapter, 'preflight', None)
        if not callable(preflight):
            return False, 'Provider adapter unavailable: adapter does not implement safety checks.'
        allowed, reason = preflight(application)
        if not allowed:
            return False, reason or 'Provider adapter unavailable: automatic submission is not enabled.'
        return True, ''

    @classmethod
    def _required_information_check(cls, user, profile, application, adapter):
        adapter_config = getattr(adapter, 'config', {}) or {}
        required_fields = adapter_config.get('required_profile_fields', [])
        if not isinstance(required_fields, list):
            return False, 'Provider configuration error: required_profile_fields must be a list.'
        missing_fields = []
        for field_name in required_fields:
            value_getter = cls.REQUIRED_PROFILE_FIELDS.get(field_name)
            if value_getter is None:
                return False, f'Provider configuration error: unsupported required profile field "{field_name}".'
            value = value_getter(profile, user, application)
            if value in (None, '', [], {}):
                missing_fields.append(field_name.replace('_', ' '))

        required_answers = adapter_config.get('required_answers', [])
        if not isinstance(required_answers, list):
            return False, 'Provider configuration error: required_answers must be a list.'
        answers = application.generated_answers or {}
        for field_name in required_answers:
            if not isinstance(field_name, str) or not field_name:
                return False, 'Provider configuration error: required_answers entries must be field names.'
            if answers.get(field_name) in (None, '', [], {}):
                missing_fields.append(field_name)

        if missing_fields:
            return False, 'Required information is missing: ' + ', '.join(missing_fields) + '.'
        return True, ''

    @classmethod
    def can_submit(
        cls,
        user,
        opportunity,
        profile=None,
        match=None,
        application=None,
        adapter=None,
    ):
        application = application or Application.objects.filter(
            user=user,
            opportunity=opportunity,
        ).first()
        if application is None:
            return False, 'Application record is missing.'

        profile = profile or getattr(user, 'profile', None)
        if profile is None:
            return False, 'User profile is missing.'
        if not profile.auto_apply:
            return False, 'Auto-apply is disabled for this user.'
        cv_available, cv_reason = cls._cv_is_available(profile)
        if not cv_available:
            return False, cv_reason
        missing_profile_fields = profile.missing_profile_fields
        if missing_profile_fields:
            return False, (
                'Required profile information is missing: '
                + ', '.join(missing_profile_fields)
                + '.'
            )
        if not (profile.full_name or user.get_full_name()).strip():
            return False, 'Required information is missing: full name.'
        if not user.email:
            return False, 'Required information is missing: email address.'
        try:
            validate_email(user.email)
        except ValidationError:
            return False, 'Required information is invalid: email address.'

        if opportunity.status != 'active':
            return False, 'Opportunity is no longer active.'
        if opportunity.is_expired():
            return False, 'Opportunity deadline has passed.'
        if not opportunity.application_url:
            contacts = [
                f'{label}: {value}'
                for label, value in (
                    ('email', opportunity.contact_email),
                    ('Telegram', opportunity.telegram_contact),
                    ('phone', opportunity.contact_phone),
                    ('physical address', opportunity.physical_address),
                )
                if value
            ]
            if contacts:
                return False, (
                    'MANUAL_CONTACT_REQUIRED: No verified online application URL was found. '
                    + '; '.join(contacts)
                    + '. Follow the preserved application instructions in the opportunity details.'
                )
            return False, 'Application destination URL is missing.'
        if application.status == 'submitted' or Application.objects.filter(
            user=user,
            opportunity=opportunity,
            status='submitted',
        ).exclude(pk=application.pk).exists():
            return False, 'This application has already been submitted.'
        today = timezone.localdate()
        if ApplicationAttempt.objects.filter(
            application=application,
            created_at__date=today,
        ).exists():
            return False, 'This application already used a submission attempt today.'
        if application.status not in {'prepared', 'pending'}:
            return False, f'Application status "{application.get_status_display()}" is not ready for submission.'
        adapter_allowed, adapter_reason = cls._adapter_preflight(adapter, application)
        if not adapter_allowed:
            return False, adapter_reason
        information_allowed, information_reason = cls._required_information_check(
            user,
            profile,
            application,
            adapter,
        )
        if not information_allowed:
            return False, information_reason

        current_match = compute_match_score(
            _profile_data(profile),
            _opportunity_data(opportunity),
        )
        if not current_match['eligible']:
            if not current_match['threshold_met']:
                return False, (
                    f"Match score {current_match['score']} is below the required "
                    f"threshold of {current_match['minimum_score']}."
                )
            return False, 'Match score or saved location/work-mode preferences do not meet the configured requirements.'

        used_today = Application.objects.filter(user=user).filter(
            Q(attempts_log__created_at__date=today)
            | Q(status='submitted') & (
                Q(submission_time__date=today)
                | Q(submission_time__isnull=True, updated_at__date=today)
            )
        ).exclude(pk=application.pk).distinct().count()
        if used_today >= profile.daily_application_limit:
            return False, 'Daily application limit reached.'
        return True, 'Ready'
