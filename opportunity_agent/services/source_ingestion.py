from __future__ import annotations
import json
import re
from html import unescape
from urllib.parse import parse_qsl, urljoin, urlsplit
import requests
from bs4 import BeautifulSoup
try:
    import feedparser
except ImportError:
    feedparser = None

HEADERS={'User-Agent':'OpportunityHubSourceMonitor/1.0'}
KEYWORDS=('job','career','vacancy','position','apply','scholarship','fellowship','internship','grant','training','program','opportunity','research')

def fetch_public_source(url, timeout=20):
    from .retries import request_with_exponential_backoff

    def request():
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=timeout,
            allow_redirects=False,
        )
        response.raise_for_status()
        return response

    r=request_with_exponential_backoff(
        request,
        description=f'fetching public source {url}',
    )
    status_code = getattr(r, 'status_code', None)
    if isinstance(status_code, int) and 300 <= status_code < 400:
        raise ValueError('Source URL redirects; configure the final public URL directly.')
    final_url = r.url if isinstance(r.url, str) and r.url else url
    body = r.text if isinstance(r.text, str) else ''
    content_type = r.headers.get('content-type', '') if hasattr(r.headers, 'get') else ''
    return final_url, body, content_type if isinstance(content_type, str) else ''

def _contact(text):
    emails=sorted(set(re.findall(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}',text,re.I)))
    phone_match = re.search(
        r'\b(?:phone|telephone|tel|mobile|contact number)\s*[:\-]?\s*'
        r'([+()\d][\d\s().\-]{5,}\d)',
        text,
        re.I,
    )
    phones = [phone_match.group(1).strip()] if phone_match else []
    telegram=sorted(set(re.findall(r'(?:https?://)?t\.me/[A-Za-z0-9_+\-/]+',text,re.I)))
    return emails[0] if emails else '', phones[0].strip() if phones else '', telegram[0] if telegram else ''


def _safe_public_http_url(value, base_url=''):
    if not isinstance(value, str) or not value.strip():
        return ''
    if not base_url and not re.match(r'^https?://', value.strip(), re.I):
        return ''
    url = urljoin(base_url, value.strip())
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        return ''
    return url


def _url_key(value):
    parsed = urlsplit(value or '')
    query = tuple(sorted(
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith('utm_') and key.lower() not in {'fbclid', 'gclid'}
    ))
    hostname = (parsed.hostname or '').lower()
    if hostname.startswith('www.'):
        hostname = hostname[4:]
    return (
        parsed.scheme.lower(),
        hostname,
        parsed.path.rstrip('/') or '/',
        query,
    )


def _is_application_destination(value, page_urls, candidate_url_is_application=False):
    base_url = page_urls[0] if page_urls else ''
    destination = _safe_public_http_url(value, base_url)
    if not destination:
        return ''
    blocked_urls = page_urls[1:] if candidate_url_is_application else page_urls
    if any(url and _url_key(destination) == _url_key(url) for url in blocked_urls):
        return ''
    return destination


def extract_contact_destinations(candidate, structured=None, text=None):
    structured = structured if isinstance(structured, dict) else {}
    candidate = candidate if isinstance(candidate, dict) else {}
    text = text if isinstance(text, str) else candidate.get('text', '') or ''
    html = candidate.get('html', '') or ''
    page_url = candidate.get('url', '') or ''
    source_url = candidate.get('source_landing_url') or candidate.get('source_url') or ''
    soup = BeautifulSoup(html, 'html.parser') if html else BeautifulSoup('', 'html.parser')

    def explicit(*keys):
        for data in (structured, candidate):
            for key in keys:
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ''

    email = explicit('contact_email', 'email')
    phone = explicit('contact_phone', 'phone', 'telephone', 'mobile')
    telegram = explicit('telegram_contact', 'telegram')
    address = explicit('physical_address', 'application_address', 'address', 'street_address')
    website = explicit('organization_website', 'website', 'organization_url')

    links = []
    linked_phone = ''
    for anchor in soup.find_all('a', href=True):
        href = anchor['href'].strip()
        label = anchor.get_text(' ', strip=True).lower()
        links.append((href, label))
        if not email and href.lower().startswith('mailto:'):
            email = href[7:].split('?', 1)[0].strip()
        if not linked_phone and href.lower().startswith('tel:'):
            linked_phone = href[4:].split('?', 1)[0].strip()
        if not telegram and re.match(r'(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/', href, re.I):
            telegram = _safe_public_http_url(href if '://' in href else 'https://' + href)
        if not website and re.search(r'\b(?:official|organization|organisation|company)?\s*website\b|\bvisit (?:our )?site\b', label):
            website = _safe_public_http_url(href, page_url)

    if not email:
        match = re.search(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', text, re.I)
        if match:
            email = match.group(0)
    if not phone:
        match = re.search(
            r'\b(?:phone|telephone|tel|mobile|contact number)\s*[:\-]?\s*'
            r'([+()\d][\d\s().\-]{5,}\d)',
            text,
            re.I,
        )
        if match:
            phone = match.group(1).strip()
    if not phone:
        phone = linked_phone
    if not telegram:
        match = re.search(r'(?:(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/[A-Za-z0-9_+\-/]+)', text, re.I)
        if match:
            value = match.group(0)
            telegram = _safe_public_http_url(value if '://' in value else 'https://' + value)
    if not website:
        match = re.search(
            r'\b(?:official\s+|organization\s+|organisation\s+)?website\s*[:\-]\s*'
            r'(https?://[^\s<>"\']+)',
            text,
            re.I,
        )
        if match:
            website = _safe_public_http_url(match.group(1).rstrip('.,);'))
    if not address:
        address_tag = soup.find('address') or soup.find(attrs={'itemprop': re.compile(r'address', re.I)})
        if address_tag:
            address = address_tag.get_text(' ', strip=True)
    if not address:
        match = re.search(
            r'\b(?:physical\s+|application\s+|postal\s+)?address\s*[:\-]\s*(.+?)'
            r'(?=\s+(?:email|phone|telephone|tel|mobile|telegram|website|application url)\s*[:\-]|$)',
            text,
            re.I,
        )
        if match:
            address = match.group(1).strip(' .;,')

    if not email:
        email = next((value for value, _ in links if value.lower().startswith('mailto:')), '')
        email = email[7:].split('?', 1)[0].strip() if email else ''
    if website:
        website = _safe_public_http_url(website)

    return {
        'contact_email': email,
        'contact_phone': phone,
        'telegram_contact': telegram,
        'physical_address': address[:500],
        'organization_website': website,
    }


def extract_candidates(source, final_url, body):
    content_type=(source.source_type or '').lower()
    if content_type == 'api':
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f'Public API source did not return valid JSON: {exc}') from exc
        if isinstance(payload, dict):
            for key in ('opportunities', 'jobs', 'results', 'items', 'data'):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
        if not isinstance(payload, list):
            raise ValueError('Public API response must be a JSON list or contain a list under data, items, results, jobs, or opportunities.')
        for item in payload[:80]:
            if not isinstance(item, dict):
                continue
            title = item.get('title') or item.get('name') or item.get('headline')
            if not title:
                continue
            link = item.get('url') or item.get('link') or item.get('application_url') or ''
            candidate_url = urljoin(final_url, link) if link else final_url
            application_url_is_candidate = not item.get('url') and not item.get('link') and bool(item.get('application_url'))
            text = '\n'.join(
                f'{key}: {value}'
                for key, value in item.items()
                if isinstance(value, (str, int, float)) and str(value).strip()
            )
            yield {
                'title': str(title),
                'url': candidate_url,
                'source_landing_url': final_url,
                'text': text,
                'html': '',
                'data': item,
                '_application_url_is_candidate': application_url_is_candidate,
                'raw_source_content': json.dumps(item, ensure_ascii=False),
            }
        return
    if content_type=='rss' or 'xml' in body[:200].lower():
        if feedparser:
            feed=feedparser.parse(body)
            for entry in feed.entries[:80]:
                link=entry.get('link','')
                title=entry.get('title','').strip()
                summary=entry.get('summary','') or entry.get('description','') or ''
                text=BeautifulSoup(summary,'html.parser').get_text(' ',strip=True)
                if title or text:
                    yield {
                        'title': title,
                        'url': urljoin(final_url, link),
                        'source_landing_url': final_url,
                        'text': text,
                        'html': summary,
                        'raw_source_content': summary,
                    }
            return
    soup=BeautifulSoup(body,'html.parser')
    for tag in soup(['script','style','noscript']): tag.decompose()
    page_text=soup.get_text('\n',strip=True)
    links=[]
    for a in soup.find_all('a',href=True):
        label=' '.join(a.get_text(' ',strip=True).split()); href=urljoin(final_url,a['href'])
        hay=(label+' '+href).lower()
        if any(k in hay for k in KEYWORDS): links.append((label,href))
    seen=set()
    for label,href in links[:80]:
        if href in seen: continue
        seen.add(href)
        yield {
            'title': label or (soup.title.get_text(' ', strip=True) if soup.title else ''),
            'url': href,
            'source_landing_url': final_url,
            'fetch_detail_page': True,
            'text': page_text,
            'html': str(soup),
            'raw_source_content': body,
        }
    if not links and page_text:
        yield {
            'title': soup.title.get_text(' ', strip=True) if soup.title else '',
            'url': final_url,
            'source_landing_url': final_url,
            'fetch_detail_page': False,
            'text': page_text,
            'html': str(soup),
            'raw_source_content': body,
        }


def is_listing_page(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    soup = BeautifulSoup(candidate.get('html', '') or '', 'html.parser')
    headings = [
        candidate.get('title', ''),
        soup.find('h1').get_text(' ', strip=True) if soup.find('h1') else '',
        soup.title.get_text(' ', strip=True) if soup.title else '',
    ]
    headings = [
        re.sub(r'\s+', ' ', value).strip().casefold()
        for value in headings if value
    ]
    explicit_listing = re.compile(
        r'^(?:browsing\s*[:\-]|search results(?:\s+for)?\b|'
        r'(?:job|scholarship|training|opportunity|career|vacancy) categories\b)',
    )
    if any(explicit_listing.search(heading) for heading in headings):
        return True
    generic_category = re.compile(
        r'(?:all\s+)?(?:jobs?|scholarships?|training|opportunities|careers?|'
        r'vacancies|undergraduate|undergraduate opportunities|our blog|blog)',
    )
    if not any(generic_category.fullmatch(heading) for heading in headings):
        return False
    opportunity_links = [
        anchor for anchor in soup.find_all('a', href=True)
        if any(
            word in (anchor.get_text(' ', strip=True) + ' ' + anchor['href']).casefold()
            for word in KEYWORDS
        )
    ]
    return len({anchor['href'].split('#', 1)[0] for anchor in opportunity_links}) >= 3


def basic_extract(candidate, source):
    from .deadlines import extract_explicit_deadline, parse_deadline

    text = candidate.get('text', '') or ''
    title=(candidate.get('title') or '').strip()[:255]
    low=(title+' '+text).lower()
    kind = ''
    for key, terms in (
        ('scholarship', ('scholarship', 'bursary')),
        ('internship', ('internship',)),
        ('fellowship', ('fellowship',)),
        ('grant', ('grant', 'funding')),
        ('training', ('training',)),
        ('volunteer', ('volunteer',)),
        ('research', ('research', 'phd')),
        ('exchange', ('exchange',)),
        ('study', ('study opportunity', 'study program', 'study programme')),
        ('competition', ('competition',)),
        ('job', ('job', 'career', 'vacancy', 'vacancies', 'employment')),
    ):
        if any(term in low for term in terms):
            kind = key
            break
    structured = candidate.get('data')
    structured = structured if isinstance(structured, dict) else {}
    contacts = extract_contact_destinations(candidate, structured, text)

    def first(*keys):
        for key in keys:
            value = structured.get(key)
            if isinstance(value, (str, int, float, bool, list)) and value not in (None, '', [], {}):
                return value
        return ''

    def text_value(*keys):
        value = first(*keys)
        if isinstance(value, (str, int, float)):
            return str(value).strip()
        if isinstance(value, list):
            return '; '.join(
                str(item).strip()
                for item in value
                if isinstance(item, (str, int, float)) and str(item).strip()
            )
        return ''

    explicit_type = first('opportunity_type', 'type', 'category')
    if isinstance(explicit_type, str):
        allowed_types = {value for value, _ in source_opportunity_types()}
        explicit_type = explicit_type.strip().lower()
        if explicit_type in allowed_types:
            kind = explicit_type

    remote = first('remote_worldwide', 'worldwide_remote')
    if isinstance(remote, str):
        remote_value = remote.strip().lower()
        remote = True if remote_value in {'true', 'yes', '1', 'worldwide'} else (
            False if remote_value in {'false', 'no', '0'} else None
        )
    elif not isinstance(remote, bool):
        remote = None
    visa = first('visa_sponsorship', 'visa_sponsorship_available')
    if isinstance(visa, str):
        visa_value = visa.strip().lower()
        visa = True if visa_value in {'true', 'yes', '1'} else (
            False if visa_value in {'false', 'no', '0'} else None
        )
    elif not isinstance(visa, bool):
        visa = None

    candidate_url = candidate.get('url') or ''
    source_landing_url = candidate.get('source_landing_url') or candidate.get('source_url') or ''
    skills = first('skills', 'required_skills')
    languages = first('languages', 'required_languages')
    def list_value(value):
        if isinstance(value, str):
            return [item.strip() for item in re.split(r'[,;\n]+', value) if item.strip()]
        if isinstance(value, list):
            return [
                str(item).strip()
                for item in value
                if isinstance(item, (str, int, float)) and str(item).strip()
            ]
        return []

    html=candidate.get('html','') or ''
    form_url=''
    for href in re.findall(r'href=[\"\']([^\"\']+)', html, re.I):
        absolute=urljoin(candidate_url,href)
        if re.search(r'(application|cv|resume|form|candidate).*\.(pdf|docx?)$', absolute, re.I) or absolute.lower().endswith(('.pdf','.doc','.docx')):
            form_url=absolute; break
    form_type='pdf' if form_url.lower().endswith('.pdf') else ('docx' if form_url.lower().endswith(('.doc','.docx')) else '')
    application_link_pattern = re.compile(
        r'\b(?:apply(?:\s+now)?|application\s+(?:form|portal)|'
        r'submit\s+(?:an?\s+)?application|online\s+application|'
        r'registration\s*/\s*application\s+form)\b',
        re.I,
    )
    application_url = ''
    page = BeautifulSoup(html, 'html.parser')
    for anchor in page.find_all('a', href=True):
        label = ' '.join(filter(None, (
            anchor.get_text(' ', strip=True),
            anchor.get('aria-label', ''),
            anchor.get('title', ''),
        )))
        if not application_link_pattern.search(label):
            continue
        destination = _safe_public_http_url(anchor['href'], candidate_url)
        if destination and not any(
            blocked_url and _url_key(destination) == _url_key(blocked_url)
            for blocked_url in (candidate_url, source_landing_url)
        ):
            application_url = destination
        else:
            application_url = ''
        if application_url:
            break
    if not application_url:
        structured_application_url = text_value('application_url', 'apply_url')
        application_url = _is_application_destination(
            structured_application_url,
            (candidate_url, source_landing_url),
            candidate.get('_application_url_is_candidate') is True,
        )
    work_mode = text_value('work_mode', 'work_arrangement').lower()
    if not work_mode and structured.get('remote') is True:
        work_mode = 'remote'
    elif not work_mode and isinstance(structured.get('remote'), str):
        remote_mode = structured['remote'].strip().lower()
        if remote_mode in {'on_site', 'hybrid', 'remote'}:
            work_mode = remote_mode
    if work_mode not in {'on_site', 'hybrid', 'remote'}:
        work_mode = ''
    deadline_value = first('deadline', 'application_deadline')
    deadline = parse_deadline(deadline_value) if deadline_value else None
    if deadline is None:
        deadline = extract_explicit_deadline(text)
    return {
        'title': title or text_value('title', 'name', 'headline')[:255],
        'organization': text_value('organization', 'employer', 'company', 'institution'),
        'opportunity_type': kind,
        'country': text_value('country', 'location_country'),
        'city': text_value('city', 'location_city'),
        'work_mode': work_mode,
        'remote_worldwide': remote,
        'description': text_value('description', 'summary') or text,
        'responsibilities': text_value('responsibilities', 'duties'),
        'requirements': text_value(
            'requirements',
            'eligibility_requirements',
            'eligibility',
            'application_requirements',
            'application_instructions',
        ),
        'qualifications': text_value('qualifications', 'required_qualifications'),
        'education_requirements': text_value(
            'education_requirements',
            'education',
            'educational_requirements',
        ),
        'experience_requirements': text_value(
            'experience_requirements',
            'experience',
            'required_experience',
        ),
        'skills': list_value(skills),
        'languages': list_value(languages),
        'salary_stipend': text_value('salary_stipend', 'salary', 'stipend', 'compensation'),
        'benefits': text_value('benefits'),
        'visa_sponsorship': visa,
        'deadline': deadline,
        'application_url': application_url,
        'application_form_url': text_value('application_form_url', 'form_url') or form_url,
        'application_form_type': text_value('application_form_type') or form_type,
        'source_url': candidate_url,
        **contacts,
        'raw_source_content': candidate.get('raw_source_content') or text,
    }


def source_opportunity_types():
    from ..models import Opportunity

    return Opportunity.OPPORTUNITY_TYPES
