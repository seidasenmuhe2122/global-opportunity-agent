from __future__ import annotations

import email
import imaplib
import re
import time
from datetime import datetime, timezone as dt_timezone
from html import unescape
from urllib.parse import urlparse

from django.utils import timezone


def _html_to_text(value: str) -> str:
    return re.sub(r'<[^>]+>', ' ', unescape(value or ''))


def _message_text(msg) -> str:
    chunks=[]
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == 'multipart':
                continue
            ctype=part.get_content_type()
            if ctype not in ('text/plain','text/html'):
                continue
            try: payload=part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8','ignore')
            except Exception: payload=''
            chunks.append(_html_to_text(payload) if ctype == 'text/html' else payload)
    else:
        try: payload=msg.get_payload(decode=True).decode(msg.get_content_charset() or 'utf-8','ignore')
        except Exception: payload=''
        chunks.append(_html_to_text(payload) if msg.get_content_type() == 'text/html' else payload)
    return '\n'.join(chunks)


def _verification_links(text: str, allowed_domains: list[str]) -> list[str]:
    urls=re.findall(r'https?://[^\s<>"\']+', text or '')
    wanted=[]
    for raw in urls:
        url=raw.rstrip(').,;\"\'')
        host=(urlparse(url).hostname or '').lower()
        if not host or not any(host == d or host.endswith('.'+d) for d in allowed_domains):
            continue
        lower=url.lower()
        if any(k in lower for k in ('verify','verification','activate','confirm','confirmation','email-confirm','validate')):
            wanted.append(url)
    return list(dict.fromkeys(wanted))


def fetch_verification_link(mailbox, allowed_domains: list[str], sender_domain: str = '', since_minutes: int = 30) -> str:
    if not mailbox.enabled:
        return ''
    host=mailbox.imap_host.strip()
    if not host:
        # Common providers can be auto-filled; custom domains should set IMAP host explicitly.
        domain=(mailbox.email.split('@')[-1] if '@' in mailbox.email else '').lower()
        host={'gmail.com':'imap.gmail.com','outlook.com':'outlook.office365.com','hotmail.com':'outlook.office365.com','live.com':'outlook.office365.com','yahoo.com':'imap.mail.yahoo.com'}.get(domain,'')
    if not host:
        raise ValueError('IMAP host is required for this mailbox.')
    password=mailbox.get_app_password()
    if not password:
        raise ValueError('Mailbox app password is missing.')
    allowed=[d.lower().strip() for d in allowed_domains if d]
    if sender_domain:
        allowed.append(sender_domain.lower().strip())
    since_date=(timezone.now()-timezone.timedelta(minutes=since_minutes)).strftime('%d-%b-%Y')
    M=imaplib.IMAP4_SSL(host, int(mailbox.imap_port)) if mailbox.imap_ssl else imaplib.IMAP4(host, int(mailbox.imap_port))
    try:
        M.login(mailbox.email,password)
        M.select('INBOX')
        typ,data=M.search(None, 'SINCE', since_date)
        if typ != 'OK': return ''
        ids=data[0].split()[::-1]
        for msg_id in ids[:50]:
            typ, msg_data=M.fetch(msg_id,'(RFC822)')
            if typ != 'OK' or not msg_data: continue
            raw=msg_data[0][1] if isinstance(msg_data[0],tuple) else b''
            msg=email.message_from_bytes(raw)
            sender=(msg.get('From') or '').lower()
            subject=(msg.get('Subject') or '').lower()
            text=_message_text(msg)
            if not any(k in (subject+' '+text.lower()) for k in ('verify','verification','activate','confirm','confirmation','welcome')):
                continue
            links=_verification_links(text, allowed)
            if links: return links[0]
        return ''
    finally:
        try: M.logout()
        except Exception: pass


def mark_checked(mailbox, error=''):
    mailbox.last_checked_at=timezone.now(); mailbox.last_error=error[:2000]
    mailbox.save(update_fields=['last_checked_at','last_error','updated_at'])
