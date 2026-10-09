from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import socket
import json
import ipaddress

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
    TelegramMessageRetry,
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
from .services.matching import (
    compute_match_score,
    opportunity_match_data,
    profile_match_data,
    refresh_user_matches,
)
from .services.ai_engine import AIClient, AIProviderError

User = get_user_model()


class OpportunityAgentTestCase(TestCase):
    def setUp(self):
        call_command('setup_roles')
        self.public_destination_validation = patch(
            'opportunity_agent.services.source_ingestion.public_addresses',
            return_value={ipaddress.ip_address('93.184.216.34')},
        )
        self.public_destination_validation.start()
        self.addCleanup(self.public_destination_validation.stop)
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

    @patch('opportunity_agent.services.source_ingestion.public_addresses')
    def test_api_application_url_is_not_used_as_detail_or_source_url(self, public_addresses):
        from .services.source_ingestion import basic_extract, extract_candidates

        public_addresses.return_value = {ipaddress.ip_address('93.184.216.34')}
        api_source = Source(
            name='Public API with application destinations',
            url='https://api.example.com/opportunities',
            source_type='api',
        )
        candidate = next(extract_candidates(
            api_source,
            api_source.url,
            json.dumps({
                'items': [{
                    'title': 'Research Fellowship',
                    'link': '/fellowships/42',
                    'application_url': 'https://apply.example.org/forms/42',
                    'description': 'A specific research fellowship.',
                }],
            }),
        ))

        extracted = basic_extract(candidate, api_source)

        self.assertEqual(candidate['url'], 'https://api.example.com/fellowships/42')
        self.assertEqual(candidate['source_landing_url'], api_source.url)
        self.assertEqual(extracted['source_url'], 'https://api.example.com/fellowships/42')
        self.assertEqual(
            extracted['application_url'],
            'https://apply.example.org/forms/42',
        )
        self.assertEqual(extracted['application_method'], 'online')

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

    def test_extraction_normalizes_nested_requirement_lists_and_string_skills(self):
        from .services.source_ingestion import basic_extract

        extracted = basic_extract(
            {
                'title': 'Structured Requirements Fellowship',
                'url': 'https://example.org/fellowship',
                'source_landing_url': 'https://example.org/opportunities',
                'text': '',
                'data': {
                    'requirements': [
                        {'text': 'Must be enrolled in a graduate program.'},
                        {'text': 'Submit a writing sample.'},
                    ],
                    'qualifications': [{'text': 'Masters degree.'}],
                    'education_requirements': [{'text': 'Masters degree required.'}],
                    'experience_requirements': [{'text': 'Two years of research.'}],
                    'skills': 'Python, data analysis',
                },
            },
            self.source,
        )

        self.assertEqual(
            extracted['requirements'],
            'Must be enrolled in a graduate program.; Submit a writing sample.',
        )
        self.assertEqual(extracted['qualifications'], 'Masters degree.')
        self.assertEqual(extracted['education_requirements'], 'Masters degree required.')
        self.assertEqual(extracted['experience_requirements'], 'Two years of research.')
        self.assertEqual(extracted['skills'], ['Python', 'data analysis'])

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
        self.assertEqual(extracted['application_method'], 'source_only')

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
        self.assertEqual(extracted['application_method'], 'source_only')
        self.assertEqual(extracted['contact_phone'], '+1 212 555 1234')
        self.assertEqual(extracted['telegram_contact'], 'https://t.me/example_fellowships')
        self.assertEqual(extracted['physical_address'], '1 Research Road, Nairobi')
        self.assertEqual(extracted['organization_website'], 'https://example.org/about')

    @patch('opportunity_agent.services.source_ingestion.public_addresses')
    def test_alternative_application_methods_require_explicit_application_instructions(self, public_addresses):
        from .services.source_ingestion import basic_extract

        public_addresses.return_value = {ipaddress.ip_address('93.184.216.34')}
        telegram = basic_extract({
            'title': 'Research Fellowship',
            'url': 'https://example.org/fellowship',
            'text': 'Apply via Telegram: https://t.me/example_fellowship',
            'html': '<a href="https://t.me/example_fellowship">Example fellowship contact</a>',
        }, self.source)
        physical = basic_extract({
            'title': 'Research Fellowship',
            'url': 'https://example.org/fellowship',
            'text': 'Mail your application to the address below.',
            'html': '<address>1 Research Road, Nairobi</address>',
        }, self.source)
        general_contact = basic_extract({
            'title': 'Research Fellowship',
            'url': 'https://example.org/fellowship',
            'text': 'For more information, contact fellowships@example.org.',
            'html': '',
        }, self.source)

        self.assertEqual(telegram['application_method'], 'telegram')
        self.assertEqual(telegram['telegram_contact'], 'https://t.me/example_fellowship')
        self.assertEqual(physical['application_method'], 'physical')
        self.assertEqual(physical['physical_address'], '1 Research Road, Nairobi')
        self.assertEqual(general_contact['application_method'], 'source_only')

    @patch('opportunity_agent.services.source_ingestion.public_addresses')
    def test_general_telegram_channel_is_not_extracted_as_application_route(self, public_addresses):
        from .services.source_ingestion import basic_extract

        public_addresses.return_value = {ipaddress.ip_address('93.184.216.34')}
        extracted = basic_extract({
            'title': 'OD Mentorship',
            'url': 'https://opportunitydesk.org/od-mentorship/',
            'text': (
                'Mentees receive application advice. For help, send materials by '
                'email. Telegram Facebook X Instagram Telegram'
            ),
            'html': (
                '<p>To become a mentee, send complete materials by email.</p>'
                '<footer><a href="https://t.me/opportunitydesk/">Telegram</a></footer>'
            ),
        }, self.source)

        self.assertEqual(extracted['telegram_contact'], 'https://t.me/opportunitydesk/')
        self.assertNotIn(
            'telegram',
            {route['method'] for route in extracted['application_methods']},
        )
        self.assertNotEqual(extracted['application_method'], 'telegram')

    @patch('opportunity_agent.services.source_ingestion.public_addresses')
    def test_application_methods_are_extracted_only_from_explicit_instructions(self, public_addresses):
        from .services.source_ingestion import basic_extract

        public_addresses.return_value = {ipaddress.ip_address('93.184.216.34')}
        cases = (
            (
                'email',
                'To apply, email your CV and supporting documents to '
                'applications@example.org.',
                '',
                'email',
            ),
            (
                'telegram',
                'Apply by contacting our coordinator on Telegram at '
                'https://t.me/apply_team.',
                '<a href="https://t.me/apply_team">Telegram application contact</a>',
                'telegram',
            ),
            (
                'physical',
                'Applicants must deliver their application in person to '
                '1 Research Road, Nairobi.',
                '<address>1 Research Road, Nairobi</address>',
                'physical',
            ),
            (
                'phone',
                'To apply by phone, call +1 212 555 1234.',
                '<a href="tel:+12125551234">Application phone</a>',
                'phone',
            ),
        )
        for index, (label, text, markup, route_method) in enumerate(cases):
            with self.subTest(method=label):
                extracted = basic_extract({
                    'title': f'{label.title()} opportunity',
                    'url': f'https://example.org/opportunities/{index}',
                    'text': text,
                    'html': markup,
                }, self.source)
                self.assertEqual(extracted['application_methods'][0]['method'], route_method)
                if route_method == 'phone':
                    self.assertEqual(extracted['application_method'], 'source_only')
                else:
                    self.assertEqual(extracted['application_method'], route_method)

    @patch('opportunity_agent.services.source_ingestion.public_addresses')
    def test_online_portal_and_labeled_application_form_are_distinguished(self, public_addresses):
        from .services.source_ingestion import basic_extract

        public_addresses.return_value = {ipaddress.ip_address('93.184.216.34')}
        portal = basic_extract({
            'title': 'Online opportunity',
            'url': 'https://example.org/opportunities/online',
            'text': 'Submit your application online.',
            'html': '<a href="/portal/42" aria-label="Apply Online">Continue</a>',
        }, self.source)
        form = basic_extract({
            'title': 'Form opportunity',
            'url': 'https://example.org/opportunities/form',
            'text': 'Complete the application form.',
            'html': '<a href="https://forms.example.org/entry/42">Application Form</a>',
        }, self.source)

        self.assertEqual(portal['application_method'], 'online')
        self.assertEqual(portal['application_url'], 'https://example.org/portal/42')
        self.assertEqual(form['application_method'], 'form')
        self.assertEqual(form['application_form_url'], 'https://forms.example.org/entry/42')
        self.assertEqual(form['application_url'], '')

    def test_general_contacts_and_ambiguous_instructions_are_not_application_routes(self):
        from .services.source_ingestion import basic_extract

        for text in (
            'For general information, contact applications@example.org.',
            'Applications are open. Contact the organization for further details.',
        ):
            with self.subTest(text=text):
                extracted = basic_extract({
                    'title': 'Research opportunity',
                    'url': 'https://example.org/opportunities/1',
                    'text': text,
                    'html': (
                        '<a href="mailto:applications@example.org">Contact email</a>'
                        '<form action="/contact"><label>Contact us</label>'
                        '<button>Send message</button></form>'
                    ),
                }, self.source)
                self.assertEqual(extracted['application_method'], 'source_only')
                self.assertEqual(extracted['application_methods'], [])

    @patch('opportunity_agent.services.source_ingestion.public_addresses')
    def test_multiple_explicit_application_methods_are_preserved(self, public_addresses):
        from .services.source_ingestion import basic_extract

        public_addresses.return_value = {ipaddress.ip_address('93.184.216.34')}
        extracted = basic_extract({
            'title': 'Multi-route opportunity',
            'url': 'https://example.org/opportunities/multi',
            'text': (
                'Apply online at https://apply.example.org/entry or, alternatively, '
                'email your CV to fellowship@example.org. Applications may also be '
                'delivered in person to 1 Research Road, Nairobi.'
            ),
            'html': (
                '<a href="https://apply.example.org/entry">Apply Now</a>'
                '<address>1 Research Road, Nairobi</address>'
            ),
        }, self.source)

        self.assertEqual(extracted['application_method'], 'online')
        self.assertEqual(
            {route['method'] for route in extracted['application_methods']},
            {'online', 'email', 'physical'},
        )
        self.assertIn('fellowship@example.org', extracted['application_instructions'])

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

    def test_listing_page_rejects_category_search_archive_and_directory_pages(self):
        from .services.source_ingestion import is_listing_page

        for heading in (
            'Browsing: Training',
            'Browsing: Conferences',
            'Browsing: PhD/Postdoctoral',
            'Search results for scholarships',
            'Scholarship Categories',
            'Opportunity Archive',
            'Training Directory',
        ):
            with self.subTest(heading=heading):
                self.assertTrue(is_listing_page({
                    'title': heading,
                    'url': 'https://example.org/opportunities',
                    'html': f'<html><h1>{heading}</h1></html>',
                }))

    def test_specific_opportunity_titles_with_category_words_are_not_listings(self):
        from .services.source_ingestion import is_listing_page

        for title in (
            'Fully Funded PhD Scholarship in Climate Research',
            'Digital Skills Training Fellowship',
            'International Conference Travel Scholarship',
            'Research Internship and Job Training Program',
        ):
            with self.subTest(title=title):
                self.assertFalse(is_listing_page({
                    'title': title,
                    'url': 'https://example.org/jobs/specific-opportunity',
                    'html': f'<html><h1>{title}</h1><p>Apply by 2035.</p></html>',
                }))

    def test_structured_opportunity_detail_overrides_a_generic_category_word_title(self):
        from .services.source_ingestion import is_listing_page

        description = (
            'The Example Institute offers this named scholarship for graduate '
            'researchers. Applicants must submit a research proposal and meet '
            'the eligibility requirements listed below.'
        )
        self.assertFalse(is_listing_page({
            'title': 'Scholarship',
            'url': 'https://example.org/scholarship/graduate-research',
            'html': (
                '<h1>Scholarship</h1><script type="application/ld+json">'
                + json.dumps({
                    '@type': 'Scholarship',
                    'name': 'Scholarship',
                    'description': description,
                    'provider': {'name': 'Example Institute'},
                    'applicationUrl': 'https://apply.example.org/graduate-research',
                })
                + '</script>'
            ),
        }))
        self.assertFalse(is_listing_page({
            'title': 'Scholarships',
            'url': 'https://example.org/scholarship/graduate-research',
            'html': (
                '<h1>Graduate Research Scholarship at Example Institute</h1>'
                '<p>Applicants can submit a research proposal through the portal.</p>'
            ),
        }))
        self.assertTrue(is_listing_page({
            'title': 'Scholarships',
            'url': 'https://example.org/scholarships',
            'html': (
                '<h1>Scholarships</h1>'
                '<script type="application/ld+json">'
                + json.dumps({
                    '@type': 'Scholarship',
                    'name': 'Featured Scholarship',
                    'description': description,
                    'provider': {'name': 'Example Institute'},
                    'applicationUrl': 'https://apply.example.org/featured',
                })
                + '</script>'
                '<a href="/one">Research Scholarship one</a>'
                '<a href="/two">Research Scholarship two</a>'
                '<a href="/three">Research Scholarship three</a>'
            ),
        }))

    def test_listing_directory_structure_and_pagination_are_detected(self):
        from .services.source_ingestion import is_listing_page
        from .management.commands.audit_application_routes import _listing_content_evidence

        candidate = {
            'title': 'Latest opportunities',
            'url': 'https://example.org/category/scholarships/page/2',
            'html': (
                '<meta name="description" content="Browse all current opportunities">'
                '<h1>Latest opportunities</h1>'
                '<article class="opportunity-card"><a href="/one">PhD Scholarship</a></article>'
                '<article class="opportunity-card"><a href="/two">Research Fellowship</a></article>'
                '<article class="opportunity-card"><a href="/three">Training Grant</a></article>'
            ),
        }
        self.assertTrue(is_listing_page(candidate))
        evidence = _listing_content_evidence(candidate)
        self.assertTrue(any('Latest opportunities' in item for item in evidence))
        self.assertTrue(any('opportunity-specific links' in item for item in evidence))
        self.assertTrue(any('repeated listing/card containers' in item for item in evidence))

    def test_reliefweb_listing_filters_are_rejected_but_detail_query_is_allowed(self):
        from .services.source_ingestion import is_listing_page, listing_url_reason

        for url in (
            'https://reliefweb.int/jobs?list=123',
            'https://reliefweb.int/jobs?advanced-search=1',
            'https://reliefweb.int/jobs?country=kenya&page=2',
            'https://reliefweb.int/jobs?organization=example',
            'https://reliefweb.int/jobs',
        ):
            with self.subTest(url=url):
                self.assertTrue(listing_url_reason(url))
                self.assertTrue(is_listing_page({
                    'title': 'Research Opportunity',
                    'url': url,
                    'html': '<h1>Research Opportunity</h1>',
                }))

        detail_url = 'https://reliefweb.int/job/1234567/research-officer?source=portal'
        self.assertFalse(listing_url_reason(detail_url))
        self.assertFalse(is_listing_page({
            'title': 'Research Officer',
            'url': detail_url,
            'html': (
                '<article><h1>Research Officer</h1>'
                '<p>Manage a research program and submit an application online. '
                'Applicants must have a relevant degree and field experience.</p></article>'
            ),
        }))

    def test_opportunity_desk_categories_archives_and_roundups_are_rejected(self):
        from .services.source_ingestion import is_listing_page, listing_url_reason

        for url in (
            'https://opportunitydesk.org/',
            'https://opportunitydesk.org/category/scholarships/',
            'https://opportunitydesk.org/2025/',
            'https://opportunitydesk.org/?s=scholarships',
        ):
            with self.subTest(url=url):
                self.assertTrue(listing_url_reason(url))
                self.assertTrue(is_listing_page({
                    'title': 'Scholarship Opportunity',
                    'url': url,
                    'html': '<h1>Scholarship Opportunity</h1>',
                }))
        self.assertTrue(is_listing_page({
            'title': 'Deadline Roundup: Opportunities Closing This Week',
            'url': 'https://opportunitydesk.org/2025/01/deadline-roundup/',
            'html': '<article><h1>Deadline Roundup: Opportunities Closing This Week</h1></article>',
        }))
        detail_url = 'https://opportunitydesk.org/2025/01/research-fellowship/'
        self.assertFalse(listing_url_reason(detail_url))
        self.assertFalse(is_listing_page({
            'title': 'Research Fellowship at Example Institute',
            'url': detail_url,
            'html': (
                '<article><h1>Research Fellowship at Example Institute</h1>'
                '<div class="entry-content"><p>Applications are open for a specific '
                'research fellowship. Applicants must submit a CV, references, and '
                'a research statement by the stated deadline.</p></div></article>'
            ),
        }))

    def test_unusual_source_detail_url_is_validated_by_content_not_domain(self):
        from .services.source_ingestion import (
            is_listing_page,
            listing_url_reason,
            opportunity_detail_validation_error,
        )

        detail = {
            'title': 'Field Research Fellowship at Example Institute',
            'url': 'https://opportunitydesk.org/fellowships/field-research-2026/?source=article',
            'html': (
                '<article><h1>Field Research Fellowship at Example Institute</h1>'
                '<div class="entry-content"><p>The Example Institute invites '
                'applications for a named 12-month field research fellowship. '
                'Applicants must hold a relevant degree, submit a CV and research '
                'proposal, and meet the eligibility criteria before the deadline.</p>'
                '<a href="https://apply.example.org/field-research">Apply</a>'
                '</div></article>'
            ),
        }

        self.assertFalse(listing_url_reason(detail['url']))
        self.assertFalse(is_listing_page(detail))
        self.assertEqual(opportunity_detail_validation_error(detail), '')

    def test_public_opportunity_surfaces_hide_listings_but_keep_route_missing_detail(self):
        listing = Opportunity.objects.create(
            source=self.source,
            title='Research Fellowships',
            source_url='https://opportunitydesk.org/category/research-fellowships/',
            description='Browse recent fellowships.',
            status='active',
            dedupe_hash='public-listing-page',
        )
        manual_review = Opportunity.objects.create(
            source=self.source,
            title='Field Research Fellowship at Example Institute',
            source_url='https://example.org/program/field-research-2026',
            description=(
                'The Example Institute offers this 12-month field research '
                'fellowship. Applicants must hold a relevant degree and submit '
                'a research proposal by the deadline.'
            ),
            status='active',
            application_method='source_only',
            dedupe_hash='public-genuine-detail-without-route',
        )

        home = self.client.get('/')
        listing_page = self.client.get('/opportunities/')
        sitemap = self.client.get('/sitemap.xml')

        self.assertEqual(home.status_code, 200)
        self.assertNotIn(listing, home.context['featured'])
        self.assertEqual(listing_page.status_code, 200)
        self.assertNotIn(listing, listing_page.context['opportunities'])
        self.assertIn(manual_review, listing_page.context['opportunities'])
        self.assertNotContains(sitemap, f'/opportunities/{listing.pk}/')
        self.assertContains(sitemap, f'/opportunities/{manual_review.pk}/')
        self.assertEqual(
            self.client.get(f'/opportunities/{listing.pk}/').status_code,
            404,
        )
        detail_response = self.client.get(f'/opportunities/{manual_review.pk}/')
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, manual_review.title)
        listing.refresh_from_db()
        self.assertEqual(listing.status, 'active')

    def test_listing_opportunity_cannot_be_applied_to_or_prepared(self):
        from .tasks import process_application_queue_task

        listing = Opportunity.objects.create(
            source=self.source,
            title='Open positions',
            source_url='https://reliefweb.int/jobs?list=123',
            dedupe_hash='listing-cannot-apply-or-queue',
        )
        self.client.force_login(self.user)
        response = self.client.post(f'/opportunities/{listing.pk}/apply/')
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Application.objects.filter(
            user=self.user,
            opportunity=listing,
        ).exists())
        application = Application.objects.create(
            user=self.user,
            opportunity=listing,
            status='queued',
        )

        result = process_application_queue_task.run(limit=5)

        application.refresh_from_db()
        self.assertEqual(application.status, 'needs_review')
        self.assertIn('not publishable', application.error_message)
        self.assertEqual(result['processed'], 0)

    def test_detail_validation_requires_specific_url_and_opportunity_content(self):
        from .services.source_ingestion import opportunity_detail_validation_error

        valid_page = {
            'title': 'Research Fellowship at Example Institute',
            'url': 'https://opportunitydesk.org/2025/01/research-fellowship/',
            'html': (
                '<article><h1>Research Fellowship at Example Institute</h1>'
                '<div class="entry-content"><p>This fellowship supports '
                'early-career researchers at the Example Institute. Applicants '
                'must submit a research proposal and curriculum vitae by the '
                'deadline. Apply using the online application portal.</p>'
                '<a href="https://apply.example.org/fellowship">Apply Now</a>'
                '</div></article>'
            ),
        }
        self.assertEqual(opportunity_detail_validation_error(valid_page), '')
        self.assertTrue(opportunity_detail_validation_error({
            'title': 'Research Fellowship',
            'url': 'https://opportunitydesk.org/category/fellowships/',
            'html': '<h1>Research Fellowship</h1>',
        }))
        self.assertTrue(opportunity_detail_validation_error({
            'title': 'Research Fellowship',
            'url': 'https://opportunitydesk.org/2025/01/research-fellowship/',
            'html': '<article><h1>Research Fellowship</h1><p>Brief notice.</p></article>',
        }))

    def test_query_based_listing_pages_are_rejected_but_reliefweb_detail_is_content_checked(self):
        from .services.source_ingestion import (
            listing_url_reason,
            opportunity_detail_validation_error,
        )

        self.assertTrue(listing_url_reason('https://example.org/opportunities?category=jobs'))
        self.assertTrue(listing_url_reason(
            'https://reliefweb.int/job/4233250/research-officer?list=jobs'
        ))
        detail = {
            'title': 'Research Officer',
            'url': 'https://reliefweb.int/job/4233250/research-officer?source=portal',
            'html': (
                '<article><h1>Research Officer</h1><div itemprop="articleBody">'
                '<p>The organization is recruiting a Research Officer. Applicants '
                'must submit an application with a relevant degree and experience '
                'by the deadline. Apply through the official recruitment portal.</p>'
                '<a href="https://apply.example.org/role">Apply online</a>'
                '</div></article>'
            ),
        }
        self.assertEqual(opportunity_detail_validation_error(detail), '')
        detail['html'] = '<article><h1>Research Officer</h1><p>Short entry.</p></article>'
        self.assertTrue(opportunity_detail_validation_error(detail))

    def test_detail_page_content_prefers_article_body_for_supported_sources(self):
        from .services.source_ingestion import detail_page_content

        body = (
            '<nav>Jobs Scholarships Opportunities</nav>'
            '<article><h1>Research Fellowship</h1><div class="entry-content">'
            '<p>Specific program description and eligibility details.</p>'
            '</div></article>'
        )
        text = detail_page_content(
            'https://opportunitydesk.org/2025/01/research-fellowship/',
            body,
        ).get_text(' ', strip=True)
        self.assertIn('Specific program description', text)
        self.assertNotIn('Jobs Scholarships Opportunities', text)

    @patch('opportunity_agent.services.source_ingestion.public_addresses')
    def test_application_link_label_variants_and_accessible_names_are_supported(
        self,
        public_addresses,
    ):
        from .services.source_ingestion import basic_extract

        public_addresses.return_value = {ipaddress.ip_address('93.184.216.34')}
        for index, label in enumerate((
            'Apply',
            'Apply Now',
            'Apply Online',
            'Application Portal',
            'Application Form',
            'Submit Application',
            'Register',
            'Register Now',
            'Start Application',
        )):
            with self.subTest(label=label):
                extracted = basic_extract({
                    'title': 'Research Fellowship',
                    'url': 'https://example.org/fellowship',
                    'source_landing_url': 'https://example.org/opportunities',
                    'text': '',
                    'html': (
                        f'<a href="/apply/{index}" aria-label="{label}">'
                        'Continue</a>'
                    ),
                }, self.source)
                if label == 'Application Form':
                    self.assertEqual(extracted['application_url'], '')
                    self.assertEqual(
                        extracted['application_form_url'],
                        f'https://example.org/apply/{index}',
                    )
                    self.assertEqual(extracted['application_method'], 'form')
                else:
                    self.assertEqual(
                        extracted['application_url'],
                        f'https://example.org/apply/{index}',
                    )

    @patch('opportunity_agent.services.source_ingestion.public_addresses')
    def test_application_url_is_resolved_from_relative_detail_link(self, public_addresses):
        from .services.source_ingestion import basic_extract

        public_addresses.return_value = {ipaddress.ip_address('93.184.216.34')}
        extracted = basic_extract({
            'title': 'Training Fellowship',
            'url': 'https://example.org/opportunities/training/42',
            'source_landing_url': 'https://example.org/opportunities',
            'text': '',
            'html': '<a title="Apply Online" href="../apply/42">Continue</a>',
        }, self.source)

        self.assertEqual(
            extracted['application_url'],
            'https://example.org/opportunities/apply/42',
        )

    def test_labeled_application_form_action_is_extracted(self):
        from .services.source_ingestion import basic_extract

        extracted = basic_extract({
            'title': 'Research Fellowship',
            'url': 'https://example.org/fellowship',
            'source_landing_url': 'https://example.org/opportunities',
            'text': 'Submit Application',
            'html': (
                '<form action="/application/submit" name="application">'
                '<button type="submit">Submit</button></form>'
            ),
        }, self.source)

        self.assertEqual(
            extracted['application_url'],
            'https://example.org/application/submit',
        )

    @patch(
        'opportunity_agent.services.source_ingestion.public_addresses',
        side_effect=ValueError('URL must resolve only to public IP addresses.'),
    )
    def test_private_application_destinations_are_rejected(self, public_addresses):
        from .services.source_ingestion import basic_extract

        extracted = basic_extract({
            'title': 'Research Fellowship',
            'url': 'https://example.org/fellowship',
            'text': 'Apply at http://127.0.0.1:8000/admin.',
            'html': '<a href="http://127.0.0.1:8000/apply">Apply Now</a>',
        }, self.source)

        self.assertEqual(extracted['application_url'], '')
        self.assertEqual(extracted['application_method'], 'source_only')
        self.assertTrue(public_addresses.called)

    def test_unsafe_schemes_and_credentialed_application_urls_are_rejected(self):
        from .services.source_ingestion import basic_extract

        for href in (
            'javascript:alert(1)',
            'https://user:password@example.org/apply',
        ):
            with self.subTest(href=href):
                extracted = basic_extract({
                    'title': 'Research Fellowship',
                    'url': 'https://example.org/fellowship',
                    'text': '',
                    'html': f'<a href="{href}">Apply Now</a>',
                }, self.source)
                self.assertEqual(extracted['application_url'], '')

    @patch('opportunity_agent.services.source_ingestion.public_addresses')
    def test_application_url_and_form_are_extracted_from_json_ld(self, public_addresses):
        from .services.source_ingestion import basic_extract

        public_addresses.return_value = {ipaddress.ip_address('93.184.216.34')}
        extracted = basic_extract({
            'title': 'Research Fellowship',
            'url': 'https://example.org/fellowship',
            'text': 'Research Fellowship details.',
            'html': (
                '<script type="application/ld+json">'
                '{"@type":"JobPosting","applicationUrl":"https://forms.example.org/apply"}'
                '</script>'
            ),
        }, self.source)

        self.assertEqual(
            extracted['application_url'],
            'https://forms.example.org/apply',
        )
        self.assertEqual(extracted['application_method'], 'online')

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
                'data': {
                    'application_url': 'https://example.org/opportunities',
                    'application_form_url': 'https://example.org/opportunities',
                },
            },
            self.source,
        )

        self.assertEqual(extracted['application_url'], '')
        self.assertEqual(extracted['application_form_url'], '')
        self.assertEqual(extracted['application_method'], 'source_only')

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
        self.assertEqual(extracted['application_method'], 'email')

    def test_email_only_application_guard_returns_manual_contact_reason(self):
        from .services.application_guard import ApplicationGuard

        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='prepared',
        )
        self.opportunity.application_url = ''
        self.opportunity.contact_email = 'general@example.org'
        self.opportunity.application_method = 'email'
        self.opportunity.application_methods = [{
            'method': 'email',
            'destination': 'applications@example.org',
            'instructions': 'Email the completed application to this address.',
        }]
        self.opportunity.save(update_fields=[
            'application_url', 'contact_email', 'application_method',
            'application_methods',
        ])

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
        self.assertIn('applications@example.org', reason)
        self.assertNotIn('general@example.org', reason)

    def test_general_contact_is_not_treated_as_an_application_method(self):
        from .services.application_guard import ApplicationGuard

        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='prepared',
        )
        self.opportunity.application_url = ''
        self.opportunity.application_method = 'source_only'
        self.opportunity.contact_email = 'info@example.org'
        self.opportunity.save(update_fields=[
            'application_url', 'application_method', 'contact_email',
        ])

        with patch.object(ApplicationGuard, '_cv_is_available', return_value=(True, '')):
            allowed, reason = ApplicationGuard.can_submit(
                self.user,
                self.opportunity,
                self.profile,
                application=application,
                adapter=Mock(),
            )

        self.assertFalse(allowed)
        self.assertIn('APPLICATION_METHOD_UNVERIFIED', reason)
        self.assertNotIn('info@example.org', reason)

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
                '<a href="/apply/123">Apply Now</a></html>',
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
    @patch('opportunity_agent.tasks.fetch_public_source')
    @patch(
        'opportunity_agent.tasks.AIClient.extract_opportunity',
        return_value={
            'application_url': 'https://invented.example/apply',
            'application_form_url': 'https://invented.example/form.pdf',
            'application_method': 'source_only',
            'application_methods': [{
                'method': 'email',
                'destination': 'invented@example.org',
                'instructions': 'Use the invented AI destination.',
            }],
            'application_instructions': 'Invented AI instructions.',
        },
    )
    @patch('opportunity_agent.tasks._upsert_opportunity', return_value=(None, False))
    def test_ai_does_not_overwrite_verified_application_destinations(
        self,
        upsert,
        extract,
        fetch,
        validate_url,
    ):
        from .tasks import scan_sources_task

        source = Source.objects.get(pk=self.source.pk)
        fetch.side_effect = [
            (
                'https://example.com/jobs',
                '<a href="/jobs/research-fellowship">Research Fellowship</a>',
                'text/html',
            ),
            (
                'https://example.com/jobs/research-fellowship',
                '<h1>Research Fellowship</h1>'
                '<p>Full fellowship details.</p>'
                '<a href="/apply/verified">Apply Now</a>',
                'text/html',
            ),
        ]

        result = scan_sources_task.run(limit=1, source_ids=[source.pk])

        self.assertEqual(result['successful'], 1)
        extracted = upsert.call_args.args[1]
        self.assertEqual(
            extracted['application_url'],
            'https://example.com/apply/verified',
        )
        self.assertEqual(extracted['application_form_url'], '')
        self.assertEqual(extracted['application_method'], 'online')
        self.assertEqual(
            extracted['application_methods'][0]['destination'],
            'https://example.com/apply/verified',
        )
        self.assertNotIn('invented@example.org', str(extracted['application_methods']))
        self.assertNotIn('Invented AI instructions.', extracted['application_instructions'])

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

    @override_settings(SOURCE_CANDIDATE_BATCH_SIZE=1)
    @patch('opportunity_agent.tasks._validate_public_source_url')
    @patch(
        'opportunity_agent.tasks.fetch_public_source',
        return_value=(
            'https://example.com/jobs',
            '<html><title>Research opportunities</title></html>',
            'text/html',
        ),
    )
    @patch('opportunity_agent.tasks.extract_candidates')
    @patch('opportunity_agent.tasks.AIClient.extract_opportunity', return_value={})
    @patch('opportunity_agent.tasks._upsert_opportunity', return_value=(None, False))
    def test_source_scan_continues_at_the_next_candidate_batch(
        self,
        upsert,
        extract,
        candidates,
        fetch,
        validate_url,
    ):
        from .tasks import scan_sources_task

        source = Source.objects.get(pk=self.source.pk)
        items = [
            {
                'title': f'Research Fellowship {index}',
                'url': f'https://example.com/jobs/{index}',
                'source_landing_url': 'https://example.com/jobs',
                'text': f'Research fellowship number {index}.',
                'html': '',
                'raw_source_content': f'Research fellowship number {index}.',
            }
            for index in (1, 2)
        ]
        candidates.side_effect = lambda *_args: iter(items)

        first = scan_sources_task.run(limit=1, source_ids=[source.pk])
        source.refresh_from_db()
        self.assertEqual(first['successful'], 1)
        self.assertEqual(upsert.call_count, 1)
        self.assertEqual(source.candidate_scan_cursor, 1)

        second = scan_sources_task.run(limit=1, source_ids=[source.pk])
        source.refresh_from_db()
        self.assertEqual(second['successful'], 1)
        self.assertEqual(upsert.call_count, 2)
        self.assertEqual(upsert.call_args.args[1]['title'], 'Research Fellowship 2')
        self.assertEqual(source.candidate_scan_cursor, 0)

    def test_short_legitimate_discovery_page_is_not_rejected_by_length(self):
        from .services.source_discovery import _inspect_candidate

        with patch(
            'opportunity_agent.services.public_http.socket.getaddrinfo',
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
            ],
        ), patch(
            'opportunity_agent.services.source_discovery._fetch_public_page',
            return_value=(
                'https://example.org/jobs',
                '<html><title>Research Jobs</title><body>Research jobs and internships.</body></html>',
            ),
        ):
            result = _inspect_candidate(
                'Research careers',
                'https://example.org/jobs',
            )

        self.assertIsNotNone(result)
        self.assertEqual(result['opportunity_types'], ['job', 'internship', 'research'])

    def test_generic_category_heading_is_rejected_but_category_word_in_detail_url_is_not(self):
        from .services.source_ingestion import is_listing_page

        self.assertTrue(is_listing_page({
            'title': 'Jobs',
            'url': 'https://example.org/jobs',
            'html': '<html><h1>Jobs</h1></html>',
        }))
        self.assertFalse(is_listing_page({
            'title': 'Research Fellowship',
            'url': 'https://example.org/jobs/research-fellowship',
            'html': '<html><h1>Research Fellowship</h1><p>Apply now.</p></html>',
        }))

    def test_legacy_heuristic_does_not_promote_source_or_article_urls_to_application(self):
        from .services.source_scanner import _heuristic_extract

        source_url = 'https://example.org/jobs/apply/research'
        extracted = _heuristic_extract(
            f'Research Fellowship. Apply now: {source_url}',
            source_url,
        )

        self.assertEqual(extracted['application_url'], '')

    def test_registration_url_must_match_the_authorized_credential_domain(self):
        from .models import EmailMailbox, SiteCredential
        from .services.account_registration import register_site_account

        mailbox = EmailMailbox.objects.create(
            user=self.user,
            name='Primary mailbox',
            email='alice@example.com',
        )
        credential = SiteCredential.objects.create(
            user=self.user,
            name='Trusted site',
            domain='trusted.example',
            registration_url='https://attacker.example/register',
            auto_register=True,
            email_mailbox=mailbox,
        )

        succeeded, reason = register_site_account(credential)

        self.assertFalse(succeeded)
        self.assertIn('authorized credential domain', reason)

    @patch('opportunity_agent.services.public_http.socket.getaddrinfo')
    def test_registration_destination_requires_authorized_public_https_host(self, getaddrinfo):
        from .services.account_registration import _authorized_registration_url

        getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
        ]
        self.assertTrue(_authorized_registration_url(
            'https://forms.trusted.example/register',
            'trusted.example',
        ))
        self.assertFalse(_authorized_registration_url(
            'https://attacker.example/register',
            'trusted.example',
        ))
        self.assertFalse(_authorized_registration_url(
            'http://trusted.example/register',
            'trusted.example',
        ))
        getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443)),
        ]
        self.assertFalse(_authorized_registration_url(
            'https://trusted.example/register',
            'trusted.example',
        ))

    def test_registration_error_redacts_passwords_and_url_tokens(self):
        from .services.account_registration import _safe_registration_error

        error = _safe_registration_error(
            'Password=site-password while visiting https://example.org/?token=mail-token',
            ('site-password', 'mail-token'),
        )

        self.assertNotIn('site-password', error)
        self.assertNotIn('mail-token', error)
        self.assertIn('[redacted]', error)

    def test_public_http_rejects_mixed_public_and_private_dns_answers(self):
        from .services.public_http import public_addresses

        with patch(
            'opportunity_agent.services.public_http.socket.getaddrinfo',
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 443)),
            ],
        ):
            with self.assertRaises(ValueError):
                public_addresses('https://example.org')

    def test_public_http_rejects_actual_connection_to_unapproved_address(self):
        from .services.public_http import validate_response_peer

        class FakeSocket(socket.socket):
            def getpeername(self):
                return ('127.0.0.1', 443)

        peer_socket = FakeSocket()
        try:
            response = requests.Response()
            response.raw = SimpleNamespace(
                _connection=SimpleNamespace(sock=peer_socket),
            )
            with self.assertRaises(ValueError):
                validate_response_peer(
                    response,
                    {ipaddress.ip_address('93.184.216.34')},
                )
        finally:
            peer_socket.close()

    @patch('opportunity_agent.services.public_http.public_addresses')
    @patch('opportunity_agent.services.public_http.requests.get')
    def test_public_http_does_not_follow_redirects(self, get, public_addresses):
        from .services.public_http import get_public_response

        response = Mock()
        response.status_code = 302
        get.return_value = response
        public_addresses.return_value = {ipaddress.ip_address('93.184.216.34')}

        with self.assertRaises(ValueError):
            get_public_response('https://example.org/path', timeout=7)

        get.assert_called_once_with(
            'https://example.org/path',
            headers=None,
            timeout=7,
            params=None,
            allow_redirects=False,
            stream=True,
        )
        response.close.assert_called_once()

    @patch('opportunity_agent.services.document_forms.get_public_response')
    def test_application_form_download_uses_safe_public_http(self, get_response):
        from .services.document_forms import download_form

        response = Mock()
        response.content = b'form data'
        response.headers = {'content-type': 'application/pdf'}
        get_response.return_value = response

        content, content_type = download_form('https://forms.example.org/form.pdf')

        self.assertEqual(content, b'form data')
        self.assertEqual(content_type, 'application/pdf')
        get_response.assert_called_once_with(
            'https://forms.example.org/form.pdf',
            timeout=30,
            headers={'User-Agent': 'OpportunityAgent/1.0'},
        )
        response.close.assert_called_once()

    @override_settings(DISCOVERY_MAX_PER_CYCLE=3, DISCOVERY_PAGE_SIZE=1)
    @patch(
        'opportunity_agent.services.source_discovery.PUBLIC_SOURCE_CANDIDATES',
        [],
    )
    @patch('opportunity_agent.services.source_discovery.get_public_response')
    def test_discovery_continues_to_next_query_after_query_failure(
        self,
        get_response,
    ):
        from .services.source_discovery import discover_public_sources

        response = Mock()
        response.text = '<html></html>'
        response.close.return_value = None
        get_response.side_effect = [requests.ConnectionError('temporary'), response]

        discover_public_sources([
            'jobs', 'careers', 'vacancies', 'scholarships', 'internships',
        ])

        self.assertEqual(get_response.call_count, 2)
        self.assertEqual(
            get_response.call_args_list[1].kwargs['params']['q'],
            '"scholarships" OR "internships"',
        )

    def test_deduplication_preserves_content_longer_than_previous_cap(self):
        from .services.deduplication import deduplicate_and_save_opportunity

        content = (
            'Long opportunity requirements. '
            + ('eligibility detail. ' * 4000)
        ).rstrip()
        opportunity, created = deduplicate_and_save_opportunity(
            self.source,
            {
                'title': 'Long Content Preservation Regression',
                'description': content,
                'source_url': 'https://example.org/long-opportunity',
            },
            raw_content=content,
        )

        self.assertTrue(created)
        self.assertEqual(opportunity.description, content)
        self.assertEqual(opportunity.raw_source_content, content)

    @patch.dict('os.environ', {'SMTP_PASSWORD': 'smtp-secret-123'}, clear=False)
    def test_application_errors_redact_configured_credentials(self):
        from .tasks import _safe_error_text

        error = _safe_error_text('SMTP failed using smtp-secret-123')

        self.assertNotIn('smtp-secret-123', error)
        self.assertIn('[redacted]', error)

    @patch('opportunity_agent.tasks._validate_public_source_url')
    @patch(
        'opportunity_agent.tasks.fetch_public_source',
        return_value=(
            'https://listing.example.net/jobs',
            '<html><title>Jobs</title><body>Browse all open jobs.</body></html>',
            'text/html',
        ),
    )
    @patch('opportunity_agent.tasks._upsert_opportunity')
    def test_listing_detection_applies_to_non_generic_source_types(
        self,
        upsert,
        fetch,
        validate_url,
    ):
        from .tasks import scan_sources_task

        source = Source.objects.create(
            name='Careers',
            url='https://listing.example.net/jobs',
            source_type='job_site',
            scan_frequency='manual',
        )

        result = scan_sources_task.run(limit=1, source_ids=[source.pk])

        self.assertEqual(result['successful'], 1)
        upsert.assert_not_called()

    def test_labeled_application_asset_is_not_accepted_as_application_url(self):
        from .services.source_ingestion import basic_extract

        extracted = basic_extract(
            {
                'title': 'Research Fellowship',
                'url': 'https://example.org/research-fellowship',
                'source_landing_url': 'https://example.org/jobs',
                'text': 'Research Fellowship details.',
                'html': (
                    '<a href="https://example.org/files/apply.png">Apply Now</a>'
                    '<a href="https://example.org/files/application.pdf">Application Form</a>'
                ),
            },
            self.source,
        )

        self.assertEqual(extracted['application_url'], '')
        self.assertEqual(
            extracted['application_form_url'],
            'https://example.org/files/application.pdf',
        )
        self.assertEqual(extracted['application_method'], 'form')

    def test_telegram_contact_link_is_not_promoted_to_application_url(self):
        from .services.source_ingestion import basic_extract

        extracted = basic_extract(
            {
                'title': 'Research Fellowship',
                'url': 'https://example.org/research',
                'source_landing_url': 'https://example.org/jobs',
                'text': 'Apply Now',
                'html': '<a href="https://t.me/example_jobs">Apply Now</a>',
            },
            self.source,
        )

        self.assertEqual(extracted['application_url'], '')
        self.assertEqual(extracted['telegram_contact'], 'https://t.me/example_jobs')

    def test_telegram_labeled_plain_text_application_link_is_detected(self):
        from .services.source_ingestion import basic_extract

        extracted = basic_extract(
            {
                'title': 'Training Fellowship',
                'url': 'https://t.me/channel/123',
                'text': 'Training Fellowship. Apply now: https://forms.example.org/submit.',
                'html': '',
            },
            self.source,
        )

        self.assertEqual(
            extracted['application_url'],
            'https://forms.example.org/submit',
        )

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

    def test_duplicate_is_enriched_with_later_verified_application_url(self):
        first, created = deduplicate_and_save_opportunity(
            self.source,
            {
                'title': 'Research Training Fellowship',
                'organization': 'Example Institute',
                'description': 'A research training fellowship for graduates.',
                'source_url': 'https://example.org/opportunities/fellowship-42',
                'application_method': 'source_only',
            },
        )
        duplicate, duplicate_created = deduplicate_and_save_opportunity(
            self.source,
            {
                'title': 'Research Training Fellowship',
                'organization': 'Example Institute',
                'description': 'A research training fellowship for graduates.',
                'source_url': 'https://example.org/opportunities/fellowship-42',
                'application_url': 'https://apply.example.org/fellowship-42',
                'application_method': 'online',
            },
        )

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.pk, first.pk)
        self.assertEqual(
            duplicate.application_url,
            'https://apply.example.org/fellowship-42',
        )
        self.assertEqual(duplicate.application_method, 'online')

    def test_unique_hash_race_merges_the_exact_matching_record(self):
        from .services import deduplication

        existing, _ = deduplication.deduplicate_and_save_opportunity(
            self.source,
            {
                'title': 'Race-safe Research Fellowship',
                'organization': 'Example Institute',
                'description': 'Research fellowship for graduates.',
                'source_url': 'https://example.org/race-safe-fellowship',
            },
        )
        existing.dedupe_hash = compute_dedupe_hash(
            'Race-safe Research Fellowship',
            'Example Institute',
            'https://apply.example.org/race-safe',
            None,
        )
        existing.save(update_fields=['dedupe_hash'])
        incoming = {
            'title': 'Race-safe Research Fellowship',
            'organization': 'Example Institute',
            'description': 'Research fellowship for graduates.',
            'source_url': 'https://example.org/race-safe-fellowship',
            'application_url': 'https://apply.example.org/race-safe',
            'application_method': 'online',
        }
        with (
            patch('opportunity_agent.services.deduplication.find_duplicate', return_value=None),
            patch.object(Opportunity.objects, 'get_or_create', side_effect=IntegrityError),
        ):
            result, created = deduplication.deduplicate_and_save_opportunity(
                self.source,
                incoming,
            )

        self.assertFalse(created)
        self.assertEqual(result.pk, existing.pk)
        self.assertEqual(result.application_url, 'https://apply.example.org/race-safe')
        self.assertEqual(
            Opportunity.objects.filter(
                dedupe_hash=existing.dedupe_hash,
            ).count(),
            1,
        )

    @patch('opportunity_agent.tasks._validate_public_source_url')
    @patch('opportunity_agent.tasks.fetch_public_source')
    @patch(
        'opportunity_agent.tasks.AIClient.extract_opportunity',
        side_effect=AIProviderError('All configured AI providers failed (google): HTTP 503'),
    )
    @patch('opportunity_agent.tasks._upsert_opportunity', return_value=(None, False))
    def test_source_scan_keeps_explicit_extraction_when_ai_is_unavailable(
        self,
        upsert,
        extract,
        fetch,
        validate_url,
    ):
        from .tasks import scan_sources_task

        source = Source.objects.get(pk=self.source.pk)
        fetch.side_effect = [
            (
                'https://example.com/jobs',
                '<a href="/jobs/research-fellowship">Research Fellowship</a>',
                'text/html',
            ),
            (
                'https://example.com/jobs/research-fellowship',
                '<article><h1>Research Fellowship</h1><p>'
                'A specific research fellowship for graduates. Applicants must '
                'submit a CV and research statement.</p>'
                '<a href="/apply/fellowship">Apply Now</a></article>',
                'text/html',
            ),
        ]

        result = scan_sources_task.run(limit=1, source_ids=[source.pk])

        self.assertEqual(result['successful'], 1)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(
            upsert.call_args.args[1]['application_url'],
            'https://example.com/apply/fellowship',
        )
        self.assertEqual(
            upsert.call_args.args[1]['application_methods'][0]['method'],
            'online',
        )
        self.assertEqual(upsert.call_args.args[1]['application_method'], 'online')

    def test_listing_review_command_defaults_to_dry_run_and_requires_backup_to_apply(self):
        import tempfile
        from pathlib import Path

        listing = Opportunity.objects.create(
            title='Research Scholarships',
            source_url='https://reliefweb.int/jobs?list=123',
            dedupe_hash='listing-review-command-test',
        )
        dry_run_output = StringIO()
        call_command('review_listing_opportunities', stdout=dry_run_output)
        listing.refresh_from_db()
        self.assertEqual(listing.status, 'active')
        self.assertIn('Dry-run only', dry_run_output.getvalue())
        with self.assertRaises(CommandError):
            call_command(
                'review_listing_opportunities',
                apply=True,
                stdout=StringIO(),
            )
        listing.refresh_from_db()
        self.assertEqual(listing.status, 'active')

        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / 'listing-backup.json'
            call_command(
                'review_listing_opportunities',
                apply=True,
                confirm='MARK LISTING OPPORTUNITIES FOR REVIEW',
                backup_file=str(backup),
                stdout=StringIO(),
            )
            listing.refresh_from_db()
            self.assertEqual(listing.status, 'needs_review')
            self.assertTrue(backup.is_file())
            self.assertIn('listing-review-command-test', backup.read_text(encoding='utf-8'))

    def test_application_route_audit_dry_run_does_not_change_opportunity(self):
        Opportunity.objects.filter(pk=self.opportunity.pk).update(
            source_url='https://reliefweb.int/jobs?list=123',
            status='needs_review',
        )
        opportunity = Opportunity.objects.create(
            source=self.source,
            title='Research Fellowship',
            source_url='https://example.org/fellowship',
            dedupe_hash='application-audit-dry-run-test',
        )
        body = (
            '<article><h1>Research Fellowship</h1><p>This research fellowship '
            'supports graduate researchers. Applicants must submit a CV and '
            'research statement before the application deadline.</p>'
            '<a href="https://apply.example.org/fellowship">Apply Now</a>'
            '<p>To apply by email, send your application to '
            'fellowships@example.org.</p></article>'
        )
        with patch(
            'opportunity_agent.management.commands.audit_application_routes.public_addresses',
            return_value={ipaddress.ip_address('93.184.216.34')},
        ), patch(
            'opportunity_agent.management.commands.audit_application_routes.fetch_public_source',
            return_value=(opportunity.source_url, body, 'text/html'),
        ) as fetch, CaptureQueriesContext(connection) as queries:
            output = StringIO()
            call_command('audit_application_routes', stdout=output)

        writes = [
            query['sql'] for query in queries
            if query['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE'))
        ]
        self.assertEqual(writes, [])
        opportunity.refresh_from_db()
        self.assertEqual(opportunity.application_method, 'source_only')
        self.assertEqual(opportunity.application_url, '')
        self.assertEqual(opportunity.application_methods, [])
        self.assertEqual(opportunity.application_instructions, '')
        self.assertEqual(opportunity.status, 'active')
        self.assertEqual(opportunity.source_url, 'https://example.org/fellowship')
        self.assertEqual(fetch.call_count, 1)
        self.assertIn('records checked=', output.getvalue())
        self.assertIn('Dry-run only; no database records were changed.', output.getvalue())
        self.assertIn('online:', output.getvalue())
        self.assertIn('email:', output.getvalue())

    def test_application_route_review_report_is_read_only_repeatable_and_preserves_saved_routes(self):
        import csv
        import tempfile
        from pathlib import Path

        Opportunity.objects.filter(pk=self.opportunity.pk).update(
            source_url='https://reliefweb.int/jobs?list=123',
            status='needs_review',
        )
        opportunity = Opportunity.objects.create(
            source=self.source,
            title='Research Fellowship',
            source_url='https://example.org/research-fellowship',
            application_method='online',
            application_url='https://verified.example.org/current',
            application_form_url='https://verified.example.org/form',
            dedupe_hash='application-route-read-only-report',
        )
        body = (
            '<article><h1>Research Fellowship at Example Institute</h1>'
            '<p>This named research fellowship supports graduate researchers. '
            'Applicants must submit a CV and research statement before the '
            'application deadline.</p>'
            '<a href="https://new.example.org/apply">Apply Now</a></article>'
        )
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / 'report-first.csv'
            second_path = Path(directory) / 'report-second.csv'
            with patch(
                'opportunity_agent.management.commands.audit_application_routes.public_addresses',
                return_value={ipaddress.ip_address('93.184.216.34')},
            ), patch(
                'opportunity_agent.management.commands.audit_application_routes.fetch_public_source',
                return_value=(opportunity.source_url, body, 'text/html'),
            ), CaptureQueriesContext(connection) as queries:
                first_output = StringIO()
                call_command(
                    'audit_application_routes',
                    report_csv=str(first_path),
                    stdout=first_output,
                )
                second_output = StringIO()
                call_command(
                    'audit_application_routes',
                    report_csv=str(second_path),
                    stdout=second_output,
                )

            writes = [
                query['sql'] for query in queries
                if query['sql'].lstrip().upper().startswith(
                    ('INSERT', 'UPDATE', 'DELETE', 'REPLACE')
                )
            ]
            self.assertEqual(writes, [])
            self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
            opportunity.refresh_from_db()
            self.assertEqual(opportunity.application_url, 'https://verified.example.org/current')
            self.assertEqual(opportunity.application_form_url, 'https://verified.example.org/form')
            self.assertEqual(opportunity.application_method, 'online')
            with first_path.open(encoding='utf-8', newline='') as stream:
                row = next(
                    row for row in csv.DictReader(stream)
                    if row['opportunity_id'] == str(opportunity.pk)
                )
            self.assertEqual(row['classification'], 'verified current route')
            self.assertEqual(
                row['current_application_url'],
                row['proposed_application_url'],
            )
            self.assertEqual(
                row['current_application_form_url'],
                row['proposed_application_form_url'],
            )
            self.assertIn('https://new.example.org/apply', row['proposed_application_methods'])
            self.assertIn('Apply Now', row['evidence'])
            self.assertEqual(
                row['destination_check'],
                'Not independently checked; source evidence only.',
            )
            self.assertIn('Read-only CSV report written', first_output.getvalue())

    def test_application_route_review_report_marks_historical_route_as_expired_candidate(self):
        import csv
        import tempfile
        from datetime import datetime, timezone as datetime_timezone
        from pathlib import Path

        Opportunity.objects.filter(pk=self.opportunity.pk).update(
            source_url='https://reliefweb.int/jobs?list=123',
            status='needs_review',
        )
        opportunity = Opportunity.objects.create(
            source=self.source,
            title='OD Impact Challenge 2025',
            source_url='https://opportunitydesk.org/odic/',
            status='inactive',
            application_method='online',
            application_url='https://opd.to/ODIC2025apply',
            deadline=datetime(2025, 12, 8, tzinfo=datetime_timezone.utc),
            dedupe_hash='historical-odic-application-route',
        )
        body = (
            '<article><h1>OD Impact Challenge 2025</h1>'
            '<p>The ODIC 2025 challenge accepted applications until December 8, 2025. '
            'Applicants submitted an entry through the 2025 application portal.</p>'
            '<a href="https://opd.to/ODIC2025apply">Apply for ODIC 2025</a></article>'
        )
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'historical-report.csv'
            with patch(
                'opportunity_agent.management.commands.audit_application_routes.public_addresses',
                return_value={ipaddress.ip_address('93.184.216.34')},
            ), patch(
                'opportunity_agent.management.commands.audit_application_routes.fetch_public_source',
                return_value=(opportunity.source_url, body, 'text/html'),
            ):
                call_command(
                    'audit_application_routes',
                    report_csv=str(report),
                    stdout=StringIO(),
                )

            with report.open(encoding='utf-8', newline='') as stream:
                row = next(
                    row for row in csv.DictReader(stream)
                    if row['opportunity_id'] == str(opportunity.pk)
                )

        self.assertEqual(row['classification'], 'potentially expired route')
        self.assertEqual(row['current_status'], 'inactive')
        self.assertEqual(row['proposed_status'], 'needs_review')
        self.assertEqual(row['current_application_url'], 'https://opd.to/ODIC2025apply')
        self.assertEqual(row['proposed_application_url'], row['current_application_url'])
        self.assertNotIn('ODIC2025apply', row['proposed_application_methods'])
        self.assertIn('2025', row['evidence'])
        opportunity.refresh_from_db()
        self.assertEqual(opportunity.application_url, 'https://opd.to/ODIC2025apply')
        self.assertEqual(opportunity.status, 'inactive')

    def test_application_route_audit_keeps_genuine_detail_without_verified_route_active(self):
        import csv
        import tempfile
        from pathlib import Path

        Opportunity.objects.filter(pk=self.opportunity.pk).update(
            source_url='https://reliefweb.int/jobs?list=123',
            status='needs_review',
        )
        opportunity = Opportunity.objects.create(
            source=self.source,
            title='Research Fellowship at Example Institute',
            source_url='https://example.org/research-fellowship',
            application_method='source_only',
            application_url='https://example.org/saved-application-route',
            dedupe_hash='detail-without-verified-application-route',
        )
        body = (
            '<article><h1>Research Fellowship at Example Institute</h1>'
            '<p>This named research fellowship supports graduate researchers. '
            'Eligibility requirements include a relevant degree and research '
            'experience. The fellowship provides a stipend.</p></article>'
        )
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'detail-without-route.csv'
            with patch(
                'opportunity_agent.management.commands.audit_application_routes.public_addresses',
                return_value={ipaddress.ip_address('93.184.216.34')},
            ), patch(
                'opportunity_agent.management.commands.audit_application_routes.fetch_public_source',
                return_value=(opportunity.source_url, body, 'text/html'),
            ):
                call_command(
                    'audit_application_routes',
                    report_csv=str(report),
                    stdout=StringIO(),
                )

            with report.open(encoding='utf-8', newline='') as stream:
                row = next(
                    row for row in csv.DictReader(stream)
                    if row['opportunity_id'] == str(opportunity.pk)
                )

        self.assertEqual(row['classification'], 'genuine detail; no route verified')
        self.assertEqual(row['proposed_fields'], '')
        self.assertEqual(row['proposed_status'], 'active')
        self.assertEqual(
            row['proposed_application_url'],
            'https://example.org/saved-application-route',
        )
        opportunity.refresh_from_db()
        self.assertEqual(opportunity.status, 'active')
        self.assertEqual(
            opportunity.application_url,
            'https://example.org/saved-application-route',
        )

    def test_https_route_probe_safely_follows_public_https_redirects(self):
        from opportunity_agent.management.commands.audit_application_routes import _https_check

        first = Mock(status_code=302, headers={'Location': 'https://apply.example.org/final'})
        second = Mock(status_code=200, headers={})
        with patch(
            'opportunity_agent.management.commands.audit_application_routes.public_addresses',
            return_value={ipaddress.ip_address('93.184.216.34')},
        ), patch(
            'opportunity_agent.management.commands.audit_application_routes.requests.get',
            side_effect=[first, second],
        ) as get, patch(
            'opportunity_agent.management.commands.audit_application_routes.validate_response_peer',
        ) as validate_peer:
            result = _https_check('http://apply.example.org/start?ref=role')

        self.assertIn('https://apply.example.org/final', result)
        self.assertIn('HTTP 200', result)
        self.assertEqual(get.call_count, 2)
        self.assertTrue(all(
            call.kwargs['allow_redirects'] is False
            for call in get.call_args_list
        ))
        self.assertEqual(validate_peer.call_count, 2)

    def test_application_route_audit_apply_updates_only_verified_route_fields(self):
        import tempfile
        from pathlib import Path

        Opportunity.objects.filter(pk=self.opportunity.pk).update(
            source_url='https://reliefweb.int/jobs?list=123',
            status='needs_review',
        )
        opportunity = Opportunity.objects.create(
            source=self.source,
            title='Research Fellowship',
            source_url='https://example.org/fellowship',
            dedupe_hash='application-audit-apply-test',
        )
        body = (
            '<article><h1>Research Fellowship</h1><p>This research fellowship '
            'supports graduate researchers. Applicants must submit a CV and '
            'research statement before the application deadline.</p>'
            '<a href="https://apply.example.org/fellowship">Apply Now</a>'
            '</article>'
        )
        fetch_patch = patch(
            'opportunity_agent.management.commands.audit_application_routes.fetch_public_source',
            return_value=(opportunity.source_url, body, 'text/html'),
        )
        address_patch = patch(
            'opportunity_agent.management.commands.audit_application_routes.public_addresses',
            return_value={ipaddress.ip_address('93.184.216.34')},
        )
        fetch_patch.start()
        address_patch.start()
        self.addCleanup(fetch_patch.stop)
        self.addCleanup(address_patch.stop)

        with self.assertRaises(CommandError):
            call_command(
                'audit_application_routes',
                apply=True,
                stdout=StringIO(),
            )
        opportunity.refresh_from_db()
        self.assertEqual(opportunity.application_method, 'source_only')

        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / 'route-audit-backup.json'
            call_command(
                'audit_application_routes',
                apply=True,
                backup_file=str(backup),
                confirm='APPLY VERIFIED ROUTE REPAIRS',
                stdout=StringIO(),
            )
            opportunity.refresh_from_db()
            self.assertEqual(opportunity.application_method, 'online')
            self.assertEqual(
                opportunity.application_url,
                'https://apply.example.org/fellowship',
            )
            self.assertTrue(opportunity.application_methods)
            self.assertIn('Apply Now', opportunity.application_instructions)
            self.assertEqual(opportunity.source_url, 'https://example.org/fellowship')
            self.assertEqual(opportunity.title, 'Research Fellowship')
            self.assertTrue(backup.is_file())
            self.assertIn(str(opportunity.pk), backup.read_text(encoding='utf-8'))
            first_routes = list(opportunity.application_methods)
            first_instructions = opportunity.application_instructions
            call_command(
                'audit_application_routes',
                apply=True,
                backup_file=str(Path(directory) / 'route-audit-rerun-backup.json'),
                confirm='APPLY VERIFIED ROUTE REPAIRS',
                stdout=StringIO(),
            )
            opportunity.refresh_from_db()
            self.assertEqual(opportunity.application_methods, first_routes)
            self.assertEqual(opportunity.application_instructions, first_instructions)

    def test_application_route_audit_preserves_existing_verified_data(self):
        import tempfile
        from pathlib import Path

        Opportunity.objects.filter(pk=self.opportunity.pk).update(
            source_url='https://reliefweb.int/jobs?list=123',
            status='needs_review',
        )
        existing_route = {
            'method': 'email',
            'destination': 'verified@example.org',
            'instructions': 'Previously verified application email.',
        }
        opportunity = Opportunity.objects.create(
            source=self.source,
            title='Research Fellowship',
            source_url='https://example.org/fellowship',
            application_method='online',
            application_url='https://verified.example.org/apply',
            application_methods=[existing_route],
            application_instructions='Previously verified application email.',
            dedupe_hash='application-audit-preserve-test',
        )
        body = (
            '<article><h1>Research Fellowship</h1><p>This research fellowship '
            'supports graduate researchers. Applicants must submit a CV and '
            'research statement before the application deadline.</p>'
            '<a href="https://new.example.org/apply">Apply Now</a></article>'
        )
        with patch(
            'opportunity_agent.management.commands.audit_application_routes.public_addresses',
            return_value={ipaddress.ip_address('93.184.216.34')},
        ), patch(
            'opportunity_agent.management.commands.audit_application_routes.fetch_public_source',
            return_value=(opportunity.source_url, body, 'text/html'),
        ), tempfile.TemporaryDirectory() as directory:
            call_command(
                'audit_application_routes',
                apply=True,
                backup_file=str(Path(directory) / 'preserve-backup.json'),
                confirm='APPLY VERIFIED ROUTE REPAIRS',
                stdout=StringIO(),
            )

        opportunity.refresh_from_db()
        self.assertEqual(opportunity.application_method, 'online')
        self.assertEqual(
            opportunity.application_url,
            'https://verified.example.org/apply',
        )
        self.assertIn(existing_route, opportunity.application_methods)
        self.assertTrue(any(
            route.get('destination') == 'https://new.example.org/apply'
            for route in opportunity.application_methods
        ))
        self.assertIn(
            'Previously verified application email.',
            opportunity.application_instructions,
        )

    def test_application_route_audit_reports_unreachable_pages_without_changes(self):
        Opportunity.objects.filter(pk=self.opportunity.pk).update(
            source_url='https://reliefweb.int/jobs?list=123',
            status='needs_review',
        )
        opportunity = Opportunity.objects.create(
            source=self.source,
            title='Unreachable Fellowship',
            source_url='https://unreachable.example.org/fellowship',
            dedupe_hash='application-audit-unreachable-test',
        )
        missing = Opportunity.objects.create(
            source=self.source,
            title='Missing Source Fellowship',
            dedupe_hash='application-audit-missing-source-test',
        )
        with patch(
            'opportunity_agent.management.commands.audit_application_routes.public_addresses',
            return_value={ipaddress.ip_address('93.184.216.34')},
        ), patch(
            'opportunity_agent.management.commands.audit_application_routes.fetch_public_source',
            side_effect=requests.ConnectionError('source unavailable'),
        ):
            output = StringIO()
            call_command('audit_application_routes', stdout=output)

        opportunity.refresh_from_db()
        self.assertEqual(opportunity.application_method, 'source_only')
        self.assertEqual(opportunity.application_methods, [])
        self.assertEqual(opportunity.status, 'active')
        missing.refresh_from_db()
        self.assertEqual(missing.application_method, 'source_only')
        self.assertEqual(missing.status, 'active')
        self.assertIn('manual review', output.getvalue())
        self.assertIn('errors=2', output.getvalue())

    def test_application_route_audit_flags_phone_only_instructions_for_review(self):
        Opportunity.objects.filter(pk=self.opportunity.pk).update(
            source_url='https://reliefweb.int/jobs?list=123',
            status='needs_review',
        )
        opportunity = Opportunity.objects.create(
            source=self.source,
            title='Phone Application Fellowship',
            source_url='https://example.org/phone-fellowship',
            dedupe_hash='application-audit-phone-only-test',
        )
        body = (
            '<article><h1>Phone Application Fellowship</h1><p>This fellowship '
            'is available to experienced researchers. To apply by phone, call '
            '+1 212 555 1234 before the application deadline. Applicants should '
            'prepare a CV and research statement.</p>'
            '<a href="tel:+12125551234">Application phone</a></article>'
        )
        with patch(
            'opportunity_agent.management.commands.audit_application_routes.public_addresses',
            return_value={ipaddress.ip_address('93.184.216.34')},
        ), patch(
            'opportunity_agent.management.commands.audit_application_routes.fetch_public_source',
            return_value=(opportunity.source_url, body, 'text/html'),
        ):
            output = StringIO()
            call_command('audit_application_routes', stdout=output)

        opportunity.refresh_from_db()
        self.assertEqual(opportunity.status, 'active')
        self.assertEqual(opportunity.application_method, 'source_only')
        self.assertIn('phone-only application instructions', output.getvalue().lower())
        self.assertIn('phone:', output.getvalue())

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

    def test_five_profiles_receive_independent_career_interest_matches(self):
        profiles = {
            'management': {
                'skills': ['management', 'project coordination'],
                'preferred_opportunity_types': ['job'],
            },
            'software': {
                'skills': ['Python', 'software development'],
                'preferred_opportunity_types': ['job'],
            },
            'scholarship': {
                'skills': ['student', 'scholarship'],
                'preferred_opportunity_types': ['scholarship', 'internship'],
            },
            'internship': {
                'skills': ['web development', 'internship'],
                'preferred_opportunity_types': ['internship'],
            },
            'finance': {
                'skills': ['finance', 'accounting'],
                'preferred_opportunity_types': ['job'],
            },
        }
        users = {}
        for interest, values in profiles.items():
            user = User.objects.create_user(
                username=f'match-{interest}',
                email=f'match-{interest}@example.com',
                password='StrongPass123!',
            )
            profile = UserProfile.objects.create(
                user=user,
                current_country='Ethiopia',
                worldwide_preference=True,
                minimum_ai_match_score=0,
                **values,
            )
            users[interest] = (user, profile)

        opportunities = {
            'management': Opportunity.objects.create(
                title='Project Coordinator',
                opportunity_type='job',
                description='Coordinate project operations and provide administrative leadership.',
                dedupe_hash='career-match-management',
            ),
            'software': Opportunity.objects.create(
                title='Python Software Developer',
                opportunity_type='job',
                description='Build web software with Python and Django.',
                dedupe_hash='career-match-software',
            ),
            'scholarship': Opportunity.objects.create(
                title='Graduate Research Scholarship',
                opportunity_type='scholarship',
                description='University scholarship supporting graduate research students.',
                dedupe_hash='career-match-scholarship',
            ),
            'internship': Opportunity.objects.create(
                title='Web Development Internship',
                opportunity_type='internship',
                description='Internship building web applications with Python.',
                dedupe_hash='career-match-internship',
            ),
            'finance': Opportunity.objects.create(
                title='Finance and Accounting Officer',
                opportunity_type='job',
                description='Financial accounting, audit, and budget reporting.',
                dedupe_hash='career-match-finance',
            ),
        }
        results_by_user = {}
        for interest, (user, profile) in users.items():
            match_map = refresh_user_matches(profile, opportunities.values())
            results_by_user[interest] = {
                label: match_map[opportunity.pk].score
                for label, opportunity in opportunities.items()
            }
            self.assertEqual(
                Match.objects.filter(user=user).count(),
                len(opportunities),
            )

        for interest in profiles:
            self.assertGreater(
                results_by_user[interest][interest],
                max(
                    score
                    for label, score in results_by_user[interest].items()
                    if label != interest
                ),
                msg=f'{interest} profile should rank its matching opportunity highest',
            )
        self.assertEqual(
            Match.objects.filter(
                user=users['software'][0],
                opportunity=opportunities['internship'],
            ).count(),
            1,
            'A relevant opportunity can be shared by overlapping profiles.',
        )
        other_user_match = Match.objects.get(
            user=users['finance'][0],
            opportunity=opportunities['finance'],
        )
        original_other_score = other_user_match.score
        management_profile = users['management'][1]
        management_profile.skills = ['finance', 'accounting']
        management_profile.save(update_fields=['skills'])
        refreshed = refresh_user_matches(management_profile, opportunities.values())
        self.assertGreater(
            refreshed[opportunities['finance'].pk].score,
            refreshed[opportunities['management'].pk].score,
        )
        other_user_match.refresh_from_db()
        self.assertEqual(other_user_match.score, original_other_score)

    def test_collected_opportunity_is_matched_for_existing_active_profiles(self):
        from .tasks import _process_telegram_candidate

        self.profile.minimum_ai_match_score = 0
        self.profile.save(update_fields=['minimum_ai_match_score'])
        candidate = {
            'title': 'Python Software Development Internship',
            'url': 'https://example.org/python-internship',
            'source_landing_url': self.source.url,
            'text': (
                'Python software development internship for web applications. '
                'Applicants should have programming experience.'
            ),
            'html': '',
        }

        opportunity, created = _process_telegram_candidate(
            self.source,
            candidate,
            Mock(extract_opportunity=Mock(return_value={})),
        )

        self.assertTrue(created)
        self.assertTrue(
            Match.objects.filter(
                user=self.user,
                opportunity=opportunity,
            ).exists(),
        )

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

    def test_unrecognized_explicit_requirement_requires_review(self):
        result = compute_match_score(
            {
                'current_country': 'Ethiopia',
                'worldwide_preference': True,
                'minimum_ai_match_score': 0,
            },
            {
                'requirements': (
                    'Applicants need to hold a current professional license.'
                ),
            },
        )

        self.assertFalse(result['eligible'])
        self.assertEqual(result['eligibility_status'], 'requirements_unconfirmed')
        self.assertEqual(result['recommended_action'], 'review')

    def test_structured_education_requirement_conflict_blocks_eligibility(self):
        result = compute_match_score(
            {
                'degree': 'BSc',
                'education': 'Bachelor of Biology',
                'skills': ['research'],
                'minimum_ai_match_score': 0,
            },
            {
                'education_requirements': 'Applicants must hold a PhD.',
                'skills': ['research'],
                'remote_worldwide': True,
                'opportunity_type': 'fellowship',
            },
        )

        self.assertFalse(result['eligible'])
        self.assertEqual(result['eligibility_status'], 'requirements_conflict')

    def test_structured_education_requirement_matching_profile_remains_eligible(self):
        result = compute_match_score(
            {
                'degree': 'PhD',
                'education': 'Doctor of Philosophy',
                'current_country': 'Ethiopia',
                'worldwide_preference': True,
                'minimum_ai_match_score': 0,
            },
            {
                'education_requirements': 'Applicants must hold a PhD.',
                'remote_worldwide': True,
                'opportunity_type': 'fellowship',
            },
        )

        self.assertTrue(result['eligible'])
        self.assertEqual(result['requirements_status'], 'clear')

    def test_structured_education_requirement_without_profile_data_requires_review(self):
        result = compute_match_score(
            {
                'skills': ['research'],
                'minimum_ai_match_score': 0,
            },
            {
                'qualifications': 'A Master degree is required.',
                'skills': ['research'],
                'remote_worldwide': True,
                'opportunity_type': 'fellowship',
            },
        )

        self.assertFalse(result['eligible'])
        self.assertEqual(result['eligibility_status'], 'requirements_unconfirmed')

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
        self.assertEqual(
            collect_channel.call_args.kwargs['min_message_id'],
            0,
        )

    @patch(
        'opportunity_agent.tasks._upsert_opportunity',
        return_value=(None, False),
    )
    @patch(
        'opportunity_agent.tasks.AIClient.extract_opportunity',
        return_value={},
    )
    @patch(
        'opportunity_agent.services.telegram_collector.collect_public_channel',
        new_callable=AsyncMock,
    )
    def test_telegram_scanning_resumes_after_last_message_and_extracts_labeled_link(
        self,
        collect_channel,
        extract,
        upsert,
    ):
        from .tasks import scan_telegram_sources_task

        source = TelegramSource.objects.create(
            name='Public opportunities channel',
            channel_url='https://t.me/public_opportunities',
            last_message_id=100,
        )
        collect_channel.return_value = [{
            'title': 'Research Fellowship',
            'url': 'https://t.me/public_opportunities/101',
            'text': 'Research Fellowship. Apply Now: https://apply.example.org/form',
            'message_id': 101,
        }]

        result = scan_telegram_sources_task.run(
            limit=1,
            telegram_source_ids=[source.pk],
        )

        source.refresh_from_db()
        self.assertEqual(result['successful'], 1)
        collect_channel.assert_awaited_once_with(
            source,
            limit=50,
            min_message_id=100,
        )
        self.assertEqual(source.last_message_id, 101)
        self.assertEqual(
            upsert.call_args.args[1]['application_url'],
            'https://apply.example.org/form',
        )

    @patch(
        'opportunity_agent.tasks.AIClient.extract_opportunity',
        return_value={},
    )
    @patch(
        'opportunity_agent.tasks._upsert_opportunity',
        side_effect=RuntimeError('temporary extraction failure'),
    )
    @patch(
        'opportunity_agent.services.telegram_collector.collect_public_channel',
        new_callable=AsyncMock,
    )
    def test_telegram_candidate_failure_persists_retry_before_advancing_cursor(
        self,
        collect_channel,
        upsert,
        extract,
    ):
        from .tasks import scan_telegram_sources_task

        source = TelegramSource.objects.create(
            name='Retry channel',
            channel_url='https://t.me/retry_channel',
        )
        collect_channel.return_value = [{
            'title': 'Research fellowship',
            'url': 'https://t.me/retry_channel/201',
            'text': 'Research fellowship with explicit eligibility details.',
            'message_id': 201,
        }]

        result = scan_telegram_sources_task.run(
            limit=1,
            telegram_source_ids=[source.pk],
        )

        source.refresh_from_db()
        retry = TelegramMessageRetry.objects.get(source=source, message_id=201)
        self.assertEqual(retry.status, 'pending')
        self.assertEqual(retry.retry_count, 1)
        self.assertGreater(retry.next_retry_at, timezone.now())
        self.assertEqual(retry.message_text, collect_channel.return_value[0]['text'])
        self.assertEqual(source.last_message_id, 201)
        self.assertEqual(result['candidate_errors'][0]['message_id'], 201)

    @patch(
        'opportunity_agent.tasks.AIClient.extract_opportunity',
        return_value={},
    )
    @patch(
        'opportunity_agent.tasks._upsert_opportunity',
        side_effect=[RuntimeError('first post is broken'), (None, False)],
    )
    @patch(
        'opportunity_agent.services.telegram_collector.collect_public_channel',
        new_callable=AsyncMock,
    )
    def test_failed_telegram_post_does_not_block_later_posts(
        self,
        collect_channel,
        upsert,
        extract,
    ):
        from .tasks import scan_telegram_sources_task

        source = TelegramSource.objects.create(
            name='Continuing channel',
            channel_url='https://t.me/continuing_channel',
        )
        collect_channel.return_value = [
            {
                'title': 'Broken fellowship',
                'url': 'https://t.me/continuing_channel/301',
                'text': 'Broken fellowship post.',
                'message_id': 301,
            },
            {
                'title': 'Working fellowship',
                'url': 'https://t.me/continuing_channel/302',
                'text': 'Working fellowship post.',
                'message_id': 302,
            },
        ]

        scan_telegram_sources_task.run(
            limit=1,
            telegram_source_ids=[source.pk],
        )

        source.refresh_from_db()
        self.assertEqual(source.last_message_id, 302)
        self.assertTrue(
            TelegramMessageRetry.objects.filter(
                source=source,
                message_id=301,
                status='pending',
            ).exists(),
        )
        self.assertEqual(upsert.call_count, 2)

    @patch(
        'opportunity_agent.tasks.AIClient.extract_opportunity',
        return_value={},
    )
    @patch(
        'opportunity_agent.services.telegram_collector.collect_public_channel',
        new_callable=AsyncMock,
        return_value=[],
    )
    def test_due_telegram_retry_success_resolves_without_duplicate_opportunity(
        self,
        collect_channel,
        extract,
    ):
        from .tasks import scan_telegram_sources_task

        source = TelegramSource.objects.create(
            name='Retry success channel',
            channel_url='https://t.me/retry_success',
        )
        text = 'Research Fellowship: apply by sending the completed form.'
        existing, created = deduplicate_and_save_opportunity(
            source,
            {
                'title': 'Research Fellowship',
                'description': text,
                'source_url': 'https://t.me/retry_success/401',
            },
            raw_content=text,
        )
        self.assertTrue(created)
        retry = TelegramMessageRetry.objects.create(
            source=source,
            channel_identifier='retry_success',
            message_id=401,
            message_text=text,
            retry_count=1,
            next_retry_at=timezone.now() - timedelta(seconds=1),
        )

        scan_telegram_sources_task.run(
            limit=1,
            telegram_source_ids=[source.pk],
        )

        retry.refresh_from_db()
        self.assertEqual(retry.status, 'resolved')
        self.assertIsNone(retry.next_retry_at)
        self.assertIsNotNone(retry.resolved_at)
        self.assertEqual(
            Opportunity.objects.filter(telegram_source=source).count(),
            1,
        )
        self.assertEqual(existing.pk, Opportunity.objects.get(telegram_source=source).pk)

    @override_settings(
        TELEGRAM_POST_RETRY_MAX_ATTEMPTS=4,
        TELEGRAM_POST_RETRY_BASE_SECONDS=10,
        TELEGRAM_POST_RETRY_MAX_SECONDS=100,
    )
    @patch(
        'opportunity_agent.tasks.AIClient.extract_opportunity',
        return_value={},
    )
    @patch(
        'opportunity_agent.tasks._upsert_opportunity',
        side_effect=RuntimeError('still temporarily broken'),
    )
    @patch(
        'opportunity_agent.services.telegram_collector.collect_public_channel',
        new_callable=AsyncMock,
        return_value=[],
    )
    def test_telegram_retry_failure_uses_exponential_backoff(
        self,
        collect_channel,
        upsert,
        extract,
    ):
        from .tasks import scan_telegram_sources_task

        source = TelegramSource.objects.create(
            name='Backoff channel',
            channel_url='https://t.me/backoff_channel',
        )
        retry = TelegramMessageRetry.objects.create(
            source=source,
            channel_identifier='backoff_channel',
            message_id=501,
            message_text='Research post with requirements.',
            retry_count=1,
            next_retry_at=timezone.now() - timedelta(seconds=1),
        )
        before = timezone.now()

        scan_telegram_sources_task.run(
            limit=1,
            telegram_source_ids=[source.pk],
        )

        retry.refresh_from_db()
        self.assertEqual(retry.retry_count, 2)
        self.assertEqual(retry.status, 'pending')
        self.assertGreaterEqual(
            (retry.next_retry_at - before).total_seconds(),
            19,
        )
        self.assertLessEqual(
            (retry.next_retry_at - before).total_seconds(),
            21,
        )

    @override_settings(
        TELEGRAM_POST_RETRY_MAX_ATTEMPTS=3,
        TELEGRAM_POST_RETRY_BASE_SECONDS=10,
        TELEGRAM_POST_RETRY_MAX_SECONDS=100,
    )
    @patch(
        'opportunity_agent.tasks.AIClient.extract_opportunity',
        return_value={},
    )
    @patch(
        'opportunity_agent.tasks._upsert_opportunity',
        side_effect=RuntimeError('permanent processing failure'),
    )
    @patch(
        'opportunity_agent.services.telegram_collector.collect_public_channel',
        new_callable=AsyncMock,
        return_value=[],
    )
    def test_telegram_retry_moves_to_dead_letter_at_max_attempts(
        self,
        collect_channel,
        upsert,
        extract,
    ):
        from .tasks import scan_telegram_sources_task

        source = TelegramSource.objects.create(
            name='Dead letter channel',
            channel_url='https://t.me/dead_letter_channel',
        )
        retry = TelegramMessageRetry.objects.create(
            source=source,
            channel_identifier='dead_letter_channel',
            message_id=601,
            message_text='Research post requiring manual review.',
            retry_count=2,
            next_retry_at=timezone.now() - timedelta(seconds=1),
        )

        scan_telegram_sources_task.run(
            limit=1,
            telegram_source_ids=[source.pk],
        )

        retry.refresh_from_db()
        self.assertEqual(retry.retry_count, 3)
        self.assertEqual(retry.status, 'dead_letter')
        self.assertIsNone(retry.next_retry_at)
        self.assertIn('permanent processing failure', retry.last_error)

    def test_legacy_telegram_scanner_is_disabled_in_favor_of_canonical_task(self):
        from .services.telegram_sources import collect_telegram_source
        from .tasks import scan_telegram_sources_task

        with self.assertRaisesRegex(RuntimeError, 'canonical Celery scanner'):
            collect_telegram_source(
                TelegramSource(
                    name='Legacy channel',
                    channel_url='https://t.me/legacy_channel',
                ),
            )
        self.assertEqual(scan_telegram_sources_task.name, 'opportunity_agent.tasks.scan_telegram_sources_task')

    def test_telegram_notification_errors_do_not_expose_bot_token(self):
        from .services.telegram import TelegramNotifier

        token = '123456:secret-telegram-token'
        destination = TelegramDestination.objects.create(
            name='Applied notifications',
            chat_id='-100123',
            type='applied',
        )
        error = requests.ConnectionError(
            f'Connection failed for https://api.telegram.org/bot{token}/sendMessage',
        )
        notifier = TelegramNotifier(bot_token=token)

        with patch(
            'opportunity_agent.services.telegram.requests.post',
            side_effect=error,
        ), patch('opportunity_agent.services.telegram.time.sleep'), self.assertLogs(
            'opportunity_agent.services.telegram',
            level='ERROR',
        ) as captured:
            result = notifier.send_to_enabled_destinations('applied', 'submitted')

        audit = AuditLog.objects.get(
            action='telegram_notification_failed',
            target=str(destination.pk),
        )
        for value in (
            result[0]['error'],
            str(audit.details),
            '\n'.join(captured.output),
        ):
            self.assertNotIn(token, value)

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
    @patch('opportunity_agent.services.public_http.socket.getaddrinfo')
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
            params=None,
            allow_redirects=False,
            stream=True,
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
    @patch('opportunity_agent.services.public_http.socket.getaddrinfo')
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
    @patch('opportunity_agent.services.public_http.socket.getaddrinfo')
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

    @override_settings(DISCOVERY_MAX_PER_CYCLE=1, DISCOVERY_PAGE_SIZE=2)
    @patch(
        'opportunity_agent.services.source_discovery._inspect_candidate',
        side_effect=lambda name, url: {
            'name': name,
            'url': url,
            'source_type': 'job_site',
            'country': 'Worldwide',
            'opportunity_types': ['job'],
            'trust_score': 0.7,
            'enabled': True,
            'scan_frequency': 'weekly',
            'notes': '',
            'auto_discovered': True,
        },
    )
    @patch('opportunity_agent.services.source_discovery.get_public_response')
    @patch('opportunity_agent.services.public_http.socket.getaddrinfo')
    def test_discovery_rotates_queries_and_preserves_unprocessed_page_results(
        self,
        getaddrinfo,
        get_response,
        inspect_candidate,
    ):
        from .models import SystemSetting
        from .services.source_discovery import discover_public_sources

        static_candidates = patch(
            'opportunity_agent.services.source_discovery.PUBLIC_SOURCE_CANDIDATES',
            [],
        )
        getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
        ]
        def query_response(*args, **kwargs):
            response = Mock()
            response.text = (
                '<a class="result__a" href="https://jobs.example.org/one">Job One</a>'
                '<a class="result__a" href="https://jobs.example.org/two">Job Two</a>'
                if kwargs['params']['q'] != '"internship"'
                else '<a class="result__a" href="https://jobs.example.org/three">Job Three</a>'
                '<a class="result__a" href="https://jobs.example.org/four">Job Four</a>'
            )
            response.close.return_value = None
            return response

        get_response.side_effect = query_response

        with static_candidates:
            first = discover_public_sources(['jobs', 'scholarship', 'fellowship', 'internship'])
            state = json.loads(SystemSetting.objects.get(
                key='public_source_discovery_state',
            ).value)
            second = discover_public_sources(['jobs', 'scholarship', 'fellowship', 'internship'])
            updated_state = json.loads(SystemSetting.objects.get(
                key='public_source_discovery_state',
            ).value)

        self.assertLessEqual(len(first), 1)
        self.assertLessEqual(len(second), 1)
        self.assertEqual(get_response.call_args_list[0].kwargs['params']['s'], 0)
        self.assertEqual(state['offsets']['0'], 1)
        self.assertEqual(get_response.call_args_list[1].kwargs['params']['q'], '"internship"')
        self.assertEqual(updated_state['query_index'], 0)
        self.assertEqual(updated_state['offsets']['1'], 1)
        self.assertEqual(inspect_candidate.call_count, 2)

    @patch('opportunity_agent.services.source_discovery._fetch_public_page')
    @patch('opportunity_agent.services.public_http.socket.getaddrinfo')
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

    @patch(
        'opportunity_agent.tasks.AIClient.generate_cover_letter',
        return_value='Reviewed cover letter',
    )
    def test_application_queue_calculates_match_before_preparing(self, generate_cover_letter):
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
            *[
                {
                    'url': f'https://example.com/apply/step-{index}',
                    'text': f'Application section {index}',
                    'fields': 0,
                    'buttons': ['Continue'],
                }
                for index in range(1, 26)
            ],
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
        self.assertEqual(application.result_url, states[-1]['url'])

    @override_settings(APPLICATION_WORKFLOW_BUDGET_SECONDS=1)
    def test_expired_application_workflow_is_routed_to_review(self):
        from .services.provider_adapters import PlaywrightConfiguredAdapter

        application = Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='pending',
            workflow_state={
                'workflow_started_at': (
                    timezone.now() - timedelta(seconds=2)
                ).isoformat(),
            },
        )
        adapter = PlaywrightConfiguredAdapter()

        result = adapter._run_dynamic_workflow(application, None, None)

        application.refresh_from_db()
        self.assertFalse(result)
        self.assertEqual(application.status, 'needs_review')
        self.assertEqual(application.workflow_state['outcome'], 'needs_review')
        self.assertIn('time budget expired', application.error_message)

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
        self.assertIn(Match.objects.get(
            user=self.user,
            opportunity=deadline_opportunity,
        ), response.context['matches'])
        self.assertEqual(
            {match.user_id for match in response.context['matches']},
            {self.user.pk},
        )
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

    def test_new_users_match_existing_opportunities_and_incomplete_profiles_get_fallback(self):
        interested_user = User.objects.create_user(
            username='new-software-user',
            email='new-software@example.com',
            password='StrongPass123!',
        )
        interested_user.groups.add(
            interested_user.groups.model.objects.get(name='USER'),
        )
        interested_profile = UserProfile.objects.create(
            user=interested_user,
            skills=['Python', 'software development'],
            preferred_opportunity_types=['job'],
            minimum_ai_match_score=0,
        )
        self.client.force_login(interested_user)
        response = self.client.get('/dashboard/')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Match.objects.filter(
            user=interested_user,
            opportunity=self.opportunity,
        ).exists())
        self.assertEqual(
            {match.user_id for match in response.context['matches']},
            {interested_user.pk},
        )

        incomplete_user = User.objects.create_user(
            username='new-incomplete-user',
            email='new-incomplete@example.com',
            password='StrongPass123!',
        )
        incomplete_user.groups.add(
            incomplete_user.groups.model.objects.get(name='USER'),
        )
        incomplete_profile = UserProfile.objects.create(user=incomplete_user)
        self.client.force_login(incomplete_user)
        fallback_response = self.client.get('/dashboard/')

        self.assertEqual(fallback_response.status_code, 200)
        self.assertEqual(fallback_response.context['profile'], incomplete_profile)
        self.assertEqual(fallback_response.context['matches'], [])
        self.assertTrue(fallback_response.context['fallback_opportunities'])
        self.assertContains(fallback_response, 'No recommendations meet your current minimum score')

    def test_saved_opportunities_are_user_scoped_and_require_authentication(self):
        saved_opportunity = Opportunity.objects.create(
            title='Privately saved opportunity',
            description='A public opportunity saved to an individual user list.',
            dedupe_hash='user-private-saved-opportunity',
        )
        anonymous_response = self.client.post(
            f'/opportunities/{saved_opportunity.pk}/save/',
            {'action': 'save'},
        )
        self.assertEqual(anonymous_response.status_code, 302)
        self.assertFalse(Match.objects.filter(
            opportunity=saved_opportunity,
            is_saved=True,
        ).exists())

        self.client.force_login(self.user)
        saved_response = self.client.post(
            f'/opportunities/{saved_opportunity.pk}/save/',
            {'action': 'save'},
        )
        self.assertEqual(saved_response.status_code, 302)
        self.assertTrue(Match.objects.get(
            user=self.user,
            opportunity=saved_opportunity,
        ).is_saved)

        other_user = User.objects.create_user(
            username='saved-list-other-user',
            email='saved-list-other@example.com',
            password='StrongPass123!',
        )
        other_user.groups.add(other_user.groups.model.objects.get(name='USER'))
        self.client.force_login(other_user)
        other_dashboard = self.client.get('/dashboard/')

        self.assertEqual(other_dashboard.status_code, 200)
        self.assertEqual(other_dashboard.context['saved_opportunities'], [])
        self.assertFalse(
            Match.objects.filter(
                user=other_user,
                opportunity=saved_opportunity,
                is_saved=True,
            ).exists(),
        )

    def test_authenticated_opportunity_search_ranks_and_filters_by_own_match_score(self):
        management_opportunity = Opportunity.objects.create(
            title='Project Management Coordinator',
            opportunity_type='job',
            description='Lead project coordination and administration.',
            dedupe_hash='personalized-search-management',
        )
        unrelated_opportunity = Opportunity.objects.create(
            title='Project Software Developer',
            opportunity_type='job',
            description='Develop software applications using Python.',
            dedupe_hash='personalized-search-software',
        )
        self.profile.skills = ['management', 'project coordination']
        self.profile.education = ''
        self.profile.degree = ''
        self.profile.minimum_ai_match_score = 0
        self.profile.save(update_fields=[
            'skills',
            'education',
            'degree',
            'minimum_ai_match_score',
        ])
        self.client.force_login(self.user)

        response = self.client.get('/opportunities/', {
            'q': 'Project',
            'minimum_score': '45',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [opportunity.pk for opportunity in response.context['opportunities']],
            [management_opportunity.pk],
            [
                (opportunity.pk, opportunity.user_match.score)
                for opportunity in response.context['opportunities']
            ],
        )
        self.assertGreater(
            response.context['opportunities'][0].user_match.score,
            compute_match_score(
                profile_match_data(self.profile),
                opportunity_match_data(unrelated_opportunity),
            )['score'],
        )
        self.assertContains(response, 'Your match:')

    def test_dashboard_and_detail_show_legacy_verified_route_and_instructions(self):
        self.opportunity.application_url = ''
        self.opportunity.application_method = 'email'
        self.opportunity.contact_email = 'applications@example.org'
        self.opportunity.application_instructions = (
            'Email your CV and application statement to this address.'
        )
        self.opportunity.save(update_fields=[
            'application_url',
            'application_method',
            'contact_email',
            'application_instructions',
        ])
        Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='queued',
        )
        self.client.force_login(self.user)

        dashboard = self.client.get('/dashboard/')
        detail = self.client.get(f'/opportunities/{self.opportunity.pk}/')

        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, 'Verified application email')
        self.assertContains(dashboard, 'href="mailto:applications@example.org"')
        self.assertContains(dashboard, 'applications@example.org')
        self.assertContains(dashboard, 'Email your CV and application statement')
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'Verified application email')
        self.assertContains(detail, 'href="mailto:applications@example.org"')
        self.assertContains(detail, 'applications@example.org')
        self.assertContains(detail, 'Email your CV and application statement')

    def test_verified_email_and_online_routes_render_as_links(self):
        self.opportunity.application_methods = [
            {
                'method': 'email',
                'destination': 'applications@example.org',
                'instructions': 'Send your CV and application statement.',
            },
            {
                'method': 'online',
                'destination': 'https://apply.example.org/role',
                'instructions': 'Complete the official form.',
            },
        ]
        self.opportunity.application_method = 'online'
        self.opportunity.application_url = 'https://apply.example.org/role'
        self.opportunity.save(update_fields=[
            'application_methods',
            'application_method',
            'application_url',
        ])
        Application.objects.create(
            user=self.user,
            opportunity=self.opportunity,
            status='queued',
        )
        self.client.force_login(self.user)

        dashboard = self.client.get('/dashboard/')
        detail = self.client.get(f'/opportunities/{self.opportunity.pk}/')

        for response in (dashboard, detail):
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'href="mailto:applications@example.org"')
            self.assertContains(
                response,
                'href="https://apply.example.org/role"',
            )

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
    def setUp(self):
        self.public_destination_validation = patch(
            'opportunity_agent.services.source_ingestion.public_addresses',
            return_value={ipaddress.ip_address('93.184.216.34')},
        )
        self.public_destination_validation.start()
        self.addCleanup(self.public_destination_validation.stop)

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
    def test_ai_rejects_application_url_not_present_in_the_source_text(self, post):
        post.return_value = {
            'title': 'Research Fellowship',
            'organization': 'Invented Organization',
            'application_url': 'https://invented.example/apply',
            'evidence': {
                'title': 'Research Fellowship',
                'organization': 'A research fellowship is open.',
                'application_url': 'Apply through the official portal.',
            },
        }

        result = AIClient().extract_opportunity(
            'Research Fellowship. A research fellowship is open. '
            'Apply through the official portal.',
            'https://example.org/fellowship',
        )

        self.assertEqual(result.get('title'), 'Research Fellowship')
        self.assertNotIn('organization', result)
        self.assertNotIn('application_url', result)

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
        self.assertEqual([entry['score'] for entry in ranked], [63, 63])

    @patch.dict('os.environ', {
        'AI_API_KEY': '',
        'AI_GOOGLE_API_KEY': 'google-key',
        'AI_GROQ_API_KEY': 'groq-key',
        'AI_OPENROUTER_API_KEY': '',
        'AI_MISTRAL_API_KEY': '',
        'AI_TOGETHER_API_KEY': '',
        'AI_HUGGINGFACE_API_KEY': '',
        'AI_PROVIDER': 'google',
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
        'AI_PROVIDER': 'google',
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

    @patch.dict('os.environ', {
        'AI_API_KEY': '',
        'AI_GOOGLE_API_KEY': '',
        'AI_GROQ_API_KEY': '',
        'AI_OPENROUTER_API_KEY': '',
        'AI_MISTRAL_API_KEY': '',
        'AI_TOGETHER_API_KEY': '',
        'AI_HUGGINGFACE_API_KEY': '',
    }, clear=False)
    @patch('opportunity_agent.services.ai_engine.requests.post')
    def test_missing_provider_fails_clearly_without_a_mock_success(self, post):
        with self.assertRaisesRegex(AIProviderError, 'No AI provider is configured'):
            AIClient().classify_opportunity('Research role')

        post.assert_not_called()

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
    def test_malformed_completion_response_falls_back(self, post):
        malformed = Mock()
        malformed.raise_for_status.return_value = None
        malformed.json.return_value = {
            'choices': [{'message': {'content': '{not valid json'}}],
        }
        success = Mock()
        success.raise_for_status.return_value = None
        success.json.return_value = {
            'choices': [{
                'message': {
                    'content': json.dumps({
                        'is_opportunity': True,
                        'opportunity_type': 'job',
                        'confidence': 0.9,
                        'evidence': {
                            'is_opportunity': 'Research role',
                            'opportunity_type': 'Research role',
                        },
                    }),
                },
            }],
        }
        post.side_effect = [malformed, success]

        result = AIClient().classify_opportunity('Research role')

        self.assertTrue(result['is_opportunity'])
        self.assertEqual(post.call_count, 2)

    @patch.dict('os.environ', {
        'AI_API_KEY': '',
        'AI_GOOGLE_API_KEY': 'google-key',
        'AI_GROQ_API_KEY': 'groq-key',
        'AI_OPENROUTER_API_KEY': '',
        'AI_MISTRAL_API_KEY': '',
        'AI_TOGETHER_API_KEY': '',
        'AI_HUGGINGFACE_API_KEY': '',
        'AI_PROVIDER': 'google',
    }, clear=False)
    @patch('opportunity_agent.services.ai_engine.requests.post')
    def test_provider_error_object_uses_fallback(self, post):
        error_response = Mock()
        error_response.raise_for_status.return_value = None
        error_response.json.return_value = {
            'error': {'message': 'The provider rejected this request.'},
        }
        fallback_response = Mock()
        fallback_response.raise_for_status.return_value = None
        fallback_response.json.return_value = {
            'choices': [{'message': {'content': '{"is_opportunity": null}'}}],
        }
        post.side_effect = [error_response, fallback_response]

        result = AIClient().classify_opportunity('Research role')

        self.assertIsNone(result['is_opportunity'])
        self.assertEqual(post.call_count, 2)

    @patch.dict('os.environ', {
        'AI_API_KEY': '',
        'AI_GOOGLE_API_KEY': 'google-key',
        'AI_GROQ_API_KEY': 'groq-key',
        'AI_OPENROUTER_API_KEY': 'openrouter-key',
        'AI_MISTRAL_API_KEY': '',
        'AI_TOGETHER_API_KEY': '',
        'AI_HUGGINGFACE_API_KEY': '',
        'AI_PROVIDER': 'groq',
    }, clear=False)
    def test_preferred_provider_is_first_and_fallback_order_is_stable(self):
        self.assertEqual(
            [provider.name for provider in AIClient()._providers()],
            ['groq', 'google', 'openrouter'],
        )

    @patch.dict('os.environ', {
        'AI_API_KEY': '',
        'AI_GOOGLE_API_KEY': 'google-key',
        'AI_GROQ_API_KEY': '',
        'AI_OPENROUTER_API_KEY': '',
        'AI_MISTRAL_API_KEY': '',
        'AI_TOGETHER_API_KEY': '',
        'AI_HUGGINGFACE_API_KEY': '',
        'AI_TIMEOUT': '17',
    }, clear=False)
    @patch('opportunity_agent.services.ai_engine.requests.post')
    def test_provider_timeout_is_applied(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'choices': [{'message': {'content': '{"is_opportunity": null}'}}],
        }
        post.return_value = response

        AIClient().classify_opportunity('Research role')

        self.assertEqual(post.call_args.kwargs['timeout'], 17)

    @patch.dict('os.environ', {
        'AI_API_KEY': '',
        'AI_GOOGLE_API_KEY': 'google-key',
        'AI_GROQ_API_KEY': 'groq-key',
        'AI_OPENROUTER_API_KEY': '',
        'AI_MISTRAL_API_KEY': '',
        'AI_TOGETHER_API_KEY': '',
        'AI_HUGGINGFACE_API_KEY': '',
        'AI_PROVIDER': 'google',
    }, clear=False)
    @patch('opportunity_agent.services.ai_engine.requests.post')
    def test_provider_timeout_falls_back(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'choices': [{'message': {'content': '{"is_opportunity": null}'}}],
        }
        post.side_effect = [requests.Timeout('First provider timed out.'), response]

        AIClient().classify_opportunity('Research role')

        self.assertEqual(post.call_count, 2)

    @patch.dict('os.environ', {
        'AI_API_KEY': '',
        'AI_GOOGLE_API_KEY': 'secret-google-key',
        'AI_GROQ_API_KEY': '',
        'AI_OPENROUTER_API_KEY': '',
        'AI_MISTRAL_API_KEY': '',
        'AI_TOGETHER_API_KEY': '',
        'AI_HUGGINGFACE_API_KEY': '',
        'AI_PROVIDER': 'google',
    }, clear=False)
    @patch('opportunity_agent.services.ai_engine.requests.post')
    def test_provider_failures_do_not_expose_api_keys(self, post):
        secret = 'secret-google-key'
        post.side_effect = requests.ConnectionError(
            f'Failed for key={secret}',
        )

        with self.assertRaises(AIProviderError) as caught:
            AIClient().classify_opportunity('Research role')

        self.assertNotIn(secret, str(caught.exception))

    @patch.dict('os.environ', {
        'AI_API_KEY': '',
        'AI_GOOGLE_API_KEY': 'google-key',
        'AI_GROQ_API_KEY': 'groq-key',
        'AI_OPENROUTER_API_KEY': 'openrouter-key',
        'AI_MISTRAL_API_KEY': '',
        'AI_TOGETHER_API_KEY': '',
        'AI_HUGGINGFACE_API_KEY': '',
        'AI_PROVIDER': 'google',
    }, clear=False)
    @patch('opportunity_agent.services.ai_engine.requests.post')
    def test_successful_preferred_provider_stops_fallback(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'choices': [{'message': {'content': '{"is_opportunity": null}'}}],
        }
        post.return_value = response

        AIClient().classify_opportunity('Research role')

        self.assertEqual(post.call_count, 1)
