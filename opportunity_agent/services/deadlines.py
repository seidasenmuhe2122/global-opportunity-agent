import re
from datetime import date, datetime, time

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime


_DATE_EXPRESSIONS = (
    re.compile(r'\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?'),
    re.compile(r'\d{1,2}[/-]\d{1,2}[/-]\d{4}'),
    re.compile(r'(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+\d{1,2},?\s+\d{4}', re.I),
    re.compile(r'\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+\d{4}', re.I),
)
_DEADLINE_CUE = re.compile(
    r'\b(?:application\s+deadline|deadline|closing\s+date|applications?\s+close|'
    r'apply\s+by|submit\s+by)\b\s*(?:(?:is|on)\s+|[:\-]\s*)?',
    re.I,
)


def parse_deadline(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.max)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        normalized = text[:-1] + '+00:00' if text.endswith(('Z', 'z')) else text
        parsed_date = parse_date(text)
        if parsed_date is not None:
            parsed = datetime.combine(parsed_date, time.max)
        else:
            parsed = parse_datetime(normalized)
        if parsed is None:
            formats = (
                '%d/%m/%Y',
                '%d-%m-%Y',
                '%B %d, %Y',
                '%B %d %Y',
                '%b %d, %Y',
                '%b %d %Y',
                '%d %B %Y',
                '%d %b %Y',
            )
            parsed_date = None
            for date_format in formats:
                try:
                    parsed_date = datetime.strptime(text, date_format).date()
                    break
                except ValueError:
                    continue
            if parsed_date is None:
                return None
            parsed = datetime.combine(parsed_date, time.max)
    else:
        return None

    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed)
    return parsed


def extract_explicit_deadline(text):
    if not isinstance(text, str) or not text.strip():
        return None
    for cue in _DEADLINE_CUE.finditer(text):
        tail = text[cue.end():cue.end() + 120]
        for expression in _DATE_EXPRESSIONS:
            match = expression.search(tail)
            if match:
                parsed = parse_deadline(match.group(0))
                if parsed is not None:
                    return parsed
    return None
