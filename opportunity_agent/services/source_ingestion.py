from __future__ import annotations
import json
import logging
import re
from html import unescape
from urllib.parse import parse_qsl, urljoin, urlsplit
from bs4 import BeautifulSoup
try:
    import feedparser
except ImportError:
    feedparser = None

from .public_http import public_addresses

logger = logging.getLogger(__name__)

HEADERS={'User-Agent':'OpportunityHubSourceMonitor/1.0'}
KEYWORDS=('job','career','vacancy','position','apply','scholarship','fellowship','internship','grant','training','program','opportunity','research')
APPLICATION_LINK_PATTERN = re.compile(
    r'\b(?:apply(?:\s+(?:now|online))?|application\s+(?:form|portal)|'
    r'submit\s+(?:an?\s+)?application|online\s+application|'
    r'register(?:\s+now)?|start\s+(?:an?\s+)?application|'
    r'registration\s*/\s*application\s+form)\b',
    re.I,
)

def fetch_public_source(url, timeout=20):
    from .retries import request_with_exponential_backoff
    from .public_http import get_public_response

    def request():
        return get_public_response(
            url,
            headers=HEADERS,
            timeout=timeout,
        )

    r=request_with_exponential_backoff(
        request,
        description=f'fetching public source {url}',
    )
    status_code = getattr(r, 'status_code', None)
    if isinstance(status_code, int) and 300 <= status_code < 400:
        raise ValueError('Source URL redirects; configure the final public URL directly.')
    final_url = r.url if isinstance(r.url, str) and r.url else url
    try:
        body = r.text if isinstance(r.text, str) else ''
        content_type = r.headers.get('content-type', '') if hasattr(r.headers, 'get') else ''
        return final_url, body, content_type if isinstance(content_type, str) else ''
    finally:
        r.close()

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
    try:
        url = urljoin(base_url, value.strip())
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {'http', 'https'}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return ''
        public_addresses(url)
    except (TypeError, ValueError) as exc:
        logger.info('Rejected an unsafe or invalid opportunity destination (%s).', exc)
        return ''
    return url


def _is_application_form_file(value, base_url=''):
    if not isinstance(value, str):
        return False
    try:
        path = urlsplit(urljoin(base_url, value.strip())).path
    except ValueError:
        return False
    return bool(re.search(r'\.(?:pdf|docx?)$', path, re.I))


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


LISTING_QUERY_KEYS = {
    'advanced-search', 'category', 'country', 'date', 'field', 'filter',
    'format', 'language', 'list', 'location', 'offset', 'organization',
    'page', 'per_page', 'q', 'query', 'search', 'sort', 'source', 'theme', 'type',
}


def listing_url_reason(value):
    """Return a reason for known source listing URLs, without rejecting every query URL."""
    try:
        parsed = urlsplit(str(value or ''))
    except ValueError:
        return ''
    host = (parsed.hostname or '').lower().removeprefix('www.')
    path = re.sub(r'/+', '/', parsed.path or '/').rstrip('/') or '/'
    params = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if host == 'reliefweb.int':
        # Individual ReliefWeb entities use singular, numeric entity routes.
        detail_route = bool(re.match(r'^/(?:job|training)/\d+(?:/|$)', path, re.I))
        query_listing_keys = params - ({'source'} if detail_route else set())
        if query_listing_keys & LISTING_QUERY_KEYS or any(
            key.startswith(('country_', 'organization_', 'field_', 'theme_', 'format_'))
            or key.endswith('[]') and key[:-2] in LISTING_QUERY_KEYS
            for key in query_listing_keys
        ):
            return 'ReliefWeb search or filter parameters'
        if not detail_route and (
            path in {'/job', '/jobs', '/training', '/search'}
            or re.match(r'^/(?:countries?|organizations?)(?:/|$)', path, re.I)
        ):
            return 'ReliefWeb category or listing route'
    if host == 'opportunitydesk.org':
        if params & {'s', 'search', 'paged', 'page', 'cat', 'category', 'tag'}:
            return 'Opportunity Desk search or archive parameters'
        if re.match(r'^/(?:category|tag|author|archives?)(?:/|$)', path, re.I):
            return 'Opportunity Desk category or archive route'
        if path == '/' or re.match(r'^/\d{4}/?$', path):
            return 'Opportunity Desk homepage or date archive'
    if params & (
        (LISTING_QUERY_KEYS - {'source'})
        | {'cat', 'paged', 'q', 's'}
    ) and not (host == 'reliefweb.int' and detail_route):
        return 'Query-based search or listing parameters'
    return ''


def detail_page_content(url, body):
    """Prefer the source article/entity body over navigation and listing text."""
    soup = BeautifulSoup(body or '', 'html.parser')
    host = (urlsplit(url).hostname or '').lower().removeprefix('www.')
    selectors = []
    if host == 'reliefweb.int':
        selectors.extend(('[itemprop="articleBody"]', '.rw-entity-content', '.rw-entity-body'))
    elif host == 'opportunitydesk.org':
        selectors.extend(('article .entry-content', '.post-content', '[itemprop="articleBody"]'))
    source_selectors = len(selectors)
    selectors.extend(('main article', 'article', 'main', '[role="main"]'))
    for index, selector in enumerate(selectors):
        node = soup.select_one(selector)
        minimum_length = 40 if index < source_selectors else 80
        if node and len(node.get_text(' ', strip=True)) >= minimum_length:
            return node
    return soup


def _is_application_destination(value, page_urls, candidate_url_is_application=False):
    base_url = page_urls[0] if page_urls else ''
    destination = _safe_public_http_url(value, base_url)
    if not destination:
        return ''
    if urlsplit(destination).path.lower().endswith((
        '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.pdf', '.doc', '.docx',
    )):
        return ''
    if (urlsplit(destination).hostname or '').casefold() in {
        't.me', 'www.t.me', 'telegram.me', 'www.telegram.me',
    }:
        return ''
    blocked_urls = page_urls[1:] if candidate_url_is_application else page_urls
    if any(url and _url_key(destination) == _url_key(url) for url in blocked_urls):
        return ''
    return destination


def _is_application_form_destination(value, page_urls):
    destination = _safe_public_http_url(
        value,
        page_urls[0] if page_urls else '',
    )
    if not destination:
        return ''
    if any(url and _url_key(destination) == _url_key(url) for url in page_urls):
        return ''
    if (urlsplit(destination).hostname or '').casefold() in {
        't.me', 'www.t.me', 'telegram.me', 'www.telegram.me',
    }:
        return ''
    if urlsplit(destination).path.lower().endswith((
        '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp',
    )):
        return ''
    return destination


def _application_link_context(text, start):
    line_start = max(text.rfind('\n', 0, start), text.rfind('\r', 0, start)) + 1
    previous_line_start = max(
        text.rfind('\n', 0, line_start),
        text.rfind('\r', 0, line_start),
    ) + 1
    return text[max(previous_line_start, start - 200):start]


def _anchor_label(anchor):
    parts = [
        anchor.get_text(' ', strip=True),
        anchor.get('aria-label', ''),
        anchor.get('title', ''),
        anchor.get('value', ''),
    ]
    labelled_by = anchor.get('aria-labelledby', '').split()
    if labelled_by:
        soup = anchor.find_parent('html') or anchor.find_parent()
        for identifier in labelled_by:
            label = soup.find(id=identifier) if soup else None
            if label:
                parts.append(label.get_text(' ', strip=True))
    return ' '.join(' '.join(part.split()) for part in parts if part)


def _structured_page_data(soup):
    extracted = {}
    aliases = {
        'applicationurl': 'application_url',
        'application_url': 'application_url',
        'applyurl': 'application_url',
        'apply_url': 'application_url',
        'applicationformurl': 'application_form_url',
        'application_form_url': 'application_form_url',
    }

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for key, child in value.items():
                normalized_key = re.sub(r'[^a-z_]', '', key.casefold())
                target = aliases.get(normalized_key)
                if target and isinstance(child, str) and child.strip():
                    extracted.setdefault(target, child.strip())
                if key == 'potentialAction' and isinstance(child, (dict, list)):
                    actions = child if isinstance(child, list) else [child]
                    for action in actions:
                        if not isinstance(action, dict):
                            continue
                        action_type = action.get('@type', '')
                        if 'ApplyAction' in action_type:
                            destination = action.get('target')
                            if isinstance(destination, dict):
                                destination = (
                                    destination.get('urlTemplate')
                                    or destination.get('url')
                                )
                            if isinstance(destination, str) and destination.strip():
                                extracted.setdefault('application_url', destination.strip())
                visit(child)

    for script in soup.find_all('script', type=re.compile(r'application/ld\+json', re.I)):
        try:
            visit(json.loads(script.string or script.get_text()))
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.debug('Ignoring malformed JSON-LD while extracting an opportunity.')
    return extracted


def _instruction_context(text, destination, window=220):
    if not destination:
        return ''
    match = re.search(re.escape(destination), text, re.I)
    if not match:
        return ''
    start = max(0, match.start() - window)
    end = min(len(text), match.end() + window)
    context = text[start:end]
    return ' '.join(context.split())[:500]


def extract_application_methods(candidate, text, contacts, application_url, form_url):
    structured = candidate.get('data')
    structured = structured if isinstance(structured, dict) else {}
    soup = BeautifulSoup(candidate.get('html', '') or '', 'html.parser')
    page_url = candidate.get('url') or ''
    source_text = ' '.join((text or '').split())
    routes = []

    def add(method, destination='', instructions=''):
        instructions = ' '.join(str(instructions or '').split())[:500]
        destination = str(destination or '').strip()
        if not destination and not instructions:
            return
        route = {
            'method': method,
            'destination': destination,
            'instructions': instructions,
        }
        if route not in routes:
            routes.append(route)

    if application_url:
        instructions = _instruction_context(source_text, application_url)
        for anchor in soup.find_all('a', href=True):
            destination = _is_application_destination(
                anchor['href'].strip(),
                (page_url,),
            )
            if destination != application_url:
                continue
            label = _anchor_label(anchor)
            parent_text = anchor.parent.get_text(' ', strip=True) if anchor.parent else ''
            explicit_context = ' '.join(filter(None, (label, parent_text)))
            if APPLICATION_LINK_PATTERN.search(explicit_context):
                instructions = ' '.join(filter(None, (instructions, explicit_context)))
                break
        add('online', application_url, instructions)
    if form_url:
        add('form', form_url, _instruction_context(source_text, form_url))

    email_instructions = (
        r'\b(?:apply|application|submit|send)\b'
        r'[\w\s,./()\-]{0,120}\b(?:by|via|through|to|email|e-?mail|cv|resume|résumé|'
        r'application|supporting documents|documents)\b|'
        r'\b(?:email|e-?mail)\b[\w\s,./()\-]{0,100}\b'
        r'(?:your\s+)?(?:application|cv|resume|résumé|supporting documents|documents)\b|'
        r'\b(?:application|cv|resume|résumé|supporting documents|documents)\b'
        r'[\w\s,./()\-]{0,100}\b(?:by|via)\s+(?:e-?mail|email)\b'
    )
    emails = {contacts.get('contact_email', '')}
    emails.update(re.findall(
        r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}',
        source_text,
        re.I,
    ))
    for anchor in soup.find_all('a', href=True):
        href = anchor['href'].strip()
        if href.lower().startswith('mailto:'):
            emails.add(href[7:].split('?', 1)[0].strip())
    for email in filter(None, emails):
        context = _instruction_context(source_text, email, 180)
        for anchor in soup.find_all('a', href=True):
            href = anchor['href'].strip()
            if not href.lower().startswith('mailto:') or href[7:].split('?', 1)[0].strip().casefold() != email.casefold():
                continue
            parent_text = anchor.parent.get_text(' ', strip=True) if anchor.parent else ''
            context = ' '.join((context, _anchor_label(anchor), parent_text))
        if re.search(email_instructions, context, re.I):
            add('email', email, context)

    telegram_destinations = {contacts.get('telegram_contact', '')}
    for anchor in soup.find_all('a', href=True):
        href = anchor['href'].strip()
        if re.match(r'^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/', href, re.I):
            safe_telegram = _safe_public_http_url(
                href if '://' in href else 'https://' + href,
            )
            if safe_telegram:
                telegram_destinations.add(safe_telegram)
    telegram_destinations.update(re.findall(
        r'(?:(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/[A-Za-z0-9_+\-/]+)',
        source_text,
        re.I,
    ))
    for telegram_candidate in filter(None, telegram_destinations):
        telegram = telegram_candidate
        if re.match(r'^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/', telegram, re.I):
            telegram = _safe_public_http_url(
                telegram if '://' in telegram else 'https://' + telegram,
            )
        if not telegram:
            continue
        context = _instruction_context(source_text, telegram, 180)
        for anchor in soup.find_all('a', href=True):
            if anchor['href'].strip().casefold() == telegram.casefold() or (
                '://' not in anchor['href']
                and ('https://' + anchor['href'].lstrip('/')).casefold() == telegram.casefold()
            ):
                parent_text = anchor.parent.get_text(' ', strip=True) if anchor.parent else ''
                context = ' '.join((context, _anchor_label(anchor), parent_text))
        if re.search(
            r'\b(?:apply|application|submit|send)\b.{0,120}\btelegram\b|'
            r'\btelegram\b.{0,120}\b(?:apply|application|submit|send)\b|'
            r'\b(?:applicants?|contact)\b.{0,120}\btelegram\b'
            r'.{0,120}\b(?:for\s+)?(?:the\s+)?application\b|'
            r'\btelegram\b.{0,120}\b(?:for\s+)?(?:the\s+)?application\b'
            r'.{0,120}\b(?:contact|applicants?)\b',
            context,
            re.I,
        ):
            add('telegram', telegram, context)

    address = contacts.get('physical_address', '')
    if address:
        context = _instruction_context(source_text, address, 220)
        address_delivery_instruction = re.search(
            r'\b(?:in person|deliver|mail|post|send|submit)\b.{0,140}'
            r'\b(?:application|documents|application form|cv|resume|résumé)\b'
            r'.{0,100}\baddress\b|'
            r'\b(?:application|documents|application form|cv|resume|résumé)\b'
            r'.{0,140}\b(?:in person|deliver|mail|post|send|submit)\b',
            source_text,
            re.I,
        )
        local_delivery_instruction = re.search(
            r'\b(?:in person|deliver|mail|post|send|submit)\b.{0,140}'
            r'\b(?:application|documents|application form|cv|resume|résumé)\b|'
            r'\b(?:application|documents|application form|cv|resume|résumé)\b'
            r'.{0,140}\b(?:in person|deliver|mail|post|send|submit)\b',
            context,
            re.I,
        )
        if address_delivery_instruction or local_delivery_instruction:
            add('physical', address, context)

    # Phone application is not a primary method in the current model choices.
    phone = contacts.get('contact_phone', '')
    if phone and re.search(re.escape(phone), source_text, re.I):
        context = _instruction_context(source_text, phone, 180)
        if re.search(
            r'\b(?:apply|application|submit)\b.{0,100}\b(?:phone|call|telephone)\b|'
            r'\b(?:phone|call|telephone)\b.{0,100}\b(?:apply|application|submit)\b',
            context,
            re.I,
        ):
            add('phone', phone, context)

    email = contacts.get('contact_email', '')
    phone = contacts.get('contact_phone', '')
    for anchor in soup.find_all('a', href=True):
        href = anchor['href'].strip()
        label = _anchor_label(anchor)
        if href.lower().startswith('mailto:'):
            linked_email = href[7:].split('?', 1)[0].strip()
            parent_text = anchor.parent.get_text(' ', strip=True) if anchor.parent else ''
            surrounding = ' '.join((
                label,
                parent_text,
                _instruction_context(source_text, label or linked_email, 160),
            ))
            if linked_email == email and re.search(email_instructions, surrounding, re.I):
                add('email', linked_email, surrounding)
        elif href.lower().startswith('tel:'):
            linked_phone = href[4:].split('?', 1)[0].strip()
            parent_text = anchor.parent.get_text(' ', strip=True) if anchor.parent else ''
            surrounding = ' '.join((
                label,
                parent_text,
                _instruction_context(source_text, label or linked_phone, 160),
            ))
            if linked_phone == phone and re.search(
                r'\b(?:apply|application|submit)\b.{0,100}\b(?:phone|call|telephone)\b|'
                r'\b(?:phone|call|telephone)\b.{0,100}\b(?:apply|application|submit)\b',
                surrounding,
                re.I,
            ):
                add('phone', linked_phone, surrounding)

    # Explicitly named structured fields are source evidence; generic contact
    # fields are intentionally insufficient to establish an application route.
    structured_methods = (
        ('email', ('application_email', 'application_contact_email')),
        ('telegram', ('application_telegram', 'application_telegram_contact')),
        ('physical', ('application_address', 'application_physical_address')),
    )
    for method, keys in structured_methods:
        for key in keys:
            destination = structured.get(key)
            if isinstance(destination, str) and destination.strip():
                if method == 'email' and not re.fullmatch(
                    r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}',
                    destination.strip(),
                    re.I,
                ):
                    continue
                if method == 'telegram' and not re.fullmatch(
                    r'(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/[A-Za-z0-9_+\-/]+|'
                    r'@?[A-Za-z][A-Za-z0-9_]{3,30}[A-Za-z0-9]',
                    destination.strip(),
                    re.I,
                ):
                    continue
                add(method, destination, f'Explicit source metadata field: {key}.')
                break

    # Keep only supported application methods as verified routes. Phone is
    # retained in the ordinary contact field and instructions but requires
    # review until the schema supports it as an application method.
    supported_order = ('online', 'form', 'email', 'telegram', 'physical', 'phone')
    return sorted(
        routes,
        key=lambda route: supported_order.index(route['method']),
    )


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

    email = explicit('application_email', 'application_contact_email', 'contact_email', 'email')
    phone = explicit('contact_phone', 'phone', 'telephone', 'mobile')
    telegram = explicit(
        'application_telegram',
        'application_telegram_contact',
        'telegram_contact',
        'telegram',
    )
    address = explicit(
        'application_address',
        'application_physical_address',
        'physical_address',
        'address',
        'street_address',
    )
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
            website = href

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
        website = _safe_public_http_url(website, page_url)

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
        for item in payload:
            if not isinstance(item, dict):
                continue
            title = item.get('title') or item.get('name') or item.get('headline')
            if not title:
                continue
            detail_link = item.get('url') or item.get('link') or ''
            if not isinstance(detail_link, str):
                detail_link = ''
            candidate_url = urljoin(final_url, detail_link) if detail_link else final_url
            text = '\n'.join(
                f'{key}: {value}'
                for key, value in item.items()
                if isinstance(value, (str, int, float)) and str(value).strip()
            )
            yield {
                'title': str(title),
                'url': candidate_url,
                'source_landing_url': final_url,
                'fetch_detail_page': bool(detail_link),
                '_url_is_source_landing': not bool(detail_link),
                'text': text,
                'html': '',
                'data': item,
                'raw_source_content': json.dumps(item, ensure_ascii=False),
            }
        return
    if content_type=='rss' or 'xml' in body[:200].lower():
        if feedparser:
            feed=feedparser.parse(body)
            for entry in feed.entries:
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
        if listing_url_reason(href):
            continue
        if any(k in hay for k in KEYWORDS): links.append((label,href))
    seen=set()
    for label,href in links:
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


def _has_structured_opportunity_detail(candidate, soup):
    nodes = []

    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            nodes.append(value)
            for child in value.values():
                collect(child)

    data = candidate.get('data')
    if isinstance(data, dict):
        collect(data)
    for script in soup.find_all('script', type=re.compile(r'application/ld\+json', re.I)):
        try:
            collect(json.loads(script.string or script.get_text()))
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.debug('Ignoring malformed JSON-LD while classifying an opportunity page.')

    detailed_nodes = {}
    for node in nodes:
        raw_type = node.get('@type', '')
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        types = {str(value).replace(' ', '').casefold() for value in types}
        title = node.get('title') or node.get('name') or ''
        description = node.get('description') or node.get('summary') or ''
        if not isinstance(title, str) or not isinstance(description, str):
            continue
        if len(description.strip()) < 80:
            continue
        is_opportunity_type = bool(types & {
            'jobposting', 'event', 'scholarship', 'opportunity',
            'educationaloccupationalprogram',
        })
        has_detail_fields = any(node.get(key) for key in (
            'applicationUrl', 'applyUrl', 'directApply', 'validThrough',
            'eligibility', 'qualifications', 'hiringOrganization',
            'organizer', 'provider', 'jobLocation', 'location',
        ))
        if is_opportunity_type and has_detail_fields:
            key = (
                str(node.get('@id') or node.get('url') or '').casefold(),
                title.strip().casefold(),
                description.strip(),
            )
            detailed_nodes[key] = node

    if len(detailed_nodes) == 1:
        return True
    if len(detailed_nodes) > 1:
        return False
    if (
        isinstance(data, dict)
        and not candidate.get('_url_is_source_landing')
        and isinstance(data.get('description'), str)
        and len(data['description'].strip()) >= 120
        and any(data.get(key) for key in (
            'application_url', 'apply_url', 'application_instructions',
            'eligibility', 'deadline',
        ))
    ):
        return True
    return False


def is_listing_page(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    url_reason = listing_url_reason(candidate.get('url', ''))
    if url_reason:
        return True
    soup = BeautifulSoup(candidate.get('html', '') or '', 'html.parser')
    h1 = soup.find('h1')
    document_title = soup.title.get_text(' ', strip=True) if soup.title else ''
    headings = [
        candidate.get('title', ''),
        h1.get_text(' ', strip=True) if h1 else '',
        document_title,
    ]
    headings = [
        re.sub(r'\s+', ' ', value).strip().casefold()
        for value in headings if value
    ]
    primary_heading = next((
        re.sub(r'\s+', ' ', value).strip().casefold()
        for value in (
            h1.get_text(' ', strip=True) if h1 else '',
            candidate.get('title', ''),
            document_title,
        )
        if value
    ), '')
    if (urlsplit(candidate.get('url', '')).hostname or '').lower().removeprefix('www.') == 'opportunitydesk.org':
        if re.search(
            r'\b(?:deadline roundup|roundup of|opportunities closing|top \d+ '
            r'(?:opportunities|scholarships|jobs)|this week(?:\'s)? opportunities)\b',
            ' '.join(headings),
            re.I,
        ):
            return True
    explicit_listing = re.compile(
        r'^(?:browsing|search results|search|archive|archives|category|categories)'
        r'\s*[:\-]?\s*(?:for\s+)?(?:all\s+)?'
        r'(?:jobs?|scholarships?|training|conferences?|phd(?:\s*/\s*postdoctoral)?|'
        r'postdoctoral(?:\s*/\s*phd)?|internships?|fellowships?|grants?|'
        r'opportunities|careers?|vacancies|programs?|programmes?)?\s*$',
        re.I,
    )
    if any(explicit_listing.search(heading) for heading in headings):
        return True
    has_structured_detail = _has_structured_opportunity_detail(candidate, soup)
    generic_category = re.compile(
        r'(?:(?:all|browse|view)\s+)?(?:jobs?|scholarships?|training|conferences?|'
        r'opportunities|careers?|vacancies|internships?|fellowships?|grants?|'
        r'phd(?:\s*/\s*postdoctoral)?|postdoctoral(?:\s*/\s*phd)?|'
        r'undergraduate(?:\s+opportunities)?|our blog|blog|archives?|'
        r'(?:job|scholarship|training|opportunity|career|vacancy)\s+categories)',
    )
    generic_directory = re.compile(
        r'(?:opportunit(?:y|ies)|jobs?|scholarships?|training|conferences?|'
        r'internships?|fellowships?|grants?)\s+(?:archives?|direct(?:ory|ories)|categories)',
        re.I,
    )
    generic_page_heading = bool(
        generic_category.fullmatch(primary_heading)
        or generic_directory.fullmatch(primary_heading)
    )

    canonical = soup.find(
        'link',
        rel=lambda value: value and any(
            str(item).casefold() == 'canonical'
            for item in (value if isinstance(value, list) else [value])
        ),
    )
    canonical_url = canonical.get('href', '') if canonical else ''
    page_url = candidate.get('url', '')
    if canonical_url and listing_url_reason(urljoin(page_url, canonical_url)):
        return True
    listing_path = any(
        re.search(
            r'/(?:category|categories|archive|archives|tag|page)(?:/|$)|'
            r'/page/\d+/?$',
            urlsplit(value).path.casefold(),
        )
        for value in (page_url, canonical_url)
        if value
    )
    description = soup.find(
        'meta',
        attrs={'name': re.compile(r'description', re.I)},
    ) or soup.find('meta', attrs={'property': 'og:description'})
    meta_text = ' '.join(filter(None, (
        description.get('content', '') if description else '',
        (soup.find('meta', attrs={'property': 'og:type'}) or {}).get('content', ''),
    ))).casefold()
    listing_meta = bool(re.search(
        r'\b(?:browse|all (?:open )?|search results|archive|directory|'
        r'latest listings|opportunities directory)\b',
        meta_text,
    ))

    relevant_links = [
        anchor for anchor in soup.find_all('a', href=True)
        if re.search(
            r'\b(?:job|career|vacanc|scholarship|fellowship|internship|grant|'
            r'training|conference|phd|postdoctoral|opportunit)\w*\b',
            ' '.join((_anchor_label(anchor), anchor.get('href', ''))),
            re.I,
        )
    ]
    repeated_cards = sum(
        1 for node in soup.find_all(['article', 'li', 'div'])
        if re.search(
            r'(?:opportunit|listing|result|job|scholarship|event|card)',
            ' '.join(node.get('class', [])),
            re.I,
        )
    )
    has_specific_heading = bool(
        primary_heading
        and not generic_category.fullmatch(primary_heading)
        and not generic_directory.fullmatch(primary_heading)
        and not explicit_listing.search(primary_heading)
    )
    if listing_path and (listing_meta or len(relevant_links) >= 3 or repeated_cards >= 3):
        return True
    if listing_meta and not has_specific_heading and len(relevant_links) >= 2:
        return True
    if len(relevant_links) >= 5 and repeated_cards >= 3 and not has_specific_heading:
        return True
    if generic_page_heading:
        if not has_structured_detail:
            return True
        if listing_path or listing_meta or len(relevant_links) >= 3 or repeated_cards >= 3:
            return True
        return False
    return False


def is_listing_opportunity(opportunity):
    """Check saved URL and page evidence without treating an unknown URL shape as a listing."""
    return is_listing_page({
        'title': opportunity.title,
        'url': opportunity.source_url,
        'html': opportunity.raw_source_content,
        'text': opportunity.description,
    })


def opportunity_detail_validation_error(candidate):
    """Return why a fetched page is not sufficiently verified as one opportunity."""
    candidate = candidate if isinstance(candidate, dict) else {}
    url = candidate.get('url', '')
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return 'Invalid detail URL'
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return 'Detail URL is not a public HTTP(S) URL'
    url_reason = listing_url_reason(url)
    if url_reason:
        return url_reason
    if is_listing_page(candidate):
        return 'Page content indicates a listing, category, archive, or roundup'

    soup = BeautifulSoup(candidate.get('html', '') or '', 'html.parser')
    title_node = soup.find('h1') or soup.title
    title = str(candidate.get('title') or '').strip()
    if title_node:
        title = title_node.get_text(' ', strip=True) or title
    generic_heading = re.compile(
        r'^(?:jobs?|training|opportunities|scholarships?|internships?|'
        r'fellowships?|grants?|search results|category|archive)$',
        re.I,
    )
    if not title or generic_heading.fullmatch(title):
        return 'Page has no specific opportunity title'

    content = detail_page_content(url, candidate.get('html', '') or '')
    text = content.get_text(' ', strip=True)
    if len(text) < 40:
        return 'Opportunity detail content is too short to verify'
    has_structured_detail = _has_structured_opportunity_detail(candidate, soup)
    has_detail_evidence = bool(re.search(
        r'\b(?:apply|application|deadline|eligibility|requirements?|'
        r'qualifications?|responsibilities|position|vacancy|employer|'
        r'organization|organisation|scholarship|fellowship|internship|'
        r'job|training|grant|stipend|salary)\b',
        text,
        re.I,
    ))
    if not has_structured_detail and not has_detail_evidence:
        return 'Page content does not contain opportunity-specific details'

    return ''


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
    page_soup = BeautifulSoup(candidate.get('html', '') or '', 'html.parser')
    structured = {**_structured_page_data(page_soup), **structured}
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
            readable = []
            for item in value:
                if isinstance(item, (str, int, float)):
                    text_item = str(item).strip()
                elif isinstance(item, dict):
                    text_item = item.get('text')
                    text_item = text_item.strip() if isinstance(text_item, str) else ''
                else:
                    text_item = ''
                if text_item:
                    readable.append(text_item)
            return '; '.join(readable)
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
    source_url = '' if candidate.get('_url_is_source_landing') else candidate_url
    skills = first('skills', 'required_skills')
    languages = first('languages', 'required_languages')
    def list_value(value):
        if isinstance(value, str):
            return [item.strip() for item in re.split(r'[,;\n]+', value) if item.strip()]
        if isinstance(value, list):
            normalized = []
            for item in value:
                if isinstance(item, (str, int, float)):
                    text_item = str(item).strip()
                elif isinstance(item, dict):
                    text_item = item.get('text') or item.get('name') or ''
                    text_item = text_item.strip() if isinstance(text_item, str) else ''
                else:
                    text_item = ''
                if text_item:
                    normalized.append(text_item)
            return normalized
        return []

    html=candidate.get('html','') or ''
    form_url=''
    application_url = ''
    for anchor in page_soup.find_all('a', href=True):
        label = _anchor_label(anchor)
        href = anchor['href'].strip()
        is_application_link = bool(APPLICATION_LINK_PATTERN.search(label))
        is_labeled_form = bool(re.search(
            r'\b(?:application|apply)\s+(?:form|forms)\b|\bform\s+for\s+applications?\b',
            label,
            re.I,
        ))
        is_form_asset = (
            _is_application_form_file(href, candidate_url)
            and bool(re.search(r'\b(?:application|apply|form)\b', label + ' ' + href, re.I))
        )
        if not is_application_link and not is_form_asset and not is_labeled_form:
            continue
        if is_form_asset or is_labeled_form:
            form_url = _is_application_form_destination(
                href,
                (candidate_url, source_landing_url),
            )
            continue
        destination = _is_application_destination(
            href,
            (candidate_url, source_landing_url),
        )
        if destination:
            application_url = destination
            break
    if not application_url:
        for form in page_soup.find_all('form', action=True):
            controls = form.find_all(['button', 'input'])
            labels = [_anchor_label(control) for control in controls]
            labels.extend(form.get(key, '') for key in ('aria-label', 'title', 'name', 'id'))
            if not APPLICATION_LINK_PATTERN.search(' '.join(labels)):
                continue
            destination = _safe_public_http_url(form['action'], candidate_url)
            destination = _is_application_destination(
                destination,
                (candidate_url, source_landing_url),
            )
            if destination:
                application_url = destination
                break
    if not application_url:
        for match in re.finditer(r'https?://[^\s<>"\']+', text, re.I):
            context = _application_link_context(text, match.start())
            if not APPLICATION_LINK_PATTERN.search(context):
                continue
            raw_destination = match.group(0).rstrip('.,);')
            if _is_application_form_file(raw_destination):
                form_url = _is_application_form_destination(
                    raw_destination,
                    (candidate_url, source_landing_url),
                )
                if form_url:
                    break
                continue
            application_url = _is_application_destination(
                raw_destination,
                (candidate_url, source_landing_url),
            )
            if application_url:
                break
    if not application_url:
        structured_application_url = text_value('application_url', 'apply_url')
        if _is_application_form_file(structured_application_url, candidate_url):
            form_url = _is_application_form_destination(
                structured_application_url,
                (candidate_url, source_landing_url),
            ) or form_url
        else:
            application_url = _is_application_destination(
                structured_application_url,
                (candidate_url, source_landing_url),
            )
    structured_form_url = text_value('application_form_url')
    if structured_form_url:
        form_url = _is_application_form_destination(
            structured_form_url,
            (candidate_url, source_landing_url),
        ) or form_url
    form_type = text_value('application_form_type').lower()
    if form_type not in {'web', 'pdf', 'docx', 'other'}:
        form_type = (
            'pdf' if form_url.lower().endswith('.pdf') else
            'docx' if form_url.lower().endswith(('.doc', '.docx')) else
            'web' if form_url else ''
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
    application_methods = extract_application_methods(
        {**candidate, 'data': structured},
        text,
        contacts,
        application_url,
        form_url,
    )
    application_method = next(
        (route['method'] for route in application_methods if route['method'] in {
            'online', 'form', 'email', 'telegram', 'physical',
        }),
        'source_only',
    )
    instructions = []
    for route in application_methods:
        route_instruction = route.get('instructions', '')
        if route_instruction and route_instruction not in instructions:
            instructions.append(route_instruction)

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
        'application_form_url': form_url,
        'application_form_type': form_type,
        'application_method': application_method,
        'application_methods': application_methods,
        'application_instructions': '\n'.join(instructions)[:4000],
        'source_url': source_url,
        **contacts,
        'raw_source_content': candidate.get('raw_source_content') or text,
    }


def source_opportunity_types():
    from ..models import Opportunity

    return Opportunity.OPPORTUNITY_TYPES
