from __future__ import annotations
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self,bot_token=None):
        self.bot_token=bot_token if bot_token is not None else os.environ.get('TELEGRAM_BOT_TOKEN','')

    def send_message(self,chat_id,message,parse_mode=None):
        if not self.bot_token:
            raise RuntimeError('TELEGRAM_BOT_TOKEN is not configured.')
        if not chat_id or not message:
            raise ValueError('chat_id and message are required.')
        payload={'chat_id':chat_id,'text':message}
        if parse_mode:
            payload['parse_mode']=parse_mode
        for attempt in range(3):
            try:
                response = requests.post(
                    f'https://api.telegram.org/bot{self.bot_token}/sendMessage',
                    json=payload,
                    timeout=15,
                )
                response.raise_for_status()
                data=response.json()
                if not data.get('ok'):
                    raise RuntimeError(data.get('description','Telegram rejected the message.'))
                return True
            except requests.RequestException as exc:
                status = getattr(getattr(exc, 'response', None), 'status_code', None)
                retryable = status is None or status == 429 or status >= 500
                if not retryable or attempt == 2:
                    raise
                delay = 2 ** attempt
                logger.warning(
                    'Transient Telegram delivery failure for chat %s; retrying in %s seconds.',
                    chat_id,
                    delay,
                    exc_info=True,
                )
                time.sleep(delay)
        return False

    def send_to_enabled_destinations(self,kind,message):
        from .audit import record_audit_event
        from ..models import TelegramDestination
        allowed_types = {value for value, _ in TelegramDestination.TYPE_CHOICES}
        if kind not in allowed_types:
            raise ValueError(
                'Telegram notifications require a configured destination type.'
            )
        results=[]
        for d in TelegramDestination.objects.filter(enabled=True,type=kind):
            try:
                self.send_message(d.chat_id,message)
            except Exception as exc:
                logger.exception(
                    'Telegram notification failed for destination %s (%s).',
                    d.pk,
                    d.name,
                )
                try:
                    record_audit_event(
                        'telegram_notification_failed',
                        d.pk,
                        {'type': kind, 'error': str(exc)[:1000]},
                    )
                except Exception:
                    logger.exception('Could not audit Telegram notification failure for destination %s.', d.pk)
                results.append({'id':d.pk,'sent':False,'error':str(exc)})
                continue
            try:
                record_audit_event(
                    'telegram_notification_sent',
                    d.pk,
                    {'type': kind},
                )
            except Exception:
                logger.exception('Telegram notification sent, but its audit event could not be saved.')
                results.append({
                    'id': d.pk,
                    'sent': True,
                    'audit_error': 'Notification delivered but audit event could not be saved.',
                })
                continue
            results.append({'id':d.pk,'sent':True})
        return results
