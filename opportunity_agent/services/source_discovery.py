from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.db import transaction
from .public_http import get_public_response, public_addresses

logger = logging.getLogger(__name__)

DISCOVERY_KEYWORDS = (
    'jobs', 'careers', 'vacancies', 'scholarships', 'internships', 'fellowships',
    'grants', 'volunteer', 'study', 'research', 'programs', 'funding', 'training',
    'exchange', 'opportunities',
)
MAX_DISCOVERY_PAGE_BYTES = 2_000_000
ACCESS_BARRIER_TERMS = (
    'verify you are human', 'verify you are a human', 'captcha', 'access denied',
    'sign in to continue', 'log in to continue', 'login to view', 'authentication required',
    'unusual traffic', 'enable javascript to continue',
)
DISCOVERY_HEADERS = {'User-Agent': 'GlobalOpportunityAgent/1.0'}

PUBLIC_SOURCE_CANDIDATES = [
    {
        'name': 'Euraxess',
        'url': 'https://euraxess.ec.europa.eu/',
        'source_type': 'university',
        'country': 'Europe',
        'opportunity_types': ['job', 'research'],
        'trust_score': 0.85,
    },
    {
        'name': 'Opportunity Desk',
        'url': 'https://opportunitydesk.org/',
        'source_type': 'scholarship_site',
        'country': 'Worldwide',
        'opportunity_types': ['scholarship', 'fellowship', 'grant'],
        'trust_score': 0.82,
    },
    {
        'name': 'Study in Germany',
        'url': 'https://www.study-in-germany.de/',
        'source_type': 'university',
        'country': 'Germany',
        'opportunity_types': ['study', 'scholarship'],
        'trust_score': 0.80,
    },
    {
        'name': 'UN Careers',
        'url': 'https://careers.un.org/',
        'source_type': 'international_org',
        'country': 'Worldwide',
        'opportunity_types': ['job'],
        'trust_score': 0.95,
    },
    {
        'name': 'UNDP Jobs',
        'url': 'https://jobs.undp.org/',
        'source_type': 'international_org',
        'country': 'Worldwide',
        'opportunity_types': ['job'],
        'trust_score': 0.94,
    },
    {
        'name': 'ReliefWeb Jobs',
        'url': 'https://reliefweb.int/jobs',
        'source_type': 'international_org',
        'country': 'Worldwide',
        'opportunity_types': ['job'],
        'trust_score': 0.90,
    },
    {
        'name': 'Erasmus+',
        'url': 'https://erasmus-plus.ec.europa.eu/',
        'source_type': 'university',
        'country': 'Europe',
        'opportunity_types': ['exchange', 'study', 'training'],
        'trust_score': 0.90,
    },
]

OPPORTUNITY_KEYWORDS = {
    'job': ('job', 'career', 'vacanc', 'employment', 'recruit'),
    'scholarship': ('scholarship', 'bursary'),
    'internship': ('internship', 'intern'),
    'fellowship': ('fellowship', 'postdoctoral'),
    'grant': ('grant', 'funding', 'fellowship'),
    'training': ('training', 'course', 'capacity building'),
    'study': ('study', 'admission', 'degree program'),
    'exchange': ('exchange', 'mobility program'),
    'volunteer': ('volunteer', 'volunteering'),
    'research': ('research', 'researcher', 'phd'),
    'competition': ('competition', 'contest', 'challenge'),
    'other': ('opportunity', 'opportunities', 'program', 'programme'),
}


def _public_url(url: str) -> bool:
    try:
        public_addresses(url)
        return True
    except ValueError:
        return False


def _classify_source(name: str, url: str, text: str) -> str:
    searchable = f'{name} {url} {text[:12000]}'.lower()
    host = (urlparse(url).hostname or '').lower()
    if host.endswith(('.gov', '.gov.uk', '.gc.ca')) or 'government' in searchable or 'ministry' in searchable:
        return 'government'
    if host.endswith('.int') or any(term in searchable for term in ('united nations', 'world bank', 'international organization')):
        return 'international_org'
    if any(term in searchable for term in ('university', 'universities', 'college', 'campus', 'study in')):
        return 'university'
    if any(term in searchable for term in ('scholarship', 'bursary', 'financial aid')):
        return 'scholarship_site'
    if any(term in searchable for term in ('fellowship', 'grant', 'funding call')):
        return 'fellowship_portal' if 'fellowship' in searchable else 'grant_portal'
    if any(term in searchable for term in ('ngo', 'non-governmental', 'nonprofit', 'humanitarian')):
        return 'ngo'
    if any(term in searchable for term in ('careers', 'career page', 'vacancies', 'jobs')):
        return 'job_site'
    return 'discovered'


def _opportunity_types(text: str) -> list[str]:
    searchable = text.lower()
    return [
        kind for kind, terms in OPPORTUNITY_KEYWORDS.items()
        if any(term in searchable for term in terms)
    ]


def _trust_score(url: str, text: str, matches: list[str]) -> float:
    host = (urlparse(url).hostname or '').lower()
    score = 0.45 + min(len(matches), 5) * 0.04
    if host.endswith(('.gov', '.gov.uk', '.gc.ca', '.edu', '.edu.au', '.int')):
        score += 0.2
    if len(text) > 500:
        score += 0.05
    return round(min(score, 0.85), 2)


def _fetch_public_page(url: str) -> tuple[str, str]:
    response = get_public_response(
        url,
        headers=DISCOVERY_HEADERS,
        timeout=8,
    )
    try:
        chunks = []
        total_size = 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total_size += len(chunk)
            if total_size > MAX_DISCOVERY_PAGE_BYTES:
                raise ValueError('Discovery candidate exceeds the page size limit.')
            chunks.append(chunk)
        body = b''.join(chunks).decode(
            getattr(response, 'encoding', None) or 'utf-8',
            errors='replace',
        )
        return getattr(response, 'url', None) or url, body
    finally:
        response.close()


def _inspect_candidate(name: str, url: str) -> dict[str, Any] | None:
    if not _public_url(url):
        return None
    try:
        final_url, body = _fetch_public_page(url)
    except (requests.RequestException, ValueError, OSError):
        logger.info('Skipping unreachable or redirected discovery candidate: %s', url)
        return None

    soup = BeautifulSoup(body, 'html.parser')
    for tag in soup(['script', 'style', 'noscript', 'svg']):
        tag.decompose()
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    text = ' '.join(soup.get_text(' ', strip=True).split())
    searchable = f'{name} {title} {final_url} {text[:12000]}'.lower()
    if any(term in searchable for term in ACCESS_BARRIER_TERMS):
        logger.info('Skipping discovery candidate displaying an access barrier: %s', final_url)
        return None

    matched_keywords = [keyword for keyword in DISCOVERY_KEYWORDS if keyword in searchable]
    opportunity_types = _opportunity_types(searchable)
    if not matched_keywords or not opportunity_types:
        return None

    parsed = urlparse(final_url)
    display_name = (title or name or parsed.hostname or '')[:255]
    return {
        'name': display_name,
        'url': final_url,
        'source_type': _classify_source(display_name, final_url, text),
        'country': 'Worldwide',
        'opportunity_types': opportunity_types,
        'trust_score': _trust_score(final_url, text, matched_keywords),
        'enabled': True,
        'scan_frequency': 'weekly',
        'notes': 'Automatically discovered from a public, accessible page.',
        'auto_discovered': True,
    }


def discover_public_sources(keywords=None) -> list[dict[str, Any]]:
    terms = [
        str(value).strip().lower()
        for value in (keywords or DISCOVERY_KEYWORDS)
        if str(value).strip()
    ]
    from ..models import Source, SystemSetting

    discovered = []
    max_candidates = settings.DISCOVERY_MAX_PER_CYCLE
    page_size = settings.DISCOVERY_PAGE_SIZE
    seen_urls = set()
    state_setting, _ = SystemSetting.objects.get_or_create(
        key='public_source_discovery_state',
        defaults={'value': '{}'},
    )
    with transaction.atomic():
        state_setting = SystemSetting.objects.select_for_update().get(
            pk=state_setting.pk,
        )
        try:
            state = json.loads(state_setting.value or '{}')
        except (TypeError, json.JSONDecodeError):
            logger.warning('Invalid public source discovery state; restarting pagination.')
            state = {}
        if not isinstance(state, dict):
            state = {}
        seen_urls = set(
            value for value in state.get('seen_urls', [])
            if isinstance(value, str)
        )
        known_urls = set(Source.objects.values_list('url', flat=True))

    static_candidates_inspected = 0
    for candidate in PUBLIC_SOURCE_CANDIDATES:
        if static_candidates_inspected >= max_candidates:
            break
        candidate_url = candidate['url']
        if candidate_url in known_urls or candidate_url in seen_urls:
            continue
        static_candidates_inspected += 1
        item = _inspect_candidate(candidate['name'], candidate_url)
        seen_urls.add(candidate_url)
        if item and item['url'] not in known_urls:
            discovered.append(item)
            seen_urls.add(item['url'])
            known_urls.add(item['url'])

    if os.environ.get('ENABLE_WEB_SOURCE_DISCOVERY', '1').lower() not in {'1', 'true', 'yes'}:
        state['seen_urls'] = list(seen_urls)[-2000:]
        state_setting.value = json.dumps(state)
        state_setting.save(update_fields=['value', 'updated_at'])
        return discovered

    groups = [
        terms[index:index + 3]
        for index in range(0, len(terms), 3)
    ]
    if not groups:
        state['seen_urls'] = list(seen_urls)[-2000:]
        state_setting.value = json.dumps(state)
        state_setting.save(update_fields=['value', 'updated_at'])
        return discovered

    query_index = state.get('query_index', 0)
    if not isinstance(query_index, int) or isinstance(query_index, bool):
        query_index = 0
    query_index %= len(groups)
    offsets = state.get('offsets', {})
    if not isinstance(offsets, dict):
        offsets = {}

    remaining_candidates = max_candidates - static_candidates_inspected
    max_queries = min(len(groups), remaining_candidates)
    search_candidates = 0
    for _ in range(max_queries):
        group = groups[query_index]
        query = ' OR '.join(f'"{term}"' for term in group)
        offset = offsets.get(str(query_index), 0)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            offset = 0
        try:
            response = get_public_response(
                'https://html.duckduckgo.com/html/',
                headers={'User-Agent': 'GlobalOpportunityAgent/1.0'},
                timeout=12,
                params={'q': query, 's': offset},
            )
            try:
                soup = BeautifulSoup(response.text, 'html.parser')
            finally:
                response.close()
            anchors = soup.select('.result__a')[:page_size]
            consumed_anchors = 0
            for anchor in anchors:
                if search_candidates >= remaining_candidates:
                    break
                url = anchor.get('href', '')
                consumed_anchors += 1
                search_candidates += 1
                if not url or url in known_urls or url in seen_urls:
                    continue
                seen_urls.add(url)
                if not _public_url(url):
                    continue
                result = _inspect_candidate(anchor.get_text(' ', strip=True), url)
                if result and result['url'] not in known_urls:
                    result['notes'] = 'Automatically discovered from public search and verified public page.'
                    discovered.append(result)
                    known_urls.add(result['url'])
                    seen_urls.add(result['url'])
            cap_reached_inside_page = consumed_anchors < len(anchors)
            if cap_reached_inside_page or len(anchors) >= page_size:
                offsets[str(query_index)] = offset + consumed_anchors
            else:
                offsets[str(query_index)] = 0
        except (requests.RequestException, ValueError, OSError) as exc:
            logger.warning(
                'Public source discovery query %s failed; continuing to the next query: %s',
                query_index,
                type(exc).__name__,
            )
        query_index = (query_index + 1) % len(groups)
        if search_candidates >= remaining_candidates:
            break

    state['query_index'] = query_index
    state['offsets'] = offsets
    state['seen_urls'] = list(seen_urls)[-2000:]
    state_setting.value = json.dumps(state)
    state_setting.save(update_fields=['value', 'updated_at'])
    return discovered
