import ast
import logging
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

from django.contrib.auth import authenticate, get_user_model
from django.contrib import admin
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.core.files.uploadedfile import SimpleUploadedFile

from .forms import SignUpForm, SiteCredentialForm, UserProfileForm
from .models import (
    Application,
    AuditLog,
    AIConversation,
    EmailMailbox,
    Opportunity,
    PrivateAccessToken,
    Source,
    SystemSetting,
    UserProfile,
    get_registration_mode,
    get_website_visibility,
    set_registration_mode,
    set_website_visibility,
)
from .services.credential_vault import decrypt_secret, encrypt_secret
from .services.file_storage import local_file_path

from global_opportunity_agent.settings import _validated_redis_url
from .middleware import SensitiveEndpointRateLimitMiddleware
from redis.exceptions import ConnectionError as RedisConnectionError


User = get_user_model()


class RedisConfigurationTests(SimpleTestCase):
    def test_redis_urls_require_a_supported_scheme_and_host(self):
        self.assertEqual(
            _validated_redis_url('REDIS_URL', 'rediss://cache.example:6380/0'),
            'rediss://cache.example:6380/0',
        )

        for value in ('cache.example:6379', 'http://cache.example:6379', 'redis://'):
            with self.subTest(value=value):
                with self.assertRaisesMessage(
                    ImproperlyConfigured,
                    'REDIS_URL must be a valid Redis URL using redis:// or rediss://.',
                ):
                    _validated_redis_url('REDIS_URL', value)

    def test_rate_limited_requests_fail_closed_when_redis_is_unavailable(self):
        rules = {
            'search': {
                'limit': 60,
                'window': 60,
                'methods': {'GET'},
                'paths': ('/opportunities/',),
            },
            'assistant': {
                'limit': 30,
                'window': 60,
                'methods': {'POST'},
                'paths': ('/assistant/new/',),
            },
        }
        middleware = SensitiveEndpointRateLimitMiddleware(
            lambda request: HttpResponse('view reached'),
        )

        cache_failures = (
            RedisConnectionError('connection refused'),
            ValueError('Redis URL must specify a supported scheme'),
        )
        with override_settings(RATE_LIMIT_RULES=rules):
            for method, path in (
                ('get', '/opportunities/'),
                ('post', '/assistant/new/'),
            ):
                for cache_failure in cache_failures:
                    with self.subTest(path=path, error=type(cache_failure).__name__):
                        request = getattr(RequestFactory(), method)(path)
                        with patch(
                            'opportunity_agent.middleware.cache.add',
                            side_effect=cache_failure,
                        ):
                            with self.assertLogs(
                                'opportunity_agent.middleware',
                                level='ERROR',
                            ) as captured:
                                response = middleware(request)

                        self.assertEqual(response.status_code, 503)
                        self.assertNotContains(
                            response,
                            str(cache_failure),
                            status_code=503,
                        )
                        self.assertIn(
                            'Traceback (most recent call last)',
                            captured.output[0],
                        )
                        self.assertIn(str(cache_failure), captured.output[0])


class ServerErrorLoggingTests(SimpleTestCase):
    def test_request_errors_log_full_tracebacks_to_stderr(self):
        logger_config = settings.LOGGING['loggers']['django.request']
        self.assertEqual(logger_config['level'], 'ERROR')
        self.assertFalse(logger_config['propagate'])

        handler_config = settings.LOGGING['handlers'][logger_config['handlers'][0]]
        self.assertEqual(handler_config['class'], 'logging.StreamHandler')
        self.assertNotIn('stream', handler_config)

        request_logger = logging.getLogger('django.request')
        self.assertTrue(any(
            isinstance(handler, logging.StreamHandler)
            and handler.stream is sys.stderr
            for handler in request_logger.handlers
        ))
        with self.assertLogs(request_logger, level='ERROR') as captured:
            try:
                raise RuntimeError('diagnostic logging regression test')
            except RuntimeError:
                request_logger.exception('Request failed')

        self.assertIn('Traceback (most recent call last)', captured.output[0])
        self.assertIn('diagnostic logging regression test', captured.output[0])


def _find_raw_sql_usage(root_dir):
    violations = []
    for path in sorted(root_dir.rglob('*.py')):
        if 'migrations' in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            if name in {'execute', 'raw', 'cursor'}:
                violations.append(f'{path.relative_to(root_dir.parent)}:{node.lineno}:{name}')
    return violations


class EmailAuthenticationTests(TestCase):
    def setUp(self):
        call_command('setup_roles')
        self.user = User.objects.create_user(
            username='email-user',
            email='person@example.com',
            password='StrongPass123!',
        )
        self.user.groups.add(Group.objects.get(name='USER'))

    def test_application_code_uses_orm_only_queries(self):
        violations = _find_raw_sql_usage(Path(__file__).resolve().parent)
        self.assertFalse(violations, f'Raw SQL calls detected: {violations}')

    def test_authentication_accepts_email_and_legacy_username(self):
        self.assertEqual(
            authenticate(username='PERSON@example.com', password='StrongPass123!'),
            self.user,
        )
        self.assertEqual(
            authenticate(username='email-user', password='StrongPass123!'),
            self.user,
        )

    def test_login_view_accepts_email_address(self):
        response = self.client.post(
            reverse('login'),
            {'username': 'person@example.com', 'password': 'StrongPass123!'},
        )
        self.assertRedirects(response, reverse('user_dashboard'))
        self.assertEqual(int(self.client.session['_auth_user_id']), self.user.pk)

    @override_settings(RATE_LIMIT_RULES={
        'login': {'limit': 2, 'window': 60, 'methods': {'POST'}, 'paths': ('/accounts/login/', '/login/')},
    })
    def test_login_endpoint_rate_limits_excessive_requests(self):
        for _ in range(2):
            response = self.client.post(
                reverse('login'),
                {'username': 'nonexistent@example.com', 'password': 'WrongPassword123!'},
            )
            self.assertNotEqual(response.status_code, 429)

        response = self.client.post(
            reverse('login'),
            {'username': 'nonexistent@example.com', 'password': 'WrongPassword123!'},
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response.headers)

    def test_logout_control_posts_and_ends_the_session(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'method="post"')
        self.assertContains(response, 'action="/accounts/logout/"')

        response = self.client.post(reverse('logout'))
        self.assertRedirects(response, '/')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_email_address_cannot_authenticate_ambiguous_duplicate_accounts(self):
        User.objects.create_user(
            username='duplicate-email',
            email='PERSON@example.com',
            password='StrongPass123!',
        )
        self.assertIsNone(authenticate(username='person@example.com', password='StrongPass123!'))

    def test_signup_rejects_duplicate_email_case_insensitively(self):
        form = SignUpForm(data={
            'username': 'another-user',
            'email': 'PERSON@example.com',
            'password1': 'AnotherStrongPass123!',
            'password2': 'AnotherStrongPass123!',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('email', form.errors)

    def test_signup_shows_validation_errors_without_creating_account(self):
        response = self.client.post(reverse('signup'), {
            'email': 'weak-password@example.com',
            'password1': 'password',
            'password2': 'password',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Account not created. Please fix the following:')
        self.assertContains(response, 'Password:')
        self.assertFalse(User.objects.filter(email='weak-password@example.com').exists())

    def test_signup_existing_email_shows_signin_instead_of_creating_duplicate(self):
        email = 'already-registered@example.com'
        User.objects.create_user(
            username='already-registered',
            email=email,
            password='ExistingStrongPass123!',
        )

        response = self.client.post(reverse('signup'), {
            'email': email,
            'password1': 'AnotherStrongPass123!',
            'password2': 'AnotherStrongPass123!',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'An account with this email address already exists.')
        self.assertContains(response, 'sign in here')
        self.assertEqual(User.objects.filter(email=email).count(), 1)

    def test_signup_creates_profile_and_logs_user_in(self):
        response = self.client.post(reverse('signup'), {
            'username': 'new-user',
            'email': 'new-user@example.com',
            'password1': 'NewStrongPass123!',
            'password2': 'NewStrongPass123!',
        })

        new_user = User.objects.get(username='new-user')
        self.assertRedirects(response, reverse('user_dashboard'))
        self.assertEqual(int(self.client.session['_auth_user_id']), new_user.pk)
        self.assertEqual(
            self.client.session['_auth_user_backend'],
            'opportunity_agent.authentication.EmailOrUsernameModelBackend',
        )
        self.assertTrue(hasattr(new_user, 'profile'))
        self.assertTrue(new_user.groups.filter(name='USER').exists())

    def test_signup_succeeds_without_entering_a_username(self):
        response = self.client.post(reverse('signup'), {
            'email': 'email-only-signup@example.com',
            'password1': 'NewStrongPass123!',
            'password2': 'NewStrongPass123!',
        })

        new_user = User.objects.get(email='email-only-signup@example.com')
        self.assertEqual(new_user.username, 'email-only-signup')
        self.assertRedirects(response, reverse('user_dashboard'))
        self.assertEqual(int(self.client.session['_auth_user_id']), new_user.pk)

    def test_signup_generates_a_unique_username_from_email(self):
        User.objects.create_user(
            username='email-only-signup',
            email='existing@example.com',
            password='OtherStrongPass123!',
        )
        form = SignUpForm(data={
            'email': 'email-only-signup@example.net',
            'password1': 'NewStrongPass123!',
            'password2': 'NewStrongPass123!',
        })

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['username'], 'email-only-signup2')

    def test_pending_registration_user_cannot_sign_in(self):
        UserProfile.objects.create(user=self.user, registration_status='pending')

        response = self.client.post(
            reverse('login'),
            {'username': 'person@example.com', 'password': 'StrongPass123!'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'awaiting administrator approval')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_signup_in_admin_approval_mode_creates_pending_account(self):
        SystemSetting.objects.update_or_create(
            key='registration_mode',
            defaults={'value': 'admin_approval'},
        )

        response = self.client.post(reverse('signup'), {
            'username': 'awaiting-approval',
            'email': 'awaiting-approval@example.com',
            'password1': 'NewStrongPass123!',
            'password2': 'NewStrongPass123!',
        })

        user = User.objects.get(email='awaiting-approval@example.com')
        self.assertEqual(user.profile.registration_status, 'pending')
        self.assertFalse(user.is_active)
        self.assertContains(response, 'waiting for administrator approval')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_admin_registration_actions_approve_reject_suspend_and_activate_accounts(self):
        admin_user = User.objects.create_superuser(
            username='approval-admin',
            email='approval-admin@example.com',
            password='StrongPass123!',
        )
        pending_user = User.objects.create_user(
            username='pending-user',
            email='pending-user@example.com',
            password='StrongPass123!',
            is_active=False,
        )
        pending_profile = UserProfile.objects.create(
            user=pending_user,
            registration_status='pending',
        )
        rejected_user = User.objects.create_user(
            username='rejected-user',
            email='rejected-user@example.com',
            password='StrongPass123!',
            is_active=False,
        )
        rejected_profile = UserProfile.objects.create(
            user=rejected_user,
            registration_status='pending',
        )
        suspended_user = User.objects.create_user(
            username='suspended-user',
            email='suspended-user@example.com',
            password='StrongPass123!',
            is_active=False,
        )
        suspended_profile = UserProfile.objects.create(
            user=suspended_user,
            registration_status='pending',
        )
        self.client.force_login(admin_user)
        user_changelist = reverse('admin:auth_user_changelist')

        response = self.client.post(user_changelist, {
            'action': 'approve_user_registrations',
            '_selected_action': [str(pending_user.pk)],
        })
        self.assertEqual(response.status_code, 302)
        pending_profile.refresh_from_db()
        pending_user.refresh_from_db()
        self.assertEqual(pending_profile.registration_status, 'active')
        self.assertTrue(pending_user.is_active)
        self.assertEqual(
            authenticate(
                username='pending-user@example.com',
                password='StrongPass123!',
            ),
            pending_user,
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action='REGISTRATION_APPROVED',
                target=str(pending_user.pk),
            ).exists()
        )

        self.client.post(user_changelist, {
            'action': 'reject_user_registrations',
            '_selected_action': [str(rejected_user.pk)],
        })
        rejected_profile.refresh_from_db()
        rejected_user.refresh_from_db()
        self.assertEqual(rejected_profile.registration_status, 'rejected')
        self.assertFalse(rejected_user.is_active)
        self.assertIsNone(
            authenticate(
                username='rejected-user',
                password='StrongPass123!',
            )
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action='REGISTRATION_REJECTED',
                target=str(rejected_user.pk),
            ).exists()
        )

        self.client.post(user_changelist, {
            'action': 'suspend_user_registrations',
            '_selected_action': [str(suspended_user.pk)],
        })
        suspended_profile.refresh_from_db()
        suspended_user.refresh_from_db()
        self.assertEqual(suspended_profile.registration_status, 'suspended')
        self.assertFalse(suspended_user.is_active)
        self.assertIsNone(
            authenticate(
                username='suspended-user',
                password='StrongPass123!',
            )
        )

        self.client.post(user_changelist, {
            'action': 'activate_user_registrations',
            '_selected_action': [str(suspended_user.pk)],
        })
        suspended_profile.refresh_from_db()
        suspended_user.refresh_from_db()
        self.assertEqual(suspended_profile.registration_status, 'active')
        self.assertTrue(suspended_user.is_active)

    def test_all_website_visibility_and_registration_mode_combinations_are_independent(self):
        combinations = (
            ('public', 'open'),
            ('public', 'admin_approval'),
            ('private', 'open'),
            ('private', 'admin_approval'),
        )
        for visibility, registration_mode in combinations:
            with self.subTest(visibility=visibility, registration_mode=registration_mode):
                set_website_visibility(visibility)
                set_registration_mode(registration_mode)
                self.assertEqual(get_website_visibility(), visibility)
                self.assertEqual(get_registration_mode(), registration_mode)

                client = Client()
                signup_url = reverse('signup')
                response = client.get(signup_url)
                if visibility == 'private':
                    self.assertRedirects(
                        response,
                        f"{reverse('private_access')}?next={quote(signup_url)}",
                    )
                    _, raw_token = PrivateAccessToken.issue_token(
                        label=f'{visibility}-{registration_mode}',
                        allowed_registration=True,
                    )
                    response = client.get(
                        reverse('private_access_token', args=[raw_token])
                    )
                    self.assertRedirects(response, reverse('home'))
                    response = client.get(signup_url)
                self.assertEqual(response.status_code, 200)

                username = f'{visibility}-{registration_mode}'
                response = client.post(signup_url, {
                    'username': username,
                    'email': f'{username}@example.com',
                    'password1': 'NewStrongPass123!',
                    'password2': 'NewStrongPass123!',
                })
                created_user = User.objects.get(username=username)
                if registration_mode == 'open':
                    self.assertTrue(created_user.is_active)
                    self.assertEqual(
                        created_user.profile.registration_status,
                        'active',
                    )
                    self.assertIn('_auth_user_id', client.session)
                else:
                    self.assertFalse(created_user.is_active)
                    self.assertEqual(
                        created_user.profile.registration_status,
                        'pending',
                    )
                    self.assertNotIn('_auth_user_id', client.session)

    def test_invalid_stored_modes_fail_closed_and_empty_updates_are_rejected(self):
        set_website_visibility('public')
        set_registration_mode('open')
        SystemSetting.objects.update_or_create(
            key='website_visibility',
            defaults={'value': 'unsupported'},
        )
        SystemSetting.objects.update_or_create(
            key='registration_mode',
            defaults={'value': 'unsupported'},
        )
        self.assertEqual(get_website_visibility(), 'private')
        self.assertEqual(get_registration_mode(), 'admin_approval')

        with self.assertRaises(ValueError):
            set_website_visibility('')
        with self.assertRaises(ValueError):
            set_registration_mode('')

    def test_website_settings_require_permission_and_changes_are_independent(self):
        superuser = User.objects.create_superuser(
            username='settings-admin',
            email='settings-admin@example.com',
            password='StrongPass123!',
        )
        self.client.force_login(superuser)

        response = self.client.post(reverse('website_visibility_toggle'), {
            'visibility': 'private',
        })
        self.assertRedirects(response, reverse('admin_dashboard'))
        self.assertEqual(get_website_visibility(), 'private')
        self.assertEqual(get_registration_mode(), 'open')

        response = self.client.post(reverse('registration_mode_toggle'), {
            'registration_mode': 'admin_approval',
        })
        self.assertRedirects(response, reverse('admin_dashboard'))
        self.assertEqual(get_registration_mode(), 'admin_approval')
        self.assertEqual(get_website_visibility(), 'private')

        self.client.logout()
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.post(reverse('website_visibility_toggle'), {
                'visibility': 'public',
            }).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(reverse('registration_mode_toggle'), {
                'registration_mode': 'open',
            }).status_code,
            403,
        )

    def test_admin_role_can_manage_private_links_but_not_security_settings(self):
        admin_user = User.objects.create_user(
            username='platform-admin',
            email='platform-admin@example.com',
            password='StrongPass123!',
            is_staff=True,
        )
        admin_user.groups.add(Group.objects.get(name='ADMIN'))
        self.client.force_login(admin_user)

        token_list = reverse(
            'admin:opportunity_agent_privateaccesstoken_changelist'
        )
        issue_link = reverse(
            'admin:opportunity_agent_privateaccesstoken_issue_link'
        )
        self.assertEqual(self.client.get(token_list).status_code, 200)
        self.assertEqual(self.client.get(issue_link).status_code, 200)
        self.assertEqual(
            self.client.post(reverse('website_visibility_toggle'), {
                'visibility': 'private',
            }).status_code,
            403,
        )

        settings_admin = admin.site.get_model_admin(SystemSetting)
        settings_form = settings_admin.form
        self.assertTrue(settings_form(data={
            'key': 'website_visibility',
            'value': 'private',
        }).is_valid())
        self.assertFalse(settings_form(data={
            'key': 'website_visibility',
            'value': 'invalid',
        }).is_valid())
        self.assertTrue(settings_form(data={
            'key': 'registration_mode',
            'value': 'admin_approval',
        }).is_valid())
        self.assertFalse(settings_form(data={
            'key': 'registration_mode',
            'value': 'invalid',
        }).is_valid())

    def test_django_admin_home_shows_website_settings_button_to_authorized_admin(self):
        admin_user = User.objects.create_superuser(
            username='settings-button-admin',
            email='settings-button-admin@example.com',
            password='StrongPass123!',
        )
        self.client.force_login(admin_user)
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Open Website Settings')
        self.assertContains(response, 'href="/admin-dashboard/#website-settings"')

    def test_admin_dashboard_shows_direct_website_settings_button_to_superuser(self):
        admin_user = User.objects.create_superuser(
            username='dashboard-settings-button-admin',
            email='dashboard-settings-button-admin@example.com',
            password='StrongPass123!',
        )
        self.client.force_login(admin_user)
        response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="#website-settings">Website settings</a>')

    def test_regular_authenticated_user_can_use_private_ai_conversations_without_admin_permissions(self):
        regular_user = User.objects.create_user(
            username='assistant-user',
            email='assistant-user@example.com',
            password='StrongPass123!',
        )
        self.client.force_login(regular_user)

        response = self.client.get(reverse('ai_chat'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Opportunity Assistant')

        response = self.client.post(reverse('ai_chat_new'))
        conversation = AIConversation.objects.get(user=regular_user)
        self.assertRedirects(
            response,
            f'{reverse("ai_chat")}?conversation={conversation.pk}',
        )

        with patch(
            'opportunity_agent.views.process_user_message',
            return_value={'message': 'Assistant response'},
        ):
            response = self.client.post(
                reverse('ai_chat_send', args=[conversation.pk]),
                data=b'{"message":"hello"}',
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {
            'success': True,
            'result': {'message': 'Assistant response'},
            'assistant_message': 'Assistant response',
        })

    def test_regular_user_cannot_send_to_another_users_ai_conversation(self):
        regular_user = User.objects.create_user(
            username='assistant-user-owner',
            email='assistant-owner@example.com',
            password='StrongPass123!',
        )
        other_user = User.objects.create_user(
            username='assistant-other-user',
            email='assistant-other@example.com',
            password='StrongPass123!',
        )
        conversation = AIConversation.objects.create(
            user=other_user,
            title='Private conversation',
        )
        self.client.force_login(regular_user)

        response = self.client.post(
            reverse('ai_chat_send', args=[conversation.pk]),
            data=b'{"message":"hello"}',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)

    def test_django_admin_home_hides_website_settings_button_from_unprivileged_staff(self):
        staff_user = User.objects.create_user(
            username='unprivileged-staff',
            email='unprivileged-staff@example.com',
            password='StrongPass123!',
            is_staff=True,
        )
        self.client.force_login(staff_user)
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Open Website Settings')

    def test_website_settings_posts_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        superuser = User.objects.create_superuser(
            username='csrf-settings-admin',
            email='csrf-settings-admin@example.com',
            password='StrongPass123!',
        )
        csrf_client.force_login(superuser)
        response = csrf_client.post(reverse('website_visibility_toggle'), {
            'visibility': 'private',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(get_website_visibility(), 'public')

    def test_private_mode_keeps_django_admin_authentication_separate(self):
        set_website_visibility('private')
        self.assertEqual(self.client.get('/admin/login/').status_code, 200)
        response = self.client.get('/admin/')
        self.assertRedirects(
            response,
            f'/admin/login/?next={quote("/admin/")}',
        )

        superuser = User.objects.create_superuser(
            username='private-admin',
            email='private-admin@example.com',
            password='StrongPass123!',
        )
        self.client.force_login(superuser)
        self.assertEqual(self.client.get('/admin/').status_code, 200)

        self.client.logout()
        self.client.force_login(self.user)
        response = self.client.get('/admin/')
        self.assertRedirects(
            response,
            f'/admin/login/?next={quote("/admin/")}',
        )

    def test_private_mode_robots_sitemap_and_noindex_behavior(self):
        response = self.client.get(reverse('robots_txt'))
        self.assertContains(response, 'Allow: /')
        self.assertIn('Sitemap:', response.content.decode())
        sitemap_response = self.client.get(reverse('sitemap'))
        self.assertEqual(sitemap_response.status_code, 200)
        self.assertIn(b'/opportunities/', sitemap_response.content)
        self.assertIsNone(sitemap_response.headers.get('X-Robots-Tag'))

        set_website_visibility('private')
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers.get('X-Robots-Tag'),
            'noindex, nofollow, noarchive',
        )
        private_page = self.client.get(reverse('private_access'))
        self.assertEqual(private_page.status_code, 200)
        self.assertContains(
            private_page,
            '<meta name="robots" content="noindex,nofollow,noarchive">',
            html=True,
        )
        self.assertNotContains(private_page, '<nav class="nav-list">', html=True)
        self.assertNotContains(private_page, '<a class="brand" href="/">', html=True)
        self.assertEqual(
            self.client.get(reverse('sitemap')).status_code,
            404,
        )
        robots = self.client.get(reverse('robots_txt'))
        self.assertContains(robots, 'Disallow: /')

    def test_private_access_session_expires_after_its_configured_lifetime(self):
        set_website_visibility('private')
        _, raw_token = PrivateAccessToken.issue_token(
            label='Expiring session',
            max_uses=2,
        )
        self.client.get(reverse('private_access_token', args=[raw_token]))
        self.assertTrue(self.client.session.get('private_access_granted'))

        session = self.client.session
        session['private_access_granted_until'] = (
            timezone.now() - timedelta(seconds=1)
        ).isoformat()
        session.save()

        response = self.client.get(reverse('home'))
        self.assertRedirects(
            response,
            f"{reverse('private_access')}?next={quote(reverse('home'))}",
        )
        self.assertNotIn('private_access_granted', self.client.session)
        self.assertNotIn('private_access_registration_allowed', self.client.session)

    def test_private_access_mode_blocks_anonymous_visitors_and_grants_access_with_token(self):
        set_website_visibility('private')
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/private-access/', response['Location'])

        token, raw_token = PrivateAccessToken.issue_token(label='Test link', created_by=self.user)
        self.assertTrue(token.is_valid)

        response = self.client.get(reverse('private_access_token', args=[raw_token]))
        self.assertRedirects(response, reverse('home'))
        self.assertTrue(self.client.session.get('private_access_granted'))

        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)

    def test_private_access_rejects_external_redirect_targets_and_allows_internal_paths(self):
        token, raw_token = PrivateAccessToken.issue_token(label='Redirect check', created_by=self.user)
        self.assertTrue(token.is_valid)

        response = self.client.get(reverse('private_access_token', args=[raw_token]), {'next': 'https://evil.example/steal'})
        self.assertRedirects(response, reverse('home'))
        self.assertNotIn('evil.example', response.url)

        token, raw_token = PrivateAccessToken.issue_token(label='Internal redirect check', created_by=self.user)
        response = self.client.get(reverse('private_access_token', args=[raw_token]), {'next': '/dashboard/'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/dashboard/')
        set_website_visibility('public')

    def test_private_access_rejects_expired_revoked_and_exhausted_tokens(self):
        set_website_visibility('private')
        expired_token, expired_raw = PrivateAccessToken.issue_token(
            label='Expired',
            created_by=self.user,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        revoked_token, revoked_raw = PrivateAccessToken.issue_token(
            label='Revoked',
            created_by=self.user,
        )
        revoked_token.revoked_at = timezone.now()
        revoked_token.save(update_fields=['revoked_at'])
        disabled_token, disabled_raw = PrivateAccessToken.issue_token(
            label='Disabled',
            created_by=self.user,
        )
        disabled_token.active = False
        disabled_token.disabled_at = timezone.now()
        disabled_token.save(update_fields=['active', 'disabled_at'])
        exhausted_token, exhausted_raw = PrivateAccessToken.issue_token(
            label='Exhausted',
            created_by=self.user,
            max_uses=1,
        )
        self.assertTrue(exhausted_token.consume())

        for raw_token in (
            expired_raw,
            revoked_raw,
            disabled_raw,
            exhausted_raw,
            'invalid-private-token',
        ):
            with self.subTest(token=raw_token):
                response = self.client.get(
                    reverse('private_access_token', args=[raw_token])
                )
                self.assertContains(
                    response,
                    'This private access link is invalid or no longer active.',
                )
                self.assertFalse(
                    self.client.session.get('private_access_granted')
                )

    @override_settings(RATE_LIMIT_RULES={
        'private_access': {
            'limit': 2,
            'window': 60,
            'methods': {'GET', 'POST'},
            'paths': ('/private-access/',),
        },
    })
    def test_private_access_token_get_attempts_are_rate_limited(self):
        cache.clear()
        for _ in range(2):
            self.assertNotEqual(
                self.client.get('/private-access/guess/').status_code,
                429,
            )
        response = self.client.get('/private-access/guess/')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['Retry-After'], '60')

    def test_admin_issues_prefixed_private_access_link_that_grants_private_site_access(self):
        admin_user = User.objects.create_superuser(
            username='private-link-admin',
            email='private-link-admin@example.com',
            password='StrongPass123!',
        )
        self.client.force_login(admin_user)
        issue_url = reverse('admin:opportunity_agent_privateaccesstoken_issue_link')

        response = self.client.post(issue_url, {
            'label': 'Invite',
            'description': '',
            'recipient_email': '',
            'expires_at': '',
            'max_uses': '1',
            'allowed_registration': 'on',
        })

        self.assertRedirects(response, issue_url, fetch_redirect_response=False)
        response = self.client.get(issue_url)
        self.assertEqual(response.status_code, 200)
        link = response.context['issued_link']['url']
        self.assertTrue(urlparse(link).path.startswith('/private-access/'))
        self.assertEqual(
            resolve(urlparse(link).path).url_name,
            'private_access_token',
        )
        token = PrivateAccessToken.objects.get(label='Invite')
        set_website_visibility('private')
        self.client.logout()
        response = self.client.get(urlparse(link).path)

        self.assertRedirects(response, reverse('home'))
        self.assertTrue(self.client.session.get('private_access_granted'))
        self.assertEqual(self.client.get(reverse('home')).status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertNotIn('is_staff', self.client.session)
        token.refresh_from_db()
        self.assertEqual(token.used_count, 1)
        self.assertFalse(token.active)
        self.assertNotEqual(token.token_hash, urlparse(link).path.rsplit('/', 2)[-2])
        self.assertNotIn(token.token_hash.encode(), response.content)

    def test_private_link_without_registration_permission_cannot_open_signup(self):
        set_website_visibility('private')
        _, raw_token = PrivateAccessToken.issue_token(
            label='No registration',
            allowed_registration=False,
        )
        response = self.client.get(
            reverse('private_access_token', args=[raw_token])
        )
        self.assertRedirects(response, reverse('home'))
        response = self.client.get(reverse('signup'))
        self.assertEqual(response.status_code, 403)

    def test_private_link_admin_can_disable_enable_and_revoke_without_exposing_hash(self):
        admin_user = User.objects.create_superuser(
            username='link-management-admin',
            email='link-management-admin@example.com',
            password='StrongPass123!',
        )
        token, raw_token = PrivateAccessToken.issue_token(
            label='Manage link',
            created_by=admin_user,
            max_uses=3,
        )
        self.client.force_login(admin_user)
        changelist = reverse(
            'admin:opportunity_agent_privateaccesstoken_changelist'
        )
        change_page = self.client.get(
            reverse(
                'admin:opportunity_agent_privateaccesstoken_change',
                args=[token.pk],
            )
        )
        self.assertEqual(change_page.status_code, 200)
        self.assertNotContains(change_page, token.token_hash)
        self.assertNotContains(change_page, raw_token)

        self.client.post(changelist, {
            'action': 'disable_links',
            '_selected_action': [str(token.pk)],
            'index': '0',
        })
        token.refresh_from_db()
        self.assertTrue(token.is_disabled)
        self.assertTrue(
            AuditLog.objects.filter(
                action='PRIVATE_ACCESS_LINK_DISABLED',
                target=str(token.pk),
            ).exists()
        )

        self.client.post(changelist, {
            'action': 'enable_links',
            '_selected_action': [str(token.pk)],
            'index': '0',
        })
        token.refresh_from_db()
        self.assertTrue(token.is_valid)

        self.client.post(changelist, {
            'action': 'revoke_links',
            '_selected_action': [str(token.pk)],
            'index': '0',
        })
        token.refresh_from_db()
        self.assertTrue(token.is_revoked)
        self.assertFalse(token.is_valid)
        self.assertTrue(
            AuditLog.objects.filter(
                action='PRIVATE_ACCESS_LINK_REVOKED',
                target=str(token.pk),
            ).exists()
        )

    def test_private_access_token_consumption_is_safe_for_stale_instances(self):
        token, _ = PrivateAccessToken.issue_token(
            label='One time link',
            max_uses=1,
        )
        stale_copy = PrivateAccessToken.objects.get(pk=token.pk)

        self.assertTrue(token.consume())
        self.assertFalse(stale_copy.consume())
        token.refresh_from_db()
        self.assertEqual(token.used_count, 1)
        self.assertFalse(token.active)

    def test_non_staff_user_cannot_open_admin_or_issue_private_access_links(self):
        issue_url = reverse('admin:opportunity_agent_privateaccesstoken_issue_link')
        self.assertEqual(self.client.get('/admin/login/').status_code, 200)
        response = self.client.get('/admin/')
        self.assertRedirects(response, f'/admin/login/?next={quote("/admin/")}')

        self.client.force_login(self.user)
        response = self.client.get(issue_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response['Location'])

    def test_legacy_auth_routes_and_public_opportunity_pages_remain_available(self):
        opportunity = Opportunity.objects.create(
            title='Public opportunity',
            dedupe_hash='public-opportunity-auth-test',
        )
        self.assertEqual(reverse('login_legacy'), '/login/')
        self.assertEqual(reverse('signup_legacy'), '/signup/')
        self.assertEqual(reverse('create_application', args=[opportunity.pk]), reverse('apply_opportunity', args=[opportunity.pk]))
        self.assertEqual(self.client.get('/opportunities/').status_code, 200)
        self.assertEqual(self.client.get(reverse('opportunity_detail', args=[opportunity.pk])).status_code, 200)

    def test_opportunity_without_application_url_displays_only_extracted_contacts(self):
        opportunity = Opportunity.objects.create(
            title='Contact application opportunity',
            contact_email='apply@example.org',
            contact_phone='+1 212 555 1234',
            telegram_contact='https://t.me/example_apply',
            physical_address='1 Research Road, Nairobi',
            organization_website='https://example.org',
            source_url='https://source.example.org/opportunities/1',
            dedupe_hash='contact-only-opportunity-detail-test',
        )

        response = self.client.get(reverse('opportunity_detail', args=[opportunity.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No application method was verified in the source')
        self.assertContains(response, 'Contact information')
        self.assertContains(response, 'apply@example.org')
        self.assertContains(response, '+1 212 555 1234')
        self.assertContains(response, 'https://t.me/example_apply')
        self.assertContains(response, '1 Research Road, Nairobi')
        self.assertContains(response, 'Organization website:')
        self.assertContains(response, 'View source')

    def test_opportunity_detail_displays_verified_alternatives_and_source_only_warning(self):
        verified = Opportunity.objects.create(
            title='Email and portal opportunity',
            application_method='online',
            application_url='https://apply.example.org/entry',
            application_methods=[
                {
                    'method': 'online',
                    'destination': 'https://apply.example.org/entry',
                    'instructions': 'Submit the application through the portal.',
                },
                {
                    'method': 'email',
                    'destination': 'cv@example.org',
                    'instructions': 'Alternatively, email your CV.',
                },
            ],
            application_instructions=(
                'Submit the application through the portal. Alternatively, email your CV.'
            ),
            source_url='https://source.example.org/opportunities/verified',
            dedupe_hash='verified-alternative-method-detail-test',
        )
        response = self.client.get(reverse('opportunity_detail', args=[verified.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Source-identified application instructions and destinations.')
        self.assertContains(response, 'Alternatively, email your CV.')
        self.assertContains(response, 'mailto:cv@example.org')

        source_only = Opportunity.objects.create(
            title='Unverified application opportunity',
            application_method='source_only',
            source_url='https://source.example.org/opportunities/unverified',
            dedupe_hash='source-only-warning-detail-test',
        )
        response = self.client.get(reverse('opportunity_detail', args=[source_only.pk]))
        self.assertContains(response, 'Needs review:')
        self.assertContains(response, source_only.source_url)
        self.assertNotContains(response, 'Open official application page')


class RolePermissionTests(TestCase):
    def test_super_admin_role_can_manage_accounts_groups_and_profiles(self):
        call_command('setup_roles')
        group = Group.objects.get(name='SUPER ADMIN')

        for codename in ('view_user', 'add_user', 'change_user', 'delete_user',
                         'view_group', 'add_group', 'change_group', 'delete_group'):
            with self.subTest(permission=codename):
                self.assertTrue(group.permissions.filter(
                    content_type__app_label='auth',
                    codename=codename,
                ).exists())

        profile_permissions = Permission.objects.filter(
            content_type__app_label='opportunity_agent',
            codename__in=(
                'view_userprofile',
                'add_userprofile',
                'change_userprofile',
                'delete_userprofile',
            ),
        )
        self.assertEqual(profile_permissions.count(), 4)
        self.assertTrue(group.permissions.filter(pk__in=profile_permissions).count() == 4)

    def test_platform_roles_have_distinct_least_privilege_permissions(self):
        call_command('setup_roles')
        roles = {
            name: Group.objects.get(name=name)
            for name in ('SUPER ADMIN', 'ADMIN', 'OPERATOR', 'REVIEWER', 'USER')
        }
        self.assertTrue(roles['SUPER ADMIN'].permissions.filter(
            content_type__app_label='opportunity_agent',
        ).count() >= Permission.objects.filter(
            content_type__app_label='opportunity_agent',
        ).count())

        self.assertTrue(roles['ADMIN'].permissions.filter(
            content_type__app_label='opportunity_agent',
            codename='manage_telegram',
        ).exists())
        self.assertFalse(roles['ADMIN'].permissions.filter(
            content_type__app_label='opportunity_agent',
            codename='manage_security_settings',
        ).exists())
        self.assertTrue(roles['OPERATOR'].permissions.filter(
            content_type__app_label='opportunity_agent',
            codename__in=('scan_sources', 'retry_application'),
        ).count() == 2)
        self.assertTrue(roles['REVIEWER'].permissions.filter(
            content_type__app_label='opportunity_agent',
            codename='review_application',
        ).exists())
        self.assertFalse(roles['REVIEWER'].permissions.filter(
            content_type__app_label='opportunity_agent',
            codename='change_application',
        ).exists())
        self.assertTrue(roles['USER'].permissions.filter(
            content_type__app_label='opportunity_agent',
            codename__in=('change_userprofile', 'view_application', 'add_application'),
        ).count() == 3)
        self.assertTrue(roles['USER'].permissions.filter(
            content_type__app_label='opportunity_agent',
            codename='add_match',
        ).exists())
        self.assertFalse(roles['USER'].permissions.filter(
            content_type__app_label='opportunity_agent',
            codename='view_auditlog',
        ).exists())

    def test_super_admin_can_assign_permissions_directly_to_a_user(self):
        call_command('setup_roles')
        group = Group.objects.get(name='SUPER ADMIN')
        actor = User.objects.create_user(
            username='role-admin',
            password='StrongPass123!',
            is_staff=True,
        )
        actor.groups.add(group)
        target = User.objects.create_user(
            username='permission-target',
            password='StrongPass123!',
        )
        permission = Permission.objects.get(
            content_type__app_label='opportunity_agent',
            codename='view_source',
        )

        request = RequestFactory().get('/admin/auth/user/')
        request.user = actor
        user_admin = admin.site.get_model_admin(User)
        self.assertIn(
            'user_permissions',
            dict(user_admin.get_fieldsets(request, target))['Permissions']['fields'],
        )

        form_class = user_admin.get_form(request, target)
        permissions_widget = form_class.base_fields['user_permissions'].widget.widget
        self.assertIsInstance(permissions_widget, FilteredSelectMultiple)
        self.assertFalse(permissions_widget.is_stacked)
        form = form_class(data={
            'username': target.username,
            'first_name': target.first_name,
            'last_name': target.last_name,
            'email': target.email,
            'is_active': 'on',
            'is_staff': '',
            'is_superuser': '',
            'groups': [],
            'user_permissions': [str(permission.pk)],
            'date_joined_0': target.date_joined.strftime('%Y-%m-%d'),
            'date_joined_1': target.date_joined.strftime('%H:%M:%S'),
        }, instance=target)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        target.refresh_from_db()
        self.assertTrue(target.user_permissions.filter(pk=permission.pk).exists())

        self.client.force_login(actor)
        response = self.client.get(reverse('admin:auth_user_change', args=[target.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'SelectFilter2.js')
        self.assertContains(response, 'user_permissions')

    def test_admin_can_manage_users_without_granting_staff_or_superuser_access(self):
        call_command('setup_roles')
        admin_user = User.objects.create_user(
            username='limited-admin',
            password='StrongPass123!',
            is_staff=True,
        )
        admin_user.groups.add(Group.objects.get(name='ADMIN'))
        protected_staff = User.objects.create_user(
            username='protected-staff',
            password='StrongPass123!',
            is_staff=True,
        )
        protected_superuser = User.objects.create_superuser(
            username='protected-superuser',
            email='protected-superuser@example.com',
            password='StrongPass123!',
        )
        request = RequestFactory().get('/admin/auth/user/')
        request.user = admin_user

        user_admin = admin.site.get_model_admin(User)
        fieldsets = user_admin.get_fieldsets(request)
        fields = {
            field
            for _, options in fieldsets
            for field in options.get('fields', ())
        }

        self.assertIn('username', fields)
        self.assertNotIn('is_superuser', fields)
        self.assertNotIn('is_staff', fields)
        self.assertNotIn('groups', fields)
        self.assertNotIn('user_permissions', fields)
        visible_users = set(user_admin.get_queryset(request).values_list('pk', flat=True))
        self.assertNotIn(protected_staff.pk, visible_users)
        self.assertNotIn(protected_superuser.pk, visible_users)

    def test_reviewer_can_only_view_review_queue_and_cannot_edit_or_delete_records(self):
        call_command('setup_roles')
        reviewer = User.objects.create_user(
            username='application-reviewer',
            password='StrongPass123!',
            is_staff=True,
        )
        reviewer.groups.add(Group.objects.get(name='REVIEWER'))
        owner = User.objects.create_user(
            username='review-queue-owner',
            password='StrongPass123!',
        )
        pending = Application.objects.create(
            user=owner,
            opportunity=Opportunity.objects.create(
                title='Pending Review Opportunity',
                dedupe_hash='pending-review-application',
            ),
            status='needs_review',
        )
        queued = Application.objects.create(
            user=owner,
            opportunity=Opportunity.objects.create(
                title='Queued Application Opportunity',
                dedupe_hash='queued-application-review-test',
            ),
            status='queued',
        )
        request = RequestFactory().get('/admin/opportunity_agent/application/')
        request.user = reviewer
        application_admin = admin.site.get_model_admin(Application)

        visible_ids = set(application_admin.get_queryset(request).values_list('pk', flat=True))

        self.assertEqual(visible_ids, {pending.pk})
        self.assertTrue(application_admin.has_change_permission(request, pending))
        self.assertFalse(application_admin.has_change_permission(request, queued))
        self.assertFalse(application_admin.has_delete_permission(request, pending))
        actions = application_admin.get_actions(request)
        self.assertIn('approve_applications', actions)
        self.assertIn('reject_applications', actions)
        self.assertNotIn('queue_apps', actions)
        self.assertNotIn('retry_failed_apps', actions)

    def test_reviewer_approval_action_queues_and_audits_application(self):
        call_command('setup_roles')
        reviewer = User.objects.create_user(
            username='approval-reviewer',
            password='StrongPass123!',
            is_staff=True,
        )
        reviewer.groups.add(Group.objects.get(name='REVIEWER'))
        owner = User.objects.create_user(username='approval-owner', password='StrongPass123!')
        application = Application.objects.create(
            user=owner,
            opportunity=Opportunity.objects.create(
                title='Needs Approval',
                dedupe_hash='reviewer-approval-test',
            ),
            status='pending',
        )

        self.client.force_login(reviewer)
        with patch('opportunity_agent.admin.process_application_queue_task.delay') as queue_task:
            response = self.client.post(
                reverse('admin:opportunity_agent_application_changelist'),
                {
                    'action': 'approve_applications',
                    '_selected_action': [str(application.pk)],
                    'index': '0',
                },
            )

        application.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(application.status, 'queued')
        queue_task.assert_called_once_with(1)
        self.assertTrue(AuditLog.objects.filter(
            actor=reviewer,
            action='application_approved',
            target=str(application.pk),
        ).exists())

    def test_operator_can_retry_failed_application_and_retry_is_audited(self):
        call_command('setup_roles')
        operator = User.objects.create_user(
            username='application-operator',
            password='StrongPass123!',
            is_staff=True,
        )
        operator.groups.add(Group.objects.get(name='OPERATOR'))
        owner = User.objects.create_user(username='retry-owner', password='StrongPass123!')
        application = Application.objects.create(
            user=owner,
            opportunity=Opportunity.objects.create(
                title='Failed Application',
                dedupe_hash='operator-retry-test',
            ),
            status='failed',
            error_message='Previous attempt failed.',
        )

        self.client.force_login(operator)
        with patch('opportunity_agent.admin.retry_applications_task.delay') as queue_task:
            response = self.client.post(
                reverse('admin:opportunity_agent_application_changelist'),
                {
                    'action': 'retry_failed_apps',
                    '_selected_action': [str(application.pk)],
                    'index': '0',
                },
            )

        application.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(application.status, 'failed')
        queue_task.assert_called_once_with(
            application_ids=[application.pk],
            status='failed',
            actor_id=operator.pk,
        )

    def test_admin_can_bulk_import_many_sources_from_csv(self):
        call_command('setup_roles')
        source_admin = User.objects.create_user(
            username='source-manager',
            password='StrongPass123!',
            is_staff=True,
        )
        source_admin.groups.add(Group.objects.get(name='ADMIN'))
        upload = SimpleUploadedFile(
            'sources.csv',
            (
                'name,url,source_type,country,opportunity_types,enabled,trust_score,scan_frequency,notes\n'
                'Job board,https://jobs.example.com,job_site,Worldwide,job|internship,true,0.8,daily,Company jobs\n'
                'Scholarship portal,https://scholarships.example.org,scholarship_site,Germany,scholarship|grant,true,0.9,weekly,Funding calls\n'
            ).encode(),
            content_type='text/csv',
        )
        self.client.force_login(source_admin)

        response = self.client.post(
            reverse('admin:opportunity_agent_source_import_csv'),
            {'file': upload},
        )

        self.assertRedirects(
            response,
            reverse('admin:opportunity_agent_source_changelist'),
        )
        job_source = Source.objects.get(url='https://jobs.example.com')
        scholarship_source = Source.objects.get(url='https://scholarships.example.org')
        self.assertEqual(job_source.source_type, 'job_site')
        self.assertEqual(job_source.opportunity_types, ['job', 'internship'])
        self.assertEqual(job_source.trust_score, 0.8)
        self.assertEqual(scholarship_source.scan_frequency, 'weekly')

    def test_source_csv_import_is_atomic_when_any_row_is_invalid(self):
        call_command('setup_roles')
        source_admin = User.objects.create_user(
            username='source-import-admin',
            password='StrongPass123!',
            is_staff=True,
        )
        source_admin.groups.add(Group.objects.get(name='ADMIN'))
        upload = SimpleUploadedFile(
            'invalid-sources.csv',
            (
                'name,url,source_type\n'
                'Valid source,https://valid.example.com,website\n'
                'Invalid source,https://invalid.example.com,not-a-source-type\n'
            ).encode(),
            content_type='text/csv',
        )
        self.client.force_login(source_admin)

        response = self.client.post(
            reverse('admin:opportunity_agent_source_import_csv'),
            {'file': upload},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Nothing was imported')
        self.assertFalse(Source.objects.filter(url='https://valid.example.com').exists())
        self.assertFalse(Source.objects.filter(url='https://invalid.example.com').exists())

    def test_source_csv_import_skips_existing_and_repeated_urls(self):
        call_command('setup_roles')
        source_admin = User.objects.create_user(
            username='source-dedup-admin',
            password='StrongPass123!',
            is_staff=True,
        )
        source_admin.groups.add(Group.objects.get(name='ADMIN'))
        Source.objects.create(name='Existing source', url='https://existing.example.com')
        upload = SimpleUploadedFile(
            'duplicate-sources.csv',
            (
                'name,url\n'
                'Existing source,https://existing.example.com\n'
                'New source,https://new.example.com\n'
                'Duplicate new source,https://new.example.com\n'
            ).encode(),
            content_type='text/csv',
        )
        self.client.force_login(source_admin)

        response = self.client.post(
            reverse('admin:opportunity_agent_source_import_csv'),
            {'file': upload},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Imported 1 source(s); skipped 2 existing or duplicate URL(s).')
        self.assertEqual(Source.objects.filter(url='https://existing.example.com').count(), 1)
        self.assertEqual(Source.objects.filter(url='https://new.example.com').count(), 1)

    def test_source_csv_import_requires_add_source_permission(self):
        staff_user = User.objects.create_user(
            username='no-source-permission',
            password='StrongPass123!',
            is_staff=True,
        )
        self.client.force_login(staff_user)

        response = self.client.get(reverse('admin:opportunity_agent_source_import_csv'))

        self.assertEqual(response.status_code, 403)

    def test_user_without_application_permission_cannot_submit_apply_action(self):
        user = User.objects.create_user(
            username='no-application-permission',
            password='StrongPass123!',
        )
        opportunity = Opportunity.objects.create(
            title='Permission-Protected Application',
            dedupe_hash='user-no-application-permission-test',
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse('apply_opportunity', args=[opportunity.pk]),
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Application.objects.filter(user=user, opportunity=opportunity).exists())


class CredentialEncryptionTests(TestCase):
    @override_settings(CREDENTIAL_ENCRYPTION_KEY='stable-test-encryption-key')
    def test_site_and_mailbox_secrets_are_encrypted_at_rest(self):
        secret = 'not-stored-in-plaintext'
        encrypted = encrypt_secret(secret)
        self.assertNotEqual(encrypted, secret)
        self.assertEqual(decrypt_secret(encrypted), secret)

    def test_profile_preferences_accept_comma_separated_countries_and_choices(self):
        user = User.objects.create_user(username='profile-preferences', password='StrongPass123!')
        profile = UserProfile.objects.create(user=user)
        form = UserProfileForm(data={
            'target_countries': 'Ethiopia, Germany',
            'worldwide_preference': 'on',
            'skills': 'Python, Django',
            'languages': 'English, Amharic',
            'preferred_opportunity_types': ['job', 'scholarship'],
            'preferred_work_modes': ['hybrid', 'remote'],
            'minimum_ai_match_score': '75',
            'daily_application_limit': '5',
        }, instance=profile)

        self.assertTrue(form.is_valid(), form.errors)
        saved_profile = form.save()
        self.assertEqual(saved_profile.target_countries, ['Ethiopia', 'Germany'])
        self.assertEqual(saved_profile.skills, ['Python', 'Django'])
        self.assertEqual(saved_profile.preferred_opportunity_types, ['job', 'scholarship'])
        self.assertEqual(saved_profile.preferred_work_modes, ['hybrid', 'remote'])

    def test_public_opportunity_list_filters_country_and_work_mode(self):
        on_site = Opportunity.objects.create(
            title='On-site Germany role',
            country='Germany',
            work_mode='on_site',
            dedupe_hash='onsite-germany-filter-test',
        )
        remote = Opportunity.objects.create(
            title='Remote Germany role',
            country='Germany',
            work_mode='remote',
            dedupe_hash='remote-germany-filter-test',
        )

        response = self.client.get('/opportunities/', {'country': 'Germany', 'mode': 'remote'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, remote.title)
        self.assertNotContains(response, on_site.title)

    def test_mailbox_form_is_scoped_to_the_current_user(self):
        owner = User.objects.create_user(username='mailbox-owner', password='StrongPass123!')
        other = User.objects.create_user(username='other-owner', password='StrongPass123!')
        own_mailbox = EmailMailbox.objects.create(user=owner, name='Primary', email='owner@example.com')
        other_mailbox = EmailMailbox.objects.create(user=other, name='Primary', email='other@example.com')

        form = SiteCredentialForm(user=owner)
        self.assertEqual(list(form.fields['email_mailbox'].queryset), [own_mailbox])
        self.assertNotIn(other_mailbox, form.fields['email_mailbox'].queryset)


class UserDataPrivacyTests(TestCase):
    def setUp(self):
        call_command('setup_roles')
        self.owner = User.objects.create_user(
            username='private-owner',
            email='owner-private@example.com',
            password='StrongPass123!',
        )
        self.other = User.objects.create_user(
            username='private-other',
            email='other-private@example.com',
            password='StrongPass123!',
        )
        self.owner_profile = UserProfile.objects.create(
            user=self.owner,
            full_name='Owner Private Name',
            phone='+111111111',
            skills=['private-owner-skill'],
        )
        self.other_profile = UserProfile.objects.create(
            user=self.other,
            full_name='Other Private Name',
            phone='+222222222',
            skills=['private-other-skill'],
        )
        user_group = Group.objects.get(name='USER')
        self.owner.groups.add(user_group)
        self.other.groups.add(user_group)

    def test_profile_page_only_exposes_signed_in_users_profile(self):
        self.client.force_login(self.owner)

        response = self.client.get(reverse('profile_edit'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Owner Private Name')
        self.assertNotContains(response, 'Other Private Name')
        self.assertNotContains(response, 'private-other-skill')

    def test_dashboard_only_shows_signed_in_users_applications(self):
        own_opportunity = Opportunity.objects.create(
            title='Owner Private Application',
            dedupe_hash='owner-private-application',
        )
        other_opportunity = Opportunity.objects.create(
            title='Other Public Opportunity',
            dedupe_hash='other-private-application',
        )
        own_application = Application.objects.create(
            user=self.owner,
            opportunity=own_opportunity,
        )
        other_application = Application.objects.create(
            user=self.other,
            opportunity=other_opportunity,
            status='submitted',
            rejection_reason='Private application decision.',
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse('user_dashboard'))

        self.assertContains(response, own_opportunity.title)
        self.assertEqual(
            [application.pk for application in response.context['applications']],
            [own_application.pk],
        )
        self.assertNotIn(
            other_application.pk,
            [application.pk for application in response.context['applications']],
        )
        self.assertNotContains(response, 'Private application decision.')

    def test_cv_is_only_downloadable_by_its_owner_or_authorized_profile_admin(self):
        with TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                self.owner_profile.cv.save(
                    'private-cv.txt',
                    SimpleUploadedFile('private-cv.txt', b'confidential cv data'),
                    save=True,
                )
                download_url = reverse(
                    'profile_cv_download',
                    kwargs={'user_id': self.owner.pk},
                )

                self.client.force_login(self.owner)
                owner_response = self.client.get(download_url)
                self.assertEqual(owner_response.status_code, 200)
                self.assertEqual(b''.join(owner_response.streaming_content), b'confidential cv data')
                self.assertEqual(owner_response['Cache-Control'], 'private, no-store')
                owner_response.close()

                self.client.force_login(self.other)
                self.assertEqual(self.client.get(download_url).status_code, 404)
                self.assertEqual(
                    self.client.get(f'/media/{self.owner_profile.cv.name}').status_code,
                    404,
                )

                profile_view_permission = Permission.objects.get(
                    content_type__app_label='opportunity_agent',
                    codename='view_userprofile',
                )
                profile_admin = User.objects.create_user(
                    username='profile-view-admin',
                    password='StrongPass123!',
                    is_staff=True,
                )
                profile_admin.user_permissions.add(profile_view_permission)
                self.client.force_login(profile_admin)
                admin_response = self.client.get(download_url)
                self.assertEqual(admin_response.status_code, 200)
                self.assertEqual(b''.join(admin_response.streaming_content), b'confidential cv data')
                admin_response.close()

                self.client.logout()
                self.assertEqual(self.client.get(download_url).status_code, 302)

                self.owner_profile.cv.delete(save=True)

    def test_profile_cv_upload_accepts_valid_pdf_and_docx_and_rejects_invalid_files(self):
        from docx import Document
        from pypdf import PdfWriter

        pdf_stream = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.write(pdf_stream)

        docx_stream = BytesIO()
        Document().save(docx_stream)
        valid_uploads = (
            ('resume.pdf', pdf_stream.getvalue()),
            ('resume.docx', docx_stream.getvalue()),
        )
        for filename, content in valid_uploads:
            with self.subTest(filename=filename):
                form = UserProfileForm(
                    data={
                        'minimum_ai_match_score': '75',
                        'daily_application_limit': '5',
                    },
                    files={'cv': SimpleUploadedFile(filename, content)},
                    instance=self.owner_profile,
                )
                self.assertTrue(form.is_valid(), form.errors)

        invalid_uploads = (
            ('resume.txt', b'not a supported CV'),
            ('resume.pdf', b'not a PDF'),
            ('resume.docx', b'not a DOCX archive'),
        )
        for filename, content in invalid_uploads:
            with self.subTest(filename=filename, content=content[:10]):
                form = UserProfileForm(
                    data={},
                    files={'cv': SimpleUploadedFile(filename, content)},
                    instance=self.owner_profile,
                )
                self.assertFalse(form.is_valid())
                self.assertIn('cv', form.errors)

    def test_profile_cv_upload_rejects_files_over_ten_megabytes(self):
        form = UserProfileForm(
            data={},
            files={
                'cv': SimpleUploadedFile(
                    'large.pdf',
                    b'%PDF-' + b'x' * (10 * 1024 * 1024),
                ),
            },
            instance=self.owner_profile,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('10 MB', str(form.errors['cv']))

    def test_uploaded_cv_is_retrievable_only_through_private_download_view(self):
        from pypdf import PdfWriter

        pdf_stream = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.write(pdf_stream)

        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            self.client.force_login(self.owner)
            response = self.client.post(
                reverse('profile_edit'),
                data={
                    'full_name': 'Owner Private Name',
                    'phone': '+111111111',
                    'current_country': 'Ethiopia',
                    'minimum_ai_match_score': '75',
                    'daily_application_limit': '5',
                    'cv': SimpleUploadedFile('resume.pdf', pdf_stream.getvalue()),
                },
            )
            self.assertEqual(response.status_code, 302)
            self.owner_profile.refresh_from_db()
            self.assertTrue(self.owner_profile.cv.name.endswith('.pdf'))

            download_url = reverse(
                'profile_cv_download',
                kwargs={'user_id': self.owner.pk},
            )
            download = self.client.get(download_url)
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download['Content-Type'], 'application/octet-stream')
            self.assertEqual(download['X-Content-Type-Options'], 'nosniff')
            download.close()

            self.assertEqual(
                self.client.get(f'/media/{self.owner_profile.cv.name}').status_code,
                404,
            )
            self.client.force_login(self.other)
            self.assertEqual(self.client.get(download_url).status_code, 404)

            self.owner_profile.cv.delete(save=True)


class SharedStorageFileTests(SimpleTestCase):
    def test_remote_file_is_materialized_temporarily_for_browser_upload(self):
        class RemoteFile:
            name = 'private/cv.pdf'

            @property
            def path(self):
                raise NotImplementedError

            def open(self, mode):
                self.asserted_mode = mode
                return BytesIO(b'cv contents')

        stored_file = RemoteFile()
        with local_file_path(stored_file) as path:
            self.assertTrue(Path(path).is_file())
            self.assertEqual(Path(path).read_bytes(), b'cv contents')
        self.assertFalse(Path(path).exists())
