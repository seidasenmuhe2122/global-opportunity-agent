from __future__ import annotations

import hashlib
import logging
import secrets
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.core.validators import MinValueValidator
from django.utils import timezone


logger = logging.getLogger(__name__)


class UserProfile(models.Model):
    REGISTRATION_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('active', 'Active'),
        ('rejected', 'Rejected'),
        ('suspended', 'Suspended'),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    full_name = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=64, blank=True)
    current_country = models.CharField(max_length=120, blank=True)
    target_countries = models.JSONField(default=list, blank=True)
    worldwide_preference = models.BooleanField(default=False)
    skills = models.JSONField(default=list, blank=True)
    education = models.TextField(blank=True)
    degree = models.CharField(max_length=255, blank=True)
    work_experience = models.TextField(blank=True)
    languages = models.JSONField(default=list, blank=True)
    certifications = models.JSONField(default=list, blank=True)
    cv = models.FileField(upload_to='cvs/', blank=True, null=True)
    portfolio_url = models.URLField(blank=True)
    linkedin_url = models.URLField(blank=True)
    github_url = models.URLField(blank=True)
    other_links = models.JSONField(default=list, blank=True)
    preferred_opportunity_types = models.JSONField(default=list, blank=True)
    preferred_work_modes = models.JSONField(default=list, blank=True)
    visa_sponsorship_preference = models.BooleanField(default=False)
    salary_stipend_preference = models.CharField(max_length=255, blank=True)
    minimum_ai_match_score = models.IntegerField(default=75)
    auto_apply = models.BooleanField(default=False)
    daily_application_limit = models.PositiveIntegerField(
        default=5,
        validators=[MinValueValidator(1)],
    )
    notification_preferences = models.JSONField(default=dict, blank=True)
    registration_status = models.CharField(
        max_length=20,
        choices=REGISTRATION_STATUS_CHOICES,
        default='active',
    )
    registration_submitted_at = models.DateTimeField(blank=True, null=True)
    registered_at = models.DateTimeField(blank=True, null=True)
    registration_approved_at = models.DateTimeField(blank=True, null=True)
    registration_rejected_at = models.DateTimeField(blank=True, null=True)
    suspended_at = models.DateTimeField(blank=True, null=True)
    last_status_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='registration_status_changes',
    )
    rejection_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['current_country']), models.Index(fields=['auto_apply'])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(minimum_ai_match_score__gte=0)
                & models.Q(minimum_ai_match_score__lte=100),
                name='profile_match_score_0_100',
            ),
            models.CheckConstraint(
                condition=models.Q(daily_application_limit__gte=1),
                name='profile_daily_limit_positive',
            ),
        ]

    @property
    def login_block_reason(self):
        if self.registration_status == 'pending':
            return 'Your account is awaiting administrator approval. You will be able to sign in after your registration is approved.'
        if self.registration_status == 'rejected':
            return 'Your account has been rejected. Please contact an administrator for more information.'
        if self.registration_status == 'suspended':
            return 'Your account is suspended and cannot sign in at this time.'
        return ''

    def set_registration_status(self, status, actor=None, reason='', save=True):
        if status not in dict(self.REGISTRATION_STATUS_CHOICES):
            raise ValueError(f'Unsupported registration status: {status}')
        now = timezone.now()
        self.registration_status = status
        self.last_status_changed_by = actor
        self.registered_at = self.registered_at or now
        if status == 'pending':
            self.user.is_active = False
            self.registration_submitted_at = self.registration_submitted_at or now
            self.registration_approved_at = None
            self.registration_rejected_at = None
            self.suspended_at = None
            self.rejection_reason = ''
        elif status == 'active':
            self.user.is_active = True
            self.registration_approved_at = now
            self.registration_rejected_at = None
            self.suspended_at = None
            self.registration_submitted_at = self.registration_submitted_at or now
            self.rejection_reason = ''
        elif status == 'rejected':
            self.user.is_active = False
            self.registration_rejected_at = now
            self.rejection_reason = reason or self.rejection_reason
        elif status == 'suspended':
            self.user.is_active = False
            self.suspended_at = now
            self.registration_rejected_at = None
        self.user.save(update_fields=['is_active'])
        if save:
            self.save()
        return self

    @property
    def profile_completeness(self):
        sections = self.profile_completeness_sections
        if not sections:
            return 0
        completed = sum(section['complete'] for section in sections)
        return round((completed / len(sections)) * 100)

    @property
    def profile_completeness_sections(self):
        full_name = self.full_name or self.user.get_full_name()
        has_preferences = bool(
            self.target_countries
            or self.worldwide_preference
            or self.preferred_opportunity_types
            or self.preferred_work_modes
        )
        definitions = [
            (
                'Personal information',
                [
                    ('Full name', bool(full_name and full_name.strip())),
                    ('Email', bool(self.user.email and self.user.email.strip())),
                    ('Phone', bool(self.phone and self.phone.strip())),
                    ('Current country', bool(self.current_country and self.current_country.strip())),
                ],
            ),
            (
                'Education',
                [
                    ('Education details', bool(self.education and self.education.strip())),
                    ('Degree or qualification', bool(self.degree and self.degree.strip())),
                ],
            ),
            ('Skills', [('Skills', bool(self.skills))]),
            ('Experience', [('Work experience', bool(self.work_experience and self.work_experience.strip()))]),
            ('CV / Resume', [('CV / Resume', bool(self.cv and self.cv.name))]),
            ('Languages', [('Languages', bool(self.languages))]),
            (
                'Preferences',
                [(
                    'Target countries, worldwide, opportunity types, or work modes',
                    has_preferences,
                )],
            ),
        ]
        sections = []
        for name, fields in definitions:
            missing = [label for label, complete in fields if not complete]
            sections.append({
                'name': name,
                'complete': not missing,
                'missing_fields': missing,
            })
        return sections

    @property
    def missing_profile_fields(self):
        return [
            field
            for section in self.profile_completeness_sections
            for field in section['missing_fields']
        ]

    def __str__(self):
        return self.full_name or self.user.username


class Source(models.Model):
    SOURCE_TYPES = [
        ('website', 'Website'),
        ('job_site', 'Job Website'),
        ('scholarship_site', 'Scholarship Website'),
        ('university', 'University'),
        ('ngo', 'NGO'),
        ('government', 'Government'),
        ('international_org', 'International Organization'),
        ('company', 'Company Career Page'),
        ('fellowship_portal', 'Fellowship Portal'),
        ('grant_portal', 'Grant Portal'),
        ('rss', 'RSS Feed'),
        ('api', 'Public API'),
        ('discovered', 'Discovered Public Source'),
        ('other', 'Other'),
    ]
    SCAN_FREQUENCIES = [
        ('manual', 'Manual only'),
        ('hourly', 'Hourly'),
        ('every_6_hours', 'Every 6 hours'),
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
    ]
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('disabled', 'Disabled'),
        ('scanning', 'Scanning'),
        ('error', 'Error'),
        ('pending', 'Pending'),
    ]

    name = models.CharField(max_length=255)
    url = models.URLField(unique=True)
    source_type = models.CharField(max_length=40, choices=SOURCE_TYPES, default='website')
    country = models.CharField(max_length=120, blank=True)
    opportunity_types = models.JSONField(default=list, blank=True)
    enabled = models.BooleanField(default=True)
    trust_score = models.FloatField(default=0.0)
    scan_frequency = models.CharField(max_length=24, choices=SCAN_FREQUENCIES, default='daily')
    last_scan = models.DateTimeField(blank=True, null=True)
    last_successful_scan = models.DateTimeField(blank=True, null=True)
    error_count = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    notes = models.TextField(blank=True)
    auto_discovered = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['enabled', 'status']), models.Index(fields=['source_type'])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(trust_score__gte=0) & models.Q(trust_score__lte=1),
                name='source_trust_score_0_1',
            ),
        ]
        permissions = [('scan_sources', 'Can scan opportunity sources')]

    def clean(self):
        if not self.url.startswith(('http://', 'https://')):
            raise ValidationError({'url': 'Source URL must start with http:// or https://.'})
        errors = {}
        if not isinstance(self.opportunity_types, list):
            errors['opportunity_types'] = 'Opportunity types must be a list.'
        else:
            allowed_types = {value for value, _ in Opportunity.OPPORTUNITY_TYPES}
            unknown_types = set(self.opportunity_types) - allowed_types
            if unknown_types:
                errors['opportunity_types'] = (
                    'Unknown opportunity types: ' + ', '.join(sorted(unknown_types))
                )
        if not 0 <= self.trust_score <= 1:
            errors['trust_score'] = 'Trust score must be between 0 and 1.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.name


class Opportunity(models.Model):
    WORK_MODE_CHOICES = [
        ('on_site', 'On-site'),
        ('hybrid', 'Hybrid'),
        ('remote', 'Remote'),
    ]
    OPPORTUNITY_TYPES = [
        ('job', 'Job'),
        ('scholarship', 'Scholarship'),
        ('internship', 'Internship'),
        ('fellowship', 'Fellowship'),
        ('grant', 'Grant'),
        ('training', 'Training Program'),
        ('study', 'Study Opportunity'),
        ('exchange', 'Exchange Program'),
        ('volunteer', 'Volunteer Opportunity'),
        ('research', 'Research Opportunity'),
        ('competition', 'Competition'),
        ('other', 'Other'),
    ]
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('expired', 'Expired'),
        ('needs_review', 'Needs Review'),
        ('rejected', 'Rejected'),
    ]

    source = models.ForeignKey(Source, on_delete=models.SET_NULL, null=True, blank=True, related_name='opportunities')
    telegram_source = models.ForeignKey(
        'TelegramSource',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='opportunities',
    )
    title = models.CharField(max_length=255)
    organization = models.CharField(max_length=255, blank=True)
    opportunity_type = models.CharField(max_length=40, choices=OPPORTUNITY_TYPES, blank=True, default='')
    country = models.CharField(max_length=120, blank=True)
    city = models.CharField(max_length=120, blank=True)
    work_mode = models.CharField(max_length=20, choices=WORK_MODE_CHOICES, blank=True)
    remote_worldwide = models.BooleanField(blank=True, null=True, default=None)
    description = models.TextField(blank=True)
    responsibilities = models.TextField(blank=True)
    requirements = models.TextField(blank=True)
    qualifications = models.TextField(blank=True)
    education_requirements = models.TextField(blank=True)
    experience_requirements = models.TextField(blank=True)
    skills = models.JSONField(default=list, blank=True)
    languages = models.JSONField(default=list, blank=True)
    salary_stipend = models.CharField(max_length=255, blank=True)
    benefits = models.TextField(blank=True)
    visa_sponsorship = models.BooleanField(blank=True, null=True, default=None)
    deadline = models.DateTimeField(blank=True, null=True)
    application_url = models.URLField(blank=True)
    application_form_url = models.URLField(blank=True)
    application_form_type = models.CharField(max_length=20, blank=True, choices=[('web','Web Form'),('pdf','PDF Form'),('docx','DOCX Form'),('other','Other')])
    source_url = models.URLField(blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=64, blank=True)
    telegram_contact = models.CharField(max_length=120, blank=True)
    physical_address = models.TextField(blank=True)
    organization_website = models.URLField(blank=True)
    raw_source_content = models.TextField(blank=True)
    content_fingerprint = models.CharField(max_length=64, blank=True, db_index=True)
    normalized_application_url = models.CharField(max_length=512, blank=True, db_index=True)
    dedupe_hash = models.CharField(max_length=255, unique=True, blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['country', 'status']), models.Index(fields=['deadline']), models.Index(fields=['organization'])]

    def is_expired(self):
        if self.deadline is None:
            return False
        from .services.deadlines import parse_deadline

        deadline = parse_deadline(self.deadline)
        if deadline is None:
            return False
        return deadline < timezone.now()

    @property
    def deadline_status(self):
        if self.deadline is None:
            return 'missing'
        from .services.deadlines import parse_deadline

        deadline = parse_deadline(self.deadline)
        if deadline is None:
            return 'missing'
        deadline_date = timezone.localtime(deadline).date()
        today = timezone.localdate()
        if deadline_date < today:
            return 'past'
        if deadline_date == today:
            return 'today'
        return 'upcoming'

    @property
    def deadline_status_label(self):
        return {
            'past': 'Past deadline',
            'today': 'Due today',
            'upcoming': 'Upcoming',
            'missing': 'Deadline not provided',
        }[self.deadline_status]

    def __str__(self):
        return self.title


class Application(models.Model):
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('matching', 'Matching'),
        ('prepared', 'Prepared'),
        ('pending', 'Pending'),
        ('submitted', 'Submitted'),
        ('rejected', 'Rejected'),
        ('failed', 'Failed'),
        ('needs_review', 'Needs Review'),
        ('cancelled', 'Cancelled'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='applications')
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name='applications')
    match_score = models.IntegerField(default=0)
    match_override = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued')
    attempts = models.IntegerField(default=0)
    error_message = models.TextField(blank=True)
    rejection_reason = models.TextField(blank=True)
    cover_letter = models.TextField(blank=True)
    generated_answers = models.JSONField(default=dict, blank=True)
    result_url = models.URLField(blank=True)
    submission_time = models.DateTimeField(blank=True, null=True)
    workflow_state = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    audit_history = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'opportunity'], name='unique_application_per_user_opportunity')]
        constraints += [
            models.CheckConstraint(
                condition=models.Q(match_score__gte=0) & models.Q(match_score__lte=100),
                name='application_match_score_0_100',
            ),
            models.CheckConstraint(
                condition=models.Q(attempts__gte=0),
                name='application_attempts_nonnegative',
            ),
        ]
        indexes = [models.Index(fields=['status', 'created_at']), models.Index(fields=['user', 'status'])]
        permissions = [
            ('retry_application', 'Can retry failed applications'),
            ('review_application', 'Can review pending applications'),
        ]

    def save(self, *args, **kwargs):
        adding = self._state.adding
        previous_status = None
        if not adding:
            previous_status = type(self).objects.filter(pk=self.pk).values_list('status', flat=True).first()

        if adding or (previous_status is not None and previous_status != self.status):
            history = list(self.audit_history or [])
            history.append({
                'time': timezone.now().isoformat(),
                'action': 'application_created' if adding else 'status_changed',
                'from_status': previous_status,
                'status': self.status,
            })
            self.audit_history = history[-50:]
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                kwargs['update_fields'] = set(update_fields) | {'audit_history'}

        result = super().save(*args, **kwargs)
        if adding or (previous_status is not None and previous_status != self.status):
            from .services.audit import record_audit_event

            action = 'application_created' if adding else {
                'submitted': 'application_submitted',
                'rejected': 'application_rejected',
                'failed': 'application_failed',
                'needs_review': 'application_needs_review',
                'cancelled': 'application_cancelled',
            }.get(self.status, 'application_status_changed')
            if previous_status in {'failed', 'rejected'} and self.status == 'queued':
                action = 'application_retried'
            record_audit_event(
                action,
                self.pk,
                {
                    'user_id': self.user_id,
                    'opportunity_id': self.opportunity_id,
                    'from_status': previous_status,
                    'status': self.status,
                    'match_score': self.match_score,
                    'attempts': self.attempts,
                },
            )

            from django.db import transaction

            if self.status in {'submitted', 'rejected', 'failed', 'needs_review'}:
                application_id = self.pk
                status = self.status

                def enqueue_status_notification():
                    try:
                        from .tasks import send_telegram_application_status_task

                        send_telegram_application_status_task.delay(application_id, status)
                    except Exception:
                        logger.exception(
                            'Could not queue Telegram notification for application %s '
                            'status %s.',
                            application_id,
                            status,
                        )

                transaction.on_commit(enqueue_status_notification)
        return result

    def __str__(self):
        return f'{self.user} -> {self.opportunity}'


class ApplicationAttempt(models.Model):
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name='attempts_log')
    attempt_number = models.IntegerField(default=1)
    status = models.CharField(max_length=20, choices=Application.STATUS_CHOICES, default='queued')
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['application', 'attempt_number'],
                name='unique_application_attempt_number',
            ),
            models.CheckConstraint(
                condition=models.Q(attempt_number__gte=1),
                name='application_attempt_number_positive',
            ),
        ]
        indexes = [models.Index(fields=['application', 'created_at'])]

    def __str__(self):
        return f'Attempt {self.attempt_number} for {self.application}'


class TelegramSource(models.Model):
    SCAN_FREQUENCIES = Source.SCAN_FREQUENCIES
    STATUS_CHOICES = Source.STATUS_CHOICES

    name = models.CharField(max_length=255)
    channel_url = models.URLField()
    country = models.CharField(max_length=120, blank=True)
    opportunity_types = models.JSONField(default=list, blank=True)
    enabled = models.BooleanField(default=True)
    trust_score = models.FloatField(default=0.0)
    scan_frequency = models.CharField(
        max_length=24,
        choices=SCAN_FREQUENCIES,
        default='daily',
    )
    last_scan = models.DateTimeField(blank=True, null=True)
    last_successful_scan = models.DateTimeField(blank=True, null=True)
    error_count = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    notes = models.TextField(blank=True)
    auto_discovered = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['enabled', 'status']),
            models.Index(fields=['scan_frequency']),
        ]
        permissions = [
            ('scan_telegram_sources', 'Can scan Telegram opportunity sources'),
        ]

    @property
    def source_type(self):
        return 'telegram'

    @property
    def url(self):
        return self.channel_url

    def clean(self):
        errors = {}
        parsed_url = urlsplit(self.channel_url)
        username = parsed_url.path.strip('/')
        if (
            parsed_url.scheme != 'https'
            or parsed_url.hostname not in {'t.me', 'telegram.me'}
            or parsed_url.username
            or parsed_url.password
            or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,30}[A-Za-z0-9]', username)
        ):
            errors['channel_url'] = (
                'Use a public channel URL such as https://t.me/channel_name; '
                'invite links and private channels are not supported.'
            )
        if not isinstance(self.opportunity_types, list):
            errors['opportunity_types'] = 'Opportunity types must be a list.'
        else:
            allowed_types = {value for value, _ in Opportunity.OPPORTUNITY_TYPES}
            unknown_types = set(self.opportunity_types) - allowed_types
            if unknown_types:
                errors['opportunity_types'] = (
                    'Unknown opportunity types: ' + ', '.join(sorted(unknown_types))
                )
        if not 0 <= self.trust_score <= 1:
            errors['trust_score'] = 'Trust score must be between 0 and 1.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.name


class Match(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='opportunity_matches')
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name='matches')
    score = models.PositiveSmallIntegerField(default=0)
    eligible = models.BooleanField(default=False)
    reasons = models.JSONField(default=list, blank=True)
    strong_matches = models.JSONField(default=list, blank=True)
    missing_requirements = models.JSONField(default=list, blank=True)
    risk_factors = models.JSONField(default=list, blank=True)
    recommended_action = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'opportunity'], name='unique_match_per_user_opportunity')]
        indexes = [
            models.Index(fields=['user', 'score'], name='opportunity_user_id_3b7a5e_idx'),
            models.Index(fields=['opportunity', 'score'], name='opportunity_opportu_9e0c48_idx'),
            models.Index(fields=['eligible', 'score'], name='opportunity_eligibl_52b9e6_idx'),
        ]
        ordering = ['-score', '-updated_at']

    def __str__(self):
        return f'{self.user} ↔ {self.opportunity} ({self.score}%)'


class TelegramDestination(models.Model):
    TYPE_CHOICES = [
        ('applied', 'Applied Applications'),
        ('rejected', 'Rejected Applications'),
        ('failed', 'Failed Applications'),
        ('needs_review', 'Needs Review Applications'),
        ('system_alert', 'System Alerts'),
        ('destination', 'Destination Group'),
        ('admin', 'Admin Destination'),
    ]
    name = models.CharField(max_length=255)
    chat_id = models.CharField(max_length=120)
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='destination')
    enabled = models.BooleanField(default=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('chat_id', 'type')
        permissions = [('manage_telegram', 'Can manage Telegram configuration')]

    def __str__(self):
        return f'{self.name} ({self.chat_id})'


class EmailMailbox(models.Model):
    """Authorized mailbox used for account registration and non-MFA verification links."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='email_mailboxes')
    name = models.CharField(max_length=255)
    email = models.EmailField()
    encrypted_app_password = models.TextField(blank=True)
    imap_host = models.CharField(max_length=255, blank=True)
    imap_port = models.PositiveIntegerField(default=993)
    imap_ssl = models.BooleanField(default=True)
    enabled = models.BooleanField(default=True)
    last_checked_at = models.DateTimeField(blank=True, null=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user','email','name'], name='unique_email_mailbox')]
        indexes = [models.Index(fields=['user','enabled'], name='emailbox_user_enabled_idx')]

    def set_app_password(self, value):
        from .services.credential_vault import encrypt_secret
        self.encrypted_app_password = encrypt_secret(value or '')

    def get_app_password(self):
        from .services.credential_vault import decrypt_secret
        return decrypt_secret(self.encrypted_app_password)

    def __str__(self):
        return f'{self.name} — {self.email}'


class SiteCredential(models.Model):
    AUTH_CHOICES = [
        ('form', 'Username / Password Form'),
        ('basic', 'HTTP Basic Auth'),
        ('token', 'Token / Secret'),
        ('other', 'Other'),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='site_credentials')
    name = models.CharField(max_length=255)
    domain = models.CharField(max_length=255, db_index=True)
    login_url = models.URLField(blank=True)
    registration_url = models.URLField(blank=True)
    auto_register = models.BooleanField(default=False)
    email_mailbox = models.ForeignKey('EmailMailbox', on_delete=models.SET_NULL, null=True, blank=True, related_name='site_credentials')
    account_status = models.CharField(max_length=30, choices=[('not_configured','Not configured'),('registration_pending','Registration pending'),('verification_pending','Verification pending'),('ready','Ready'),('failed','Failed'),('disabled','Disabled')], default='not_configured')
    registration_error = models.TextField(blank=True)
    last_registration_at = models.DateTimeField(blank=True, null=True)
    username = models.CharField(max_length=255, blank=True)
    encrypted_password = models.TextField(blank=True)
    encrypted_secret = models.TextField(blank=True)
    auth_type = models.CharField(max_length=20, choices=AUTH_CHOICES, default='form')
    enabled = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
    last_used_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'domain', 'name'], name='unique_site_credential')]
        indexes = [models.Index(fields=['user', 'domain', 'enabled'], name='sitecred_user_domain_idx')]

    def set_password(self, value):
        from .services.credential_vault import encrypt_secret
        self.encrypted_password = encrypt_secret(value or '')

    def get_password(self):
        from .services.credential_vault import decrypt_secret
        return decrypt_secret(self.encrypted_password)

    def set_secret(self, value):
        from .services.credential_vault import encrypt_secret
        self.encrypted_secret = encrypt_secret(value or '')

    def get_secret(self):
        from .services.credential_vault import decrypt_secret
        return decrypt_secret(self.encrypted_secret)

    def __str__(self):
        return f'{self.name} — {self.domain}'


class ApplicationFormTemplate(models.Model):
    FORM_TYPES = [('pdf','PDF'), ('docx','DOCX'), ('web','Web Form')]
    name = models.CharField(max_length=255)
    domains = models.JSONField(default=list, blank=True)
    form_type = models.CharField(max_length=20, choices=FORM_TYPES, default='web')
    download_url = models.URLField(blank=True)
    file = models.FileField(upload_to='application_forms/', blank=True, null=True)
    field_map = models.JSONField(default=dict, blank=True)
    selector_map = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def matches_domain(self, domain):
        domain = (domain or '').lower().split(':')[0]
        return any(domain == str(d).lower() or domain.endswith('.' + str(d).lower()) for d in (self.domains or []))

    def __str__(self):
        return self.name


class ApplicationArtifact(models.Model):
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name='artifacts')
    kind = models.CharField(max_length=40)
    file = models.FileField(upload_to='application_artifacts/')
    label = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.kind}: {self.label or self.file.name}'


class AuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    action = models.CharField(max_length=255)
    target = models.CharField(max_length=255, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['action', 'timestamp'])]

    def __str__(self):
        return f'{self.action} at {self.timestamp}'


class ProviderAdapter(models.Model):
    name = models.CharField(max_length=255, unique=True)
    adapter_type = models.CharField(max_length=80, default='generic')
    enabled = models.BooleanField(default=True)
    config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [('manage_automation', 'Can manage automation configuration')]

    def __str__(self):
        return self.name


class AutomationRun(models.Model):
    STATUS_CHOICES = [
        ('running', 'Running'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='running')
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(blank=True, null=True)
    details = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f'{self.status} at {self.started_at}'


class SystemSetting(models.Model):
    key = models.CharField(max_length=255, unique=True)
    value = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [
            ('manage_ai_settings', 'Can manage AI configuration'),
            ('manage_security_settings', 'Can manage security settings'),
        ]

    def __str__(self):
        return self.key


REGISTRATION_MODE_OPEN = 'open'
REGISTRATION_MODE_ADMIN_APPROVAL = 'admin_approval'
REGISTRATION_MODES = {
    REGISTRATION_MODE_OPEN: 'Open registration',
    REGISTRATION_MODE_ADMIN_APPROVAL: 'Admin approval required',
}

WEBSITE_VISIBILITY_PUBLIC = 'public'
WEBSITE_VISIBILITY_PRIVATE = 'private'
WEBSITE_VISIBILITY_MODES = {
    WEBSITE_VISIBILITY_PUBLIC: 'Public / active',
    WEBSITE_VISIBILITY_PRIVATE: 'Private / hidden',
}


def get_website_visibility():
    value = SystemSetting.objects.filter(key='website_visibility').values_list(
        'value',
        flat=True,
    ).first()
    if value is None:
        return WEBSITE_VISIBILITY_PUBLIC
    normalized = value.strip().lower()
    return (
        normalized
        if normalized in WEBSITE_VISIBILITY_MODES
        else WEBSITE_VISIBILITY_PRIVATE
    )


def set_website_visibility(value, actor=None):
    mode = (value or '').strip().lower()
    if mode not in WEBSITE_VISIBILITY_MODES:
        raise ValueError(f'Unsupported website visibility: {value}')
    setting, _ = SystemSetting.objects.get_or_create(key='website_visibility')
    setting.value = mode
    setting.save(update_fields=['value', 'updated_at'])
    if actor is not None:
        from .services.audit import record_audit_event
        record_audit_event(
            'WEBSITE_VISIBILITY_CHANGED',
            'website_visibility',
            {'website_visibility': mode, 'actor_id': actor.pk},
            actor=actor,
        )
    return setting


def validate_private_access_token(raw_token):
    token = (raw_token or '').strip()
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
    now = timezone.now()
    return (
        PrivateAccessToken.objects.filter(token_hash=token_hash)
        .filter(active=True, revoked_at__isnull=True, disabled_at__isnull=True)
        .filter(models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now))
        .filter(used_count__lt=models.F('max_uses'))
        .select_related('created_by')
        .first()
    )


def get_registration_mode():
    value = SystemSetting.objects.filter(key='registration_mode').values_list(
        'value',
        flat=True,
    ).first()
    if value is None:
        return REGISTRATION_MODE_OPEN
    normalized = value.strip().lower()
    return (
        normalized
        if normalized in REGISTRATION_MODES
        else REGISTRATION_MODE_ADMIN_APPROVAL
    )


def set_registration_mode(value, actor=None):
    mode = (value or '').strip().lower()
    if mode not in REGISTRATION_MODES:
        raise ValueError(f'Unsupported registration mode: {value}')
    setting, _ = SystemSetting.objects.get_or_create(key='registration_mode')
    setting.value = mode
    setting.save(update_fields=['value', 'updated_at'])
    if actor is not None:
        from .services.audit import record_audit_event
        record_audit_event(
            'REGISTRATION_MODE_CHANGED',
            'registration_mode',
            {'registration_mode': mode, 'actor_id': actor.pk},
            actor=actor,
        )
    return setting


class PrivateAccessToken(models.Model):
    token_hash = models.CharField(max_length=128, unique=True)
    label = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='private_access_tokens',
    )
    recipient_email = models.EmailField(blank=True)
    expires_at = models.DateTimeField(blank=True, null=True)
    revoked_at = models.DateTimeField(blank=True, null=True)
    disabled_at = models.DateTimeField(blank=True, null=True)
    active = models.BooleanField(default=True)
    allowed_registration = models.BooleanField(default=True)
    max_uses = models.PositiveIntegerField(default=1)
    used_count = models.PositiveIntegerField(default=0)
    last_used_at = models.DateTimeField(blank=True, null=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at', '-pk')
        indexes = [
            models.Index(fields=['active', 'expires_at']),
            models.Index(fields=['revoked_at', 'disabled_at']),
        ]

    @property
    def is_expired(self):
        return self.expires_at is not None and timezone.now() >= self.expires_at

    @property
    def is_revoked(self):
        return self.revoked_at is not None

    @property
    def is_disabled(self):
        return self.disabled_at is not None or not self.active

    @property
    def is_valid(self):
        return (
            self.active
            and not self.is_expired
            and not self.is_revoked
            and not self.is_disabled
            and self.used_count < self.max_uses
        )

    def consume(self):
        now = timezone.now()
        updated = (
            type(self).objects.filter(
                pk=self.pk,
                active=True,
                revoked_at__isnull=True,
                disabled_at__isnull=True,
                used_count__lt=models.F('max_uses'),
            )
            .filter(
                models.Q(expires_at__isnull=True)
                | models.Q(expires_at__gt=now)
            )
            .update(
                active=models.Case(
                    models.When(
                        used_count__gte=models.F('max_uses') - 1,
                        then=models.Value(False),
                    ),
                    default=models.Value(True),
                    output_field=models.BooleanField(),
                ),
                used_count=models.F('used_count') + 1,
                last_used_at=now,
                updated_at=now,
            )
        )
        if not updated:
            return False

        self.used_count += 1
        self.last_used_at = now
        if self.used_count >= self.max_uses:
            self.active = False
        return True

    @staticmethod
    def issue_token(created_by=None, *, label='', description='', recipient_email='', expires_at=None, max_uses=1, allowed_registration=True):
        raw_token = secrets.token_urlsafe(32)
        token = PrivateAccessToken.objects.create(
            token_hash=hashlib.sha256(raw_token.encode('utf-8')).hexdigest(),
            label=label or 'Private access link',
            description=description,
            created_by=created_by,
            recipient_email=recipient_email,
            expires_at=expires_at,
            allowed_registration=allowed_registration,
            max_uses=max_uses,
        )
        token._raw_token = raw_token
        return token, raw_token

    @property
    def link(self):
        raw_token = getattr(self, '_raw_token', None)
        if not raw_token:
            return ''
        site_url = getattr(settings, 'SITE_URL', '').strip().rstrip('/')
        if not site_url:
            return f'/private-access/{raw_token}/'
        return f'{site_url}/private-access/{raw_token}/'

    def __str__(self):
        return self.label or f'Private access token {self.pk}'


class AIConversation(models.Model):
    CHANNEL_CHOICES = (
        ('web', 'Web chat'),
        ('telegram', 'Telegram'),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='ai_conversations',
    )
    channel = models.CharField(max_length=12, choices=CHANNEL_CHOICES, default='web')
    title = models.CharField(max_length=160, default='New conversation')
    pending_confirmation = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-updated_at', '-pk')
        indexes = [models.Index(fields=('user', '-updated_at'))]

    def __str__(self):
        return f'{self.title} ({self.user})'


class AIMessage(models.Model):
    ROLE_CHOICES = (
        ('user', 'User'),
        ('assistant', 'Assistant'),
        ('tool', 'Tool'),
    )

    conversation = models.ForeignKey(
        AIConversation,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    role = models.CharField(max_length=12, choices=ROLE_CHOICES)
    content = models.TextField()
    tool_name = models.CharField(max_length=80, blank=True)
    tool_arguments = models.JSONField(default=dict, blank=True)
    tool_result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('created_at', 'pk')
        indexes = [models.Index(fields=('conversation', 'created_at'))]

    def __str__(self):
        return f'{self.role}: {self.content[:80]}'
