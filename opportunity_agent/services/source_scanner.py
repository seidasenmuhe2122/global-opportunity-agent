from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..models import Opportunity, Source
from .ai_engine import AIClient, AIProviderError
from .deduplication import deduplicate_and_save_opportunity

UA = 'GlobalOpportunityAgent/2.0 (+public-opportunity-collector)'
TIMEOUT = 20
JOB_WORDS = ('job', 'career', 'vacancy', 'position', 'internship', 'fellowship', 'scholarship', 'grant', 'opportunity', 'apply', 'research')


def public_url(url: str) -> bool:
    p = urlsplit(url)
    return p.scheme in {'http', 'https'} and bool(p.hostname) and not p.username and not p.password


def fetch(url: str) -> tuple[str, str]:
    from ..tasks import _validate_public_source_url
    from .source_ingestion import fetch_public_source

    _validate_public_source_url(url)
    final_url, content, _content_type = fetch_public_source(url, TIMEOUT)
    return final_url, content


def _clean(text: str) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()


def _candidate_links(base: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, 'html.parser')
    out = []
    seen = set()
    for a in soup.find_all('a', href=True):
        href = urljoin(base, a.get('href', ''))
        label = _clean(a.get_text(' ', strip=True)).lower()
        hay = f'{label} {href.lower()}'
        if not public_url(href) or href.split('#')[0] in seen:
            continue
        if any(word in hay for word in JOB_WORDS):
            seen.add(href.split('#')[0])
            out.append(href.split('#')[0])
    return out


def _heuristic_extract(text: str, source_url: str) -> dict:
    from .source_ingestion import (
        _application_link_context,
        _is_application_destination,
        extract_contact_destinations,
    )
    from .deadlines import extract_explicit_deadline

    lines = [x.strip() for x in re.split(r'[\n\r]+', text) if x.strip()]
    title = next((x for x in lines if 8 <= len(x) <= 180), '')
    app = ''
    application_link_pattern = re.compile(
        r'\b(?:apply(?:\s+now)?|application\s+(?:form|portal)|'
        r'submit\s+(?:an?\s+)?application|online\s+application)\b',
        re.I,
    )
    for match in re.finditer(r'https?://[^\s<>"\']+', text, re.I):
        context = _application_link_context(text, match.start())
        if not application_link_pattern.search(context):
            continue
        app = _is_application_destination(
            match.group(0).rstrip('.,);'),
            (source_url,),
        )
        if app:
            break
    contacts = extract_contact_destinations(
        {'text': text, 'url': source_url},
        text=text,
    )
    return {
        'title': title[:255],
        'organization': '',
        'opportunity_type': '',
        'country': '',
        'description': text,
        'skills': [],
        'languages': [],
        'application_url': app,
        'deadline': extract_explicit_deadline(text),
        'source_url': source_url,
        **contacts,
    }


def _extract_json(client: AIClient, text: str, source_url: str) -> dict:
    try:
        result = client.extract_opportunity(text, source_url=source_url)
    except AIProviderError:
        return _heuristic_extract(text, source_url)
    if isinstance(result, dict) and result.get('status') == 'mock':
        return _heuristic_extract(text, source_url)
    if isinstance(result, dict) and 'raw' in result and isinstance(result['raw'], str):
        import json
        try:
            result = json.loads(result['raw'])
        except Exception:
            result = {}
    result = result if isinstance(result, dict) else {}
    if not result.get('title') or not result.get('description'):
        fallback = _heuristic_extract(text, source_url)
        for key, value in fallback.items():
            if not result.get(key): result[key] = value
    result.setdefault('source_url', source_url)
    return result


def _parse_deadline(value):
    from .deadlines import parse_deadline

    return parse_deadline(value)


def _save_opportunity(source: Source, data: dict, raw: str) -> tuple[Opportunity | None, bool]:
    if not data.get('description'):
        data = {**data, 'description': raw}
    return deduplicate_and_save_opportunity(source, data, raw_content=raw)




def scan_rss_source(source: Source) -> dict:
    return scan_source(source)

def scan_source(source: Source) -> dict:
    from ..tasks import scan_sources_task

    if not source.pk:
        raise ValueError('Source must be saved before it can be scanned.')
    result = scan_sources_task.run(limit=1, source_ids=[source.pk])
    return {
        'source_id': source.pk,
        'created': result['new_opportunities'],
        'updated': result['updated_opportunities'],
        'pages': result['pages_processed'],
        'errors': result['errors'],
        'candidate_errors': result['candidate_errors'],
    }
