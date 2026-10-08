from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
try:
    import feedparser
except ImportError:
    feedparser = None

from django.utils import timezone

from ..models import Opportunity, Source
from .ai_engine import AIClient
from .deduplication import deduplicate_and_save_opportunity

UA = 'GlobalOpportunityAgent/2.0 (+public-opportunity-collector)'
TIMEOUT = 20
MAX_LINKS = 35
JOB_WORDS = ('job', 'career', 'vacancy', 'position', 'internship', 'fellowship', 'scholarship', 'grant', 'opportunity', 'apply', 'research')


def public_url(url: str) -> bool:
    p = urlsplit(url)
    return p.scheme in {'http', 'https'} and bool(p.hostname) and not p.username and not p.password


def fetch(url: str) -> tuple[str, str]:
    if not public_url(url):
        raise ValueError('Only public HTTP/HTTPS source URLs are supported.')
    from .retries import request_with_exponential_backoff

    def request():
        response = requests.get(
            url,
            headers={'User-Agent': UA},
            timeout=TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()
        return response

    r = request_with_exponential_backoff(
        request,
        description=f'fetching opportunity page {url}',
    )
    return r.url, r.text


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
        if len(out) >= MAX_LINKS:
            break
    return out


def _heuristic_extract(text: str, source_url: str) -> dict:
    from .source_ingestion import extract_contact_destinations
    from .deadlines import extract_explicit_deadline

    lines = [x.strip() for x in re.split(r'[\n\r]+', text) if x.strip()]
    title = next((x for x in lines if 8 <= len(x) <= 180), '')
    app = ''
    m = re.search(r'https?://[^\s<>"\']+', text, re.I)
    if m:
        url = m.group(0).rstrip('.,);')
        if any(word in url.lower() for word in ('apply', 'application', 'career', 'vacanc', 'jobs')):
            app = url
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
    result = client.extract_opportunity(text, source_url=source_url)
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
    if feedparser is None:
        raise RuntimeError('feedparser is not installed.')
    feed = feedparser.parse(source.url)
    client = AIClient(); created = 0; updated = 0
    for entry in feed.entries[:50]:
        link = entry.get('link') or source.url
        text = _clean(' '.join([entry.get('title',''), entry.get('summary',''), entry.get('description','')]))
        if len(text) < 20: continue
        data = _extract_json(client, text, link)
        data.setdefault('source_url', link)
        obj, was_created = _save_opportunity(source, data, text)
        if obj:
            created += int(was_created); updated += int(not was_created)
    return {'source_id': source.pk, 'created': created, 'updated': updated, 'pages': len(feed.entries[:50])}

def scan_source(source: Source) -> dict:
    if source.source_type == 'rss':
        return scan_rss_source(source)
    if source.source_type == 'api':
        final_url, raw = fetch(source.url)
        try:
            import json
            payload = json.loads(raw)
        except Exception:
            payload = {}
        items = payload if isinstance(payload, list) else payload.get('items', payload.get('results', [])) if isinstance(payload, dict) else []
        created = updated = 0
        for item in items[:50]:
            text = json.dumps(item, ensure_ascii=False)
            data = _extract_json(AIClient(), text, item.get('url', final_url) if isinstance(item, dict) else final_url)
            obj, was_created = _save_opportunity(source, data, text)
            if obj: created += int(was_created); updated += int(not was_created)
        return {'source_id': source.pk, 'created': created, 'updated': updated, 'pages': min(len(items),50)}
    final_url, html = fetch(source.url)
    soup = BeautifulSoup(html, 'html.parser')
    plain = _clean(soup.get_text(' ', strip=True))
    client = AIClient()
    created = 0
    updated = 0
    processed_urls = []

    # Parse the source landing page first.
    pages = [(final_url, plain)]
    for link in _candidate_links(final_url, html):
        try:
            detail_url, detail_html = fetch(link)
            detail_text = _clean(BeautifulSoup(detail_html, 'html.parser').get_text(' ', strip=True))
            if len(detail_text) > 300:
                pages.append((detail_url, detail_text))
        except requests.RequestException:
            continue

    for page_url, text in pages[:MAX_LINKS + 1]:
        try:
            data = _extract_json(client, text, page_url)
            if not data.get('is_opportunity', True) and not data.get('is_job', True):
                continue
            data.setdefault('source_url', page_url)
            data.setdefault('application_url', page_url if 'apply' in page_url.lower() else '')
            obj, was_created = _save_opportunity(source, data, text)
            if obj:
                processed_urls.append(page_url)
                if was_created:
                    created += 1
                else:
                    updated += 1
        except Exception:
            # One bad page must not stop the entire source.
            continue

    return {'source_id': source.pk, 'created': created, 'updated': updated, 'pages': len(processed_urls)}
