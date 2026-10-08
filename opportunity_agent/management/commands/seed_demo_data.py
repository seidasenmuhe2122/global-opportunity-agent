from django.core.management import BaseCommand

from opportunity_agent.models import Opportunity, Source


class Command(BaseCommand):
    help = 'Create demo source and opportunity records for local development.'

    def handle(self, *args, **options):
        source, _ = Source.objects.get_or_create(
            url='https://example.com/jobs',
            defaults={
                'name': 'Demo Source',
                'source_type': 'website',
                'country': 'Worldwide',
                'opportunity_types': ['job'],
                'enabled': True,
                'trust_score': 0.9,
                'status': 'active',
                'auto_discovered': False,
            },
        )
        Opportunity.objects.get_or_create(
            title='Remote Python Developer',
            defaults={
                'source': source,
                'organization': 'Demo Org',
                'opportunity_type': 'job',
                'country': 'Worldwide',
                'remote_worldwide': True,
                'description': 'A demo remote role for local testing.',
                'requirements': 'Python and problem solving.',
                'skills': ['python'],
                'languages': ['English'],
                'visa_sponsorship': True,
                'status': 'active',
                'application_url': 'https://example.com/apply',
                'source_url': 'https://example.com/jobs',
            },
        )
        self.stdout.write(self.style.SUCCESS('Demo source data created.'))
