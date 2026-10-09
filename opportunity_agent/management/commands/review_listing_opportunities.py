import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone

from opportunity_agent.models import Opportunity
from opportunity_agent.services.source_ingestion import (
    is_listing_page,
    listing_url_reason,
)
from opportunity_agent.tasks import _safe_source_url


class Command(BaseCommand):
    help = (
        'Find opportunities whose source URL or saved page content indicates a '
        'listing page. Dry-run is the default; apply mode only marks records for review.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--backup-file')
        parser.add_argument('--confirm', default='')

    def handle(self, *args, **options):
        opportunities = Opportunity.objects.all().order_by('pk')
        candidates = []
        for opportunity in opportunities.iterator():
            reason = listing_url_reason(opportunity.source_url)
            if not reason and is_listing_page({
                'title': opportunity.title,
                'url': opportunity.source_url,
                'html': opportunity.raw_source_content,
                'text': opportunity.description,
            }):
                reason = 'Saved title or page content indicates a listing page'
            if reason:
                candidates.append((opportunity, reason))

        if not candidates:
            self.stdout.write('No listing-page opportunity records found.')
            return

        self.stdout.write(
            'ID | Title | Source URL | Reason | Applications | Matches'
        )
        for opportunity, reason in candidates:
            self.stdout.write(
                f'{opportunity.pk} | {opportunity.title} | '
                f'{_safe_source_url(opportunity.source_url)} | {reason} | '
                f'{opportunity.applications.count()} | {opportunity.matches.count()}'
            )
        self.stdout.write(f'Candidates: {len(candidates)}')

        if not options['apply']:
            self.stdout.write('Dry-run only; no records were changed.')
            return

        backup_file = options.get('backup_file')
        if not backup_file:
            raise CommandError('--apply requires --backup-file.')
        if options.get('confirm') != 'MARK LISTING OPPORTUNITIES FOR REVIEW':
            raise CommandError(
                '--apply requires --confirm "MARK LISTING OPPORTUNITIES FOR REVIEW".'
            )

        backup_path = Path(backup_file).expanduser()
        if not backup_path.parent.exists():
            raise CommandError('The backup directory must already exist.')
        try:
            with backup_path.open('x', encoding='utf-8') as stream:
                json.dump(
                    [
                        {
                            'opportunity': {
                                field.attname: getattr(opportunity, field.attname)
                                for field in opportunity._meta.concrete_fields
                            },
                            'applications': opportunity.applications.count(),
                            'matches': opportunity.matches.count(),
                            'review_reason': reason,
                        }
                        for opportunity, reason in candidates
                    ],
                    stream,
                    cls=DjangoJSONEncoder,
                    ensure_ascii=False,
                    indent=2,
                )
                stream.write('\n')
        except OSError as exc:
            raise CommandError(f'Could not write backup file: {exc}') from exc

        candidate_ids = [opportunity.pk for opportunity, _ in candidates]
        with transaction.atomic():
            updated = Opportunity.objects.filter(pk__in=candidate_ids).exclude(
                status__in=('needs_review', 'rejected'),
            ).update(status='needs_review', updated_at=timezone.now())
        self.stdout.write(
            f'Backup written to {backup_path}; marked {updated} record(s) needs_review. '
            'No records or related data were deleted.'
        )
