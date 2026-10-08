from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from ..models import Opportunity, TelegramSource
from .source_scanner import _extract_json, _save_opportunity
from .ai_engine import AIClient


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
    try:
        from telethon import TelegramClient
    except ImportError:
        return {'created': 0, 'error': 'Telethon is not installed.'}
    api_id = os.environ.get('TELEGRAM_API_ID', '')
    api_hash = os.environ.get('TELEGRAM_API_HASH', '')
    session = os.environ.get('TELEGRAM_SESSION', 'opportunity_agent')
    if not api_id or not api_hash:
        return {'created': 0, 'error': 'TELEGRAM_API_ID and TELEGRAM_API_HASH are required for Telegram source collection.'}

    import asyncio

    async def run():
        client = TelegramClient(session, int(api_id), api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return {
                    'created': 0,
                    'error': (
                        'Telegram collection requires a pre-authorized session; '
                        'automated login is not attempted.'
                    ),
                }
            channel = channel_from_source(source)
            entity = await client.get_entity(channel)
            ai = AIClient()
            created = 0
            updated = 0
            async for message in client.iter_messages(entity, limit=limit):
                text = message.message or ''
                if len(text.strip()) < 30:
                    continue
                source_url = f'https://t.me/{channel}/{message.id}'
                data = _extract_json(ai, text, source_url)
                data.setdefault('source_url', source_url)
                data.setdefault('application_url', '')
                obj, was_created = _save_opportunity(source, data, text)
                if obj:
                    created += int(was_created)
                    updated += int(not was_created)
            return {'created': created, 'updated': updated}
        finally:
            await client.disconnect()

    return asyncio.run(run())
