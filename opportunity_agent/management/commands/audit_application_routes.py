import json
import csv
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone
import requests

from opportunity_agent.models import Opportunity
from opportunity_agent.services.source_ingestion import (
    basic_extract,
    detail_page_content,
    fetch_public_source,
    is_listing_page,
    is_listing_opportunity,
    listing_url_reason,
    opportunity_detail_validation_error,
)
from opportunity_agent.services.public_http import public_addresses, validate_response_peer
from opportunity_agent.tasks import _safe_error_text, _safe_source_url


SUPPORTED_METHODS = ('online', 'form', 'email', 'telegram', 'physical')
APPLY_CONFIRMATION = 'APPLY VERIFIED ROUTE REPAIRS'
REPORT_FIELDS = (
    'opportunity_id',
    'title',
    'classification',
    'source_url',
    'saved_deadline',
    'proposed_fields',
    'current_status',
    'proposed_status',
    'current_application_url',
    'proposed_application_url',
    'current_application_form_url',
    'proposed_application_form_url',
    'current_application_method',
    'proposed_application_method',
    'current_application_methods',
    'proposed_application_methods',
    'application_instructions_changed',
    'current_application_instructions',
    'proposed_application_instructions',
    'https_checks',
    'destination_check',
    'evidence',
)


def _json_value(value):
    return json.dumps(value, cls=DjangoJSONEncoder, ensure_ascii=False, sort_keys=True)


def _potential_expiry_evidence(opportunity, routes, extracted, page_text):
    evidence = []
    current_time = timezone.now()
    deadline = extracted.get('deadline') or opportunity.deadline
    if deadline and deadline < current_time:
        evidence.append(f'Explicit deadline is in the past: {deadline.isoformat()}')

    for route in routes:
        destination = str(route.get('destination') or '')
        instructions = str(route.get('instructions') or '')
        years = [
            int(year) for year in re.findall(r'(?<!\d)(20\d{2})(?!\d)', destination)
            if int(year) < current_time.year
        ]
        if years:
            evidence.append(
                f"Route destination contains past cycle year(s) {', '.join(map(str, years))}: "
                f'{destination}'
            )
        if re.search(
            r'\b(?:application|applications|registration|submissions?)\b.{0,50}'
            r'\b(?:closed|ended|expired|no longer accepted)\b|'
            r'\b(?:closed|ended|expired|no longer accepting)\b.{0,50}'
            r'\b(?:application|applications|registration|submissions?)\b',
            instructions,
            re.I,
        ):
            evidence.append(
                f'Application instructions explicitly indicate closure: {instructions}'
            )

    closed_match = re.search(
        r'.{0,100}\b(?:applications? (?:are )?closed|no longer accepting applications|'
        r'application period has ended|application deadline has passed)\b.{0,100}',
        page_text or '',
        re.I,
    )
    if closed_match:
        evidence.append(
            'Fetched page states application is closed: '
            + ' '.join(closed_match.group(0).split())
        )
    return evidence


def _route_evidence(route):
    method = str(route.get('method') or '')
    destination = str(route.get('destination') or '')
    instructions = ' '.join(str(route.get('instructions') or '').split())
    parts = [f'Extracted {method} route']
    if destination:
        parts.append(f'destination={destination}')
    if instructions:
        parts.append(f'source instructions/context="{instructions}"')
    return '; '.join(parts)


def _saved_telegram_context(opportunity, source_html='', page_text=''):
    if not opportunity.telegram_contact:
        return ''
    soup = BeautifulSoup(
        source_html or opportunity.raw_source_content or '',
        'html.parser',
    )
    for anchor in soup.find_all('a', href=True):
        if opportunity.telegram_contact.rstrip('/').casefold() in anchor['href'].rstrip('/').casefold():
            label = ' '.join(anchor.get_text(' ', strip=True).split())
            if label:
                application_excerpt = re.search(
                    r'.{0,100}\b(?:to become|apply|application)\b.{0,220}',
                    page_text or '',
                    re.I,
                )
                excerpt = (
                    ' '.join(application_excerpt.group(0).split())
                    if application_excerpt else ''
                )
                return (
                    f'Saved telegram_contact={opportunity.telegram_contact}; '
                    f'fetched source link label="{label}"; '
                    + (
                        f'application-related page text="{excerpt}"; '
                        if excerpt else ''
                    )
                    + 'no application-specific instruction associates the Telegram link.'
                )
    return (
        f'Saved telegram_contact={opportunity.telegram_contact}; it is a generic contact field '
        'and is not treated as an application route without explicit application instructions.'
    )


def _https_check(url):
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != 'http':
        return ''
    current_url = urlunsplit(('https', parsed.netloc, parsed.path, parsed.query, ''))
    checked_urls = set()
    for _ in range(6):
        if current_url in checked_urls:
            return f'{current_url}: HTTPS redirect loop detected'
        checked_urls.add(current_url)
        response = None
        try:
            expected_addresses = public_addresses(current_url)
            response = requests.get(
                current_url,
                headers={'User-Agent': 'OpportunityHubSourceMonitor/1.0'},
                timeout=15,
                allow_redirects=False,
                stream=True,
            )
            validate_response_peer(response, expected_addresses)
            if 300 <= response.status_code < 400:
                location = response.headers.get('Location')
                if not location:
                    return f'{current_url}: HTTPS returned {response.status_code} without Location'
                next_url = urljoin(current_url, location)
                if urlsplit(next_url).scheme.casefold() != 'https':
                    return (
                        f'{current_url}: HTTPS returned {response.status_code} and redirects '
                        f'outside HTTPS to {next_url}; no downgrade followed'
                    )
                current_url = next_url
                continue
            response.raise_for_status()
            return (
                f'{current_url}: HTTPS returned HTTP {response.status_code}; '
                'the endpoint responded successfully'
            )
        except Exception as exc:
            if response is not None:
                return (
                    f'{current_url}: HTTPS outcome inconclusive; received HTTP '
                    f'{response.status_code}, but the public connection could not be '
                    f'safely verified ({_safe_error_text(exc)})'
                )
            return f'{current_url}: HTTPS check failed ({_safe_error_text(exc)})'
        finally:
            if response is not None:
                response.close()
    return f'{current_url}: HTTPS redirect limit exceeded'


def _listing_content_evidence(candidate):
    soup = BeautifulSoup(candidate.get('html', '') or '', 'html.parser')
    heading = soup.find('h1') or soup.find('title')
    evidence = []
    canonical = soup.find(
        'link',
        rel=lambda value: value and any(
            str(item).casefold() == 'canonical'
            for item in (value if isinstance(value, list) else [value])
        ),
    )
    canonical_url = urljoin(
        candidate.get('url', ''),
        canonical.get('href', ''),
    ) if canonical else ''
    canonical_reason = listing_url_reason(canonical_url)
    if canonical_reason:
        evidence.append(
            f'Canonical URL="{canonical_url}" matches explicit listing pattern: '
            f'{canonical_reason}'
        )
    if heading:
        evidence.append(
            f'Fetched page heading="{ " ".join(heading.get_text(" ", strip=True).split()) }"'
        )
    meta = soup.find(
        'meta',
        attrs={'name': re.compile(r'description', re.I)},
    ) or soup.find('meta', attrs={'property': 'og:description'})
    if meta and meta.get('content'):
        evidence.append(f'Fetched listing metadata="{meta["content"].strip()}"')
    content_root = (
        soup.select_one('main')
        or soup.select_one('.site-main')
        or soup.select_one('#primary')
        or soup.body
        or soup
    )
    relevant_links = [
        ' '.join(anchor.get_text(' ', strip=True).split())
        for anchor in content_root.find_all('a', href=True)
        if re.search(
            r'\b(?:job|career|vacanc|scholarship|fellowship|internship|grant|'
            r'training|conference|phd|postdoctoral|opportunit)\w*\b',
            anchor.get_text(' ', strip=True),
            re.I,
        )
        and len(anchor.get_text(' ', strip=True).strip()) >= 16
    ]
    relevant_links = list(dict.fromkeys(relevant_links))
    if relevant_links:
        evidence.append(
            f'{len(relevant_links)} opportunity-specific links, including: '
            + ', '.join(f'"{label}"' for label in relevant_links[:5])
        )
    article_titles = []
    for article in content_root.find_all('article'):
        title = article.find(re.compile(r'^h[1-6]$'))
        if title:
            label = ' '.join(title.get_text(' ', strip=True).split())
            if label and label not in article_titles:
                article_titles.append(label)
    if article_titles:
        evidence.append(
            f'{len(article_titles)} article cards with titles, including: '
            + ', '.join(f'"{label}"' for label in article_titles[:5])
        )
    repeated_cards = [
        node for node in soup.find_all(['article', 'li', 'div'])
        if re.search(
            r'(?:opportunit|listing|result|job|scholarship|event|card)',
            ' '.join(node.get('class', [])),
            re.I,
        )
    ]
    if repeated_cards:
        evidence.append(f'{len(repeated_cards)} repeated listing/card containers')
    return evidence


def _report_row(record):
    opportunity = record['opportunity']
    updates = record['updates']
    return {
        'opportunity_id': opportunity.pk,
        'title': opportunity.title,
        'classification': record.get('classification', record['label']),
        'source_url': opportunity.source_url,
        'saved_deadline': (
            opportunity.deadline.isoformat() if opportunity.deadline else ''
        ),
        'proposed_fields': ','.join(sorted(updates)),
        'current_status': opportunity.status,
        'proposed_status': updates.get('status', opportunity.status),
        'current_application_url': opportunity.application_url,
        'proposed_application_url': updates.get('application_url', opportunity.application_url),
        'current_application_form_url': opportunity.application_form_url,
        'proposed_application_form_url': updates.get(
            'application_form_url',
            opportunity.application_form_url,
        ),
        'current_application_method': opportunity.application_method,
        'proposed_application_method': updates.get(
            'application_method',
            opportunity.application_method,
        ),
        'current_application_methods': _json_value(opportunity.application_methods),
        'proposed_application_methods': _json_value(
            updates.get('application_methods', opportunity.application_methods)
        ),
        'application_instructions_changed': (
            'yes' if 'application_instructions' in updates else 'no'
        ),
        'current_application_instructions': opportunity.application_instructions,
        'proposed_application_instructions': updates.get(
            'application_instructions',
            opportunity.application_instructions,
        ),
        'https_checks': ' | '.join(record.get('https_checks', [])),
        'destination_check': record.get(
            'destination_check',
            'Not independently checked; source evidence only.'
            if record.get('routes') else 'Not applicable; no route extracted.',
        ),
        'evidence': ' | '.join(record.get('evidence', [])),
    }


def _write_review_report(path_value, records):
    path = Path(path_value).expanduser()
    if not path.parent.exists():
        raise CommandError('The report directory must already exist.')
    try:
        with path.open('x', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=REPORT_FIELDS)
            writer.writeheader()
            writer.writerows(_report_row(record) for record in records)
    except FileExistsError as exc:
        raise CommandError(
            f'Report file already exists; choose a new path: {path}'
        ) from exc
    except OSError as exc:
        raise CommandError(f'Could not write review report: {exc}') from exc
    return path


def _route_updates(opportunity, extracted):
    routes = extracted.get('application_methods')
    routes = routes if isinstance(routes, list) else []
    verified_routes = [
        route for route in routes
        if isinstance(route, dict)
        and route.get('method') in (*SUPPORTED_METHODS, 'phone')
        and (route.get('destination') or route.get('instructions'))
    ]
    if not verified_routes:
        return {}

    updates = {}
    current_routes = (
        list(opportunity.application_methods)
        if isinstance(opportunity.application_methods, list)
        else []
    )
    merged_routes = list(current_routes)
    for route in verified_routes:
        if route not in merged_routes:
            merged_routes.append(route)
    if merged_routes != current_routes:
        updates['application_methods'] = merged_routes

    instruction_additions = []
    existing_instruction_lines = {
        line.strip()
        for line in str(opportunity.application_instructions or '').splitlines()
        if line.strip()
    }
    for route in verified_routes:
        instruction = str(route.get('instructions') or '').strip()
        if (
            instruction
            and instruction not in existing_instruction_lines
            and instruction not in instruction_additions
        ):
            instruction_additions.append(instruction)
    current_instructions = str(opportunity.application_instructions or '').strip()
    if instruction_additions:
        updates['application_instructions'] = '\n'.join(
            value for value in (current_instructions, '\n'.join(instruction_additions)) if value
        )[:4000]

    supported_routes = [
        route for route in verified_routes
        if route.get('method') in SUPPORTED_METHODS
    ]
    if opportunity.application_method == 'source_only' and supported_routes:
        method = next(
            (
                route['method'] for route in supported_routes
                if route['method'] in {'online', 'form', 'email', 'telegram', 'physical'}
            ),
            '',
        )
        if method:
            updates['application_method'] = method
    if not opportunity.application_url:
        online = next(
            (
                route.get('destination', '')
                for route in verified_routes
                if route.get('method') == 'online' and route.get('destination')
            ),
            '',
        )
        if online:
            updates['application_url'] = online
    if not opportunity.application_form_url:
        form_url = next(
            (
                route.get('destination', '')
                for route in verified_routes
                if route.get('method') == 'form' and route.get('destination')
            ),
            '',
        )
        if form_url:
            updates['application_form_url'] = form_url

    return updates


def _mark_for_review(opportunity):
    if opportunity.status not in {'needs_review', 'rejected'}:
        return {'status': 'needs_review'}
    return {}


class Command(BaseCommand):
    help = (
        'Audit saved opportunities for listing pages and source-verified application '
        'routes. Dry-run is the default; --apply requires a backup file and confirmation.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--backup-file')
        parser.add_argument('--confirm', default='')
        parser.add_argument(
            '--report-csv',
            help='Write a read-only detailed review report to a new CSV file.',
        )

    def handle(self, *args, **options):
        if options['apply'] and options.get('report_csv'):
            raise CommandError('--report-csv cannot be combined with --apply.')
        records = []
        checked = listing_candidates = verified_routes_found = errors = 0
        counts = {
            'confirmed listing pages': 0,
            'verified current routes': 0,
            'potentially expired routes': 0,
            'detail pages without verified routes': 0,
            'manual review': 0,
        }

        for opportunity in Opportunity.objects.all().order_by('pk').iterator():
            checked += 1
            result = {
                'opportunity': opportunity,
                'updates': {},
                'label': 'unchanged',
                'routes': [],
                'reason': '',
                'evidence': [],
                'https_checks': [],
                'destination_check': '',
            }
            url = opportunity.source_url
            reason = listing_url_reason(url)
            if not reason and is_listing_opportunity(opportunity):
                reason = (
                    f'Saved source URL/title/content passed listing-page classifier '
                    f'(title="{opportunity.title}"; source URL="{url}")'
                )
            if reason:
                listing_candidates += 1
                counts['confirmed listing pages'] += 1
                result['label'] = 'listing-page candidate'
                result['classification'] = 'confirmed listing page'
                result['reason'] = reason
                result['evidence'].append(reason)
                if not listing_url_reason(url):
                    result['evidence'].append(
                        f'Saved-record classifier evidence: title="{opportunity.title}"'
                    )
                    result['evidence'].extend(_listing_content_evidence({
                        'html': opportunity.raw_source_content,
                    }))
                result['updates'] = _mark_for_review(opportunity)
                records.append(result)
                continue

            if not url:
                errors += 1
                counts['manual review'] += 1
                result['label'] = 'manual review'
                result['reason'] = 'No source URL is saved'
                result['classification'] = 'manual review'
                result['evidence'].append(result['reason'])
                result['updates'] = _mark_for_review(opportunity)
                records.append(result)
                continue

            try:
                parsed = urlsplit(url)
                if (
                    parsed.scheme not in {'http', 'https'}
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                ):
                    raise ValueError('Source URL is not a public HTTP(S) URL.')
                public_addresses(url)
                final_url, body, _content_type = fetch_public_source(url)
            except Exception as exc:
                errors += 1
                counts['manual review'] += 1
                result['label'] = 'manual review'
                result['reason'] = _safe_error_text(exc)
                result['classification'] = 'manual review'
                result['evidence'].append(
                    f'Source fetch could not verify page or routes: {result["reason"]}'
                )
                result['updates'] = _mark_for_review(opportunity)
                records.append(result)
                continue

            page = {
                'title': opportunity.title,
                'url': final_url,
                'html': body,
                'raw_source_content': body,
            }
            soup_node = detail_page_content(final_url, body)
            page['text'] = soup_node.get_text('\n', strip=True)
            title_node = soup_node.find('h1') or soup_node.find('title')
            if title_node:
                page['title'] = title_node.get_text(' ', strip=True)

            reason = listing_url_reason(final_url)
            if not reason and is_listing_page(page):
                reason = 'Fetched source content indicates a listing page'
            if reason:
                listing_candidates += 1
                counts['confirmed listing pages'] += 1
                result['label'] = 'listing-page candidate'
                result['classification'] = 'confirmed listing page'
                result['reason'] = reason
                result['evidence'].append(
                    f'Fetched source URL="{final_url}"; {reason}'
                )
                result['evidence'].extend(_listing_content_evidence(page))
                result['updates'] = _mark_for_review(opportunity)
                records.append(result)
                continue

            detail_error = opportunity_detail_validation_error(page)
            if detail_error:
                counts['manual review'] += 1
                result['label'] = 'manual review'
                result['reason'] = detail_error
                result['classification'] = 'manual review'
                result['evidence'].append(
                    f'Fetched source URL="{final_url}"; {detail_error}'
                )
                result['updates'] = _mark_for_review(opportunity)
                records.append(result)
                continue

            extracted = basic_extract(page, opportunity.source)
            result['routes'] = extracted.get('application_methods', [])
            expiry_evidence = _potential_expiry_evidence(
                opportunity,
                result['routes'],
                extracted,
                page['text'],
            )
            historical_routes = bool(expiry_evidence)
            for route in result['routes']:
                result['evidence'].append(_route_evidence(route))
                destination = str(route.get('destination') or '')
                https_check = _https_check(destination)
                if https_check:
                    result['https_checks'].append(https_check)
            if result['routes']:
                if any('inconclusive' in check for check in result['https_checks']):
                    result['destination_check'] = (
                        'HTTPS outcome inconclusive; no current reachability claim.'
                    )
                elif result['https_checks']:
                    result['destination_check'] = (
                        'HTTPS was probed; the response does not confirm application acceptance.'
                    )
                else:
                    result['destination_check'] = (
                        'Not independently checked; source evidence only.'
                    )
            if not result['routes'] and opportunity.telegram_contact:
                telegram_evidence = _saved_telegram_context(
                    opportunity,
                    source_html=body,
                    page_text=page['text'],
                )
                if telegram_evidence:
                    result['evidence'].append(telegram_evidence)

            if historical_routes:
                result['classification'] = 'potentially expired route'
                result['label'] = 'potentially expired route'
                result['reason'] = '; '.join(expiry_evidence)
                result['evidence'].extend(expiry_evidence)
                result['updates'] = _mark_for_review(opportunity)
                counts['potentially expired routes'] += 1
                records.append(result)
                continue

            if result['routes']:
                verified_routes_found += 1
                result['updates'] = _route_updates(opportunity, extracted)
                supported_routes = [
                    route for route in result['routes']
                    if isinstance(route, dict)
                    and route.get('method') in SUPPORTED_METHODS
                ]
                if not supported_routes and opportunity.application_method == 'source_only':
                    result['updates'].update(_mark_for_review(opportunity))
                    result['label'] = 'manual review'
                    result['reason'] = 'Phone-only application instructions are not a supported route'
                    result['classification'] = 'manual review'
                    counts['manual review'] += 1
                else:
                    result['classification'] = 'verified current route'
                    counts['verified current routes'] += 1
                    result['label'] = (
                        'verified route(s)' if result['updates'] else 'unchanged'
                    )
                if not result['updates'] and opportunity.application_method == 'source_only':
                    result['reason'] = 'Source has explicit routes, but no safe field update was available'
                if result['reason']:
                    result['evidence'].append(result['reason'])
            else:
                counts['detail pages without verified routes'] += 1
                result['label'] = 'unchanged'
                result['classification'] = 'genuine detail; no route verified'
                result['evidence'].append(
                    f'Fetched source URL="{final_url}"; no explicit application method '
                    'was verified. Status and saved application routes remain unchanged.'
                )
            records.append(result)

        changes = [record for record in records if record['updates']]

        for record in records:
            opportunity = record['opportunity']
            route_summary = ', '.join(
                f"{route.get('method')}: {_safe_source_url(route.get('destination', ''))}"
                for route in record['routes']
            )
            self.stdout.write(
                f"ID {opportunity.pk} | {record['label']} | "
                f"source={_safe_source_url(opportunity.source_url)}"
                + (f" | routes={route_summary}" if route_summary else '')
                + (f" | review={record['reason']}" if record['reason'] else '')
            )

        report_path = None
        if options.get('report_csv'):
            report_path = _write_review_report(options['report_csv'], records)

        self.stdout.write(
            'Summary: '
            f'records checked={checked}; '
            f'listing-page candidates={listing_candidates}; '
            f'records with verified routes={verified_routes_found}; '
            f'proposed updates={len(changes)}; '
            f'unchanged={checked - len(changes)}; '
            f'errors={errors}.'
        )
        self.stdout.write(
            'Review classifications: '
            + '; '.join(f'{label}={count}' for label, count in counts.items())
            + '.'
        )
        self.stdout.write(
            'Route verification is source-content based; destination reachability and '
            'current submission availability are not inferred.'
        )
        proposed_fields = {
            field: sum(field in record['updates'] for record in records)
            for field in (
                'status',
                'application_url',
                'application_form_url',
                'application_method',
                'application_methods',
                'application_instructions',
            )
        }
        self.stdout.write(
            'Proposed field changes: '
            + '; '.join(f'{field}={count}' for field, count in proposed_fields.items())
            + '.'
        )
        if report_path:
            self.stdout.write(f'Read-only CSV report written to {report_path}.')
        if not options['apply']:
            self.stdout.write('Dry-run only; no database records were changed.')
            return

        backup_file = options.get('backup_file')
        if not backup_file:
            raise CommandError('--apply requires --backup-file.')
        if options.get('confirm') != APPLY_CONFIRMATION:
            raise CommandError(
                f'--apply requires --confirm "{APPLY_CONFIRMATION}".'
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
                                field.attname: getattr(record['opportunity'], field.attname)
                                for field in record['opportunity']._meta.concrete_fields
                            },
                            'proposed_updates': record['updates'],
                            'reason': record['reason'],
                        }
                        for record in changes
                    ],
                    stream,
                    cls=DjangoJSONEncoder,
                    ensure_ascii=False,
                    indent=2,
                )
                stream.write('\n')
        except OSError as exc:
            raise CommandError(f'Could not write backup file: {exc}') from exc

        with transaction.atomic():
            for record in changes:
                opportunity = record['opportunity']
                for field, value in record['updates'].items():
                    setattr(opportunity, field, value)
                opportunity.save(
                    update_fields=set(record['updates']) | {'updated_at'},
                )
        self.stdout.write(
            f'Backup written to {backup_path}; applied {len(changes)} approved update(s). '
            'No records or related data were deleted.'
        )
