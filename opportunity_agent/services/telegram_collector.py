from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from .source_ingestion import _contact


async def collect_public_channel(source, limit=50, min_message_id=0):
    try:
        from telethon import TelegramClient
    except ImportError: raise RuntimeError('Telethon is not installed.')
    parsed_url = urlparse(source.url)
    channel = parsed_url.path.strip('/')
    if (
        parsed_url.scheme != 'https'
        or parsed_url.hostname not in {'t.me', 'telegram.me'}
        or parsed_url.username
        or parsed_url.password
        or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,30}[A-Za-z0-9]', channel)
    ):
        raise ValueError('Only public Telegram channel username URLs can be scanned.')
    api_id = os.environ.get('TELEGRAM_API_ID')
    api_hash = os.environ.get('TELEGRAM_API_HASH')
    session = os.environ.get('TELEGRAM_SESSION', 'opportunity_hub')
    if not api_id or not api_hash:
        raise RuntimeError(
            'TELEGRAM_API_ID and TELEGRAM_API_HASH are required for public channel collection.'
        )

    from telethon import TelegramClient

    client = TelegramClient(session, int(api_id), api_hash)
    results = []
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError(
                'Telegram collection requires a pre-authorized session; automated login is not attempted.'
            )
        async for message in client.iter_messages(
            channel,
            limit=limit,
            min_id=min_message_id,
            reverse=True,
        ):
            text = message.message or ''
            link = f'https://t.me/{channel}/{message.id}'
            if text.strip():
                email, phone, telegram_contact = _contact(text)
                results.append({
                    'title': text.splitlines()[0][:255],
                    'url': link,
                    'text': text,
                    'message_id': message.id,
                    'contact_email': email,
                    'contact_phone': phone,
                    'telegram_contact': telegram_contact,
                })
    finally:
        await client.disconnect()
    return results
