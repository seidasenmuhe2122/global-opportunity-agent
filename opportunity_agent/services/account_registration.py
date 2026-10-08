from __future__ import annotations

import re
import secrets
import string
import time
from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone

from .email_account import fetch_verification_link, mark_checked


def generate_site_password(length=20):
    alphabet=string.ascii_letters+string.digits+'!@#$%^&*_-+='; return ''.join(secrets.choice(alphabet) for _ in range(length))


def _domain(url): return (urlparse(url or '').hostname or '').lower()


def _security_challenge(page):
    try: text=page.locator('body').inner_text(timeout=3000).lower()
    except Exception: return False
    return any(k in text for k in ('captcha','recaptcha','hcaptcha','two-factor authentication','multi-factor authentication','security challenge'))


def _fill_first(page, selectors, value):
    if not value: return False
    for selector in selectors:
        try:
            loc=page.locator(selector).first
            if loc.count(): loc.fill(value); return True
        except Exception: pass
    return False


def register_site_account(credential, provider_config=None):
    """Create an account using the user's authorized mailbox. Does not defeat CAPTCHA/MFA."""
    provider_config=provider_config or {}
    if not credential.auto_register: return False,'Auto-registration is disabled for this site credential.'
    mailbox=credential.email_mailbox
    if not mailbox or not mailbox.enabled: return False,'An enabled email mailbox with an app password is required for auto-registration.'
    registration_url=credential.registration_url or provider_config.get('registration_url') or credential.login_url
    if not registration_url: return False,'Registration URL is not configured.'
    try:
        from playwright.sync_api import sync_playwright
    except ImportError: return False,'Playwright is not installed.'
    site_domain=_domain(registration_url)
    password=credential.get_password() or generate_site_password()
    username=credential.username or mailbox.email
    selectors=provider_config.get('registration',{})
    email_selectors=selectors.get('email_selectors',['input[type=email]','input[name*=email i]','input[id*=email i]'])
    user_selectors=selectors.get('username_selectors',['input[name*=user i]','input[id*=user i]','input[name*=login i]'])
    pass_selectors=selectors.get('password_selectors',['input[type=password]','input[name*=password i]'])
    submit_selector=selectors.get('submit_selector','')
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(
                headless=bool(provider_config.get('headless', settings.BROWSER_HEADLESS)),
            )
            context=browser.new_context(accept_downloads=True)
            page=context.new_page()
            page.set_default_timeout(
                int(provider_config.get('timeout_ms', settings.BROWSER_TIMEOUT_MS)),
            )
            page.goto(
                registration_url,
                wait_until='domcontentloaded',
                timeout=int(provider_config.get('timeout_ms', settings.BROWSER_TIMEOUT_MS)),
            )
            if _security_challenge(page): return False,'CAPTCHA/MFA/security challenge detected; manual review required.'
            if not _fill_first(page,email_selectors,mailbox.email): return False,'Registration email field could not be detected.'
            _fill_first(page,user_selectors,username)
            if not _fill_first(page,pass_selectors,password): return False,'Registration password field could not be detected.'
            confirm_selectors=selectors.get('confirm_password_selectors',['input[name*=confirm i][type=password]','input[id*=confirm i][type=password]'])
            _fill_first(page,confirm_selectors,password)
            if _security_challenge(page): return False,'CAPTCHA/MFA/security challenge detected; manual review required.'
            if submit_selector: page.locator(submit_selector).click()
            else:
                buttons=page.get_by_role('button',name=re.compile(r'register|sign up|create account|create',re.I))
                if buttons.count(): buttons.first.click()
                else: return False,'Registration submit button could not be detected.'
            page.wait_for_load_state('domcontentloaded')
            body=''
            try: body=page.locator('body').inner_text(timeout=3000).lower()
            except Exception: pass
            if _security_challenge(page): return False,'CAPTCHA/MFA/security challenge detected; manual review required.'
            if any(k in body for k in ('already registered','already exists','email already')):
                credential.account_status='ready'; credential.registration_error='Account already exists; using the saved website credential.'
            else:
                credential.account_status='verification_pending'
                credential.registration_error='Registration submitted; waiting for email verification.'
            credential.username=username; credential.set_password(password); credential.last_registration_at=timezone.now(); credential.save(update_fields=['username','encrypted_password','account_status','registration_error','last_registration_at','updated_at'])
            # Email activation is allowed; email OTP/MFA is intentionally not automated.
            link=fetch_verification_link(mailbox,[site_domain],site_domain,since_minutes=30)
            if link:
                link_domain=_domain(link)
                if link_domain == site_domain or link_domain.endswith('.'+site_domain):
                    page.goto(link,wait_until='domcontentloaded')
                    if _security_challenge(page): return False,'Verification page triggered a CAPTCHA/MFA/security challenge; manual review required.'
                    credential.account_status='ready'; credential.registration_error=''; credential.save(update_fields=['account_status','registration_error','updated_at'])
                    mark_checked(mailbox,'')
                    return True,'Account registered and email verification link completed.'
            mark_checked(mailbox,'No verification link found yet; account remains verification pending.')
            return False,'Registration submitted. Verification link was not found yet; manual review or another retry may be required.'
    except Exception as exc:
        credential.account_status='failed'; credential.registration_error=str(exc)[:2000]; credential.last_registration_at=timezone.now(); credential.save(update_fields=['account_status','registration_error','last_registration_at','updated_at'])
        try: mark_checked(mailbox,str(exc))
        except Exception: pass
        return False,str(exc)[:2000]
