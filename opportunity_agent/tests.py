from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.core.management import call_command
from django.test import TestCase
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import socket
import json

import requests

from .models import (
    Application,
    ApplicationAttempt,
    AuditLog,
    AutomationRun,
    Match,
    Opportunity,
    ProviderAdapter,
    Source,
    TelegramDestination,
    TelegramSource,
    UserProfile,
)
from .services.deduplication import (
    compute_dedupe_hash,
    content_fingerprint,
    deduplicate_and_save_opportunity,
    normalize_url,
)
from .services.matching import compute_match_score
from .services.ai_engine import AIClient, AIProviderError

User = get_user_model()


class OpportunityAgentTestCase(TestCase):
    def setUp(self):
        call_command('setup_roles')
        self.user = User.objects.create_user(username='alice', email='alice@example.com', password='StrongPass123!')
        self.user.groups.add(self.user.groups.model.objects.get(name='USER'))
        self.profile, _ = UserProfile.objects.get_or_create(
            user=self.user,
            defaults={
                'full_name': 'Alice Example',
                'phone': '+251900000000',
                'current_country': 'Ethiopia',
                'target_countries': ['Ethiopia', 'Germany'],
                'worldwide_preference': True,
                'skills': ['python', 'django', 'sql'],
                'education': 'Computer Science',
                'degree': 'BSc',
                'work_experience': '2 years experience',
                'languages': ['English', 'Amharic'],
                'preferred_opportunity_types': ['job', 'internship'],
                'preferred_work_modes': ['remote', 'hybrid'],
                'visa_sponsorship_preference': True,
                'minimum_ai_match_score': 75,
                'auto_apply': True,
                'daily_application_limit': 5,
            }
        )
        self.profile.cv.name = 'cvs/test-profile.pdf'
        self.profile.save(update_fields=['cv'])
        self.source = Source.objects.create(
            name='Example Opportunities',
            url='https://example.com/jobs',
            source_type='website',
            country='Ethiopia',
            opportunity_types=['job'],
            enabled=True,
            trust_score=0.9,
        )
        self.opportunity = Opportunity.objects.create(
            source=self.source,
            title='Senior Python Engineer',
            organization='Example Org',
            opportunity_type='job',
            country='Germany',
            remote_worldwide=True,
            description='Python Django job',
            requirements='Python and Django',
            skills=['python', 'django'],
            languages=['English'],
            visa_sponsorship=True,
            deadline='2035-01-01T00:00:00Z',
            application_url='https://example.com/apply',
            source_url='https://example.com/jobs?id=123',
            status='active',
        )

    def test_user_creation_and_profile(self):
        self.assertTrue(self.user.is_authenticated)
        self.assertEqual(self.profile.current_country, 'Ethiopia')

    def test_deterministic_intent_supports_common_chat_commands(self):
        from .services.agent_conversations import _deterministic_intent

        self.assertEqual(
            _deterministic_intent('Germany ውስጥ ያሉትን ብቻ ፈልግ')['action'],
            'search_opportunities',
        )
        self.assertEqual(
            _deterministic_intent('Scholarship የሚለውን ጨምር')['action'],
            'set_opportunity_type_filter',
        )
        self.assertEqual(
            _deterministic_intent('Match 80% በላይ የሆኑትን apply አድርግ')['action'],
            'bulk_apply',
        )
        self.assertEqual(
            _deterministic_intent('Canada አቁም')['action'],
            'remove_target_country',
        )
        self.assertEqual(
            _deterministic_intent('Disable auto apply for user alice.')['action'],
            'set_auto_apply',
        )

    def test_database_constraints_prevent_duplicate_records_and_invalid_limits(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Source.objects.create(
                    name='Duplicate URL',
                    url=self.source.url,
                )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.profile.minimum_ai_match_score = 101
                self.profile.save(update_fields=['minimum_ai_match_score'])
        self.profile.refresh_from_db()

        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            attempts=1,
        )
        attempt = ApplicationAttempt.objects.create(
            application=application,
            attempt_number=1,
            status='failed',
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ApplicationAttempt.objects.create(
                    application=application,
                    attempt_number=attempt.attempt_number,
                    status='failed',
                )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProviderAdapter.objects.create(name='Generic adapter')
                ProviderAdapter.objects.create(name='Generic adapter')

    def test_automatic_application_rejects_unsupported_saved_cv_extension(self):
        from .services.application_guard import ApplicationGuard

        self.profile.cv.name = 'cvs/resume.txt'
        available, reason = ApplicationGuard._cv_is_available(self.profile)

        self.assertFalse(available)
        self.assertEqual(reason, 'The saved CV must be a PDF or DOCX file.')

    def test_source_validation(self):
        source = Source(name='Good Source', url='https://good.example.com', source_type='website')
        source.full_clean()
        self.assertIn('https://', source.url)

    def test_source_validation_rejects_out_of_range_trust_and_unknown_types(self):
        for field, value in (
            ('trust_score', 1.5),
            ('opportunity_types', ['not-a-supported-type']),
            ('scan_frequency', 'every_minute'),
        ):
            source = Source(
                name='Invalid Source',
                url='https://invalid.example.com',
                **{field: value},
            )
            with self.subTest(field=field), self.assertRaises(ValidationError):
                source.full_clean()

    def test_source_scan_frequency_selects_only_due_enabled_sources(self):
        from .tasks import _sources_due_for_scan
        from django.utils import timezone
        from datetime import timedelta

        now = timezone.now()
        hourly_due = Source.objects.create(
            name='Hourly due',
            url='https://hourly-due.example.com',
            scan_frequency='hourly',
            last_scan=now - timedelta(hours=2),
        )
        hourly_not_due = Source.objects.create(
            name='Hourly not due',
            url='https://hourly-not-due.example.com',
            scan_frequency='hourly',
            last_scan=now - timedelta(minutes=10),
        )
        manual = Source.objects.create(
            name='Manual only',
            url='https://manual-source.example.com',
            scan_frequency='manual',
        )
        disabled = Source.objects.create(
            name='Disabled source',
            url='https://disabled-source.example.com',
            scan_frequency='daily',
            enabled=False,
        )

        due_ids = set(_sources_due_for_scan(now).values_list('pk', flat=True))

        self.assertIn(hourly_due.pk, due_ids)
        self.assertNotIn(hourly_not_due.pk, due_ids)
        self.assertNotIn(manual.pk, due_ids)
        self.assertNotIn(disabled.pk, due_ids)

    def test_api_source_yields_candidates_from_json(self):
        from .services.source_ingestion import extract_candidates

        api_source = Source(
            name='Public API',
            url='https://api.example.com/opportunities',
            source_type='api',
        )
        candidates = list(extract_candidates(
            api_source,
            api_source.url,
            '{"results":[{"title":"Research Fellowship","url":"/fellowships/1","description":"Open role"}]}',
        ))

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['title'], 'Research Fellowship')
        self.assertEqual(candidates[0]['url'], 'https://api.example.com/fellowships/1')
        self.assertEqual(candidates[0]['source_landing_url'], api_source.url)

    def test_extraction_preserves_explicit_api_fields_and_leaves_unknowns_empty(self):
        from .services.source_ingestion import basic_extract, extract_candidates
        from .tasks import _upsert_opportunity

        payload = {
            'title': 'Research Fellowship',
            'organization': 'Example Institute',
            'opportunity_type': 'fellowship',
            'country': 'Kenya',
            'city': 'Nairobi',
            'remote_worldwide': True,
            'description': 'Research training with field work.',
            'responsibilities': 'Conduct field research.',
            'requirements': 'Submit an application.',
            'qualifications': 'Masters degree.',
            'education_requirements': 'Masters degree required.',
            'experience_requirements': 'Two years of experience.',
            'skills': ['research', 'data analysis'],
            'languages': ['English'],
            'salary_stipend': '$2,000 monthly',
            'benefits': 'Health insurance.',
            'visa_sponsorship': False,
            'deadline': '2035-05-10',
            'application_url': 'https://example.org/apply',
            'contact_email': 'contact@example.org',
            'contact_phone': '+1 212 555 1234',
            'telegram_contact': 'https://t.me/example',
            'physical_address': '1 Research Road, Nairobi',
            'organization_website': 'https://example.org',
        }
        source = Source.objects.create(
            name='Opportunity Source',
            url='https://example.org/opportunities',
            source_type='api',
            country='Source-country-must-not-be-used',
        )
        candidate = next(extract_candidates(
            source,
            source.url,
            json.dumps({'items': [payload]}),
        ))
        extracted = basic_extract(candidate, source)

        self.assertEqual(extracted['organization'], 'Example Institute')
        self.assertEqual(extracted['country'], 'Kenya')
        self.assertEqual(extracted['city'], 'Nairobi')
        self.assertTrue(extracted['remote_worldwide'])
        self.assertFalse(extracted['visa_sponsorship'])
        self.assertEqual(extracted['application_url'], payload['application_url'])
        self.assertEqual(extracted['physical_address'], payload['physical_address'])
        self.assertEqual(extracted['raw_source_content'], json.dumps(payload, ensure_ascii=False))
        opportunity, created = _upsert_opportunity(source, extracted)
        self.assertTrue(created)
        self.assertEqual(opportunity.country, 'Kenya')
        self.assertEqual(opportunity.deadline.date().isoformat(), '2035-05-10')
        self.assertEqual(opportunity.raw_source_content, extracted['raw_source_content'])
        self.assertTrue(AuditLog.objects.filter(
            action='opportunity_discovered',
            target=str(opportunity.pk),
            details__source_id=source.pk,
        ).exists())

    def test_extraction_does_not_infer_missing_opportunity_facts(self):
        from .services.source_ingestion import basic_extract

        extracted = basic_extract(
            {
                'title': 'Open opportunity',
                'url': 'https://example.org/opportunities/1',
                'text': 'Open opportunity. Please read the page for details.',
            },
            self.source,
        )

        self.assertEqual(extracted['organization'], '')
        self.assertEqual(extracted['country'], '')
        self.assertEqual(extracted['city'], '')
        self.assertEqual(extracted['opportunity_type'], '')
        self.assertIsNone(extracted['remote_worldwide'])
        self.assertIsNone(extracted['visa_sponsorship'])
        self.assertEqual(extracted['application_url'], '')
        self.assertEqual(extracted['physical_address'], '')
        self.assertEqual(extracted['contact_email'], '')
        self.assertEqual(extracted['contact_phone'], '')
        self.assertEqual(extracted['telegram_contact'], '')
        self.assertEqual(extracted['organization_website'], '')

    def test_extraction_finds_explicit_contact_destinations_without_application_url(self):
        from .services.source_ingestion import basic_extract

        candidate = {
            'title': 'Research Fellowship',
            'url': 'https://example.org/fellowship',
            'text': (
                'Research Fellowship. Contact: fellowships@example.org. '
                'Phone: +1 212 555 1234. Telegram: https://t.me/example_fellowships. '
                'Address: 1 Research Road, Nairobi. Website: https://institute.example.org.'
            ),
            'html': (
                '<a href="mailto:fellowships@example.org">Email</a>'
                '<a href="tel:+12125551234">Phone</a>'
                '<a href="https://t.me/example_fellowships">Telegram</a>'
                '<a href="/about">Official website</a>'
                '<address>1 Research Road, Nairobi</address>'
            ),
        }

        extracted = basic_extract(candidate, self.source)

        self.assertEqual(extracted['application_url'], '')
        self.assertEqual(extracted['contact_email'], 'fellowships@example.org')
        self.assertEqual(extracted['contact_phone'], '+1 212 555 1234')
        self.assertEqual(extracted['telegram_contact'], 'https://t.me/example_fellowships')
        self.assertEqual(extracted['physical_address'], '1 Research Road, Nairobi')
        self.assertEqual(extracted['organization_website'], 'https://example.org/about')

    def test_extraction_detects_explicit_application_link_and_keeps_long_description(self):
        from .services.source_ingestion import basic_extract

        long_text = 'Eligibility and application details. ' * 500
        candidate = {
            'title': 'Research Fellowship',
            'url': 'https://example.org/fellowship',
            'source_url': 'https://example.org/opportunities',
            'text': long_text,
            'html': (
                '<a href="/apply/123">Apply Now</a>'
                '<a href="/about">Organization website</a>'
            ),
        }

        extracted = basic_extract(candidate, self.source)

        self.assertEqual(extracted['description'], long_text)
        self.assertEqual(extracted['source_url'], 'https://example.org/fellowship')
        self.assertEqual(extracted['application_url'], 'https://example.org/apply/123')

    def test_listing_category_page_is_not_treated_as_an_opportunity(self):
        from .services.source_ingestion import is_listing_page

        self.assertTrue(is_listing_page({
            'title': 'Browsing: Scholarships',
            'url': 'https://example.org/scholarships',
            'html': '<h1>Browsing: Scholarships</h1>',
        }))

    def test_generic_category_page_with_multiple_opportunity_links_is_a_listing(self):
        from .services.source_ingestion import is_listing_page

        self.assertTrue(is_listing_page({
            'title': 'Scholarships',
            'url': 'https://example.org/scholarships',
            'html': (
                '<h1>Scholarships</h1>'
                '<a href="/scholarship/1">Research Scholarship</a>'
                '<a href="/scholarship/2">Graduate Scholarship</a>'
                '<a href="/scholarship/3">Community Scholarship</a>'
            ),
        }))

    def test_structured_source_url_is_not_accepted_as_application_destination(self):
        from .services.source_ingestion import basic_extract

        extracted = basic_extract(
            {
                'title': 'Research Fellowship',
                'url': 'https://example.org/fellowship',
                'source_landing_url': 'https://example.org/opportunities',
                'text': '',
                'data': {'application_url': 'https://example.org/opportunities'},
            },
            self.source,
        )

        self.assertEqual(extracted['application_url'], '')

    def test_structured_detail_url_is_not_accepted_as_application_destination(self):
        from .services.source_ingestion import basic_extract

        extracted = basic_extract(
            {
                'title': 'Research Fellowship',
                'url': 'https://example.org/fellowship',
                'source_landing_url': 'https://example.org/opportunities',
                'text': '',
                'data': {'application_url': 'https://example.org/fellowship'},
            },
            self.source,
        )

        self.assertEqual(extracted['application_url'], '')

    def test_email_only_opportunity_remains_valid_without_application_destination(self):
        from .services.source_ingestion import basic_extract

        extracted = basic_extract(
            {
                'title': 'Research Fellowship',
                'url': 'https://example.org/fellowship',
                'text': 'To apply, email fellowships@example.org.',
            },
            self.source,
        )

        self.assertEqual(extracted['title'], 'Research Fellowship')
        self.assertEqual(extracted['application_url'], '')
        self.assertEqual(extracted['contact_email'], 'fellowships@example.org')

    def test_email_only_application_guard_returns_manual_contact_reason(self):
        from .services.application_guard import ApplicationGuard

        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='prepared',
        )
        self.opportunity.application_url = ''
        self.opportunity.contact_email = 'fellowships@example.org'
        self.opportunity.save(update_fields=['application_url', 'contact_email'])

        with patch.object(
            ApplicationGuard,
            '_cv_is_available',
            return_value=(True, ''),
        ):
            allowed, reason = ApplicationGuard.can_submit(
                self.user,
                self.opportunity,
                self.profile,
                application=application,
                adapter=Mock(),
            )

        self.assertFalse(allowed)
        self.assertIn('MANUAL_CONTACT_REQUIRED', reason)
        self.assertIn('fellowships@example.org', reason)

    def test_structured_requirement_values_are_normalized_without_inference(self):
        from .services.source_ingestion import basic_extract

        extracted = basic_extract(
            {
                'title': 'Research Fellowship',
                'url': 'https://example.org/fellowship',
                'text': '',
                'data': {
                    'requirements': ['Must be enrolled in a PhD program', 'Submit a CV'],
                    'qualifications': ['Research experience'],
                    'education': ['PhD candidate'],
                    'experience': ['Two years in a lab'],
                    'skills': 'Python; Data analysis',
                },
            },
            self.source,
        )

        self.assertEqual(
            extracted['requirements'],
            'Must be enrolled in a PhD program; Submit a CV',
        )
        self.assertEqual(extracted['qualifications'], 'Research experience')
        self.assertEqual(extracted['education_requirements'], 'PhD candidate')
        self.assertEqual(extracted['experience_requirements'], 'Two years in a lab')
        self.assertEqual(extracted['skills'], ['Python', 'Data analysis'])

    @patch('opportunity_agent.tasks._validate_public_source_url')
    @patch('opportunity_agent.tasks.fetch_public_source')
    @patch('opportunity_agent.tasks.AIClient.extract_opportunity', return_value={})
    @patch('opportunity_agent.tasks._upsert_opportunity', return_value=(None, False))
    def test_source_scan_fetches_detail_page_before_extracting(
        self,
        upsert,
        extract,
        fetch,
        validate_url,
    ):
        from .tasks import scan_sources_task

        source = Source.objects.get(pk=self.source.pk)
        landing_url = 'https://example.com/jobs'
        detail_url = 'https://example.com/jobs/research-fellowship'
        fetch.side_effect = [
            (landing_url, '<a href="/jobs/research-fellowship">Research Fellowship</a>', 'text/html'),
            (
                detail_url,
                '<html><h1>Research Fellowship</h1><p>Full detail text and requirements.</p>'
                '<a href="/apply/123">Application Form</a></html>',
                'text/html',
            ),
        ]

        result = scan_sources_task.run(limit=1, source_ids=[source.pk])

        self.assertEqual(result['successful'], 1)
        self.assertEqual(fetch.call_count, 2)
        saved_data = upsert.call_args.args[1]
        self.assertEqual(saved_data['title'], 'Research Fellowship')
        self.assertIn('Full detail text and requirements.', saved_data['description'])
        self.assertEqual(saved_data['source_url'], detail_url)
        self.assertEqual(upsert.call_args.args[0].url, landing_url)
        self.assertEqual(saved_data['application_url'], 'https://example.com/apply/123')

    @patch('opportunity_agent.tasks._validate_public_source_url')
    @patch(
        'opportunity_agent.tasks.fetch_public_source',
        return_value=(
            'https://example.com/scholarships',
            '<html><title>Browsing: Scholarships</title><body>Browse all scholarships.</body></html>',
            'text/html',
        ),
    )
    @patch('opportunity_agent.tasks.AIClient.extract_opportunity', return_value={})
    @patch('opportunity_agent.tasks._upsert_opportunity')
    def test_source_scan_does_not_save_explicit_category_page(
        self,
        upsert,
        extract,
        fetch,
        validate_url,
    ):
        from .tasks import scan_sources_task

        result = scan_sources_task.run(limit=1, source_ids=[self.source.pk])

        self.assertEqual(result['successful'], 1)
        upsert.assert_not_called()

    @patch('opportunity_agent.tasks._validate_public_source_url')
    @patch('opportunity_agent.tasks.fetch_public_source')
    @patch(
        'opportunity_agent.tasks.AIClient.extract_opportunity',
        side_effect=[
            {'application_url': 'https://example.com/jobs'},
            {'application_url': 'https://example.com/jobs/research-fellowship'},
        ],
    )
    @patch('opportunity_agent.tasks._upsert_opportunity', return_value=(None, False))
    def test_source_scan_does_not_accept_ai_source_or_detail_url_as_application(
        self,
        upsert,
        extract,
        fetch,
        validate_url,
    ):
        from .tasks import scan_sources_task

        source = Source.objects.get(pk=self.source.pk)
        landing_url = 'https://example.com/jobs'
        detail_url = 'https://example.com/jobs/research-fellowship'
        fetch.side_effect = [
            (
                landing_url,
                '<a href="/jobs/research-fellowship">Research Fellowship</a>',
                'text/html',
            ),
            (
                detail_url,
                '<html><h1>Research Fellowship</h1><p>Full fellowship details.</p></html>',
                'text/html',
            ),
        ] * 2

        scan_sources_task.run(limit=1, source_ids=[source.pk])
        scan_sources_task.run(limit=1, source_ids=[source.pk])

        self.assertEqual(extract.call_count, 2)
        self.assertEqual(upsert.call_count, 2)
        self.assertEqual(
            [call.args[1]['application_url'] for call in upsert.call_args_list],
            ['', ''],
        )

    @patch('opportunity_agent.tasks._validate_public_source_url')
    @patch('opportunity_agent.tasks.fetch_public_source')
    @patch('opportunity_agent.tasks.AIClient.extract_opportunity', return_value={})
    @patch('opportunity_agent.tasks._upsert_opportunity', return_value=(None, False))
    def test_source_scan_continues_after_candidate_detail_fetch_failure(
        self,
        upsert,
        extract,
        fetch,
        validate_url,
    ):
        from .tasks import scan_sources_task

        source = Source.objects.get(pk=self.source.pk)
        landing_url = 'https://example.com/jobs'
        fetch.side_effect = [
            (
                landing_url,
                '<a href="/jobs/broken">Broken Fellowship</a>'
                '<a href="/jobs/valid">Valid Fellowship</a>',
                'text/html',
            ),
            ValueError('detail page unavailable'),
            (
                'https://example.com/jobs/valid',
                '<html><h1>Valid Fellowship</h1><p>Valid opportunity details.</p></html>',
                'text/html',
            ),
        ]

        result = scan_sources_task.run(limit=1, source_ids=[source.pk])

        self.assertEqual(result['successful'], 1)
        self.assertEqual(len(result['candidate_errors']), 1)
        self.assertIn('/jobs/broken', result['candidate_errors'][0]['url'])
        self.assertEqual(upsert.call_count, 1)
        self.assertEqual(upsert.call_args.args[1]['title'], 'Valid Fellowship')

    def test_heuristic_source_extraction_does_not_treat_source_url_as_contact_website(self):
        from .services.source_scanner import _heuristic_extract

        extracted = _heuristic_extract(
            'Research Fellowship. Deadline: 2035-05-10.',
            'https://example.org/fellowship',
        )

        self.assertEqual(extracted['application_url'], '')
        self.assertEqual(extracted['contact_email'], '')
        self.assertEqual(extracted['contact_phone'], '')
        self.assertEqual(extracted['telegram_contact'], '')
        self.assertEqual(extracted['physical_address'], '')
        self.assertEqual(extracted['organization_website'], '')

    def test_remote_work_mode_does_not_imply_worldwide_eligibility(self):
        from .services.source_ingestion import basic_extract

        extracted = basic_extract(
            {
                'title': 'Remote role',
                'url': 'https://example.org/roles/1',
                'text': 'Remote role.',
                'data': {'remote': True},
            },
            self.source,
        )

        self.assertEqual(extracted['work_mode'], 'remote')
        self.assertIsNone(extracted['remote_worldwide'])

    def test_deduplication_hash(self):
        value = compute_dedupe_hash('Senior Python Engineer', 'Example Org', 'https://example.com/jobs?id=123')
        self.assertTrue(value)

    def test_deduplication_normalizes_tracking_urls_and_query_order(self):
        first = normalize_url(
            'HTTPS://www.example.com/jobs/42/?utm_source=newsletter&ref=homepage&id=42#apply'
        )
        second = normalize_url('https://example.com/jobs/42?id=42')

        self.assertEqual(first, second)

    def test_duplicate_opportunities_merge_across_sources_and_tracking_urls(self):
        from .tasks import _upsert_opportunity

        second_source = Source.objects.create(
            name='Syndicated Opportunities',
            url='https://syndicated.example.org/jobs',
        )
        shared = {
            'organization': 'Example Institute',
            'opportunity_type': 'job',
            'country': 'Kenya',
            'deadline': '2035-05-10',
            'description': 'Develop research software and support field data analysis.',
            'application_url': 'https://example.org/jobs/42?utm_source=telegram',
        }
        first, was_created = deduplicate_and_save_opportunity(
            self.source,
            {'title': 'Senior Research Software Engineer', **shared},
            raw_content='Senior Research Software Engineer. Develop research software and support field data analysis.',
        )
        duplicate, duplicate_created = _upsert_opportunity(
            second_source,
            {
                'title': 'Senior Research Software Engineer — Apply Now',
                **{
                    **shared,
                    'application_url': 'https://www.example.org/jobs/42?fbclid=tracking',
                    'contact_email': 'jobs@example.org',
                    'raw_source_content': 'Senior Research Software Engineer — Apply Now. Develop research software and support field data analysis!',
                },
            },
        )

        self.assertTrue(was_created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first.pk, duplicate.pk)
        self.assertEqual(duplicate.contact_email, 'jobs@example.org')
        self.assertEqual(duplicate.content_fingerprint, content_fingerprint(
            'Senior Research Software Engineer',
            'Example Institute',
            first.raw_source_content,
            first.deadline,
        ))
        self.assertEqual(
            duplicate.normalized_application_url,
            'https://example.org/jobs/42',
        )

    def test_rss_scanner_uses_shared_duplicate_detector(self):
        from .services.source_scanner import _save_opportunity

        first, was_created = _save_opportunity(
            self.source,
            {
                'title': 'Data Analyst Internship',
                'organization': 'Example Research Institute',
                'opportunity_type': 'internship',
                'description': 'Analyze survey data and create research reports.',
                'application_url': 'https://example.org/jobs/analyst?utm_source=rss',
            },
            'Data Analyst Internship. Analyze survey data and create research reports.',
        )
        duplicate, duplicate_created = _save_opportunity(
            self.source,
            {
                'title': 'Data Analyst Intern',
                'organization': 'Example Research Institute',
                'opportunity_type': 'internship',
                'description': 'Analyze survey data and create research reports.',
                'application_url': 'https://www.example.org/jobs/analyst',
                'contact_email': 'internships@example.org',
            },
            'Data Analyst Intern. Analyze survey data and create research reports.',
        )

        self.assertTrue(was_created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first.pk, duplicate.pk)
        self.assertEqual(duplicate.contact_email, 'internships@example.org')

    def test_deduplication_keeps_distinct_opportunity_deadlines_separate(self):
        common = {
            'title': 'Graduate Research Fellowship',
            'organization': 'Example University',
            'opportunity_type': 'fellowship',
            'description': 'Research fellowship supporting graduate science students.',
            'application_url': 'https://example.edu/fellowship/apply',
        }
        first, created_first = deduplicate_and_save_opportunity(
            self.source,
            {**common, 'deadline': '2035-05-10'},
            raw_content='Research fellowship supporting graduate science students. Deadline May 10, 2035.',
        )
        second, created_second = deduplicate_and_save_opportunity(
            self.source,
            {**common, 'deadline': '2035-09-30'},
            raw_content='Research fellowship supporting graduate science students. Deadline September 30, 2035.',
        )

        self.assertTrue(created_first)
        self.assertTrue(created_second)
        self.assertNotEqual(first.pk, second.pk)

    def test_content_fingerprint_ignores_formatting_changes(self):
        first = content_fingerprint(
            'Scholarship: Research Program',
            'Example Institute Ltd.',
            'Funding for research students. Submit the application online.',
            '2035-05-10',
        )
        second = content_fingerprint(
            'Scholarship: Research Program',
            'Example Institute',
            '<p>Funding for research students!</p> Submit the application online.',
            '2035-05-10T00:00:00Z',
        )

        self.assertEqual(first, second)

    def test_matching(self):
        result = compute_match_score({
            'skills': ['python', 'django'],
            'current_country': 'Ethiopia',
            'preferred_opportunity_types': ['job'],
            'minimum_ai_match_score': 75,
            'visa_sponsorship_preference': True,
        }, {
            'skills': ['python', 'django'],
            'country': 'Germany',
            'remote_worldwide': True,
            'opportunity_type': 'job',
            'visa_sponsorship': True,
        })
        self.assertGreaterEqual(result['score'], 75)

    def test_matching_respects_selected_work_modes(self):
        profile = {
            'skills': ['python', 'django'],
            'current_country': 'Ethiopia',
            'target_countries': ['Germany'],
            'preferred_opportunity_types': ['job'],
            'preferred_work_modes': ['remote'],
            'minimum_ai_match_score': 50,
        }
        on_site_result = compute_match_score(profile, {
            'skills': ['python', 'django'],
            'country': 'Germany',
            'work_mode': 'on_site',
            'opportunity_type': 'job',
        })
        remote_result = compute_match_score(profile, {
            'skills': ['python', 'django'],
            'country': 'Germany',
            'work_mode': 'remote',
            'opportunity_type': 'job',
        })

        self.assertFalse(on_site_result['eligible'])
        self.assertEqual(on_site_result['eligibility_status'], 'work_mode_mismatch')
        self.assertIn('Work mode is outside your saved preferences.', on_site_result['missing'])
        self.assertTrue(remote_result['eligible'])
        self.assertIn('Work mode matches your preference.', remote_result['reasons'])

    def test_matching_returns_bounded_score_and_explanations_for_profile_factors(self):
        result = compute_match_score(
            {
                'skills': ['Python', 'Django'],
                'education': 'Master of Computer Science',
                'degree': 'MSc',
                'certifications': ['Cloud Security'],
                'work_experience': 'Python developer with data analysis experience',
                'languages': ['English'],
                'target_countries': ['Kenya'],
                'preferred_opportunity_types': ['job'],
                'preferred_work_modes': ['remote'],
                'visa_sponsorship_preference': True,
                'salary_stipend_preference': '',
                'minimum_ai_match_score': 75,
            },
            {
                'skills': ['Python', 'Django'],
                'education_requirements': 'Master degree in Computer Science',
                'qualifications': 'MSc and Cloud Security certification',
                'experience_requirements': 'Python developer experience',
                'languages': ['English'],
                'country': 'Kenya',
                'work_mode': 'remote',
                'opportunity_type': 'job',
                'visa_sponsorship': True,
            },
        )

        self.assertGreaterEqual(result['score'], 0)
        self.assertLessEqual(result['score'], 100)
        self.assertTrue(result['eligible'])
        self.assertEqual(result['eligibility_status'], 'eligible')
        self.assertIn('why_it_matches', result)
        self.assertIn('strong_matches', result)
        self.assertIn('missing_requirements', result)
        self.assertIn('risk_factors', result)
        self.assertIn('recommended_action', result)
        self.assertIn('qualifications', result['factor_scores'])

    def test_generic_residency_requirement_matching_profile_is_eligible(self):
        result = compute_match_score(
            {
                'current_country': 'Ethiopia',
                'worldwide_preference': True,
                'minimum_ai_match_score': 0,
            },
            {
                'requirements': 'Applicants must currently reside in Ethiopia.',
            },
        )

        self.assertTrue(result['eligible'])
        self.assertEqual(result['requirements_status'], 'clear')

    def test_generic_residency_requirement_conflicting_with_profile_is_ineligible(self):
        result = compute_match_score(
            {
                'current_country': 'Ethiopia',
                'worldwide_preference': True,
                'minimum_ai_match_score': 0,
            },
            {
                'requirements': 'Applicants must currently reside in Kenya.',
            },
        )

        self.assertFalse(result['eligible'])
        self.assertEqual(result['eligibility_status'], 'requirements_conflict')
        self.assertEqual(result['requirements_status'], 'conflict')
        self.assertEqual(result['recommended_action'], 'review')

    def test_unavailable_profile_fact_in_generic_requirement_requires_review(self):
        result = compute_match_score(
            {
                'current_country': 'Ethiopia',
                'worldwide_preference': True,
                'minimum_ai_match_score': 0,
            },
            {
                'requirements': 'Applicants must be citizens of Ethiopia.',
            },
        )

        self.assertFalse(result['eligible'])
        self.assertEqual(result['eligibility_status'], 'requirements_unconfirmed')
        self.assertEqual(result['requirements_status'], 'unconfirmed')
        self.assertEqual(result['recommended_action'], 'review')

    @patch('opportunity_agent.tasks.AIClient.generate_cover_letter', return_value='Reviewed cover letter')
    def test_manual_match_threshold_override_is_explicit_and_audited(self, generate_cover_letter):
        self.profile.minimum_ai_match_score = 95
        self.profile.skills = []
        self.profile.education = ''
        self.profile.degree = ''
        self.profile.work_experience = ''
        self.profile.languages = []
        self.profile.save()
        self.client.force_login(self.user)

        response = self.client.post(
            f'/opportunities/{self.opportunity.pk}/apply/',
            {'match_override': '1'},
        )

        self.assertEqual(response.status_code, 302)
        application = Application.objects.get(user=self.user, opportunity=self.opportunity)
        self.assertEqual(application.status, 'queued')
        self.assertTrue(application.match_override)
        self.assertEqual(application.audit_history[-1]['action'], 'match_threshold_override')
        self.assertTrue(
            AuditLog.objects.filter(
                actor=self.user,
                action='match_threshold_override',
                target=str(application.pk),
            ).exists()
        )
        from .tasks import process_application_queue_task

        result = process_application_queue_task.run(limit=5)

        self.assertEqual(result['processed'], 1)
        application.refresh_from_db()
        self.assertEqual(application.status, 'prepared')
        self.assertTrue(application.match_override)

    def test_below_threshold_manual_application_requires_override(self):
        self.profile.minimum_ai_match_score = 95
        self.profile.skills = []
        self.profile.education = ''
        self.profile.degree = ''
        self.profile.work_experience = ''
        self.profile.languages = []
        self.profile.save()
        self.client.force_login(self.user)

        self.client.post(f'/opportunities/{self.opportunity.pk}/apply/')

        application = Application.objects.get(user=self.user, opportunity=self.opportunity)
        self.assertEqual(application.status, 'needs_review')
        self.assertFalse(application.match_override)

        self.client.post(
            f'/opportunities/{self.opportunity.pk}/apply/',
            {'match_override': '1'},
        )
        application.refresh_from_db()
        self.assertEqual(application.status, 'queued')
        self.assertTrue(application.match_override)

    def test_manual_threshold_override_cannot_bypass_country_preferences(self):
        self.profile.minimum_ai_match_score = 95
        self.profile.skills = []
        self.profile.worldwide_preference = False
        self.profile.target_countries = ['Ethiopia']
        self.profile.save()
        self.opportunity.country = 'Germany'
        self.opportunity.remote_worldwide = False
        self.opportunity.work_mode = 'on_site'
        self.opportunity.save()
        self.client.force_login(self.user)

        detail = self.client.get(f'/opportunities/{self.opportunity.pk}/')
        response = self.client.post(
            f'/opportunities/{self.opportunity.pk}/apply/',
            {'match_override': '1'},
        )

        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'Missing or unconfirmed requirements')
        self.assertNotContains(detail, 'Queue anyway despite')
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Application.objects.filter(user=self.user, opportunity=self.opportunity).exists())

    def test_country_and_worldwide_filters(self):
        self.assertTrue(self.opportunity.remote_worldwide or self.opportunity.country)

    def test_deadline_validation(self):
        self.assertFalse(self.opportunity.is_expired())

    def test_deadline_status_detects_past_today_upcoming_and_missing(self):
        from datetime import datetime, time, timedelta
        from django.utils import timezone

        today = timezone.localdate()
        past = Opportunity(deadline=timezone.make_aware(
            datetime.combine(today - timedelta(days=1), time.min)
        ))
        due_today = Opportunity(deadline=timezone.make_aware(
            datetime.combine(today, time.max)
        ))
        upcoming = Opportunity(deadline=timezone.make_aware(
            datetime.combine(today + timedelta(days=1), time.max)
        ))
        missing = Opportunity()

        self.assertEqual(past.deadline_status, 'past')
        self.assertEqual(due_today.deadline_status, 'today')
        self.assertFalse(due_today.is_expired())
        self.assertEqual(upcoming.deadline_status, 'upcoming')
        self.assertEqual(missing.deadline_status, 'missing')

    def test_deadline_parser_handles_explicit_labels_and_leaves_unlabeled_dates_empty(self):
        from .services.deadlines import extract_explicit_deadline, parse_deadline
        from .services.source_ingestion import basic_extract

        parsed = extract_explicit_deadline(
            'Applications close on October 15, 2035. This program began on May 3, 2020.'
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.date().isoformat(), '2035-10-15')
        self.assertEqual(parsed.hour, 23)
        self.assertEqual(
            extract_explicit_deadline('The program began on May 3, 2020.'),
            None,
        )
        self.assertIsNone(parse_deadline('dates will be announced'))
        extracted = basic_extract(
            {
                'title': 'Open Fellowship',
                'url': 'https://example.org/fellowship',
                'text': 'Apply by 2035-10-15. No application URL is listed.',
            },
            self.source,
        )
        self.assertEqual(extracted['deadline'].date().isoformat(), '2035-10-15')

    def test_ingestion_normalizes_date_only_deadline_to_end_of_day(self):
        from .services.deadlines import parse_deadline

        parsed = parse_deadline('2035-05-10')

        self.assertEqual(parsed.date().isoformat(), '2035-05-10')
        self.assertEqual((parsed.hour, parsed.minute, parsed.second), (23, 59, 59))

    def test_application_duplicate_prevention(self):
        app = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='queued',
            generated_answers={'#work_authorization': 'Yes'},
        )
        self.assertEqual(Application.objects.filter(user=self.user, opportunity=self.opportunity).count(), 1)
        self.assertEqual(app.user_id, self.user.pk)
        self.assertEqual(app.generated_answers, {'#work_authorization': 'Yes'})
        self.assertEqual(app.audit_history[-1]['action'], 'application_created')

        app.status = 'matching'
        app.save(update_fields=['status'])
        app.refresh_from_db()
        self.assertEqual(app.audit_history[-1]['from_status'], 'queued')
        self.assertEqual(app.audit_history[-1]['status'], 'matching')

        with self.assertRaises(IntegrityError), transaction.atomic():
            Application.objects.create(user=self.user, opportunity=self.opportunity)

    def test_daily_application_limit(self):
        self.profile.daily_application_limit = 1
        self.profile.save()
        self.assertEqual(self.profile.daily_application_limit, 1)

    def test_daily_limit_counts_failed_attempts_per_user(self):
        from .services.application_guard import ApplicationGuard

        self.profile.daily_application_limit = 1
        self.profile.save(update_fields=['daily_application_limit'])
        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='prepared',
        )

        other_user = User.objects.create_user(
            username='bob',
            email='bob@example.com',
            password='StrongPass123!',
        )
        other_opportunity = Opportunity.objects.create(
            title='Other opportunity',
            application_url='https://example.com/other',
            dedupe_hash='daily-limit-other-user',
        )
        other_application = Application.objects.create(
            user=other_user,
            opportunity=other_opportunity,
            status='failed',
        )
        ApplicationAttempt.objects.create(
            application=other_application,
            attempt_number=1,
            status='failed',
        )

        adapter = Mock()
        adapter.config = {}
        adapter.preflight.return_value = (True, '')
        with patch.object(ApplicationGuard, '_cv_is_available', return_value=(True, '')):
            allowed, reason = ApplicationGuard.can_submit(
                self.user,
                self.opportunity,
                self.profile,
                application=application,
                adapter=adapter,
            )
            self.assertTrue(allowed, reason)

            another_opportunity = Opportunity.objects.create(
                title='Earlier attempt opportunity',
                application_url='https://example.com/earlier',
                dedupe_hash='daily-limit-earlier-attempt',
            )
            earlier_application = Application.objects.create(
                user=self.user,
                opportunity=another_opportunity,
                status='failed',
            )
            ApplicationAttempt.objects.create(
                application=earlier_application,
                attempt_number=1,
                status='failed',
            )
            allowed, reason = ApplicationGuard.can_submit(
                self.user,
                self.opportunity,
                self.profile,
                application=application,
                adapter=adapter,
            )
            self.assertFalse(allowed)
            self.assertEqual(reason, 'Daily application limit reached.')

            ApplicationAttempt.objects.create(
                application=application,
                attempt_number=1,
                status='failed',
            )
            allowed, reason = ApplicationGuard.can_submit(
                self.user,
                self.opportunity,
                self.profile,
                application=application,
                adapter=adapter,
            )
            self.assertFalse(allowed)
            self.assertEqual(reason, 'This application already used a submission attempt today.')

    def test_telegram_sources_are_separate_from_notification_destinations(self):
        from django.contrib.auth.models import Group

        telegram_source = TelegramSource.objects.create(
            name='Public opportunities channel',
            channel_url='https://t.me/public_opportunities',
        )
        destination = TelegramDestination.objects.create(
            name='Notification group',
            chat_id='-1001234567890',
            type='destination',
        )
        self.assertEqual(telegram_source.opportunities.count(), 0)
        self.assertEqual(destination.type, 'destination')
        self.assertNotIn('telegram', dict(Source.SOURCE_TYPES))
        for role in ('ADMIN', 'OPERATOR'):
            self.assertTrue(
                Group.objects.get(name=role).permissions.filter(
                    codename='scan_telegram_sources',
                ).exists(),
            )

        from .services.telegram import TelegramNotifier

        with self.assertRaisesMessage(
            ValueError,
            'Telegram notifications require a configured destination type.',
        ):
            TelegramNotifier().send_to_enabled_destinations('source', 'Should not send')

    @patch(
        'opportunity_agent.services.telegram_collector.collect_public_channel',
        new_callable=AsyncMock,
    )
    def test_telegram_source_scanner_updates_its_own_health(self, collect_channel):
        from .tasks import scan_telegram_sources_task

        collect_channel.return_value = []
        telegram_source = TelegramSource.objects.create(
            name='Public opportunities channel',
            channel_url='https://t.me/public_opportunities',
            scan_frequency='manual',
        )
        result = scan_telegram_sources_task.run(
            limit=1,
            telegram_source_ids=[telegram_source.pk],
        )

        telegram_source.refresh_from_db()
        self.assertEqual(result['successful'], 1)
        self.assertEqual(result['telegram_source_ids'], [telegram_source.pk])
        self.assertEqual(telegram_source.status, 'active')
        self.assertIsNotNone(telegram_source.last_successful_scan)
        collect_channel.assert_called_once()

    def test_telegram_collection_never_starts_an_unauthorized_login(self):
        import asyncio
        from .services.telegram_collector import collect_public_channel

        telegram_source = TelegramSource(
            name='Public opportunities channel',
            channel_url='https://t.me/public_opportunities',
        )
        client = Mock()
        client.connect = AsyncMock()
        client.is_user_authorized = AsyncMock(return_value=False)
        client.disconnect = AsyncMock()

        with patch.dict(
            'os.environ',
            {'TELEGRAM_API_ID': '12345', 'TELEGRAM_API_HASH': 'test-hash'},
        ), patch('telethon.TelegramClient', return_value=client):
            with self.assertRaisesRegex(RuntimeError, 'pre-authorized session'):
                asyncio.run(collect_public_channel(telegram_source))

        client.start.assert_not_called()
        client.disconnect.assert_awaited_once()

    def test_admin_telegram_bot_exposes_separate_source_command(self):
        from .services.telegram_bot import build_application_bot

        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'test-token'}):
            app = build_application_bot()

        commands = {
            command
            for handlers in app.handlers.values()
            for handler in handlers
            for command in getattr(handler, 'commands', ())
        }
        self.assertTrue({
            'status', 'users', 'sources', 'opportunities', 'applications',
            'rejected', 'failed', 'review', 'retry', 'apply', 'cancel',
            'autoapply', 'country', 'worldwide', 'setmatch', 'setlimit',
            'source', 'scan', 'discover', 'retry_failed', 'retry_rejected',
            'help',
        }.issubset(commands))

    def test_telegram_admin_authorization_checks_user_and_optional_chat(self):
        from types import SimpleNamespace
        from .services.telegram_bot import authorized

        def update(user_id, chat_id, chat_type='group'):
            return SimpleNamespace(
                effective_user=SimpleNamespace(id=user_id),
                effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
            )

        with patch.dict(
            'os.environ',
            {
                'TELEGRAM_ADMIN_USER_IDS': '123,456',
                'TELEGRAM_ADMIN_CHAT_IDS': '-10099',
            },
            clear=False,
        ):
            self.assertTrue(authorized(update(123, -10099)))
            self.assertFalse(authorized(update(789, -10099)))
            self.assertFalse(authorized(update(123, -10088)))

        with patch.dict(
            'os.environ',
            {
                'TELEGRAM_ADMIN_USER_IDS': '',
                'TELEGRAM_ADMIN_CHAT_IDS': '123',
            },
            clear=False,
        ):
            self.assertTrue(authorized(update(123, 123, 'private')))
            self.assertFalse(authorized(update(789, 123, 'private')))
            self.assertFalse(authorized(update(123, -10099, 'group')))

    def test_telegram_natural_language_is_explicit_and_deterministic(self):
        from .services.telegram_bot import handle_natural_language

        result = handle_natural_language('Enable worldwide opportunities.')
        self.assertIn('enabled', result)
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.worldwide_preference)

        result = handle_natural_language('Set minimum match score to 80.')
        self.assertIn('80', result)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.minimum_ai_match_score, 80)

        result = handle_natural_language('Disable auto apply for user alice.')
        self.assertIn('disabled', result)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.auto_apply)

        self.assertIn(
            'exact opportunity title',
            handle_natural_language('Why was this opportunity rejected?'),
        )
        self.assertIn(
            'could not safely interpret',
            handle_natural_language('Delete all opportunities.'),
        )

    def test_application_guard_enforces_automatic_submission_safety_gates(self):
        from django.utils import timezone
        from datetime import timedelta
        from .services.application_guard import ApplicationGuard

        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='prepared',
        )
        adapter = Mock()
        adapter.config = {}
        adapter.preflight.return_value = (True, '')

        with patch.object(ApplicationGuard, '_cv_is_available', return_value=(True, '')):
            self.profile.auto_apply = False
            self.profile.save(update_fields=['auto_apply'])
            allowed, reason = ApplicationGuard.can_submit(
                self.user, self.opportunity, self.profile, application=application, adapter=adapter,
            )
            self.assertFalse(allowed)
            self.assertIn('Auto-apply is disabled', reason)

            self.profile.auto_apply = True
            self.profile.daily_application_limit = 5
            self.profile.save(update_fields=['auto_apply', 'daily_application_limit'])
            self.opportunity.status = 'inactive'
            self.opportunity.save(update_fields=['status', 'updated_at'])
            allowed, reason = ApplicationGuard.can_submit(
                self.user, self.opportunity, self.profile, application=application, adapter=adapter,
            )
            self.assertFalse(allowed)
            self.assertIn('no longer active', reason)

            self.opportunity.status = 'active'
            self.opportunity.deadline = timezone.now() - timedelta(days=1)
            self.opportunity.save(update_fields=['status', 'deadline', 'updated_at'])
            allowed, reason = ApplicationGuard.can_submit(
                self.user, self.opportunity, self.profile, application=application, adapter=adapter,
            )
            self.assertFalse(allowed)
            self.assertIn('deadline has passed', reason)

            self.opportunity.deadline = timezone.now() + timedelta(days=10)
            self.opportunity.save(update_fields=['deadline', 'updated_at'])
            self.profile.daily_application_limit = 1
            self.profile.save(update_fields=['daily_application_limit'])
            other_opportunity = Opportunity.objects.create(
                title='Already submitted opportunity',
                application_url='https://example.com/other-apply',
                status='active',
                dedupe_hash='guard-daily-limit-opportunity',
            )
            Application.objects.create(
                user=self.user,
                opportunity=other_opportunity,
                status='submitted',
                submission_time=timezone.now(),
            )
            allowed, reason = ApplicationGuard.can_submit(
                self.user, self.opportunity, self.profile, application=application, adapter=adapter,
            )
            self.assertFalse(allowed)
            self.assertIn('Daily application limit', reason)

    def test_application_guard_requires_match_adapter_and_required_information(self):
        from .services.application_guard import ApplicationGuard

        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='prepared',
        )
        adapter = Mock()
        adapter.config = {'required_profile_fields': ['certifications']}
        adapter.preflight.return_value = (True, '')

        with patch.object(ApplicationGuard, '_cv_is_available', return_value=(True, '')):
            allowed, reason = ApplicationGuard.can_submit(
                self.user,
                self.opportunity,
                self.profile,
                application=application,
                adapter=adapter,
            )
            self.assertFalse(allowed)
            self.assertIn('Required information is missing: certifications', reason)

            adapter.config = {'required_answers': ['#work-authorization']}
            application.generated_answers = {}
            application.save(update_fields=['generated_answers'])
            allowed, reason = ApplicationGuard.can_submit(
                self.user,
                self.opportunity,
                self.profile,
                application=application,
                adapter=adapter,
            )
            self.assertFalse(allowed)
            self.assertIn('#work-authorization', reason)

            adapter.preflight.return_value = (False, 'Provider adapter is not verified.')
            adapter.config = {}
            allowed, reason = ApplicationGuard.can_submit(
                self.user,
                self.opportunity,
                self.profile,
                application=application,
                adapter=adapter,
            )
            self.assertFalse(allowed)
            self.assertIn('not verified', reason)

    def test_profile_completeness_explains_missing_sections(self):
        original_cv = self.profile.cv.name
        self.profile.education = ''
        self.profile.languages = []
        self.profile.target_countries = []
        self.profile.preferred_opportunity_types = []
        self.profile.preferred_work_modes = []
        self.profile.worldwide_preference = False
        self.profile.cv = ''
        self.profile.save()

        self.assertLess(self.profile.profile_completeness, 100)
        sections = {
            section['name']: section
            for section in self.profile.profile_completeness_sections
        }
        self.assertFalse(sections['Education']['complete'])
        self.assertEqual(sections['Education']['missing_fields'], ['Education details'])
        self.assertFalse(sections['CV / Resume']['complete'])
        self.assertFalse(sections['Preferences']['complete'])
        self.assertIn('Languages', self.profile.missing_profile_fields)
        self.assertIn(
            'Target countries, worldwide, opportunity types, or work modes',
            self.profile.missing_profile_fields,
        )
        self.client.force_login(self.user)
        profile_response = self.client.get('/profile/')
        self.assertEqual(profile_response.status_code, 200)
        self.assertContains(profile_response, 'Education details')
        self.assertContains(profile_response, 'CV / Resume')
        self.assertContains(profile_response, 'Languages')
        self.assertContains(profile_response, 'Profile: ')

        self.profile.education = 'Computer Science'
        self.profile.languages = ['English']
        self.profile.target_countries = ['Ethiopia']
        self.profile.cv.name = original_cv
        self.profile.save()

    def test_application_guard_blocks_submission_if_any_profile_section_is_missing(self):
        from .services.application_guard import ApplicationGuard

        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='prepared',
        )
        self.profile.work_experience = ''
        self.profile.save(update_fields=['work_experience', 'updated_at'])
        adapter = Mock()
        adapter.config = {}
        adapter.preflight.return_value = (True, '')

        with patch.object(ApplicationGuard, '_cv_is_available', return_value=(True, '')):
            allowed, reason = ApplicationGuard.can_submit(
                self.user,
                self.opportunity,
                self.profile,
                application=application,
                adapter=adapter,
            )

        self.assertFalse(allowed)
        self.assertIn('Required profile information is missing: Work experience', reason)

    def test_explicit_match_override_does_not_bypass_automatic_submission_threshold(self):
        from .services.application_guard import ApplicationGuard

        self.profile.minimum_ai_match_score = 100
        self.profile.save()
        self.opportunity.skills = ['rust', 'golang', 'elixir']
        self.opportunity.save(update_fields=['skills', 'updated_at'])
        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='prepared',
            match_override=True,
        )
        adapter = Mock()
        adapter.config = {}
        adapter.preflight.return_value = (True, '')

        with patch.object(ApplicationGuard, '_cv_is_available', return_value=(True, '')):
            allowed, reason = ApplicationGuard.can_submit(
                self.user,
                self.opportunity,
                self.profile,
                application=application,
                adapter=adapter,
            )

        self.assertFalse(allowed)
        self.assertIn('below the required threshold', reason)

    def test_application_guard_blocks_already_submitted_application(self):
        from django.utils import timezone
        from .services.application_guard import ApplicationGuard

        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='submitted',
            submission_time=timezone.now(),
        )
        adapter = Mock()
        adapter.config = {}
        adapter.preflight.return_value = (True, '')

        with patch.object(ApplicationGuard, '_cv_is_available', return_value=(True, '')):
            allowed, reason = ApplicationGuard.can_submit(
                self.user,
                self.opportunity,
                self.profile,
                application=application,
                adapter=adapter,
            )

        self.assertFalse(allowed)
        self.assertIn('already been submitted', reason)

    def test_queue_safety_failure_does_not_attempt_submission(self):
        from .tasks import execute_application_queue_task

        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='prepared',
        )
        adapter = Mock()
        adapter.config = {}
        adapter.preflight.return_value = (True, '')
        with patch('opportunity_agent.tasks.adapter_for', return_value=adapter):
            result = execute_application_queue_task.run(limit=1)

        application.refresh_from_db()
        self.assertEqual(result['submitted'], 0)
        self.assertEqual(application.status, 'needs_review')
        self.assertIn('saved CV file is missing', application.error_message)
        self.assertEqual(application.attempts, 0)
        adapter.submit.assert_not_called()
        self.assertFalse(ApplicationAttempt.objects.filter(application=application).exists())

    def test_queue_does_not_submit_below_threshold_even_with_manual_override(self):
        from .tasks import execute_application_queue_task

        self.profile.minimum_ai_match_score = 100
        self.profile.save()
        self.opportunity.skills = ['rust', 'golang', 'elixir']
        self.opportunity.save(update_fields=['skills', 'updated_at'])
        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='prepared',
            match_override=True,
        )
        adapter = Mock()
        adapter.config = {}
        adapter.preflight.return_value = (True, '')
        with patch('opportunity_agent.tasks.adapter_for', return_value=adapter), \
             patch('opportunity_agent.services.application_guard.ApplicationGuard._cv_is_available', return_value=(True, '')):
            result = execute_application_queue_task.run(limit=1)

        application.refresh_from_db()
        self.assertEqual(result['submitted'], 0)
        self.assertEqual(application.status, 'needs_review')
        self.assertIn('below the required threshold', application.error_message)
        self.assertEqual(application.attempts, 0)
        adapter.submit.assert_not_called()

    def test_application_statuses(self):
        app = Application.objects.create(user=self.user, opportunity=self.opportunity, status='queued')
        app.status = 'submitted'
        app.save()
        self.assertEqual(Application.objects.get(pk=app.pk).status, 'submitted')
        self.assertTrue(AuditLog.objects.filter(
            action='application_created',
            target=str(app.pk),
        ).exists())
        self.assertTrue(AuditLog.objects.filter(
            action='application_submitted',
            target=str(app.pk),
            details__from_status='queued',
        ).exists())

    def test_application_retry_transition_is_audited(self):
        app = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='failed',
        )
        app.status = 'queued'
        app.save(update_fields=['status'])

        self.assertTrue(AuditLog.objects.filter(
            action='application_retried',
            target=str(app.pk),
            details__from_status='failed',
        ).exists())

    def test_new_match_and_authorized_admin_command_are_audited(self):
        from .services.telegram_bot import _execute_audited_command
        from .tasks import _save_match

        match, created = _save_match(
            self.user,
            self.opportunity,
            {
                'score': 88,
                'eligible': True,
                'reasons': ['Skill match.'],
                'missing': [],
            },
        )
        self.assertTrue(created)
        self.assertTrue(AuditLog.objects.filter(
            action='opportunity_matched',
            target=str(match.pk),
            actor__isnull=True,
        ).exists())

        response = _execute_audited_command(
            'status',
            [],
            telegram_user_id=987654,
            chat_id=987654,
        )
        self.assertIn('Users: 1', response)
        command_event = AuditLog.objects.get(
            action='admin_command_executed',
            target='status',
        )
        self.assertEqual(command_event.details['telegram_user_id'], '987654')
        self.assertEqual(command_event.details['chat_id'], '987654')

    def test_telegram_authorization_and_audit_log(self):
        from opportunity_agent.services.telegram import TelegramNotifier
        telegram_response = Mock()
        telegram_response.json.return_value = {'ok': True}
        telegram_response.raise_for_status.return_value = None
        with patch('opportunity_agent.services.telegram.requests.post', return_value=telegram_response) as post:
            notifier = TelegramNotifier(bot_token='x')
            self.assertTrue(notifier.send_message('123', 'Hello'))
        self.assertEqual(post.call_args.kwargs['json'], {'chat_id': '123', 'text': 'Hello'})

    def test_telegram_transient_send_retries_with_exponential_backoff(self):
        from requests import ConnectionError
        from opportunity_agent.services.telegram import TelegramNotifier

        response = Mock()
        response.json.return_value = {'ok': True}
        response.raise_for_status.return_value = None
        with patch(
            'opportunity_agent.services.telegram.requests.post',
            side_effect=[ConnectionError('temporary outage'), ConnectionError('temporary outage'), response],
        ) as post, patch('opportunity_agent.services.telegram.time.sleep') as sleep:
            self.assertTrue(TelegramNotifier(bot_token='test-token').send_message('123', 'Hello'))

        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_telegram_notifier_reports_missing_configuration(self):
        from opportunity_agent.services.telegram import TelegramNotifier
        with self.assertRaisesRegex(RuntimeError, 'TELEGRAM_BOT_TOKEN'):
            TelegramNotifier(bot_token='').send_message('123', 'Hello')

    def test_telegram_destinations_route_categories_to_enabled_groups_only(self):
        from opportunity_agent.services.telegram import TelegramNotifier

        applied = TelegramDestination.objects.create(
            name='Applied',
            chat_id='-1001',
            type='applied',
        )
        TelegramDestination.objects.create(
            name='Disabled applied',
            chat_id='-1002',
            type='applied',
            enabled=False,
        )
        rejected = TelegramDestination.objects.create(
            name='Rejected',
            chat_id='-1003',
            type='rejected',
        )
        notifier = TelegramNotifier(bot_token='test-token')
        with patch.object(notifier, 'send_message', return_value=True) as send:
            results = notifier.send_to_enabled_destinations('applied', 'Application submitted')

        self.assertEqual(results, [{'id': applied.pk, 'sent': True}])
        self.assertTrue(AuditLog.objects.filter(
            action='telegram_notification_sent',
            target=str(applied.pk),
            details__type='applied',
        ).exists())
        send.assert_called_once_with(applied.chat_id, 'Application submitted')
        self.assertNotEqual(applied.chat_id, rejected.chat_id)
        with self.assertRaisesMessage(
            ValueError,
            'Telegram notifications require a configured destination type.',
        ):
            notifier.send_to_enabled_destinations('telegram_source', 'Do not send')

    def test_telegram_destination_failure_is_audited_without_stopping_later_destinations(self):
        from opportunity_agent.services.telegram import TelegramNotifier

        failed = TelegramDestination.objects.create(
            name='Unavailable',
            chat_id='-101',
            type='applied',
        )
        delivered = TelegramDestination.objects.create(
            name='Available',
            chat_id='-102',
            type='applied',
        )
        notifier = TelegramNotifier(bot_token='test-token')
        with patch.object(
            notifier,
            'send_message',
            side_effect=[RuntimeError('Telegram unavailable'), True],
        ):
            results = notifier.send_to_enabled_destinations('applied', 'Application submitted')

        self.assertEqual([result['sent'] for result in results], [False, True])
        self.assertTrue(AuditLog.objects.filter(
            action='telegram_notification_failed',
            target=str(failed.pk),
        ).exists())
        self.assertTrue(AuditLog.objects.filter(
            action='telegram_notification_sent',
            target=str(delivered.pk),
        ).exists())

    def test_application_status_change_queues_matching_telegram_notification(self):
        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='pending',
        )
        with patch(
            'opportunity_agent.tasks.send_telegram_application_status_task.delay',
        ) as enqueue, self.captureOnCommitCallbacks(execute=True):
            application.status = 'submitted'
            application.save(update_fields=['status'])

        enqueue.assert_called_once_with(application.pk, 'submitted')

    def test_application_status_telegram_messages_include_required_fields(self):
        from django.utils import timezone
        from .tasks import send_telegram_application_status_task

        cases = [
            (
                'submitted',
                'applied',
                [
                    'APPLICATION SUBMITTED',
                    'User: Alice Example',
                    'Opportunity: Senior Python Engineer (submitted)',
                    'Organization: Example Org',
                    'Match Score: 88%',
                    'Application URL: https://example.com/confirmed',
                    'Submission Time:',
                ],
            ),
            (
                'rejected',
                'rejected',
                [
                    'APPLICATION REJECTED',
                    'Opportunity: Senior Python Engineer (rejected)',
                    'Reason: Eligibility criteria not met',
                    'Match Score: 88%',
                    'Source URL: https://example.com/jobs/engineer',
                ],
            ),
            (
                'failed',
                'failed',
                [
                    'APPLICATION FAILED',
                    'Opportunity: Senior Python Engineer (failed)',
                    'Error: Provider timed out',
                    'Attempt Count: 3',
                ],
            ),
            (
                'needs_review',
                'needs_review',
                [
                    'MANUAL REVIEW REQUIRED',
                    'Opportunity: Senior Python Engineer (needs_review)',
                    'Reason: CAPTCHA detected',
                    'Required Action: Review the application manually',
                ],
            ),
        ]
        for status, destination_type, expected_parts in cases:
            with self.subTest(status=status):
                opportunity = Opportunity.objects.create(
                    source=self.source,
                    title=f'Senior Python Engineer ({status})',
                    organization='Example Org',
                    opportunity_type='job',
                    country='Germany',
                    remote_worldwide=True,
                    description='Build reliable software systems.',
                    application_url='https://example.com/apply',
                    source_url='https://example.com/jobs/engineer',
                    status='active',
                    dedupe_hash=f'telegram-status-{status}',
                )
                application = Application.objects.create(
                    user=self.user,
                    opportunity=opportunity,
                    status=status,
                    match_score=88,
                    attempts=3,
                    error_message=(
                        'Provider timed out' if status == 'failed'
                        else 'CAPTCHA detected' if status == 'needs_review'
                        else ''
                    ),
                    rejection_reason=(
                        'Eligibility criteria not met' if status == 'rejected' else ''
                    ),
                    result_url=(
                        'https://example.com/confirmed' if status == 'submitted' else ''
                    ),
                    submission_time=timezone.now() if status == 'submitted' else None,
                )
                TelegramDestination.objects.create(
                    name=f'{destination_type} group',
                    chat_id=f'-100{len(status)}',
                    type=destination_type,
                )
                with patch(
                    'opportunity_agent.services.telegram.TelegramNotifier.'
                    'send_to_enabled_destinations',
                    return_value=[{'id': 1, 'sent': True}],
                ) as send:
                    result = send_telegram_application_status_task.run(
                        application.pk,
                        status,
                    )

                self.assertEqual(result['type'], destination_type)
                notified_message = send.call_args.args[1]
                for expected in expected_parts:
                    self.assertIn(expected, notified_message)

    @patch('opportunity_agent.tasks.requests.get')
    @patch('opportunity_agent.tasks.socket.getaddrinfo')
    def test_source_scan_checks_url_and_records_success(self, getaddrinfo, get):
        from .tasks import scan_sources_task
        getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
        ]
        response = Mock()
        response.raise_for_status.return_value = None
        get.return_value = response
        source = Source.objects.get(pk=self.source.pk)
        source.status = 'pending'
        source.save(update_fields=['status', 'updated_at'])

        result = scan_sources_task.run(limit=1)

        source.refresh_from_db()
        self.assertEqual(result['source_ids'], [source.pk])
        self.assertEqual(result['scanned'], 1)
        self.assertEqual(result['successful'], 1)
        self.assertEqual(source.status, 'active')
        self.assertIsNotNone(source.last_successful_scan)
        get.assert_called_once_with(
            source.url,
            headers={'User-Agent': 'OpportunityHubSourceMonitor/1.0'},
            timeout=15,
            allow_redirects=False,
        )

    @patch('opportunity_agent.tasks.requests.get')
    def test_source_scan_rejects_local_urls(self, get):
        from .tasks import scan_sources_task
        source = Source.objects.get(pk=self.source.pk)
        source.url = 'http://127.0.0.1:8000/internal'
        source.save(update_fields=['url', 'updated_at'])

        result = scan_sources_task.run(limit=1)

        source.refresh_from_db()
        self.assertEqual(result['scanned'], 1)
        self.assertEqual(result['successful'], 0)
        self.assertEqual(source.status, 'error')
        self.assertTrue(AuditLog.objects.filter(
            action='source_scan_failed',
            target=str(source.pk),
        ).exists())
        get.assert_not_called()

    @patch('opportunity_agent.tasks.requests.get')
    @patch('opportunity_agent.tasks.socket.getaddrinfo')
    def test_source_scan_continues_after_an_individual_source_failure(self, getaddrinfo, get):
        from .tasks import scan_sources_task

        getaddrinfo.side_effect = lambda host, port: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                '',
                ('127.0.0.1' if host == '127.0.0.1' else '93.184.216.34', port),
            ),
        ]
        response = Mock()
        response.raise_for_status.return_value = None
        response.url = 'https://healthy.example.com'
        response.text = ''
        response.headers = {'content-type': 'text/html'}
        get.return_value = response

        failed_source = Source.objects.get(pk=self.source.pk)
        failed_source.url = 'http://127.0.0.1:8000/internal'
        failed_source.save(update_fields=['url', 'updated_at'])
        healthy_source = Source.objects.create(
            name='Healthy source',
            url='https://healthy.example.com',
            scan_frequency='manual',
        )

        result = scan_sources_task.run(
            limit=2,
            source_ids=[failed_source.pk, healthy_source.pk],
        )

        failed_source.refresh_from_db()
        healthy_source.refresh_from_db()
        self.assertEqual(result['successful'], 1)
        self.assertEqual([item['source_id'] for item in result['errors']], [failed_source.pk])
        self.assertEqual(failed_source.status, 'error')
        self.assertEqual(healthy_source.status, 'active')

    @patch('opportunity_agent.services.source_discovery._fetch_public_page')
    @patch('opportunity_agent.services.source_discovery.socket.getaddrinfo')
    def test_discovery_verifies_public_relevant_pages_and_classifies_them(self, getaddrinfo, fetch):
        from .services.source_discovery import _inspect_candidate

        getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
        ]
        fetch.return_value = (
            'https://example.edu/jobs',
            '<html><title>University Careers</title><body>'
            '<p>Open jobs and internships are available for research students.</p>'
            + ('More information about careers and fellowships. ' * 12)
            + '</body></html>',
        )

        result = _inspect_candidate('University', 'https://example.edu/jobs')

        self.assertIsNotNone(result)
        self.assertEqual(result['source_type'], 'university')
        self.assertEqual(result['opportunity_types'], ['job', 'internship', 'fellowship', 'grant', 'research'])
        self.assertGreaterEqual(result['trust_score'], 0.7)
        self.assertTrue(result['auto_discovered'])
        fetch.assert_called_once()

    @patch('opportunity_agent.services.source_discovery._fetch_public_page')
    @patch('opportunity_agent.services.source_discovery.socket.getaddrinfo')
    def test_discovery_rejects_access_barriers_and_irrelevant_pages(self, getaddrinfo, fetch):
        from .services.source_discovery import _inspect_candidate

        getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
        ]
        fetch.return_value = (
            'https://example.com/',
            '<html><title>Careers</title><body>Verify you are human to continue '
            + ('jobs and careers are listed here. ' * 20)
            + '</body></html>',
        )

        self.assertIsNone(_inspect_candidate('Careers', 'https://example.com/'))
        fetch.return_value = (
            'https://example.com/',
            '<html><title>Welcome</title><body>' + ('Unrelated news article. ' * 20) + '</body></html>',
        )
        self.assertIsNone(_inspect_candidate('Welcome', 'https://example.com/'))

    @patch('opportunity_agent.tasks.expire_deadlines_task.run', return_value={'expired': 0})
    @patch('opportunity_agent.tasks.execute_application_queue_task.run', return_value={'submitted': 0})
    @patch('opportunity_agent.tasks.process_application_queue_task.run', return_value={'processed': 0})
    @patch('opportunity_agent.tasks.refresh_matches_task.run', return_value={'matches_refreshed': 0})
    @patch('opportunity_agent.tasks.scan_sources_task.run', side_effect=RuntimeError('source scan failed'))
    def test_automation_cycle_continues_after_source_scan_stage_failure(
        self, scan, matches, queue, execution, expiration,
    ):
        from .tasks import automation_cycle_task

        result = automation_cycle_task.run()

        self.assertEqual(result['scan']['error'], 'source scan failed')
        self.assertEqual(result['stage_errors'][0]['stage'], 'scan')
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(matches.call_count, 1)
        self.assertEqual(queue.call_count, 1)
        self.assertEqual(execution.call_count, 1)
        self.assertEqual(expiration.call_count, 1)

    def test_application_queue_calculates_match_before_preparing(self):
        from .tasks import process_application_queue_task
        application = Application.objects.create(user=self.user, opportunity=self.opportunity, status='queued')

        result = process_application_queue_task.run()

        application.refresh_from_db()
        self.assertEqual(result['application_ids'], [application.pk])
        self.assertEqual(application.status, 'prepared')
        self.assertGreaterEqual(application.match_score, self.profile.minimum_ai_match_score)
        self.assertEqual(
            [entry['status'] for entry in application.audit_history[-2:]],
            ['matching', 'prepared'],
        )

    def test_unverified_provider_adapter_is_never_allowed_to_submit(self):
        from .services.provider_adapters import PlaywrightConfiguredAdapter

        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='prepared',
        )
        adapter = PlaywrightConfiguredAdapter(
            config={
                'allowed_domains': ['example.com'],
                'allow_submit': True,
                'verified': False,
            },
        )

        result = adapter.submit(application)

        application.refresh_from_db()
        self.assertFalse(result)
        self.assertEqual(application.status, 'needs_review')
        self.assertIn('not been verified', application.error_message)

    def test_application_navigation_intent_recognizes_dynamic_page_actions(self):
        from .services.provider_adapters import _action_intent, _state_url

        self.assertEqual(_action_intent('Save and Continue'), 'continue')
        self.assertEqual(_action_intent('Application Instructions'), 'instructions')
        self.assertEqual(_action_intent('Create Account'), 'authentication')
        self.assertEqual(_action_intent('Apply Now'), 'apply')
        self.assertEqual(_action_intent('Submit Application'), 'submit')
        self.assertEqual(
            _state_url('https://example.com/verify/secret?token=abc&step=2'),
            'https://example.com/verify/redacted?step=2',
        )

    def test_dynamic_workflow_follows_multiple_pages_and_requires_confirmation(self):
        from .services.provider_adapters import PlaywrightConfiguredAdapter

        states = [
            {
                'url': 'https://example.com/apply',
                'text': 'Continue to your application',
                'fields': 0,
                'buttons': ['Continue'],
            },
            {
                'url': 'https://example.com/apply/details',
                'text': 'Application details',
                'fields': 1,
                'buttons': ['Submit Application'],
            },
            {
                'url': 'https://example.com/apply/confirmation',
                'text': 'Your application was successfully submitted.',
                'fields': 0,
                'buttons': [],
            },
        ]

        class FakeLocator:
            def __init__(self, page, selector='', label=''):
                self.page = page
                self.selector = selector
                self.label = label
                self.first = self

            def all(self):
                return []

            def count(self):
                if self.selector == 'input[type=password]':
                    return 0
                if self.selector == 'input[type=file]':
                    return 0
                if 'input:not([type=hidden])' in self.selector:
                    return states[self.page.index]['fields']
                if self.selector.startswith('input:required'):
                    return 0
                return 0

            def is_visible(self):
                return True

            def is_enabled(self):
                return True

            def inner_text(self, **kwargs):
                return self.label

            def get_attribute(self, name):
                return None

            def evaluate(self, script, *args):
                return ''

            def click(self, **kwargs):
                if self.label:
                    self.page.index += 1

        class FakeRoleCollection:
            def __init__(self, locators):
                self.locators = locators

            def all(self):
                return self.locators

        class FakePage:
            def __init__(self):
                self.index = 0
                self.url = states[0]['url']

            def locator(self, selector):
                if selector == 'body':
                    return FakeBodyLocator(self)
                return FakeLocator(self, selector)

            def get_by_role(self, role):
                labels = states[self.index]['buttons'] if role == 'button' else []
                return FakeRoleCollection([
                    FakeLocator(self, label=label) for label in labels
                ])

            def set_default_timeout(self, timeout):
                return None

            def goto(self, url, **kwargs):
                self.url = url

            def wait_for_timeout(self, timeout):
                self.url = states[self.index]['url']

            def wait_for_load_state(self, *args, **kwargs):
                return None

            def title(self):
                return states[self.index]['url'].rsplit('/', 1)[-1]

        class FakeBodyLocator(FakeLocator):
            def inner_text(self, **kwargs):
                return states[self.page.index]['text']

        class FakeContext:
            def __init__(self):
                self.page = FakePage()
                self.pages = [self.page]

            def route(self, *args):
                return None

            def new_page(self):
                return self.page

        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='pending',
        )
        context = FakeContext()
        adapter = PlaywrightConfiguredAdapter(
            config={'confirmation_words': ['successfully submitted']},
        )

        result = adapter._run_dynamic_workflow(application, context, None)

        application.refresh_from_db()
        self.assertTrue(result)
        self.assertEqual(application.status, 'submitted')
        self.assertEqual(
            application.workflow_state['outcome'],
            'submitted',
        )
        self.assertIn('Continue', application.workflow_state['completed_actions'])
        self.assertIsNotNone(application.submission_time)
        self.assertEqual(application.result_url, states[2]['url'])

    def test_unsupported_provider_is_sent_to_review_with_reason(self):
        from .services.provider_adapters import ManualReviewAdapter

        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='pending',
        )

        result = ManualReviewAdapter().submit(application)

        application.refresh_from_db()
        self.assertFalse(result)
        self.assertEqual(application.status, 'needs_review')
        self.assertIn('Unsupported application provider', application.error_message)

    def test_automatic_application_requires_confirmed_result_fields(self):
        from .tasks import execute_application_queue_task

        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='prepared',
        )

        def falsely_confirmed_submit(app):
            app.status = 'submitted'
            app.save(update_fields=['status'])
            return True

        adapter = Mock()
        adapter.submit.side_effect = falsely_confirmed_submit
        with patch('opportunity_agent.tasks.ApplicationGuard.can_submit', return_value=(True, 'Ready')), \
             patch('opportunity_agent.tasks.adapter_for', return_value=adapter):
            result = execute_application_queue_task.run(limit=1)

        application.refresh_from_db()
        attempt = ApplicationAttempt.objects.get(application=application)
        self.assertEqual(result['submitted'], 0)
        self.assertEqual(application.status, 'needs_review')
        self.assertIsNone(application.submission_time)
        self.assertFalse(application.result_url)
        self.assertIn('could not be verified', application.error_message)
        self.assertEqual(attempt.status, 'needs_review')

    def test_automatic_application_records_success_only_with_confirmation_data(self):
        from django.utils import timezone
        from .tasks import execute_application_queue_task

        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='prepared',
        )

        def confirmed_submit(app):
            app.status = 'submitted'
            app.result_url = 'https://example.com/applications/confirmation/123'
            app.submission_time = timezone.now()
            app.save(update_fields=['status', 'result_url', 'submission_time'])
            return True

        adapter = Mock()
        adapter.submit.side_effect = confirmed_submit
        with patch('opportunity_agent.tasks.ApplicationGuard.can_submit', return_value=(True, 'Ready')), \
             patch('opportunity_agent.tasks.adapter_for', return_value=adapter):
            result = execute_application_queue_task.run(limit=1)

        application.refresh_from_db()
        attempt = ApplicationAttempt.objects.get(application=application)
        self.assertEqual(result['submitted'], 1)
        self.assertEqual(application.status, 'submitted')
        self.assertTrue(application.result_url)
        self.assertIsNotNone(application.submission_time)
        self.assertEqual(attempt.status, 'submitted')

    def test_provider_exception_after_attempt_is_sent_to_manual_review(self):
        from .tasks import execute_application_queue_task

        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='prepared',
        )
        adapter = Mock()
        adapter.submit.side_effect = RuntimeError('connection dropped')

        with patch(
            'opportunity_agent.tasks.ApplicationGuard.can_submit',
            return_value=(True, 'Ready'),
        ), patch('opportunity_agent.tasks.adapter_for', return_value=adapter):
            result = execute_application_queue_task.run(limit=1)

        application.refresh_from_db()
        attempt = ApplicationAttempt.objects.get(application=application)
        self.assertEqual(result['submitted'], 0)
        self.assertEqual(application.status, 'needs_review')
        self.assertIn('outcome may be uncertain', application.error_message)
        self.assertEqual(attempt.status, 'needs_review')

    def test_expiration_only_changes_open_opportunities(self):
        from django.utils import timezone
        from datetime import timedelta
        from .tasks import expire_deadlines_task
        self.opportunity.deadline = timezone.now() - timedelta(days=1)
        self.opportunity.save(update_fields=['deadline', 'updated_at'])
        inactive = Opportunity.objects.create(
            source=self.source,
            title='Already rejected',
            status='rejected',
            deadline=timezone.now() - timedelta(days=1),
            dedupe_hash='already-rejected-opportunity',
        )
        legacy_expired = Opportunity.objects.create(
            source=self.source,
            title='Legacy expired status',
            status='expired',
            deadline=timezone.now() - timedelta(days=2),
            dedupe_hash='legacy-expired-status-opportunity',
        )

        result = expire_deadlines_task.run()

        self.opportunity.refresh_from_db()
        inactive.refresh_from_db()
        legacy_expired.refresh_from_db()
        self.assertEqual(result['expired'], 2)
        self.assertEqual(self.opportunity.status, 'inactive')
        self.assertEqual(legacy_expired.status, 'inactive')
        self.assertEqual(inactive.status, 'rejected')

    def test_task_limit_is_validated(self):
        from .tasks import scan_sources_task
        with self.assertRaises(ValueError):
            scan_sources_task.run(limit=0)

    def test_retry_helper_uses_exponential_backoff_for_transient_requests(self):
        from requests import Timeout
        from .services.retries import request_with_exponential_backoff

        operation = Mock(side_effect=[Timeout('first'), Timeout('second'), 'success'])
        with patch('opportunity_agent.services.retries.time.sleep') as sleep:
            result = request_with_exponential_backoff(
                operation,
                description='test source',
            )

        self.assertEqual(result, 'success')
        self.assertEqual(operation.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_application_preparation_isolated_from_other_queued_applications(self):
        from .tasks import process_application_queue_task

        first_opportunity = Opportunity.objects.create(
            title='First isolated application',
            dedupe_hash='application-isolation-first',
        )
        second_opportunity = Opportunity.objects.create(
            title='Second isolated application',
            dedupe_hash='application-isolation-second',
        )
        first = Application.objects.create(
            user=self.user,
            opportunity=first_opportunity,
            status='queued',
        )
        second = Application.objects.create(
            user=self.user,
            opportunity=second_opportunity,
            status='queued',
            cover_letter='Prepared cover letter.',
        )
        valid_match = {
            'score': 90,
            'eligible': True,
            'manual_override_allowed': False,
            'reasons': ['Relevant skills.'],
            'missing': [],
            'risks': [],
            'recommended_action': 'review_and_apply',
        }
        with patch(
            'opportunity_agent.tasks.compute_match_score',
            side_effect=[RuntimeError('one application failed'), valid_match],
        ):
            result = process_application_queue_task.run(limit=2)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, 'failed')
        self.assertIn('one application failed', first.error_message)
        self.assertEqual(second.status, 'prepared')
        self.assertEqual(result['application_ids'], [second.pk])

    def test_application_execution_isolated_from_other_prepared_applications(self):
        from .tasks import execute_application_queue_task

        first = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='prepared',
        )
        second_opportunity = Opportunity.objects.create(
            title='Second execution application',
            dedupe_hash='application-execution-isolation-second',
        )
        second = Application.objects.create(
            user=self.user,
            opportunity=second_opportunity,
            status='prepared',
        )
        with patch(
            'opportunity_agent.tasks._execute_one_application',
            side_effect=[RuntimeError('isolated execution error'), False],
        ):
            result = execute_application_queue_task.run(limit=2)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(result['submitted'], 0)
        self.assertEqual(first.status, 'failed')
        self.assertIn('isolated execution error', first.error_message)
        self.assertTrue(AuditLog.objects.filter(
            action='application_failed',
            target=str(first.pk),
        ).exists())
        self.assertEqual(second.status, 'prepared')

    def test_retry_task_queues_only_matching_failed_applications(self):
        from .tasks import retry_applications_task

        failed = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='failed',
            error_message='Temporary provider error.',
        )
        rejected_opportunity = Opportunity.objects.create(
            title='Rejected opportunity for retry task',
            dedupe_hash='retry-task-rejected-opportunity',
        )
        rejected = Application.objects.create(
            user=self.user,
            opportunity=rejected_opportunity,
            status='rejected',
        )
        with patch(
            'opportunity_agent.tasks.process_application_queue_task.delay',
        ) as process, self.captureOnCommitCallbacks(execute=True):
            result = retry_applications_task.run(
                [failed.pk, rejected.pk],
                status='failed',
                actor_id=self.user.pk,
            )

        failed.refresh_from_db()
        rejected.refresh_from_db()
        self.assertEqual(result, {'queued': 1, 'application_ids': [failed.pk]})
        self.assertEqual(failed.status, 'queued')
        self.assertEqual(failed.error_message, '')
        self.assertEqual(rejected.status, 'rejected')
        process.assert_called_once_with(1)
        self.assertTrue(AuditLog.objects.filter(
            actor=self.user,
            action='application_retry_queued',
            target=str(failed.pk),
        ).exists())

    def test_celery_beat_schedules_background_automation(self):
        from django.conf import settings

        schedule = settings.CELERY_BEAT_SCHEDULE
        scheduled_tasks = {entry['task'] for entry in schedule.values()}
        self.assertIn('opportunity_agent.tasks.automation_cycle_task', scheduled_tasks)
        self.assertIn('opportunity_agent.tasks.discover_sources_task', scheduled_tasks)
        self.assertIn('opportunity_agent.tasks.expire_deadlines_task', scheduled_tasks)
        self.assertIn('opportunity_agent.tasks.process_application_queue_task', scheduled_tasks)
        self.assertIn('opportunity_agent.tasks.health_check_task', scheduled_tasks)

    def test_user_dashboard_renders_application_and_opportunity_metrics(self):
        from datetime import timedelta
        from django.utils import timezone

        Application.objects.create(user=self.user, opportunity=self.opportunity, status='queued')
        deadline_opportunity = Opportunity.objects.create(
            title='Deadline opportunity',
            deadline=timezone.now() + timedelta(days=3),
            application_url='https://example.com/deadline',
            dedupe_hash='user-dashboard-deadline',
        )
        Match.objects.create(
            user=self.user,
            opportunity=deadline_opportunity,
            score=91,
            eligible=True,
            reasons=['Skills align'],
        )
        self.client.force_login(self.user)

        response = self.client.get('/dashboard/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['application_count'], 1)
        self.assertEqual(response.context['in_progress_count'], 1)
        self.assertEqual(response.context['active_opportunity_count'], 2)
        self.assertEqual(response.context['profile'], self.profile)
        self.assertEqual(response.context['needs_review_count'], 0)
        self.assertEqual(response.context['daily_count'], 0)
        self.assertEqual(list(response.context['matches']), [Match.objects.get(
            user=self.user,
            opportunity=deadline_opportunity,
        )])
        self.assertEqual(
            list(response.context['upcoming_deadlines']),
            [deadline_opportunity],
        )
        self.assertEqual(response.context['applied_opportunities'].count(), 0)
        self.assertContains(response, 'Recent applications')
        self.assertContains(response, 'Recommended opportunities')
        self.assertContains(response, 'Upcoming deadlines')
        self.assertContains(response, 'Applied opportunities')
        self.assertContains(response, 'Rejected applications')
        self.assertContains(response, 'Failed applications')
        self.assertContains(response, 'Needs review')
        self.assertContains(response, 'Auto Apply:')
        self.assertContains(response, 'Daily application count')

    def test_user_dashboard_status_lists_and_daily_attempt_count_are_private(self):
        from django.utils import timezone

        status_opportunities = {}
        for status in ('submitted', 'rejected', 'failed', 'needs_review'):
            status_opportunities[status] = Opportunity.objects.create(
                title=f'Private {status} opportunity',
                application_url='https://example.com/apply',
                dedupe_hash=f'private-dashboard-{status}',
            )
        submitted = Application.objects.create(
            user=self.user,
            opportunity=status_opportunities['submitted'],
            status='submitted',
            submission_time=timezone.now(),
        )
        Application.objects.create(
            user=self.user,
            opportunity=status_opportunities['rejected'],
            status='rejected',
            rejection_reason='Eligibility criteria not met.',
        )
        failed = Application.objects.create(
            user=self.user,
            opportunity=status_opportunities['failed'],
            status='failed',
            error_message='Provider failed.',
            attempts=2,
        )
        review = Application.objects.create(
            user=self.user,
            opportunity=status_opportunities['needs_review'],
            status='needs_review',
            error_message='Manual review required.',
        )
        ApplicationAttempt.objects.create(
            application=failed,
            attempt_number=1,
            status='failed',
        )

        other_user = User.objects.create_user(
            username='private-other',
            email='private-other@example.com',
            password='StrongPass123!',
        )
        other_opportunity = Opportunity.objects.create(
            title='Other user private opportunity',
            application_url='https://example.com/other',
            dedupe_hash='other-user-dashboard-private',
        )
        Application.objects.create(
            user=other_user,
            opportunity=other_opportunity,
            status='submitted',
            submission_time=timezone.now(),
        )

        self.client.force_login(self.user)
        response = self.client.get('/dashboard/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['daily_count'], 2)
        self.assertEqual(response.context['submitted_count'], 1)
        self.assertEqual(response.context['rejected_count'], 1)
        self.assertEqual(response.context['failed_count'], 1)
        self.assertEqual(response.context['needs_review_count'], 1)
        self.assertEqual(list(response.context['applied_opportunities']), [submitted])
        self.assertEqual(
            list(response.context['rejected_applications'].values_list('pk', flat=True)),
            [Application.objects.get(
                user=self.user,
                status='rejected',
            ).pk],
        )
        self.assertEqual(list(response.context['failed_applications']), [failed])
        self.assertEqual(list(response.context['review_applications']), [review])
        self.assertContains(response, 'Private rejected opportunity')
        self.assertContains(response, 'Private failed opportunity')
        self.assertContains(response, 'Private needs_review opportunity')
        self.assertNotContains(response, 'Other user private opportunity')

    def test_admin_index_shows_operational_dashboard_metrics(self):
        from django.test import RequestFactory
        from django.utils import timezone
        from .context_processors import admin_metrics

        today = timezone.localdate()
        self.source.status = 'error'
        self.source.save(update_fields=['status', 'updated_at'])
        TelegramSource.objects.create(
            name='Healthy Telegram source',
            channel_url='https://t.me/public_opportunities',
            enabled=True,
            status='active',
        )
        AutomationRun.objects.create(status='success')

        statuses = ('submitted', 'rejected', 'failed', 'needs_review')
        for status in statuses:
            opportunity = Opportunity.objects.create(
                title=f'Dashboard {status}',
                application_url='https://example.com/apply',
                source_url='https://example.com/jobs',
                dedupe_hash=f'dashboard-{status}',
            )
            Application.objects.create(
                user=self.user,
                opportunity=opportunity,
                status=status,
            )

        request = RequestFactory().get('/admin/')
        request.user = self.user
        request.user.is_staff = True
        request.resolver_match = SimpleNamespace(url_name='index')
        metrics = admin_metrics(request)['admin_metrics']

        self.assertEqual(metrics['users'], User.objects.count())
        self.assertGreaterEqual(metrics['active_users'], 1)
        self.assertEqual(metrics['sources'], 2)
        self.assertEqual(metrics['active_sources'], 1)
        self.assertEqual(metrics['opportunities_today'], 5)
        self.assertEqual(metrics['opportunities_week'], 5)
        self.assertEqual(metrics['applications_today'], 4)
        self.assertEqual(metrics['submitted'], 1)
        self.assertEqual(metrics['rejected'], 1)
        self.assertEqual(metrics['failed'], 1)
        self.assertEqual(metrics['needs_review'], 1)
        self.assertEqual(metrics['automation_status'], 'Success')
        self.assertIsNotNone(metrics['last_successful_run'])
        self.assertEqual(metrics['source_errors'], 1)

        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save(update_fields=['is_staff', 'is_superuser'])
        self.client.force_login(self.user)
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Opportunities today')
        self.assertContains(response, 'Last successful cycle')
        self.assertContains(response, 'Source errors')

    def test_admin_metrics_are_not_added_outside_admin_index(self):
        from django.test import RequestFactory
        from .context_processors import admin_metrics

        request = RequestFactory().get('/admin/opportunity_agent/application/')
        request.user = self.user
        request.user.is_staff = True
        request.resolver_match = SimpleNamespace(url_name='opportunity_agent_application_changelist')
        self.assertEqual(admin_metrics(request), {})

    def test_admin_dashboard_is_available_to_admin_group(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.user.groups.add(self.user.groups.model.objects.get(name='ADMIN'))
        self.client.force_login(self.user)

        response = self.client.get('/admin-dashboard/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['user_count'], 1)
        self.assertContains(response, 'Quick management')


class AIClientTestCase(TestCase):
    @patch('opportunity_agent.services.ai_engine.AIClient._request')
    def test_opportunity_extraction_processes_long_text_in_chunks(self, request):
        first_requirement = 'At least five years of field work required.'
        final_requirement = 'Applicants must hold a doctorate in ecology.'

        def extract_chunk(task, schema, instruction, **inputs):
            text = inputs['input']
            requirement = next(
                (
                    value
                    for value in (first_requirement, final_requirement)
                    if value in text
                ),
                '',
            )
            return {
                **schema,
                'requirements': requirement,
                'evidence': {'requirements': requirement} if requirement else {},
            }

        request.side_effect = extract_chunk
        text = (
            first_requirement
            + '\n'
            + ('General opportunity information. ' * 1800)
            + '\n'
            + final_requirement
        )

        result = AIClient().extract_opportunity(text)

        self.assertGreater(request.call_count, 1)
        self.assertIn(first_requirement, result['requirements'])
        self.assertIn(final_requirement, result['requirements'])
        self.assertGreater(
            sum(len(call.kwargs['input']) for call in request.call_args_list),
            len(text),
        )

    @patch('opportunity_agent.services.ai_engine.AIClient._request')
    def test_requirement_extraction_processes_long_text_in_chunks(self, request):
        first_requirement = 'Applicants must be citizens of Kenya.'
        final_requirement = 'Applicants must have completed a medical degree.'

        def extract_chunk(task, schema, instruction, **inputs):
            text = inputs['input']
            quote = next(
                (
                    value
                    for value in (first_requirement, final_requirement)
                    if value in text
                ),
                '',
            )
            return {
                'requirements': (
                    [{'text': quote, 'evidence': quote}] if quote else []
                ),
            }

        request.side_effect = extract_chunk
        text = (
            first_requirement
            + '\n'
            + ('Program information. ' * 1800)
            + '\n'
            + final_requirement
        )

        result = AIClient().extract_requirements(text)

        self.assertGreater(request.call_count, 1)
        self.assertIn(first_requirement, result['requirements'])
        self.assertIn(final_requirement, result['requirements'])

    @patch('opportunity_agent.services.ai_engine.AIClient._post')
    def test_opportunity_ai_extract_requires_matching_source_evidence(self, post):
        post.return_value = {
            'title': 'Invented role',
            'organization': 'Invented Organization',
            'country': 'Invented Country',
            'application_url': 'https://invented.example/apply',
            'remote_worldwide': True,
            'visa_sponsorship': False,
            'evidence': {},
        }

        result = AIClient().extract_opportunity(
            'A public fellowship in Nairobi. Visa sponsorship is available.',
            'https://example.org/fellowship',
            'Not an organization name',
        )

        self.assertEqual(result, {'source_url': 'https://example.org/fellowship'})

    @patch('opportunity_agent.services.ai_engine.AIClient._post')
    def test_opportunity_ai_extract_keeps_only_quoted_supported_facts(self, post):
        post.return_value = {
            'title': 'Research Fellowship',
            'organization': 'Example Institute',
            'country': 'Nairobi',
            'application_url': 'https://example.org/apply',
            'remote_worldwide': True,
            'visa_sponsorship': None,
            'evidence': {
                'title': 'Research Fellowship',
                'organization': 'Example Institute',
                'country': 'Nairobi',
                'application_url': 'Apply at https://example.org/apply',
                'remote_worldwide': 'Remote worldwide applicants are welcome.',
            },
        }

        result = AIClient().extract_opportunity(
            'Research Fellowship at Example Institute in Nairobi. '
            'Remote worldwide applicants are welcome. Apply at https://example.org/apply.',
            'https://example.org/fellowship',
        )

        self.assertEqual(result['organization'], 'Example Institute')
        self.assertEqual(result['country'], 'Nairobi')
        self.assertEqual(result['application_url'], 'https://example.org/apply')
        self.assertTrue(result['remote_worldwide'])
        self.assertNotIn('visa_sponsorship', result)
        self.assertEqual(result['source_url'], 'https://example.org/fellowship')

    @patch('opportunity_agent.services.ai_engine.AIClient._post')
    def test_opportunity_ai_contact_destinations_must_match_their_source_text(self, post):
        post.return_value = {
            'contact_email': 'invented@example.org',
            'contact_phone': '+1 555 555 0100',
            'telegram_contact': 'https://t.me/invented_contact',
            'physical_address': '99 Imaginary Road',
            'organization_website': 'https://invented.example.org',
            'evidence': {
                'contact_email': 'Contact the fellowship office for details.',
                'contact_phone': 'Contact the fellowship office for details.',
                'telegram_contact': 'Contact the fellowship office for details.',
                'physical_address': 'Contact the fellowship office for details.',
                'organization_website': 'Contact the fellowship office for details.',
            },
        }

        result = AIClient().extract_opportunity(
            'Contact the fellowship office for details.',
            'https://example.org/fellowship',
        )

        for field in (
            'contact_email',
            'contact_phone',
            'telegram_contact',
            'physical_address',
            'organization_website',
        ):
            with self.subTest(field=field):
                self.assertNotIn(field, result)

    @patch('opportunity_agent.services.ai_engine.AIClient._post')
    def test_opportunity_ai_keeps_verbatim_contact_destinations(self, post):
        quote = (
            'Email jobs@example.org or call +1 212 555 1234. '
            'Telegram: https://t.me/example_jobs. Address: 1 Main Road. '
            'Website: https://example.org.'
        )
        post.return_value = {
            'contact_email': 'jobs@example.org',
            'contact_phone': '+1 212 555 1234',
            'telegram_contact': 'https://t.me/example_jobs',
            'physical_address': '1 Main Road',
            'organization_website': 'https://example.org',
            'evidence': {
                'contact_email': quote,
                'contact_phone': quote,
                'telegram_contact': quote,
                'physical_address': quote,
                'organization_website': quote,
            },
        }

        result = AIClient().extract_opportunity(quote)

        self.assertEqual(result['contact_email'], 'jobs@example.org')
        self.assertEqual(result['contact_phone'], '+1 212 555 1234')
        self.assertEqual(result['telegram_contact'], 'https://t.me/example_jobs')
        self.assertEqual(result['physical_address'], '1 Main Road')
        self.assertEqual(result['organization_website'], 'https://example.org')

    @patch('opportunity_agent.services.ai_engine.AIClient._post')
    def test_classification_requires_evidence_and_known_type(self, post):
        post.return_value = {
            'is_opportunity': True,
            'opportunity_type': 'job',
            'confidence': 0.92,
            'evidence': {
                'is_opportunity': 'Applications are open for a research job.',
                'opportunity_type': 'This is a job.',
            },
        }
        result = AIClient().classify_opportunity(
            'Applications are open for a research job.'
        )
        self.assertTrue(result['is_opportunity'])
        self.assertEqual(result['opportunity_type'], '')
        self.assertEqual(result['confidence'], 0.92)

    @patch('opportunity_agent.services.ai_engine.AIClient._post')
    def test_requirement_extraction_keeps_only_verbatim_evidence(self, post):
        post.return_value = {
            'requirements': [
                {'text': 'Two years experience', 'evidence': 'At least two years of experience are required.'},
                {'text': 'Must relocate', 'evidence': 'Applicants should have leadership skills.'},
            ],
            'education_requirements': [
                {'text': 'Master degree', 'evidence': 'A master degree is required.'},
            ],
            'experience_requirements': [],
            'skills': [],
            'languages': [],
        }
        result = AIClient().extract_requirements(
            'At least two years of experience are required. A master degree is required.'
        )
        self.assertEqual(result['requirements'], ['Two years experience'])
        self.assertEqual(result['education_requirements'], ['Master degree'])

    @patch('opportunity_agent.services.ai_engine.AIClient._post')
    def test_ai_match_only_returns_explanations_supported_by_both_sides(self, post):
        post.return_value = {
            'strengths': [
                {
                    'text': 'Python skill matches the role.',
                    'profile_evidence': 'Python',
                    'opportunity_evidence': 'Python',
                },
                {
                    'text': 'Expert cloud architect.',
                    'profile_evidence': 'Expert cloud architect',
                    'opportunity_evidence': 'Cloud architecture preferred.',
                },
            ],
            'gaps': [],
        }
        result = AIClient().match_user_to_opportunity(
            {'skills': ['Python'], 'minimum_ai_match_score': 0},
            {'skills': ['Python'], 'opportunity_type': 'job'},
        )
        self.assertEqual(result['strengths'], ['Python skill matches the role.'])
        self.assertIn('score', result)

    @patch('opportunity_agent.services.ai_engine.AIClient._post')
    def test_rejection_reasoning_can_select_only_supplied_reasons(self, post):
        post.return_value = {'reason_indices': [1, 999, 1], 'summary': 'Invented explanation.'}
        result = AIClient().explain_rejection(
            {'skills': ['Python']},
            {'requirements': 'Five years of Java'},
            {'missing': ['Java skill not listed'], 'risks': ['Experience not specified']},
        )
        self.assertEqual(result['reasons'], ['Experience not specified'])
        self.assertEqual(result['summary'], 'Experience not specified')

    @patch('opportunity_agent.services.ai_engine.AIClient._post')
    def test_application_document_uses_only_profile_and_opportunity_facts(self, post):
        post.return_value = {
            'profile_quotes': ['Python, Django'],
            'opportunity_quotes': ['Python and Django experience is required.'],
        }
        document = AIClient().generate_application_document(
            {
                'full_name': 'Sam Example',
                'skills': ['Python, Django'],
                'degree': '',
            },
            {
                'title': 'Backend Engineer',
                'organization': 'Example Org',
                'requirements': 'Python and Django experience is required.',
            },
            'cover_letter',
        )
        self.assertIn('Sam Example', document['content'])
        self.assertIn('“Python, Django”', document['content'])
        self.assertIn('“Python and Django experience is required.”', document['content'])
        self.assertNotIn('PhD', document['content'])

    @patch('opportunity_agent.services.ai_engine.AIClient._post')
    def test_ranking_uses_only_ids_from_input(self, post):
        post.return_value = {'ranked_ids': [2, 1]}
        ranked = AIClient().rank_opportunities(
            {'skills': ['Python'], 'minimum_ai_match_score': 0},
            [
                {'id': 1, 'skills': ['Python'], 'opportunity_type': 'job'},
                {'id': 2, 'skills': ['Python'], 'opportunity_type': 'job'},
            ],
        )
        self.assertEqual([entry['id'] for entry in ranked], [2, 1])
        self.assertEqual([entry['score'] for entry in ranked], [43, 43])

    @patch.dict('os.environ', {
        'AI_API_KEY': '',
        'AI_GOOGLE_API_KEY': 'google-key',
        'AI_GROQ_API_KEY': 'groq-key',
        'AI_OPENROUTER_API_KEY': '',
        'AI_MISTRAL_API_KEY': '',
        'AI_TOGETHER_API_KEY': '',
        'AI_HUGGINGFACE_API_KEY': '',
    }, clear=False)
    @patch('opportunity_agent.services.ai_engine.requests.post')
    def test_uses_next_provider_when_first_provider_fails(self, post):
        failure = Mock()
        failure.raise_for_status.side_effect = requests.HTTPError('Unavailable')
        success = Mock()
        success.json.return_value = {'choices': [{'message': {'content': json.dumps({
            'is_opportunity': True,
            'opportunity_type': 'job',
            'confidence': 0.9,
            'evidence': {
                'is_opportunity': 'Python role',
                'opportunity_type': 'Python role',
            },
        })}}]}
        success.raise_for_status.return_value = None
        post.side_effect = [failure, success]

        result = AIClient().classify_opportunity('Python role')

        self.assertTrue(result['is_opportunity'])
        self.assertEqual(result['opportunity_type'], 'job')
        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            post.call_args_list[0].args[0],
            'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
        )
        self.assertEqual(
            post.call_args_list[1].kwargs['headers']['Authorization'],
            'Bearer ' + 'groq' + '-key',
        )

    @patch.dict('os.environ', {
        'AI_API_KEY': '',
        'AI_GOOGLE_API_KEY': 'google-key',
        'AI_GROQ_API_KEY': '',
        'AI_OPENROUTER_API_KEY': '',
        'AI_MISTRAL_API_KEY': '',
        'AI_TOGETHER_API_KEY': '',
        'AI_HUGGINGFACE_API_KEY': '',
    }, clear=False)
    @patch('opportunity_agent.services.ai_engine.requests.post')
    def test_raises_when_all_configured_providers_fail(self, post):
        post.side_effect = requests.ConnectionError('Unavailable')

        with self.assertRaisesRegex(AIProviderError, 'google'):
            AIClient().classify_opportunity('Python role')

    @patch.dict('os.environ', {
        'AI_API_KEY': '',
        'AI_GOOGLE_API_KEY': 'google-key',
        'AI_GROQ_API_KEY': 'groq-key',
        'AI_OPENROUTER_API_KEY': 'openrouter-key',
        'AI_MISTRAL_API_KEY': 'mistral-key',
        'AI_TOGETHER_API_KEY': 'together-key',
        'AI_HUGGINGFACE_API_KEY': 'huggingface-key',
    }, clear=False)
    @patch('opportunity_agent.services.ai_engine.requests.post')
    def test_tries_all_providers_in_configured_fallback_order(self, post):
        post.side_effect = requests.ConnectionError('Unavailable')

        with self.assertRaisesRegex(
            AIProviderError,
            'google, groq, openrouter, mistral, together, huggingface',
        ):
            AIClient().classify_opportunity('Python role')

        self.assertEqual(
            [call.args[0] for call in post.call_args_list],
            [
                'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
                'https://api.groq.com/openai/v1/chat/completions',
                'https://openrouter.ai/api/v1/chat/completions',
                'https://api.mistral.ai/v1/chat/completions',
                'https://api.together.xyz/v1/chat/completions',
                'https://api-inference.huggingface.co/v1/chat/completions',
            ],
        )
