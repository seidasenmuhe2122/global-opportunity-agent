from __future__ import annotations

import re
from urllib.parse import urlparse

from ..models import TelegramSource


def channel_from_source(source: TelegramSource) -> str:
    parsed_url = urlparse(source.channel_url or '')
    channel = parsed_url.path.strip('/')
    if (
        parsed_url.scheme != 'https'
        or parsed_url.hostname not in {'t.me', 'telegram.me'}
        or parsed_url.username
        or parsed_url.password
        or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,30}[A-Za-z0-9]', channel)
    ):
        raise ValueError('Only public Telegram channel username URLs can be scanned.')
    return channel


def collect_telegram_source(source: TelegramSource, limit: int = 100) -> dict:
    raise RuntimeError(
        'Legacy Telegram scanning is disabled; use the canonical Celery scanner '
        'opportunity_agent.tasks.scan_telegram_sources_task.'
    )
