from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import date, datetime
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.db import IntegrityError, transaction
from django.db.models import Q


TRACKING_PARAMETERS = {
    'fbclid', 'gclid', 'dclid', 'msclkid', 'yclid', 'mc_cid', 'mc_eid',
    'ref', 'referrer', 'source',
}
TITLE_NOISE = {
    'a', 'an', 'and', 'at', 'for', 'in', 'of', 'on', 'the', 'to', 'with',
    'apply', 'application', 'now', 'opening', 'opportunity', 'position',
    'vacancy', 'vacancies',
}
ORGANIZATION_SUFFIXES = {
    'inc', 'incorporated', 'llc', 'ltd', 'limited', 'corp', 'corporation',
    'company', 'co', 'plc', 'gmbh', 'sa', 'ag', 'the',
}
SEMANTIC_GROUPS = (
    {'job', 'jobs', 'employment', 'career', 'careers', 'vacancy', 'vacancies', 'position', 'positions'},
    {'scholarship', 'scholarships', 'bursary', 'bursaries', 'funding', 'studentship'},
    {'intern', 'internship', 'internships'},
    {'fellow', 'fellowship', 'fellowships'},
    {'grant', 'grants', 'funding', 'award', 'awards'},
    {'program', 'programs', 'programme', 'programmes'},
    {'research', 'researcher', 'researchers', 'researching'},
    {'remote', 'telecommute', 'telework', 'work-from-home'},
)
TOKEN_SYNONYMS = {
    token: group
    for group in SEMANTIC_GROUPS
    for token in group
}


def normalize_url(url: str) -> str:
    if not url:
        return ''
    try:
        parsed = urlsplit(str(url).strip())
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or '').lower().rstrip('.')
        if not scheme or not host or parsed.username or parsed.password:
            return ''
        if host.startswith('www.'):
            host = host[4:]
        port = parsed.port
        netloc = host if not port or (scheme == 'https' and port == 443) or (scheme == 'http' and port == 80) else f'{host}:{port}'
        path = re.sub(r'/+', '/', parsed.path or '/').rstrip('/') or '/'
        ignored = TRACKING_PARAMETERS
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in ignored and not key.lower().startswith('utm_')
        ]
        query.sort()
        return urlunsplit((scheme, netloc, path, urlencode(query, doseq=True), ''))
    except ValueError:
        return ''


def normalize_text(value: str, *, organization: bool = False, title: bool = False) -> str:
    value = html.unescape(str(value or ''))
    value = re.sub(r'<[^>]+>', ' ', value)
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    tokens = re.findall(r'[a-z0-9]+', value.casefold())
    if organization:
        tokens = [token for token in tokens if token not in ORGANIZATION_SUFFIXES]
    if title:
        tokens = [token for token in tokens if token not in TITLE_NOISE]
    return ' '.join(tokens)


def _semantic_tokens(value: str) -> set[str]:
    tokens = normalize_text(value).split()
    return {
        synonym
        for token in tokens
        for synonym in TOKEN_SYNONYMS.get(token, {token})
    }


def _similarity(left: str, right: str, *, organization=False, title=False) -> float:
    a = normalize_text(left, organization=organization, title=title)
    b = normalize_text(right, organization=organization, title=title)
    if not a or not b:
        return 0.0
    sequence_score = SequenceMatcher(None, a, b).ratio()
    a_tokens = _semantic_tokens(a)
    b_tokens = _semantic_tokens(b)
    jaccard_score = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
    return max(sequence_score, jaccard_score)


def content_fingerprint(title: str, organization: str, content: str, deadline=None) -> str:
    normalized_content = ' '.join(sorted(_semantic_tokens(content)))
    material = '\x1f'.join((
        normalize_text(title, title=True),
        normalize_text(organization, organization=True),
        normalized_content,
        _deadline_key(deadline),
    ))
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def _deadline_value(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _deadline_key(value) -> str:
    deadline = _deadline_value(value)
    return deadline.isoformat() if deadline else ''


def _deadlines_compatible(left, right, tolerance_days=14) -> bool:
    a, b = _deadline_value(left), _deadline_value(right)
    return not a or not b or abs((a - b).days) <= tolerance_days


def _content_similarity(left: str, right: str) -> float:
    a = _semantic_tokens(left)
    b = _semantic_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _candidate_opportunities(data, canonical_url, fingerprint):
    from ..models import Opportunity

    exact = Q()
    if canonical_url:
        exact |= Q(normalized_application_url=canonical_url)
    if fingerprint:
        exact |= Q(content_fingerprint=fingerprint)
    candidates = list(Opportunity.objects.filter(exact).order_by('-updated_at')[:300]) if exact else []
    seen = {opportunity.pk for opportunity in candidates}

    title_tokens = sorted(
        _semantic_tokens(data.get('title', '')) - TITLE_NOISE,
        key=lambda token: (-len(token), token),
    )
    org = normalize_text(data.get('organization', ''), organization=True)
    search = Q()
    for token in title_tokens[:3]:
        search |= Q(title__icontains=token)
    if org:
        for token in org.split()[:2]:
            search |= Q(organization__icontains=token)
    if search:
        candidates.extend(
            opportunity
            for opportunity in Opportunity.objects.filter(search).order_by('-updated_at')[:700]
            if opportunity.pk not in seen
        )
    return candidates


def find_duplicate(data, *, raw_content=''):
    title = str(data.get('title') or '')
    organization = str(data.get('organization') or '')
    application_url = normalize_url(data.get('application_url') or '')
    deadline = data.get('deadline')
    content = raw_content or data.get('description') or ''
    fingerprint = content_fingerprint(title, organization, content, deadline)
    candidates = _candidate_opportunities(data, application_url, fingerprint)

    best = None
    best_score = 0.0
    for candidate in candidates:
        old_app_url = candidate.normalized_application_url or normalize_url(candidate.application_url)
        old_deadline = candidate.deadline
        if not _deadlines_compatible(deadline, old_deadline):
            continue
        old_fingerprint = candidate.content_fingerprint or content_fingerprint(
            candidate.title,
            candidate.organization,
            candidate.raw_source_content or candidate.description,
            candidate.deadline,
        )
        same_content = bool(fingerprint and fingerprint == old_fingerprint)
        title_score = _similarity(title, candidate.title, title=True)
        same_application_url = bool(
            application_url
            and application_url == old_app_url
            and title_score >= 0.55
        )
        organization_score = _similarity(
            organization,
            candidate.organization,
            organization=True,
        )
        content_score = _content_similarity(
            content,
            candidate.raw_source_content or candidate.description,
        )
        known_organization = bool(normalize_text(organization, organization=True)) and bool(
            normalize_text(candidate.organization, organization=True)
        )

        duplicate = (
            same_application_url
            or same_content
            or (
                known_organization
                and title_score >= 0.88
                and organization_score >= 0.82
                and (content_score >= 0.18 or title_score >= 0.96)
            )
            or (
                not known_organization
                and title_score >= 0.94
                and content_score >= 0.72
            )
        )
        if not duplicate:
            continue
        score = (
            1.0 if same_application_url else
            0.99 if same_content else
            0.55 * title_score + 0.25 * organization_score + 0.20 * content_score
        )
        if score > best_score:
            best = candidate
            best_score = score
    return best


def _merge_missing_fields(existing, data, raw_content):
    from .deadlines import parse_deadline

    fields = (
        'organization', 'opportunity_type', 'country', 'city', 'work_mode',
        'remote_worldwide', 'description', 'responsibilities', 'requirements',
        'qualifications', 'education_requirements', 'experience_requirements',
        'skills', 'languages', 'salary_stipend', 'benefits', 'visa_sponsorship',
        'deadline', 'application_url', 'source_url', 'contact_email',
        'contact_phone', 'telegram_contact', 'physical_address',
        'organization_website', 'application_form_url', 'application_form_type',
    )
    changed = []
    for field in fields:
        incoming = data.get(field)
        current = getattr(existing, field)
        is_empty = current is None or current == '' or current == [] or current == {}
        if not is_empty or incoming is None or incoming == '' or incoming == [] or incoming == {}:
            continue
        if field in {'remote_worldwide', 'visa_sponsorship'} and not isinstance(incoming, bool):
            continue
        if field in {'skills', 'languages'} and not isinstance(incoming, list):
            continue
        if field == 'deadline':
            incoming = parse_deadline(incoming)
        if incoming not in (None, '', [], {}):
            setattr(existing, field, incoming)
            changed.append(field)
    if raw_content and not existing.raw_source_content:
        existing.raw_source_content = raw_content
        changed.append('raw_source_content')
    source = data.get('_source_reference')
    if source is not None:
        from ..models import Source, TelegramSource

        if isinstance(source, Source) and existing.source_id is None:
            existing.source = source
            changed.append('source')
        elif (
            isinstance(source, TelegramSource)
            and existing.source_id is None
            and existing.telegram_source_id is None
        ):
            existing.telegram_source = source
            changed.append('telegram_source')
    canonical_application_url = normalize_url(data.get('application_url') or '')
    if canonical_application_url and not existing.normalized_application_url:
        existing.normalized_application_url = canonical_application_url
        changed.append('normalized_application_url')
    if not existing.content_fingerprint:
        existing.content_fingerprint = content_fingerprint(
            existing.title,
            existing.organization,
            existing.raw_source_content or existing.description,
            existing.deadline,
        )
        changed.append('content_fingerprint')
    if changed:
        existing.save(update_fields=changed + ['updated_at'])
    return existing


def deduplicate_and_save_opportunity(source, data, *, raw_content=''):
    from ..models import Opportunity, Source, TelegramSource

    data = dict(data)
    title = str(data.get('title') or '').strip()
    if not title:
        return None, False
    data['_source_reference'] = source
    data['title'] = title[:255]
    raw_content = str(raw_content or data.get('raw_source_content') or data.get('description') or '')[:60000]
    application_url = normalize_url(data.get('application_url') or '')
    fingerprint = content_fingerprint(
        data['title'],
        str(data.get('organization') or ''),
        raw_content,
        data.get('deadline'),
    )
    duplicate = find_duplicate(data, raw_content=raw_content)
    if duplicate:
        return _merge_missing_fields(duplicate, data, raw_content), False

    canonical_hash = compute_dedupe_hash(
        data['title'],
        data.get('organization') or '',
        application_url or normalize_url(data.get('source_url') or ''),
        data.get('deadline'),
    )
    defaults = _opportunity_defaults(data, source, raw_content)
    defaults.update({
        'dedupe_hash': canonical_hash,
        'content_fingerprint': fingerprint,
        'normalized_application_url': application_url,
    })
    try:
        with transaction.atomic():
            opportunity = Opportunity.objects.create(**defaults)
            from .audit import record_audit_event

            record_audit_event(
                'opportunity_discovered',
                opportunity.pk,
                {
                    'source_id': opportunity.source_id,
                    'telegram_source_id': opportunity.telegram_source_id,
                    'deadline': opportunity.deadline.isoformat() if opportunity.deadline else '',
                    'application_url_present': bool(opportunity.application_url),
                },
            )
            return opportunity, True
    except IntegrityError:
        duplicate = find_duplicate(data, raw_content=raw_content)
        if duplicate:
            return _merge_missing_fields(duplicate, data, raw_content), False
        raise


def _opportunity_defaults(data, source, raw_content):
    from django.utils import timezone
    from ..models import Source, TelegramSource
    from .deadlines import parse_deadline

    deadline = parse_deadline(data.get('deadline'))
    opportunity_type = data.get('opportunity_type') or ''
    allowed_types = {value for value, _ in source_opportunity_types()}
    if opportunity_type not in allowed_types:
        opportunity_type = ''
    work_mode = data.get('work_mode') or ''
    if work_mode not in {'on_site', 'hybrid', 'remote'}:
        work_mode = ''
    return {
        'source': source if isinstance(source, Source) else None,
        'telegram_source': source if isinstance(source, TelegramSource) else None,
        'title': data['title'],
        'organization': _bounded_text(data.get('organization'), 255),
        'opportunity_type': opportunity_type,
        'country': _bounded_text(data.get('country'), 120),
        'city': _bounded_text(data.get('city'), 120),
        'work_mode': work_mode,
        'remote_worldwide': data.get('remote_worldwide') if isinstance(data.get('remote_worldwide'), bool) else None,
        'visa_sponsorship': data.get('visa_sponsorship') if isinstance(data.get('visa_sponsorship'), bool) else None,
        'description': _bounded_text(data.get('description')),
        'responsibilities': _bounded_text(data.get('responsibilities')),
        'requirements': _bounded_text(data.get('requirements')),
        'qualifications': _bounded_text(data.get('qualifications')),
        'education_requirements': _bounded_text(data.get('education_requirements')),
        'experience_requirements': _bounded_text(data.get('experience_requirements')),
        'skills': data.get('skills') if isinstance(data.get('skills'), list) else [],
        'languages': data.get('languages') if isinstance(data.get('languages'), list) else [],
        'salary_stipend': _bounded_text(data.get('salary_stipend'), 255),
        'benefits': _bounded_text(data.get('benefits')),
        'deadline': deadline,
        'application_url': _bounded_text(data.get('application_url')),
        'application_form_url': _bounded_text(data.get('application_form_url')),
        'application_form_type': _bounded_text(data.get('application_form_type'), 20),
        'source_url': _bounded_text(data.get('source_url')),
        'contact_email': _bounded_text(data.get('contact_email'), 254),
        'contact_phone': _bounded_text(data.get('contact_phone'), 64),
        'telegram_contact': _bounded_text(data.get('telegram_contact'), 120),
        'physical_address': _bounded_text(data.get('physical_address')),
        'organization_website': _bounded_text(data.get('organization_website')),
        'raw_source_content': raw_content,
        'status': 'inactive' if deadline and deadline < timezone.now() else 'active',
    }


def _bounded_text(value, limit=None):
    if not isinstance(value, (str, int, float)):
        return ''
    text = str(value).strip()
    return text[:limit] if limit else text


def source_opportunity_types():
    from ..models import Opportunity

    return Opportunity.OPPORTUNITY_TYPES


def compute_dedupe_hash(title, organization, source_url, deadline=None):
    canonical = normalize_url(source_url)
    stem = '::'.join((
        normalize_text(title, title=True),
        normalize_text(organization, organization=True),
        canonical,
        _deadline_key(deadline),
    ))
    return hashlib.sha256(stem.encode('utf-8')).hexdigest()
