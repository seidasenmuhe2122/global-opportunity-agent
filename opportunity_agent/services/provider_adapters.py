from __future__ import annotations

import re
import os
import hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit
from django.conf import settings
from django.utils import timezone
from django.core.files.base import ContentFile
from .document_forms import download_form, fill_pdf, fill_docx
from .file_storage import local_file_path
from .account_registration import register_site_account
from .public_http import public_addresses
from ..models import ApplicationArtifact


TOKEN_MAP = {
    'full_name': lambda p, a: p.full_name or a.user.get_full_name() or a.user.username,
    'email': lambda p, a: a.user.email,
    'phone': lambda p, a: p.phone,
    'country': lambda p, a: p.current_country,
    'degree': lambda p, a: p.degree,
    'education': lambda p, a: p.education,
    'experience': lambda p, a: p.work_experience,
    'skills': lambda p, a: ', '.join(p.skills or []),
    'languages': lambda p, a: ', '.join(p.languages or []),
    'certifications': lambda p, a: ', '.join(p.certifications or []),
    'portfolio_url': lambda p, a: p.portfolio_url,
    'linkedin_url': lambda p, a: p.linkedin_url,
    'github_url': lambda p, a: p.github_url,
    'cover_letter': lambda p, a: a.cover_letter,
    'job_title': lambda p, a: a.opportunity.title,
    'organization': lambda p, a: a.opportunity.organization,
}


def _domain(url):
    return (urlparse(url or '').hostname or '').lower().split(':')[0]


def _safe_navigation_url(url):
    try:
        public_addresses(url)
    except ValueError:
        return False
    return True


def _state_url(url):
    parsed = urlsplit(url or '')
    sensitive = {
        'token', 'access_token', 'refresh_token', 'auth', 'code', 'state',
        'session', 'sessionid', 'sid', 'key', 'ticket', 'password',
    }
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in sensitive
    ]
    path_parts = parsed.path.split('/')
    for index, part in enumerate(path_parts[:-1]):
        if part.casefold() in {
            'verify', 'verification', 'token', 'reset', 'password-reset',
            'activate', 'activation',
        }:
            path_parts[index + 1] = 'redacted'
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        '/'.join(path_parts),
        urlencode(query),
        '',
    ))


def _safe_error_text(value):
    message = str(value or '')[:4000]
    for name, secret in os.environ.items():
        upper_name = name.upper()
        if secret and len(secret) >= 6 and any(
            marker in upper_name
            for marker in ('PASSWORD', 'SECRET', 'TOKEN', 'API_KEY', 'AUTH', 'BOT')
        ):
            message = message.replace(secret, '[redacted]')
    message = re.sub(
        r'(?i)([?&](?:token|access_token|refresh_token|auth|code|state|session|key|ticket|password)=)[^&\s]+',
        r'\1[redacted]',
        message,
    )
    return re.sub(
        r'(?i)\b(password|passwd|secret|token|api[_-]?key)\s*([:=]\s*)[^\s,;]+',
        r'\1\2[redacted]',
        message,
    )


def _action_intent(label, context='', href=''):
    label = re.sub(r'\s+', ' ', label or '').strip().casefold()
    context = re.sub(r'\s+', ' ', context or '').strip().casefold()
    path = urlsplit(href or '').path.casefold()
    if re.search(r'\b(?:save\s+and\s+continue|continue|next|proceed|review)\b', label):
        return 'continue'
    if (
        re.search(r'\b(?:instructions?|how to apply|application process|application guidelines)\b', label)
        or re.search(r'\b(?:instructions?|how to apply|application process|application guidelines)\b', path)
        or (
            re.search(r'\b(?:instructions?|how to apply|application process|application guidelines)\b', context)
            and re.search(r'\b(?:read|view|see|follow|review)\b', context)
        )
    ):
        return 'instructions'
    if re.search(r'\b(?:register|create\s+account|sign\s+up|sign\s+in|log\s*in|login)\b', label):
        return 'authentication'
    if re.search(r'\b(?:submit|finish|confirm|send)\b', label):
        return 'submit'
    if re.search(r'\b(?:start|begin|complete)\s+(?:an?\s+)?application\b|\bapply(?:\s+(?:now|here))?\b', label):
        return 'apply'
    return ''


def _token_value(token, profile, application):
    if callable(TOKEN_MAP.get(token)):
        return TOKEN_MAP[token](profile, application) or ''
    return str(token)


def _close_browser_context(context, browser):
    context.close()
    if browser is not None:
        browser.close()


class ProviderAdapter:
    adapter_type = 'generic'
    def __init__(self, name='generic', config=None, credential=None, form_template=None):
        self.name = name
        self.config = config or {}
        self.credential = credential
        self.form_template = form_template

    def can_apply(self, opportunity):
        return bool(opportunity.application_url)

    def preflight(self, application):
        return False, 'Unsupported application provider: no verified submission adapter is available.'

    def submit(self, application):
        application.status = 'needs_review'
        application.error_message = 'Unsupported application provider: no provider adapter is configured for this domain.'
        application.save(update_fields=['status', 'error_message', 'updated_at'])
        return False


class ManualReviewAdapter(ProviderAdapter):
    adapter_type = 'manual'


class PlaywrightConfiguredAdapter(ProviderAdapter):
    adapter_type = 'playwright_configured'

    def can_apply(self, opportunity):
        domain = _domain(opportunity.application_url)
        allowed = [str(x).lower() for x in self.config.get('allowed_domains', [])]
        return bool(opportunity.application_url and allowed and (domain in allowed or any(domain.endswith('.' + x) for x in allowed)))

    def preflight(self, application):
        if not self.can_apply(application.opportunity):
            return False, 'Unsupported application provider: application domain is not allowed by this adapter.'
        if not self.config.get('verified', False):
            return False, 'Provider adapter is not verified.'
        if not self.config.get('allow_submit', False):
            return False, 'Provider adapter is not enabled for automatic submission.'
        if self.credential and not self.credential.enabled:
            return False, 'Manual authentication required: the configured user credential is disabled.'
        if self.credential and self.credential.auth_type == 'form':
            has_username = bool(
                self.credential.username
                or (
                    self.credential.email_mailbox
                    and self.credential.email_mailbox.email
                )
            )
            if not has_username:
                return False, 'Manual authentication required: the website login username is missing.'
            if not self.credential.encrypted_password and not self.credential.auto_register:
                return False, 'Manual authentication required: the website password is missing.'
            if (
                self.credential.auto_register
                and self.credential.account_status != 'ready'
                and not self.credential.email_mailbox_id
            ):
                return False, 'Manual authentication required: site registration needs a configured verification mailbox.'
        elif self.credential and self.credential.auth_type != 'form':
            return False, 'Unsupported authentication method for this provider adapter; manual authentication is required.'
        elif not self.credential and not self.config.get('public_application_page', False):
            return False, 'Manual authentication required: configure an authorized user credential or verify a public application page.'
        return True, ''

    def _fill_selector_map(self, page, selector_map, profile, application):
        for selector, token in (selector_map or {}).items():
            value = _token_value(token, profile, application)
            if value is None:
                continue
            locator = page.locator(selector).first
            locator.fill(str(value))

    def _smart_fill(self, page, profile, application):
        labels = {
            'email': 'email',
            'e-mail': 'email',
            'first name': 'first_name',
            'given name': 'first_name',
            'last name': 'last_name',
            'surname': 'last_name',
            'full name': 'full_name',
            'name': 'full_name',
            'phone': 'phone',
            'mobile': 'phone',
            'country of residence': 'country',
            'degree': 'degree',
            'education': 'education',
            'experience': 'experience',
            'work experience': 'experience',
            'skills': 'skills',
            'languages': 'languages',
            'certification': 'certifications',
            'linkedin': 'linkedin_url',
            'portfolio': 'portfolio_url',
            'github': 'github_url',
            'cover letter': 'cover_letter',
        }
        profile_name = profile.full_name or application.user.get_full_name() or application.user.username
        name_parts = profile_name.split()
        values = {
            'first_name': name_parts[0] if name_parts else '',
            'last_name': ' '.join(name_parts[1:]) if len(name_parts) > 1 else '',
        }
        filled = []
        for locator in page.locator('input:not([type=hidden]), textarea, select').all():
            if not locator.is_visible():
                continue
            tag = locator.evaluate('(e)=>e.tagName.toLowerCase()')
            input_type = (locator.get_attribute('type') or '').lower()
            if input_type in {'password', 'checkbox', 'radio', 'submit', 'button', 'file'}:
                continue
            name = ' '.join(filter(None, [
                locator.get_attribute('name'),
                locator.get_attribute('id'),
                locator.get_attribute('placeholder'),
                locator.get_attribute('aria-label'),
                locator.evaluate(
                    """e => e.labels ? [...e.labels].map(label => label.innerText).join(' ') : ''"""
                ),
            ])).casefold()
            if re.search(r'\b(?:nationality|citizenship|citizen)\b', name):
                continue
            token = next((
                value
                for key, value in sorted(labels.items(), key=lambda item: -len(item[0]))
                if re.search(r'\b' + re.escape(key) + r'\b', name)
            ), None)
            if not token:
                continue
            value = values.get(token) or _token_value(token, profile, application)
            if tag == 'select':
                options = locator.locator('option').all_text_contents()
                match = next((option for option in options if value and str(value).casefold() == option.strip().casefold()), None)
                if match:
                    locator.select_option(label=match)
                    filled.append(name)
            elif value and tag in {'input', 'textarea'}:
                locator.fill(str(value))
                filled.append(name)
        return filled

    def _required_field_issues(self, page):
        issues = []
        for locator in page.locator(
            'input:required, textarea:required, select:required, '
            '[aria-required="true"]'
        ).all():
            if not locator.is_visible() or not locator.is_enabled():
                continue
            state = locator.evaluate(
                """element => ({
                    valid: typeof element.checkValidity === 'function' ? element.checkValidity() : true,
                    value: element.type === 'checkbox' || element.type === 'radio'
                        ? (element.type === 'radio'
                            ? ([...document.querySelectorAll('input[type="radio"]')]
                                .some(input => input.name === element.name && input.checked) ? 'checked' : '')
                            : (element.checked ? 'checked' : ''))
                        : element.value,
                    label: element.labels && element.labels.length
                        ? element.labels[0].innerText
                        : (element.getAttribute('aria-label') || element.getAttribute('placeholder')
                            || element.name || element.id || element.type || element.tagName)
                })"""
            )
            if not state.get('valid') or not state.get('value'):
                issues.append(str(state.get('label') or 'unnamed required field').strip())
        return issues

    def _security_challenge_present(self, page):
        text=page.locator('body').inner_text(timeout=3000).lower()
        challenge_words = (
            'captcha', 'recaptcha', 'hcaptcha', 'two-factor authentication',
            'multi-factor authentication', 'verification code', 'security challenge',
            'verify you are human', 'unusual traffic', 'checking your browser',
            'access denied',
        )
        challenge_frame = page.locator(
            'iframe[src*="captcha" i], iframe[title*="captcha" i], '
            'iframe[src*="challenge" i]'
        ).count() > 0
        return challenge_frame or any(word in text for word in challenge_words)

    def _save_workflow(self, application, state, *, stage, page=None, action='', error=''):
        now = timezone.now().isoformat()
        current_url = _state_url(page.url) if page is not None else state.get('current_url', '')
        previous_url = state.get('current_url', '')
        state.update({
            'stage': stage,
            'previous_url': previous_url,
            'current_url': current_url,
            'pending_action': action,
            'updated_at': now,
            'retry_count': max(0, application.attempts - 1),
        })
        if error:
            state.setdefault('errors', []).append({'time': now, 'message': error})
        if page is not None:
            state.setdefault('steps', []).append({
                'time': now,
                'stage': stage,
                'url': current_url,
                'action': action,
            })
        application.workflow_state = state
        application.save(update_fields=['workflow_state', 'updated_at'])

    def _workflow_action(self, page):
        candidates = []
        for role in ('button', 'link'):
            for locator in page.get_by_role(role).all():
                if not locator.is_visible() or not locator.is_enabled():
                    continue
                label = ' '.join(filter(None, (
                    locator.inner_text().strip(),
                    locator.get_attribute('aria-label'),
                    locator.get_attribute('title'),
                    locator.get_attribute('value'),
                )))
                context = locator.evaluate(
                    """e => [e.parentElement?.innerText, e.parentElement?.parentElement?.innerText]
                        .filter(Boolean).join(' ')"""
                )
                href = locator.get_attribute('href') or ''
                intent = _action_intent(label, context, href)
                if intent:
                    candidates.append((intent, label, locator))
        configured_selector = self.config.get('submit_selector')
        if configured_selector:
            configured = page.locator(configured_selector).first
            if configured.count() and configured.is_visible() and configured.is_enabled():
                label = ' '.join(filter(None, (
                    configured.inner_text().strip(),
                    configured.get_attribute('aria-label'),
                    configured.get_attribute('title'),
                    configured.get_attribute('value'),
                ))) or 'Configured submit action'
                candidates.append(('submit', label, configured))
        priority = {
            'continue': 0,
            'instructions': 1,
            'authentication': 2,
            'apply': 3,
            'submit': 4,
        }
        candidates.sort(key=lambda item: priority[item[0]])
        return candidates[0] if candidates else None

    def _page_type(self, page, text):
        if re.search(r'\b(?:create your account|register your account|sign up for an account)\b', text, re.I):
            return 'registration'
        if page.locator('input[type=password]').count():
            return 'login'
        if page.locator('input[type=file]').count():
            return 'documents'
        if re.search(
            r'\b(?:review your application|review and submit|final review|check your application)\b',
            text,
            re.I,
        ):
            return 'review'
        if page.locator('input:not([type=hidden]), textarea, select').count():
            return 'form'
        if re.search(r'\b(?:register|create account|sign up)\b', text, re.I):
            return 'registration'
        if re.search(r'\b(?:instruction|how to apply|application process)\b', text, re.I):
            return 'instructions'
        return 'navigation'

    def _upload_documents(self, page, profile, application, state):
        from ..models import ApplicationArtifact

        missing = []
        uploaded = set(state.get('documents_uploaded', []))
        configured_cv_selector = self.config.get('cv_upload_selector')
        inputs = page.locator('input[type=file]').all()
        for index, locator in enumerate(inputs):
            if not locator.is_enabled():
                continue
            label = ' '.join(filter(None, (
                locator.get_attribute('aria-label'),
                locator.get_attribute('name'),
                locator.get_attribute('id'),
                locator.evaluate(
                    """e => e.labels ? [...e.labels].map(label => label.innerText).join(' ') : ''"""
                ),
            ))).strip()
            configured_cv = False
            if configured_cv_selector:
                configured_locator = page.locator(configured_cv_selector).first
                configured_cv = bool(
                    configured_locator.count()
                    and locator.evaluate(
                        '(element, selector) => element.matches(selector)',
                        configured_cv_selector,
                    )
                )
            if configured_cv:
                label = label or 'CV/resume'
            required = bool(
                locator.get_attribute('required')
                or locator.get_attribute('aria-required') == 'true'
            )
            request = label.casefold()
            key = f'{_state_url(page.url)}:{index}:{request}'
            if key in uploaded:
                continue
            document = None
            if re.search(r'\b(?:cv|resume|curriculum vitae)\b', request) and profile.cv:
                document = profile.cv
            if document is None and request:
                document_types = {
                    'cv': {'cv', 'resume', 'curriculum vitae'},
                    'cover_letter': {'cover letter'},
                    'transcript': {'transcript', 'academic record'},
                    'degree': {'degree', 'diploma'},
                    'certificate': {'certificate', 'certification'},
                    'recommendation': {'recommendation', 'reference letter'},
                    'portfolio': {'portfolio'},
                    'passport': {'passport'},
                    'identity': {'identity card', 'national id', 'identification'},
                }
                requested_types = {
                    kind for kind, aliases in document_types.items()
                    if any(alias in request for alias in aliases)
                }
                for artifact in ApplicationArtifact.objects.filter(application=application):
                    artifact_description = f'{artifact.kind} {artifact.label}'.casefold()
                    artifact_types = {
                        kind for kind, aliases in document_types.items()
                        if any(alias in artifact_description for alias in aliases)
                    }
                    if requested_types and requested_types & artifact_types:
                        document = artifact.file
                        break
            if document is not None:
                with local_file_path(document) as document_path:
                    locator.set_input_files(document_path)
                uploaded.add(key)
            elif required:
                missing.append(label or 'Unlabeled required document')
        state['documents_uploaded'] = sorted(uploaded)
        return missing

    def _upload_form_template(self, page, application, profile, state):
        if (
            not self.form_template
            or self.form_template.form_type not in {'pdf', 'docx'}
            or state.get('form_template_uploaded')
        ):
            return
        if not page.locator('input[type=file]').count():
            return
        from io import BytesIO

        if self.form_template.file:
            source = self.form_template.file
        elif self.form_template.download_url:
            source_bytes, _ = download_form(self.form_template.download_url)
            source = BytesIO(source_bytes)
        else:
            raise ValueError('Application form template has no file or download URL.')
        if self.form_template.form_type == 'pdf':
            raw = fill_pdf(source, self.form_template.field_map, profile, application)
            extension = 'pdf'
        else:
            raw = fill_docx(source, profile, application)
            extension = 'docx'
        artifact = ApplicationArtifact(
            application=application,
            kind='filled_form',
            label=self.form_template.name,
        )
        artifact.file.save(
            f'filled-{application.pk}-{self.form_template.pk}.{extension}',
            ContentFile(raw),
            save=True,
        )
        upload_selector = (
            self.config.get('form_upload_selector')
            or (self.form_template.selector_map or {}).get('__upload__')
        )
        if not upload_selector or not page.locator(upload_selector).count():
            raise ValueError('No form_upload_selector configured for the PDF application form.')
        with local_file_path(artifact.file) as artifact_path:
            page.locator(upload_selector).set_input_files(artifact_path)
        state['form_template_uploaded'] = True

    def _workflow_fingerprint(self, page):
        body = page.locator('body').inner_text(timeout=10000) or ''
        title = page.title() or ''
        value = '\n'.join((page.url, title, re.sub(r'\s+', ' ', body)))
        return hashlib.sha256(value.encode('utf-8')).hexdigest(), body

    def _workflow_confirmation(self, page, previous_text, selector_was_visible=False):
        text = (page.locator('body').inner_text(timeout=10000) or '').casefold()
        configured = self.config.get('confirmation_selector')
        selector_confirmed = bool(
            not selector_was_visible
            and
            configured
            and page.locator(configured).count()
            and page.locator(configured).is_visible()
        )
        phrases = self.config.get('confirmation_words', (
            'thank you', 'application received', 'successfully submitted',
            'application submitted', 'submission complete',
        ))
        text_confirmed = any(
            str(phrase).strip().casefold() in text
            and str(phrase).strip().casefold() not in previous_text
            for phrase in phrases
            if str(phrase).strip()
        )
        return selector_confirmed or text_confirmed

    def _workflow_result(self, application, state, outcome, error, page=None):
        status = 'needs_review'
        state['outcome'] = outcome
        state['human_action_required'] = outcome == 'human_action_required'
        self._save_workflow(
            application,
            state,
            stage=outcome,
            page=page,
            error=error,
        )
        application.status = status
        application.error_message = error
        application.result_url = _state_url(page.url) if page is not None else ''
        application.save(update_fields=[
            'status', 'error_message', 'result_url', 'workflow_state', 'updated_at',
        ])
        return False

    def _workflow_remaining_seconds(self, state):
        started_at = state.get('workflow_started_at')
        if not started_at:
            state['workflow_started_at'] = timezone.now().isoformat()
            return float(settings.APPLICATION_WORKFLOW_BUDGET_SECONDS)
        try:
            started_at = datetime.fromisoformat(started_at)
            if timezone.is_naive(started_at):
                started_at = timezone.make_aware(started_at)
        except (TypeError, ValueError):
            return 0
        elapsed = (timezone.now() - started_at).total_seconds()
        if elapsed < 0:
            return 0
        state['workflow_elapsed_seconds'] = max(0, int(elapsed))
        return settings.APPLICATION_WORKFLOW_BUDGET_SECONDS - elapsed

    def _workflow_budget_exceeded(self, state):
        return self._workflow_remaining_seconds(state) <= 0

    def _workflow_operation_timeout(self, state, maximum_ms):
        remaining_ms = int(self._workflow_remaining_seconds(state) * 1000)
        return max(1, min(maximum_ms, remaining_ms))

    def _run_dynamic_workflow(self, application, context, browser):
        profile = getattr(application.user, 'profile', None)
        if profile is None:
            return self._workflow_result(
                application,
                application.workflow_state or {},
                'blocked',
                'User profile is missing.',
            )
        state = dict(application.workflow_state or {})
        state.setdefault('steps', [])
        state.setdefault('completed_actions', [])
        state.setdefault('fields_completed', [])
        state.setdefault('required_documents', [])
        state.setdefault('errors', [])
        if self._workflow_budget_exceeded(state):
            return self._workflow_result(
                application,
                state,
                'needs_review',
                'Application workflow time budget expired; manual review is required.',
            )
        self._save_workflow(
            application,
            state,
            stage='in_progress',
            action='workflow_started',
        )
        entry_url = application.opportunity.application_url
        resume_url = state.get('current_url') if state.get('stage') == 'in_progress' else ''
        login_url = self.credential.login_url if self.credential else ''
        login_entry = bool(login_url and not resume_url)
        if login_entry and _domain(login_url) != self.credential.domain.casefold():
            return self._workflow_result(
                application,
                state,
                'human_action_required',
                'HUMAN_ACTION_REQUIRED: Configured login URL does not match the authorized credential domain.',
            )
        start_url = (
            resume_url if resume_url and _safe_navigation_url(resume_url)
            else login_url if login_entry
            else entry_url
        )
        blocked_navigation = []

        def route_request(route):
            request = route.request
            scheme = urlsplit(request.url).scheme.casefold()
            if scheme in {'http', 'https'} and not _safe_navigation_url(request.url):
                blocked_navigation.append(_state_url(request.url))
                route.abort()
                return
            if scheme not in {'http', 'https', 'data', 'blob', 'about'}:
                blocked_navigation.append(_state_url(request.url))
                route.abort()
                return
            route.continue_()

        context.route('**/*', route_request)
        page = context.new_page()
        if self._workflow_budget_exceeded(state):
            return self._workflow_result(
                application,
                state,
                'needs_review',
                'Application workflow time budget expired; manual review is required.',
            )
        page.set_default_timeout(self._workflow_operation_timeout(
            state,
            int(self.config.get('timeout_ms', settings.BROWSER_TIMEOUT_MS)),
        ))
        self._save_workflow(application, state, stage='in_progress', page=page, action='open')
        try:
            page.goto(
                start_url,
                wait_until='domcontentloaded',
                timeout=self._workflow_operation_timeout(
                    state,
                    int(self.config.get('timeout_ms', settings.BROWSER_TIMEOUT_MS)),
                ),
            )
        except Exception:
            if blocked_navigation:
                return self._workflow_result(
                    application,
                    state,
                    'blocked',
                    'Navigation to a non-public or unsafe destination was blocked.',
                    page,
                )
            raise
        visited = set()
        submission_started = False

        while True:
            if self._workflow_budget_exceeded(state):
                return self._workflow_result(
                    application,
                    state,
                    'needs_review',
                    'Application workflow time budget expired; manual review is required.',
                    page,
                )
            if blocked_navigation:
                return self._workflow_result(
                    application,
                    state,
                    'blocked',
                    'Navigation to a non-public or unsafe destination was blocked.',
                    page,
                )
            if self._security_challenge_present(page):
                return self._workflow_result(
                    application,
                    state,
                    'human_action_required',
                    'HUMAN_ACTION_REQUIRED: CAPTCHA, MFA, or another security challenge was detected.',
                    page,
                )

            fingerprint, body = self._workflow_fingerprint(page)
            if fingerprint in visited:
                return self._workflow_result(
                    application,
                    state,
                    'blocked',
                    'Workflow stopped because the application page repeated without progress.',
                    page,
                )
            visited.add(fingerprint)
            state['page_type'] = self._page_type(page, body)
            state['current_url'] = _state_url(page.url)
            text = body.casefold()
            page_type = state['page_type']
            if any(phrase in text for phrase in (
                'already applied', 'already submitted an application',
                'application has already been received',
            )):
                return self._workflow_result(
                    application,
                    state,
                    'already_applied',
                    'ALREADY_APPLIED: The application portal indicates this account already applied.',
                    page,
                )
            if any(phrase in text for phrase in (
                'application deadline has passed', 'applications are closed',
                'position is no longer accepting applications', 'opportunity has expired',
            )):
                return self._workflow_result(
                    application,
                    state,
                    'expired',
                    'EXPIRED: The application portal indicates applications are closed.',
                    page,
                )

            password_fields = page.locator('input[type=password]')
            if password_fields.count() and page_type == 'registration':
                return self._workflow_result(
                    application,
                    state,
                    'human_action_required',
                    'HUMAN_ACTION_REQUIRED: Account registration requires the configured registration and verification workflow.',
                    page,
                )
            if login_entry and not password_fields.count():
                page.goto(
                    entry_url,
                    wait_until='domcontentloaded',
                    timeout=self._workflow_operation_timeout(
                        state,
                        int(self.config.get('timeout_ms', settings.BROWSER_TIMEOUT_MS)),
                    ),
                )
                login_entry = False
                continue
            if password_fields.count():
                if (
                    not self.credential
                    or not self.credential.enabled
                    or self.credential.auth_type != 'form'
                    or _domain(page.url) != self.credential.domain.casefold()
                ):
                    return self._workflow_result(
                        application,
                        state,
                        'human_action_required',
                        'HUMAN_ACTION_REQUIRED: Authentication is required and no authorized credential matches this site.',
                        page,
                    )
                username_selector = self.config.get(
                    'login', {},
                ).get('username_selector', 'input[type=email], input[name*=user i], input[id*=user i]')
                username = page.locator(username_selector).first
                if not username.count():
                    return self._workflow_result(
                        application,
                        state,
                        'human_action_required',
                        'HUMAN_ACTION_REQUIRED: The site login form could not be safely identified.',
                        page,
                    )
                username.fill(
                    self.credential.username
                    or getattr(self.credential.email_mailbox, 'email', '')
                )
                password_fields.first.fill(self.credential.get_password())
                login_action = self._workflow_action(page)
                if not login_action or login_action[0] not in {'authentication', 'continue'}:
                    return self._workflow_result(
                        application,
                        state,
                        'human_action_required',
                        'HUMAN_ACTION_REQUIRED: No recognized sign-in action was found.',
                        page,
                    )
                self._save_workflow(
                    application,
                    state,
                    stage='in_progress',
                    page=page,
                    action='sign_in',
                )
                if self._workflow_budget_exceeded(state):
                    return self._workflow_result(
                        application,
                        state,
                        'needs_review',
                        'Application workflow time budget expired; manual review is required.',
                        page,
                    )
                login_action[2].click()
                page.wait_for_timeout(min(
                    500,
                    self._workflow_operation_timeout(state, 500),
                ))
                if login_entry:
                    page.goto(
                        entry_url,
                        wait_until='domcontentloaded',
                        timeout=self._workflow_operation_timeout(
                            state,
                            int(self.config.get('timeout_ms', settings.BROWSER_TIMEOUT_MS)),
                        ),
                    )
                    login_entry = False
                continue

            filled_fields = self._smart_fill(page, profile, application)
            self._upload_form_template(page, application, profile, state)
            if self.config.get('application_fields'):
                self._fill_selector_map(
                    page,
                    self.config.get('application_fields', {}),
                    profile,
                    application,
                )
            if self.form_template and self.form_template.selector_map:
                self._fill_selector_map(
                    page,
                    {
                        selector: token
                        for selector, token in self.form_template.selector_map.items()
                        if selector != '__upload__'
                    },
                    profile,
                    application,
                )
            state['fields_completed'] = list(dict.fromkeys(
                state['fields_completed'] + filled_fields
            ))
            missing_documents = self._upload_documents(
                page,
                profile,
                application,
                state,
            )
            action = self._workflow_action(page)
            required_issues = self._required_field_issues(page)
            state['required_documents'] = list(dict.fromkeys(
                state['required_documents'] + missing_documents
            ))
            if missing_documents:
                return self._workflow_result(
                    application,
                    state,
                    'blocked',
                    'DOCUMENT_REQUIRED_BUT_MISSING: ' + ', '.join(missing_documents),
                    page,
                )
            if required_issues:
                return self._workflow_result(
                    application,
                    state,
                    'blocked',
                    'Required information is missing or cannot be safely matched: '
                    + ', '.join(required_issues),
                    page,
                )
            if action is None:
                return self._workflow_result(
                    application,
                    state,
                    'blocked',
                    'NEEDS_REVIEW: No safe next application action could be identified on this page.',
                    page,
                )

            intent, label, locator = action
            has_form_fields = page.locator(
                'input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select'
            ).count() > 0
            is_link = locator.evaluate('(element) => element.tagName.toLowerCase()') == 'a'
            if intent == 'submit' and not has_form_fields and not (
                state['page_type'] == 'review'
                and (state['fields_completed'] or state['completed_actions'])
            ):
                if not is_link:
                    return self._workflow_result(
                        application,
                        state,
                        'blocked',
                        'NEEDS_REVIEW: A submit control was found without a completed application form.',
                        page,
                    )
                intent = 'apply'
            if intent == 'submit' or (intent == 'apply' and has_form_fields):
                if (
                    intent == 'submit'
                    and not self.config.get('submit_selector')
                    and not self.config.get('smart_submit', True)
                ):
                    return self._workflow_result(
                        application,
                        state,
                        'blocked',
                        'Automatic submission is disabled for this adapter.',
                        page,
                    )
                before_text = body.casefold()
                confirmation_selector = self.config.get('confirmation_selector')
                selector_was_visible = bool(
                    confirmation_selector
                    and page.locator(confirmation_selector).count()
                    and page.locator(confirmation_selector).is_visible()
                )
                if self._workflow_budget_exceeded(state):
                    return self._workflow_result(
                        application,
                        state,
                        'needs_review',
                        'Application workflow time budget expired; manual review is required.',
                        page,
                    )
                submission_started = True
                state['submission_started'] = True
                state['stage'] = 'submitting'
                self._save_workflow(
                    application,
                    state,
                    stage='submitting',
                    page=page,
                    action=label,
                )
                locator.click(timeout=self._workflow_operation_timeout(state, 10000))
                page.wait_for_timeout(min(
                    int(self.config.get('post_submit_wait_ms', 2500)),
                    self._workflow_operation_timeout(
                        state,
                        int(self.config.get('post_submit_wait_ms', 2500)),
                    ),
                ))
                if self._workflow_budget_exceeded(state):
                    return self._workflow_result(
                        application,
                        state,
                        'needs_review',
                        'Application workflow exceeded its time budget during submission; manual review is required.',
                        page,
                    )
                if self._security_challenge_present(page):
                    return self._workflow_result(
                        application,
                        state,
                        'human_action_required',
                        'HUMAN_ACTION_REQUIRED: A security challenge appeared during submission.',
                        page,
                    )
                if not self._workflow_confirmation(
                    page,
                    before_text,
                    selector_was_visible,
                ):
                    return self._workflow_result(
                        application,
                        state,
                        'needs_review',
                        'Submission was initiated but a confirmation could not be verified.',
                        page,
                    )
                application.status = 'submitted'
                application.submission_time = timezone.now()
                application.result_url = _state_url(page.url)
                state['outcome'] = 'submitted'
                self._save_workflow(
                    application,
                    state,
                    stage='submitted',
                    page=page,
                    action='confirmation_verified',
                )
                application.error_message = ''
                application.save(update_fields=[
                    'status', 'submission_time', 'result_url', 'error_message',
                    'workflow_state', 'updated_at',
                ])
                return True

            if intent == 'authentication':
                return self._workflow_result(
                    application,
                    state,
                    'human_action_required',
                    'HUMAN_ACTION_REQUIRED: Registration or account verification requires the configured registration workflow.',
                    page,
                )

            old_fingerprint = fingerprint
            old_url = page.url
            self._save_workflow(
                application,
                state,
                stage='in_progress',
                page=page,
                action=label,
            )
            if self._workflow_budget_exceeded(state):
                return self._workflow_result(
                    application,
                    state,
                    'needs_review',
                    'Application workflow time budget expired; manual review is required.',
                    page,
                )
            locator.click(timeout=self._workflow_operation_timeout(state, 10000))
            page.wait_for_timeout(min(
                500,
                self._workflow_operation_timeout(state, 500),
            ))
            pages = context.pages
            if pages and pages[-1] is not page:
                page = pages[-1]
            if page.url == old_url:
                page.wait_for_timeout(min(
                    500,
                    self._workflow_operation_timeout(state, 500),
                ))
            new_fingerprint, _ = self._workflow_fingerprint(page)
            if new_fingerprint == old_fingerprint:
                return self._workflow_result(
                    application,
                    state,
                    'blocked',
                    'The selected application action did not change page or application state.',
                    page,
                )
            state.setdefault('completed_actions', []).append(label)
            self._save_workflow(
                application,
                state,
                stage='in_progress',
                page=page,
                action='page_transition',
            )

    def submit(self, application):
        if not self.can_apply(application.opportunity):
            application.status = 'needs_review'
            application.error_message = 'Unsupported application provider: adapter domain is not allowed.'
            application.save(update_fields=['status', 'error_message', 'updated_at'])
            return False
        if not self.config.get('verified', False):
            application.status = 'needs_review'
            application.error_message = 'Provider adapter unavailable: this adapter has not been verified.'
            application.save(update_fields=['status', 'error_message', 'updated_at'])
            return False
        if not self.config.get('allow_submit', False):
            application.status = 'needs_review'
            application.error_message = 'Provider adapter is configured for preparation only; automatic submission is disabled.'
            application.save(update_fields=['status', 'error_message', 'updated_at'])
            return False
        if self.credential and not self.credential.enabled:
            application.status = 'needs_review'
            application.error_message = 'Manual authentication required: the configured website credential is disabled.'
            application.save(update_fields=['status', 'error_message', 'updated_at'])
            return False
        if not self.credential and not self.config.get('public_application_page', False):
            application.status = 'needs_review'
            application.error_message = 'Manual authentication required: configure an enabled user credential or verify that this is a public application page.'
            application.save(update_fields=['status', 'error_message', 'updated_at'])
            return False
        if self.credential and self.credential.account_status in ('not_configured','failed','verification_pending') and self.credential.auto_register:
            ok, reason = register_site_account(self.credential, self.config)
            if not ok and self.credential.account_status != 'ready':
                application.status = 'needs_review' if 'manual review' in reason.lower() or 'verification' in reason.lower() else 'failed'
                application.error_message = reason
                application.save(update_fields=['status','error_message','updated_at'])
                return False
        if self.credential and self.credential.account_status == 'disabled':
            application.status='needs_review'; application.error_message='Website credential is disabled.'
            application.save(update_fields=['status','error_message','updated_at']); return False
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            application.status = 'needs_review'; application.error_message = 'Playwright is not installed.'
            application.save(update_fields=['status', 'error_message', 'updated_at']); return False

        browser = None
        context = None
        submission_started = bool((application.workflow_state or {}).get('submission_started'))
        state = dict(application.workflow_state or {})
        try:
            with sync_playwright() as p:
                browser_type = getattr(p, self.config.get('browser', 'chromium'), p.chromium)
                state_root = self.config.get('user_data_dir') or os.environ.get('PLAYWRIGHT_STATE_DIR')
                if state_root:
                    domain = re.sub(r'[^a-z0-9.-]', '_', _domain(application.opportunity.application_url))
                    state_dir = Path(state_root) / str(application.user_id) / domain
                    state_dir.mkdir(parents=True, exist_ok=True)
                    context = browser_type.launch_persistent_context(
                        str(state_dir),
                        headless=bool(self.config.get('headless', settings.BROWSER_HEADLESS)),
                        accept_downloads=True,
                    )
                else:
                    browser = browser_type.launch(
                        headless=bool(self.config.get('headless', settings.BROWSER_HEADLESS)),
                    )
                    context = browser.new_context(accept_downloads=True)
                result = self._run_dynamic_workflow(application, context, browser)
                if result and self.credential:
                    self.credential.last_used_at = timezone.now()
                    self.credential.save(update_fields=['last_used_at', 'updated_at'])
                return result
        except Exception as exc:
            try:
                application.refresh_from_db()
                state = dict(application.workflow_state or state)
            except Exception:
                pass
            message = _safe_error_text(exc)
            budget_expired = self._workflow_budget_exceeded(state)
            if budget_expired:
                application.status = 'needs_review'
                message = (
                    'Application workflow time budget expired; manual review is required. '
                    + message
                )
            else:
                application.status = (
                    'needs_review'
                    if submission_started or state.get('submission_started')
                    else 'failed'
                )
            if application.status == 'needs_review':
                if not budget_expired:
                    message = 'Submission outcome could not be verified; manual review is required. ' + message
            application.error_message = message
            state.setdefault('errors', []).append({
                'time': timezone.now().isoformat(),
                'message': message,
            })
            application.workflow_state = state
            application.save(update_fields=[
                'status', 'error_message', 'workflow_state', 'updated_at',
            ])
            return False
        finally:
            if context is not None:
                try:
                    context.close()
                finally:
                    if browser is not None:
                        browser.close()


def adapter_for(opportunity, adapter_record=None, credential=None, form_template=None):
    if not adapter_record:
        if credential and credential.enabled:
            config=dict(credential.metadata or {})
            config.setdefault('allowed_domains', [_domain(opportunity.application_url)])
            config.setdefault('smart_fill', True)
            config.setdefault('smart_submit', True)
            config.setdefault('verified', False)
            config.setdefault('allow_submit', False)
            return PlaywrightConfiguredAdapter('generic-authorized-site', config, credential, form_template)
        return ManualReviewAdapter(credential=credential, form_template=form_template)
    if adapter_record.adapter_type == 'playwright_configured':
        return PlaywrightConfiguredAdapter(adapter_record.name, adapter_record.config, credential, form_template)
    return ManualReviewAdapter(adapter_record.name, adapter_record.config, credential, form_template)
