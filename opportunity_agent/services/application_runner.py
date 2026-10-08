from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import Application, ApplicationAttempt, ApplicationFormTemplate, ProviderAdapter, SiteCredential, UserProfile
from .application_guard import ApplicationGuard
from .ai_engine import AIClient
from .file_storage import local_file_path
from .provider_adapters import adapter_for

CAPTCHA_WORDS = ('captcha', 'recaptcha', 'hcaptcha', 'i am not a robot', 'verify you are human')
MFA_WORDS = ('two-factor', '2fa', 'one-time password', 'otp', 'verification code', 'multi-factor')


def _domain_matches(host: str, patterns: list[str]) -> bool:
    host = host.lower().split(':')[0]
    for pattern in patterns:
        pattern = pattern.lower().strip()
        if pattern.startswith('*.') and host.endswith(pattern[1:]):
            return True
        if host == pattern or host.endswith('.' + pattern):
            return True
    return False


def select_adapter(opportunity):
    host = (urlsplit(opportunity.application_url).hostname or '').lower()
    for adapter in ProviderAdapter.objects.filter(enabled=True):
        config = adapter.config or {}
        patterns = config.get('domains', [])
        if isinstance(patterns, str):
            patterns = [patterns]
        if _domain_matches(host, patterns):
            return adapter
    return None


def safety_check(application: Application, adapter=None, profile=None) -> tuple[bool, str]:
    profile = profile or getattr(application.user, 'profile', None)
    if adapter is None:
        domain = (urlsplit(application.opportunity.application_url).hostname or '').lower()
        adapter_record = next((
            candidate
            for candidate in ProviderAdapter.objects.filter(enabled=True)
            if domain in [
                str(value).lower()
                for value in (candidate.config or {}).get('allowed_domains', [])
            ] or any(
                domain.endswith('.' + str(value).lower())
                for value in (candidate.config or {}).get('allowed_domains', [])
            )
        ), None)
        credential = SiteCredential.objects.filter(
            user=application.user,
            enabled=True,
            domain__iexact=domain,
        ).order_by('-updated_at').first()
        form_template = next((
            template
            for template in ApplicationFormTemplate.objects.filter(enabled=True)
            if template.matches_domain(domain)
        ), None)
        adapter = adapter_for(
            application.opportunity,
            adapter_record,
            credential,
            form_template,
        )
    match = application.user.opportunity_matches.filter(
        opportunity=application.opportunity,
    ).first()
    return ApplicationGuard.can_submit(
        application.user,
        application.opportunity,
        profile,
        match,
        application=application,
        adapter=adapter,
    )


def prepare_application(application: Application) -> Application:
    profile = application.user.profile
    ai = AIClient()
    cover = ai.generate_cover_letter(
        {
            'name': profile.full_name or application.user.get_full_name() or application.user.username,
            'skills': profile.skills,
            'education': profile.education,
            'degree': profile.degree,
            'experience': profile.work_experience,
            'languages': profile.languages,
        },
        {
            'title': application.opportunity.title,
            'organization': application.opportunity.organization,
            'requirements': application.opportunity.requirements,
            'description': application.opportunity.description,
            'country': application.opportunity.country,
        },
    )
    if cover:
        application.cover_letter = cover[:12000]
    application.status = 'prepared'
    application.error_message = ''
    application.save(update_fields=['cover_letter', 'status', 'error_message', 'updated_at'])
    return application


def _find_value(page, selectors, labels):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count():
                return locator
        except Exception:
            pass
    for label in labels:
        try:
            locator = page.get_by_label(re.compile(label, re.I)).first
            if locator.count():
                return locator
        except Exception:
            pass
    return None


def _unfilled_required_fields(page):
    missing = []
    selectors = page.locator(
        'input:required, textarea:required, select:required, '
        '[aria-required="true"]'
    )
    for locator in selectors.all():
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
            missing.append(str(state.get('label') or 'unnamed required field').strip())
    return missing


def submit_with_playwright(application: Application, adapter: ProviderAdapter) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {'status': 'needs_review', 'error': 'Playwright is not installed. Run: playwright install chromium'}

    config = adapter.config or {}
    if not config.get('verified', False):
        return {'status': 'needs_review', 'error': 'Provider adapter unavailable: adapter is not verified.'}
    if not config.get('allow_submit', False):
        return {'status': 'needs_review', 'error': 'Provider adapter is configured for preparation only; automatic submission is disabled.'}

    profile = application.user.profile
    submission_started = False
    try:
        with sync_playwright() as p:
            browser_type = getattr(p, config.get('browser', 'chromium'))
            state_dir = config.get('user_data_dir') or os.environ.get('PLAYWRIGHT_STATE_DIR', '')
            if state_dir:
                Path(state_dir).mkdir(parents=True, exist_ok=True)
                context = browser_type.launch_persistent_context(
                    state_dir,
                    headless=config.get('headless', settings.BROWSER_HEADLESS),
                )
                browser = None
            else:
                browser = browser_type.launch(
                    headless=config.get('headless', settings.BROWSER_HEADLESS),
                )
                context = browser.new_context()
            page = context.new_page()
            page.goto(
                application.opportunity.application_url,
                wait_until='domcontentloaded',
                timeout=int(config.get('timeout_ms', settings.BROWSER_TIMEOUT_MS)),
            )
            page.wait_for_timeout(1000)
            text = (page.locator('body').inner_text(timeout=10000) or '').lower()
            if any(word in text for word in CAPTCHA_WORDS):
                context.close()
                if browser: browser.close()
                return {'status': 'needs_review', 'error': 'CAPTCHA/human verification detected; manual review required.'}
            if page.locator('input[type=password]').count():
                context.close()
                if browser: browser.close()
                return {'status': 'needs_review', 'error': 'Manual authentication required before the application form can be accessed.'}
            if any(word in text for word in MFA_WORDS) and config.get('manual_mfa_required', True):
                context.close()
                if browser: browser.close()
                return {'status': 'needs_review', 'error': 'MFA/OTP verification detected; manual review required.'}

            fields = config.get('fields', {})
            name_loc = _find_value(page, fields.get('name', ['input[name*=name]', 'input[autocomplete="name"]']), ['full name', 'name'])
            email_loc = _find_value(page, fields.get('email', ['input[type=email]', 'input[name*=email]']), ['email', 'e-mail'])
            phone_loc = _find_value(page, fields.get('phone', ['input[type=tel]', 'input[name*=phone]']), ['phone', 'telephone', 'mobile'])
            cover_loc = _find_value(page, fields.get('cover_letter', ['textarea[name*=cover]', 'textarea']), ['cover letter', 'coverletter', 'message'])

            if name_loc:
                name_loc.fill(profile.full_name or application.user.get_full_name() or application.user.username)
            if email_loc:
                email_loc.fill(application.user.email)
            if phone_loc and profile.phone:
                phone_loc.fill(profile.phone)
            if cover_loc and application.cover_letter:
                cover_loc.fill(application.cover_letter)

            if profile.cv:
                file_inputs = page.locator('input[type=file]')
                if file_inputs.count():
                    with local_file_path(profile.cv) as cv_path:
                        file_inputs.first.set_input_files(cv_path)

            # Only fill explicitly configured, deterministic answers. Never invent answers.
            for field_key, value in (application.generated_answers or {}).items():
                if not value:
                    continue
                loc = page.locator(field_key).first
                if loc.count():
                    loc.fill(str(value))

            required_fields = _unfilled_required_fields(page)
            if required_fields:
                context.close()
                if browser: browser.close()
                return {
                    'status': 'needs_review',
                    'error': 'Required field could not be identified or completed: '
                    + ', '.join(required_fields[:10]) + '.',
                }

            submit_selector = config.get('submit_selector', 'button[type=submit], input[type=submit]')
            submit = page.locator(submit_selector).first
            if not submit.count():
                context.close()
                if browser: browser.close()
                return {'status': 'needs_review', 'error': 'No configured/recognized submit button was found.'}

            submission_started = True
            submit.click(timeout=10000)
            page.wait_for_timeout(int(config.get('post_submit_wait_ms', 2500)))
            after = (page.locator('body').inner_text(timeout=10000) or '').lower()
            if any(word in after for word in CAPTCHA_WORDS + MFA_WORDS):
                context.close()
                if browser: browser.close()
                return {'status': 'needs_review', 'error': 'CAPTCHA/MFA/security verification appeared after submission; manual review required.', 'result_url': page.url}
            confirmation_words = config.get('confirmation_words', [
                'thank you', 'application received', 'successfully submitted',
                'application submitted',
            ])
            confirmation_selector = config.get('confirmation_selector')
            selector_confirmed = bool(
                confirmation_selector
                and page.locator(confirmation_selector).count()
            )
            text_confirmed = any(
                str(word).strip().lower() in after
                for word in confirmation_words
                if str(word).strip()
            )
            confirmed = selector_confirmed or text_confirmed
            result_url = page.url
            context.close()
            if browser: browser.close()
            if confirmed:
                return {'status': 'submitted', 'result_url': result_url}
            return {'status': 'needs_review', 'error': 'Submission click completed but confirmation could not be verified.', 'result_url': result_url}
    except Exception as exc:
        message = str(exc)[:2000]
        manual_review_reasons = (
            'captcha', 'mfa', 'multi-factor', 'security verification',
            'manual authentication', 'required field', 'confirmation',
            'access denied', 'anti-bot', 'selector', 'locator', 'not found',
        )
        status = 'needs_review' if submission_started or any(
            reason in message.casefold() for reason in manual_review_reasons
        ) else 'failed'
        if submission_started:
            message = 'Submission outcome could not be verified; manual review is required. ' + message
        return {'status': status, 'error': message}


def execute_application(application_id: int) -> dict:
    with transaction.atomic():
        application = Application.objects.select_for_update().select_related('opportunity', 'user').get(pk=application_id)
        profile = UserProfile.objects.select_for_update().filter(
            user_id=application.user_id,
        ).first()
        domain = (urlsplit(application.opportunity.application_url).hostname or '').lower()
        adapter_record = select_adapter(application.opportunity)
        credential = SiteCredential.objects.filter(
            user=application.user,
            enabled=True,
            domain__iexact=domain,
        ).order_by('-updated_at').first()
        form_template = next((
            template
            for template in ApplicationFormTemplate.objects.filter(enabled=True)
            if template.matches_domain(domain)
        ), None)
        adapter = adapter_for(
            application.opportunity,
            adapter_record,
            credential,
            form_template,
        )
        ok, reason = safety_check(application, adapter, profile)
        if not ok:
            application.status = 'needs_review'
            application.error_message = reason
            application.save(update_fields=['status', 'error_message', 'updated_at'])
            return {'status': 'needs_review', 'error': reason}
        if not application.cover_letter:
            prepare_application(application)
        application.status = 'pending'
        application.attempts += 1
        application.save(update_fields=['status', 'attempts', 'updated_at'])
        attempt = ApplicationAttempt.objects.create(application=application, attempt_number=application.attempts, status='pending')

    result = submit_with_playwright(application, adapter)
    with transaction.atomic():
        application = Application.objects.select_for_update().get(pk=application_id)
        application.status = result.get('status', 'failed')
        application.error_message = result.get('error', '')
        application.result_url = result.get('result_url', '') or ''
        if application.status == 'submitted':
            application.submission_time = timezone.now()
        history = application.audit_history or []
        history.append({'time': timezone.now().isoformat(), 'status': application.status, 'error': application.error_message, 'result_url': application.result_url})
        application.audit_history = history[-50:]
        application.save(update_fields=['status', 'error_message', 'result_url', 'submission_time', 'audit_history', 'updated_at'])
        attempt = ApplicationAttempt.objects.get(pk=attempt.pk)
        attempt.status = application.status
        attempt.error_message = application.error_message
        attempt.save(update_fields=['status', 'error_message'])
    return result
