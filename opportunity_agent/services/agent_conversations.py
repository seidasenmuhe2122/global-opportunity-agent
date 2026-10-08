from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from ..models import AIConversation, AIMessage
from .agent_tools import TOOL_HANDLERS, execute_tool, tool_requires_confirmation
from .ai_engine import AIClient, AIProviderError

logger = logging.getLogger(__name__)

CONFIRMATION_LIFETIME = timedelta(minutes=10)
MAX_MESSAGE_LENGTH = 4000
AFFIRMATIVE = {'yes', 'confirm', 'confirm action', 'proceed'}
NEGATIVE = {'no', 'cancel', 'do not proceed'}


def _deterministic_intent(text: str) -> dict[str, Any] | None:
    normalized = re.sub(r'\s+', ' ', text.strip())
    lowered = normalized.casefold()
    search_text = normalized.rstrip('.!?')

    search_aliases = {
        'jobs': 'job',
        'job': 'job',
        'scholarships': 'scholarship',
        'scholarship': 'scholarship',
        'internships': 'internship',
        'internship': 'internship',
        'fellowships': 'fellowship',
        'fellowship': 'fellowship',
        'grants': 'grant',
        'grant': 'grant',
    }

    match = re.fullmatch(
        r'(?:find|search for|show me)\s+(.+)',
        normalized,
        flags=re.IGNORECASE,
    )
    if match:
        query = match.group(1).strip().rstrip('.!?')
        args: dict[str, Any] = {'query': query, 'limit': 20}
        country_match = re.search(
            r'\b(?:in|for)\s+([A-Za-z][A-Za-z ]{1,40}?)(?:\s+(?:jobs?|scholarships?|internships?|fellowships?|remote|worldwide)\b|$)',
            query,
            re.I,
        )
        if country_match:
            args['country'] = country_match.group(1).strip()
        else:
            country_match = re.search(
                r'([A-Za-z][A-Za-z ]{1,40})\s*(?:ውስጥ|in)\b',
                query,
                re.I,
            )
            if country_match:
                args['country'] = country_match.group(1).strip()
        for phrase, opportunity_type in search_aliases.items():
            if re.search(rf'\b{re.escape(phrase)}\b', query, re.I):
                args['opportunity_type'] = opportunity_type
                break
        if re.search(r'\bremote\b', query, re.I) or 'remote' in lowered:
            args['work_mode'] = 'remote'
            args['remote_only'] = True
        if re.search(r'\bworldwide\b', query, re.I):
            args['remote_only'] = True
        score_match = re.search(r'\b(?:above|at least|over)\s+(\d{1,3})\s*%?\s*match', query, re.I)
        if score_match:
            args['minimum_score'] = int(score_match.group(1))
        return {'action': 'search_opportunities', 'arguments': args}

    if re.search(r'(?:ብቻ|only).*\b(?:ፈልግ|find|search|show me)\b|\b(?:remote jobs?|jobs?)\s+(?:ብቻ|only)\b', normalized, re.I):
        query = ' '.join(part for part in re.split(r'\s+', normalized) if part not in {'ብቻ', 'only'}).strip()
        if not query:
            query = 'jobs'
        country_match = re.search(r'([A-Za-z][A-Za-z ]{1,40})\s*(?:ውስጥ|in)\b', normalized, re.I)
        args: dict[str, Any] = {'query': query, 'limit': 20, 'remote_only': True}
        if country_match:
            args['country'] = country_match.group(1).strip()
        if re.search(r'\bjobs?\b', query, re.I):
            args['opportunity_type'] = 'job'
        return {'action': 'search_opportunities', 'arguments': args}

    if re.search(r'(?:scholarship|ስኮላርሺፕ|internship|job).*?(?:ጨምር|add|include)', normalized, re.I):
        kind = 'scholarship'
        if re.search(r'\b(?:internship|internships)\b', normalized, re.I):
            kind = 'internship'
        elif re.search(r'\b(?:jobs?|job)\b', normalized, re.I):
            kind = 'job'
        return {'action': 'set_opportunity_type_filter', 'arguments': {'types': [kind]}}

    country_add_match = re.search(r'([A-Za-z][A-Za-z ]{1,40})\s*(?:እንደገና|again)\s*(?:enable|አድርግ)', normalized, re.I)
    if country_add_match:
        return {'action': 'add_target_country', 'arguments': {'country': country_add_match.group(1).strip()}}
    if re.search(r'(?:country\s+filter|target country|country).*?(?:add|enable|እንደገና|include)', normalized, re.I):
        country = re.search(r'([A-Za-z][A-Za-z ]{1,40})', normalized, re.I)
        if country:
            return {'action': 'add_target_country', 'arguments': {'country': country.group(1).strip()}}

    country_remove_match = re.search(r'([A-Za-z][A-Za-z ]{1,40})\s*(?:አቁም|disable|stop|remove)', normalized, re.I)
    if country_remove_match:
        return {'action': 'remove_target_country', 'arguments': {'country': country_remove_match.group(1).strip()}}
    if re.search(r'(?:country\s+filter|target country|country).*?(?:remove|disable|stop|አቁም)', normalized, re.I):
        country = re.search(r'([A-Za-z][A-Za-z ]{1,40})', normalized, re.I)
        if country:
            return {'action': 'remove_target_country', 'arguments': {'country': country.group(1).strip()}}

    if re.search(r'(?:s?et\s+)?(?:minimum\s+)?match\s+score\s*(?:to)?\s*(\d{1,3})', normalized, re.I):
        score = re.search(r'(?:s?et\s+)?(?:minimum\s+)?match\s+score\s*(?:to)?\s*(\d{1,3})', normalized, re.I)
        if score:
            return {'action': 'set_match_threshold', 'arguments': {'score': int(score.group(1))}}

    patterns = (
        (r'(?:why was|explain) application\s*#?(\d+)\s+(?:rejected|failed)', 'analyze_rejection'),
        (r'(?:fix it and )?retry application\s*#?(\d+)', 'retry_application'),
        (r'(?:fix|fix it and apply again)(?: application)?\s*#?(\d+)', 'fix_and_retry'),
        (r'cancel application\s*#?(\d+)', 'cancel_application'),
        (r'(?:submit|apply to) application\s*#?(\d+)', 'submit_application'),
        (r'prepare application for opportunity\s*#?(\d+)', 'prepare_application'),
        (r'(?:calculate|check) (?:the )?match for opportunity\s*#?(\d+)', 'calculate_match'),
        (r'generate (?:a )?cover letter for application\s*#?(\d+)', 'generate_cover_letter'),
        (r'scan source\s*#?(\d+)', 'scan_source'),
    )
    for pattern, action in patterns:
        match = re.fullmatch(pattern, search_text, re.I)
        if match:
            field = 'source_id' if action == 'scan_source' else (
                'opportunity_id' if action in {'prepare_application', 'calculate_match'}
                else 'application_id'
            )
            return {'action': action, 'arguments': {field: int(match.group(1))}}

    if re.fullmatch(r'(?:worldwide|worldwide opportunities)\s+(?:on|enable)', lowered, re.I):
        return {'action': 'update_preferences', 'arguments': {'worldwide_preference': True}}
    if re.fullmatch(r'(?:worldwide|worldwide opportunities)\s+(?:off|disable)', lowered, re.I):
        return {'action': 'update_preferences', 'arguments': {'worldwide_preference': False}}
    if re.fullmatch(r'remote only', lowered, re.I):
        return {'action': 'set_remote_only', 'arguments': {'enabled': True}}
    if re.fullmatch(r'(?:any work mode|remove remote only)', lowered, re.I):
        return {'action': 'set_remote_only', 'arguments': {'enabled': False}}
    if re.search(r'^(?:enable|turn on|on)\s+auto.?apply(?:\s+for.*)?$', lowered, re.I):
        return {'action': 'set_auto_apply', 'arguments': {'enabled': True}}
    if re.search(r'^(?:disable|turn off|off)\s+auto.?apply(?:\s+for.*)?$', lowered, re.I):
        return {'action': 'set_auto_apply', 'arguments': {'enabled': False}}
    if re.search(r'^auto.?apply\s+(?:on|enable|enabled)(?:\s+for.*)?$', lowered, re.I):
        return {'action': 'set_auto_apply', 'arguments': {'enabled': True}}
    if re.search(r'^auto.?apply\s+(?:off|disable|disabled)(?:\s+for.*)?$', lowered, re.I):
        return {'action': 'set_auto_apply', 'arguments': {'enabled': False}}
    if re.search(r'auto.?apply.*(?:on|enable|enabled)', lowered, re.I) and not re.search(r'auto.?apply.*(?:off|disable|disabled)', lowered, re.I):
        return {'action': 'set_auto_apply', 'arguments': {'enabled': True}}
    if re.search(r'auto.?apply.*(?:off|disable|disabled)', lowered, re.I):
        return {'action': 'set_auto_apply', 'arguments': {'enabled': False}}
    if re.fullmatch(r'(?:show )?(?:automation )?status', lowered, re.I):
        return {'action': 'automation_status', 'arguments': {}}
    if re.fullmatch(r'scan now', lowered, re.I):
        return {'action': 'scan_now', 'arguments': {}}
    if re.fullmatch(r'discover (?:new )?sources', lowered, re.I):
        return {'action': 'discover_sources', 'arguments': {}}
    bulk_match = re.search(r'\b(?:match|bulk)\s+(\d{1,3})\s*%?\s*(?:በላይ|above|over|and above|more than|greater than)\b', normalized, re.I)
    if bulk_match:
        return {'action': 'bulk_apply', 'arguments': {'minimum_score': int(bulk_match.group(1))}}
    if re.search(r'\b(?:ፈልግ|find|search|show me)\b', normalized, re.I):
        country_match = re.search(r'([A-Za-z][A-Za-z ]{1,40})\s*(?:ውስጥ|in)\b', normalized, re.I)
        args: dict[str, Any] = {'query': normalized.rstrip('.!?'), 'limit': 20}
        if country_match:
            args['country'] = country_match.group(1).strip()
        return {'action': 'search_opportunities', 'arguments': args}
    if re.fullmatch(r'(?:[A-Za-z]+)\s*(?:ስለ|about)\s*(?:this|it)', normalized, re.I):
        return {'action': 'respond', 'arguments': {'reply': 'Please provide a concrete application or opportunity reference so I can inspect the real record.'}}
    return None


def _intent_for_user(user, text: str, conversation: AIConversation) -> dict[str, Any]:
    intent = _deterministic_intent(text)
    if intent is not None:
        return intent
    recent = list(
        conversation.messages.order_by('-created_at')
        .values('role', 'content')[:10]
    )
    recent.reverse()
    try:
        return AIClient().interpret_agent_intent(
            text,
            recent,
            sorted(TOOL_HANDLERS),
        )
    except AIProviderError:
        raise
    except Exception as exc:
        logger.exception('Could not interpret conversational request for user %s.', user.pk)
        raise AIProviderError(
            'The AI provider returned an invalid action proposal.'
        ) from exc


def _assistant_message(
    conversation: AIConversation,
    content: str,
    *,
    tool_name: str = '',
    arguments: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> AIMessage:
    return AIMessage.objects.create(
        conversation=conversation,
        role='assistant',
        content=content[:12000],
        tool_name=tool_name,
        tool_arguments=arguments or {},
        tool_result=result or {},
    )


def _confirmation_message(
    conversation: AIConversation,
    action: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    token = secrets.token_urlsafe(24)
    expires_at = timezone.now() + CONFIRMATION_LIFETIME
    conversation.pending_confirmation = {
        'token': token,
        'action': action,
        'arguments': arguments,
        'expires_at': expires_at.isoformat(),
    }
    conversation.save(update_fields=('pending_confirmation', 'updated_at'))
    result['data'] = {
        **result.get('data', {}),
        'confirmation_required': True,
        'confirmation_token': token,
        'expires_at': expires_at.isoformat(),
    }
    result['message'] = 'Please confirm this action within 10 minutes.'
    _assistant_message(
        conversation,
        result['message'],
        tool_name=action,
        arguments=arguments,
        result=result,
    )
    return result


def _execute_and_store(
    user,
    conversation: AIConversation,
    action: str,
    arguments: dict[str, Any],
    *,
    confirmed: bool = False,
    channel: str = 'web',
) -> dict[str, Any]:
    result = execute_tool(
        user,
        action,
        arguments,
        confirmed=confirmed,
        confirmation_channel=channel,
    )
    content = result.get('message') or 'The action returned no message.'
    _assistant_message(
        conversation,
        content,
        tool_name=action,
        arguments=arguments,
        result=result,
    )
    return result


@transaction.atomic
def process_user_message(
    user,
    conversation: AIConversation,
    text: str,
    *,
    confirmation_token: str = '',
    channel: str = 'web',
) -> dict[str, Any]:
    if not getattr(user, 'is_authenticated', False) or not user.is_active:
        raise PermissionError('Authentication is required.')
    if conversation.user_id != user.pk:
        raise PermissionError('Conversation not found.')
    try:
        conversation = AIConversation.objects.select_for_update().get(
            pk=conversation.pk,
            user=user,
        )
    except AIConversation.DoesNotExist as exc:
        raise PermissionError('Conversation not found.') from exc
    if not isinstance(text, str):
        raise ValueError('Message must be text.')
    text = text.strip()
    if not text or len(text) > MAX_MESSAGE_LENGTH:
        raise ValueError(f'Message must contain 1 to {MAX_MESSAGE_LENGTH} characters.')

    AIMessage.objects.create(
        conversation=conversation,
        role='user',
        content=text,
    )
    if conversation.title == 'New conversation':
        conversation.title = text[:157] + ('...' if len(text) > 160 else '')

    pending = conversation.pending_confirmation or {}
    if pending:
        lowered = text.casefold().strip().rstrip('.!?')
        if lowered in NEGATIVE:
            conversation.pending_confirmation = {}
            conversation.save(update_fields=('pending_confirmation', 'title', 'updated_at'))
            result = {
                'success': True,
                'action': pending.get('action', ''),
                'status': 'cancelled',
                'message': 'The pending action was cancelled; no action was taken.',
                'data': {},
            }
            _assistant_message(
                conversation,
                result['message'],
                tool_name=result['action'],
                result=result,
            )
            return result

        try:
            expires_at = datetime.fromisoformat(pending['expires_at'])
            if timezone.is_naive(expires_at):
                expires_at = timezone.make_aware(expires_at)
        except (KeyError, TypeError, ValueError):
            expires_at = timezone.now() - timedelta(seconds=1)
        if expires_at <= timezone.now():
            conversation.pending_confirmation = {}
            conversation.save(update_fields=('pending_confirmation', 'title', 'updated_at'))
            result = {
                'success': False,
                'action': pending.get('action', ''),
                'status': 'confirmation_expired',
                'message': 'Confirmation expired. Request the action again if you still want it.',
                'data': {},
            }
            _assistant_message(conversation, result['message'], result=result)
            return result
        if (
            lowered not in AFFIRMATIVE
            or not confirmation_token
            or not secrets.compare_digest(
                str(pending.get('token', '')),
                str(confirmation_token),
            )
        ):
            result = {
                'success': False,
                'action': pending.get('action', ''),
                'status': 'confirmation_required',
                'message': 'Confirm or cancel the pending action using its confirmation control.',
                'data': {'confirmation_required': True},
            }
            _assistant_message(conversation, result['message'], result=result)
            return result

        action = pending.get('action', '')
        arguments = pending.get('arguments', {})
        conversation.pending_confirmation = {}
        conversation.save(update_fields=('pending_confirmation', 'title', 'updated_at'))
        return _execute_and_store(
            user,
            conversation,
            action,
            arguments,
            confirmed=True,
            channel=channel,
        )

    try:
        intent = _intent_for_user(user, text, conversation)
    except AIProviderError as exc:
        result = {
            'success': False,
            'action': 'interpret_intent',
            'status': 'ai_unavailable',
            'message': str(exc),
            'data': {},
        }
        conversation.save(update_fields=('title', 'updated_at'))
        _assistant_message(conversation, result['message'], result=result)
        return result

    action = intent.get('action')
    arguments = intent.get('arguments', {})
    if action == 'respond':
        reply = intent.get('reply', '').strip()
        result = {
            'success': bool(reply),
            'action': 'respond',
            'status': 'success' if reply else 'invalid_response',
            'message': reply or 'Please clarify what you want to do.',
            'data': {},
        }
        conversation.save(update_fields=('title', 'updated_at'))
        _assistant_message(conversation, result['message'], result=result)
        return result
    if not isinstance(action, str) or action not in TOOL_HANDLERS:
        result = {
            'success': False,
            'action': 'interpret_intent',
            'status': 'invalid_action',
            'message': 'The requested action is not available.',
            'data': {},
        }
        conversation.save(update_fields=('title', 'updated_at'))
        _assistant_message(conversation, result['message'], result=result)
        return result
    if not isinstance(arguments, dict):
        result = {
            'success': False,
            'action': action,
            'status': 'invalid_arguments',
            'message': 'The action arguments must be an object.',
            'data': {},
        }
        conversation.save(update_fields=('title', 'updated_at'))
        _assistant_message(conversation, result['message'], result=result)
        return result

    conversation.save(update_fields=('title', 'updated_at'))
    if tool_requires_confirmation(action, arguments):
        confirmation = execute_tool(
            user,
            action,
            arguments,
            confirmed=False,
            confirmation_channel=channel,
        )
        if confirmation.get('status') != 'confirmation_required':
            _assistant_message(
                conversation,
                confirmation['message'],
                tool_name=action,
                arguments=arguments,
                result=confirmation,
            )
            return confirmation
        return _confirmation_message(
            conversation,
            action,
            arguments,
            confirmation,
        )
    return _execute_and_store(
        user,
        conversation,
        action,
        arguments,
        channel=channel,
    )
