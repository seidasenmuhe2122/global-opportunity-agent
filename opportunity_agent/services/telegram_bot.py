from __future__ import annotations

import os
import re

from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Q
from django.utils import timezone

from ..models import (
    Application,
    Opportunity,
    Source,
    TelegramSource,
    UserProfile,
)
from ..tasks import (
    automation_cycle_task,
    discover_sources_task,
    process_application_queue_task,
    scan_sources_task,
    scan_telegram_sources_task,
)


COMMANDS = (
    'status', 'users', 'sources', 'opportunities', 'applications',
    'rejected', 'failed', 'review', 'retry', 'apply', 'cancel',
    'autoapply', 'country', 'worldwide', 'setmatch', 'setlimit',
    'source', 'scan', 'discover', 'retry_failed', 'retry_rejected',
    'cycle', 'help',
)


def _env_ids(name):
    return {
        item.strip()
        for item in os.environ.get(name, '').split(',')
        if item.strip()
    }


def authorized(update) -> bool:
    user = getattr(update, 'effective_user', None)
    chat = getattr(update, 'effective_chat', None)
    if user is None or chat is None:
        return False

    user_id = str(user.id)
    chat_id = str(chat.id)
    allowed_users = _env_ids('TELEGRAM_ADMIN_USER_IDS')
    allowed_chats = _env_ids('TELEGRAM_ADMIN_CHAT_IDS')

    if user_id in allowed_users:
        return not allowed_chats or chat_id in allowed_chats
    return (
        getattr(chat, 'type', None) == 'private'
        and chat_id == user_id
        and chat_id in allowed_chats
    )


def _queued_application(application, allowed_statuses):
    if application.status not in allowed_statuses:
        return False, f'Cannot queue application {application.pk} from status {application.status}.'
    application.status = 'queued'
    application.error_message = ''
    application.rejection_reason = ''
    application.save(update_fields=[
        'status',
        'error_message',
        'rejection_reason',
        'updated_at',
    ])
    return True, f'Application {application.pk} queued for safe processing.'


def _retry_applications(status, limit=20):
    applications = list(
        Application.objects.filter(status=status).order_by('updated_at', 'pk')[:limit]
    )
    queued = 0
    for application in applications:
        changed, _ = _queued_application(application, {status})
        queued += int(changed)
    return queued


def _source_by_id(source_id):
    web_source = Source.objects.filter(pk=source_id).first()
    telegram_source = TelegramSource.objects.filter(pk=source_id).first()
    if web_source and telegram_source:
        raise ValueError(
            'That ID exists in both source lists; use /scan web ID or /scan telegram ID.'
        )
    return web_source or telegram_source


def _list_applications(status, limit=10, today_only=False):
    applications = Application.objects.select_related(
        'opportunity',
    ).filter(status=status)
    if today_only:
        applications = applications.filter(updated_at__date=timezone.localdate())
    applications = applications.order_by('-updated_at', '-pk')[:limit]
    rows = [
        f'#{application.pk} {application.opportunity.title} — '
        f'{application.get_status_display()}'
        for application in applications
    ]
    title = (
        f'{status.replace("_", " ").title()} applications updated today'
        if today_only
        else f'{status.replace("_", " ").title()} applications'
    )
    return title + ':\n' + ('\n'.join(rows) if rows else 'None.')


def handle_natural_language(text):
    """Handle only deterministic, narrowly scoped administrative phrases."""
    normalized = re.sub(r'\s+', ' ', (text or '').strip()).strip()
    normalized = re.sub(r'[.!?]+$', '', normalized).strip()
    lowered = normalized.lower()

    if re.fullmatch(r'(enable|turn on) worldwide opportunities[.!]?', lowered):
        count = UserProfile.objects.update(worldwide_preference=True)
        return f'Worldwide opportunities enabled for {count} profiles.'
    if re.fullmatch(r'(disable|turn off) worldwide opportunities[.!]?', lowered):
        count = UserProfile.objects.update(worldwide_preference=False)
        return f'Worldwide opportunities disabled for {count} profiles.'

    match = re.fullmatch(
        r'(?:set (?:the )?(?:minimum )?match score to|setmatch)\s+(\d{1,3})[.!]?',
        lowered,
    )
    if match:
        score = int(match.group(1))
        if not 0 <= score <= 100:
            return 'Match score must be between 0 and 100.'
        count = UserProfile.objects.update(minimum_ai_match_score=score)
        return f'Minimum match score set to {score} for {count} profiles.'

    match = re.fullmatch(
        r'(enable|turn on|disable|turn off) auto.?apply'
        r'(?: for user (.+))?[.!]?',
        normalized,
        re.IGNORECASE,
    )
    if match:
        enabled = match.group(1).lower() in {'enable', 'turn on'}
        target = (match.group(2) or '').strip().rstrip('.!?').strip()
        profiles = UserProfile.objects.all()
        if target:
            profiles = profiles.filter(
                Q(full_name__iexact=target)
                | Q(user__username__iexact=target)
                | Q(user__email__iexact=target)
            )
            if profiles.count() != 1:
                return (
                    'User name is ambiguous or not found. Specify the exact username '
                    'or email address.'
                )
        count = profiles.update(auto_apply=enabled)
        action = 'enabled' if enabled else 'disabled'
        return f'Auto-apply {action} for {count} profile(s).'

    if re.fullmatch(
        r'(?:show me )?applications that failed today[.!]?',
        lowered,
    ):
        return _list_applications('failed', today_only=True)

    match = re.fullmatch(
        r'why was (?:opportunity )?(.+?) rejected[.!]?',
        normalized,
        re.IGNORECASE,
    )
    if match:
        opportunity_name = match.group(1).strip()
        if opportunity_name.casefold() in {'this opportunity', 'the opportunity'}:
            return (
                'Please include the exact opportunity title, for example: '
                '"Why was Senior Analyst rejected?"'
            )
        rejected = Application.objects.select_related('opportunity').filter(
            status='rejected',
            opportunity__title__iexact=opportunity_name,
        ).order_by('-updated_at')[:5]
        if not rejected:
            return f'No rejected applications found for "{opportunity_name}".'
        return '\n'.join(
            f'Application #{application.pk}: '
            f'{application.rejection_reason or application.error_message or "No reason recorded."}'
            for application in rejected
        )

    match = re.fullmatch(r'retry application (\d+)[.!]?', lowered)
    if match:
        application = Application.objects.filter(pk=int(match.group(1))).first()
        if not application:
            return 'Application not found.'
        queued, message = _queued_application(application, {'failed', 'rejected'})
        if queued:
            process_application_queue_task.delay(1)
        return message

    return (
        'I could not safely interpret that request. Use /help for supported commands; '
        'administrative natural-language requests must match a supported phrase exactly.'
    )


def _execute_command(command, args):
    if command == 'help':
        return (
            '/status\n/users\n/sources\n/opportunities\n/applications\n'
            '/rejected\n/failed\n/review\n/retry APPLICATION_ID\n/apply APPLICATION_ID\n'
            '/cancel APPLICATION_ID\n/autoapply on|off\n/country add|remove COUNTRY\n'
            '/worldwide on|off\n/setmatch SCORE\n/setlimit LIMIT\n'
            '/source add NAME | URL\n/source disable SOURCE_ID\n/scan [SOURCE_ID]\n'
            '/discover\n/retry_failed\n/retry_rejected\n/cycle\n/help\n'
            'Natural language: "Enable worldwide opportunities.", '
            '"Set minimum match score to 80.", "Show me applications that failed today.", '
            '"Retry application 125.", or "Disable auto apply for user USERNAME."'
        )

    if command == 'status':
        return (
            f'Users: {UserProfile.objects.count()}\n'
            f'Web sources: {Source.objects.filter(enabled=True).count()}\n'
            f'Telegram sources: {TelegramSource.objects.filter(enabled=True).count()}\n'
            f'Opportunities: {Opportunity.objects.filter(status="active").count()}\n'
            f'Applications: {Application.objects.count()}\n'
            f'Submitted: {Application.objects.filter(status="submitted").count()}\n'
            f'Rejected: {Application.objects.filter(status="rejected").count()}\n'
            f'Failed: {Application.objects.filter(status="failed").count()}\n'
            f'Needs review: {Application.objects.filter(status="needs_review").count()}'
        )

    if command == 'users':
        profiles = UserProfile.objects.select_related('user').order_by('-created_at')[:10]
        rows = [
            f'{profile.full_name or profile.user.get_username()} '
            f'({profile.user.email or "no email"})'
            for profile in profiles
        ]
        return (
            f'Users ({UserProfile.objects.count()} total):\n'
            + ('\n'.join(rows) if rows else 'None.')
        )

    if command == 'sources':
        web_sources = list(
            Source.objects.filter(enabled=True)
            .order_by('name')
            .values_list('pk', 'name')[:10]
        )
        telegram_sources = list(
            TelegramSource.objects.filter(enabled=True)
            .order_by('name')
            .values_list('pk', 'name')[:10]
        )
        return '\n'.join([
            f'Enabled web sources ({Source.objects.filter(enabled=True).count()}):',
            *(f'- {pk}: {name}' for pk, name in web_sources),
            f'Enabled Telegram sources ({TelegramSource.objects.filter(enabled=True).count()}):',
            *(f'- {pk}: {name}' for pk, name in telegram_sources),
        ])

    if command == 'opportunities':
        rows = Opportunity.objects.filter(status='active').order_by('-created_at')[:10]
        text = '\n'.join(f'#{item.pk} {item.title}' for item in rows) or 'None.'
        return f'Active opportunities ({Opportunity.objects.filter(status="active").count()}):\n{text}'

    if command == 'applications':
        rows = Application.objects.select_related('opportunity').order_by('-updated_at')[:10]
        text = '\n'.join(
            f'#{item.pk} {item.opportunity.title} — {item.get_status_display()}'
            for item in rows
        ) or 'None.'
        return f'Recent applications ({Application.objects.count()} total):\n{text}'

    status_commands = {
        'rejected': 'rejected',
        'failed': 'failed',
        'review': 'needs_review',
    }
    if command in status_commands:
        return _list_applications(status_commands[command])

    if command in {'retry', 'apply', 'cancel'}:
        if len(args) != 1 or not args[0].isdigit():
            return f'Usage: /{command} APPLICATION_ID'
        application = Application.objects.filter(pk=int(args[0])).first()
        if not application:
            return 'Application not found.'
        if command == 'retry':
            changed, message = _queued_application(application, {'failed', 'rejected'})
            if changed:
                process_application_queue_task.delay(1)
            return message
        if command == 'apply':
            changed, message = _queued_application(
                application,
                {
                    'queued', 'matching', 'prepared', 'pending', 'failed',
                    'rejected', 'needs_review', 'cancelled',
                },
            )
            if changed:
                process_application_queue_task.delay(1)
            return message
        if application.status in {'submitted', 'cancelled'}:
            return f'Cannot cancel application {application.pk} from status {application.status}.'
        application.status = 'cancelled'
        application.save(update_fields=['status', 'updated_at'])
        return f'Application {application.pk} cancelled.'

    if command in {'retry_failed', 'retry_rejected'}:
        if args:
            return f'Usage: /{command}'
        status = 'failed' if command == 'retry_failed' else 'rejected'
        count = _retry_applications(status)
        if count:
            process_application_queue_task.delay(count)
        return f'Requeued {count} {status} application(s).'

    if command == 'autoapply':
        if len(args) != 1 or args[0].lower() not in {'on', 'off'}:
            return 'Usage: /autoapply on|off'
        enabled = args[0].lower() == 'on'
        count = UserProfile.objects.update(auto_apply=enabled)
        return f'Auto-apply {"enabled" if enabled else "disabled"} for {count} profiles.'

    if command == 'country':
        if len(args) < 2 or args[0].lower() not in {'add', 'remove'}:
            return 'Usage: /country add COUNTRY or /country remove COUNTRY'
        action = args[0].lower()
        target = ' '.join(args[1:]).strip()
        updated = 0
        for profile in UserProfile.objects.all().only('pk', 'target_countries'):
            countries = list(profile.target_countries or [])
            matching = next(
                (item for item in countries if item.casefold() == target.casefold()),
                None,
            )
            if action == 'add' and matching is None:
                countries.append(target)
            elif action == 'remove' and matching is not None:
                countries.remove(matching)
            else:
                continue
            profile.target_countries = countries
            profile.save(update_fields=['target_countries', 'updated_at'])
            updated += 1
        verb = 'Added' if action == 'add' else 'Removed'
        return f'{verb} {target} for {updated} profile(s).'

    if command == 'worldwide':
        if len(args) != 1 or args[0].lower() not in {'on', 'off'}:
            return 'Usage: /worldwide on|off'
        enabled = args[0].lower() == 'on'
        count = UserProfile.objects.update(worldwide_preference=enabled)
        return (
            f'Worldwide preference {"enabled" if enabled else "disabled"} '
            f'for {count} profiles.'
        )

    if command == 'setmatch':
        if len(args) != 1 or not args[0].isdigit():
            return 'Usage: /setmatch SCORE (0–100)'
        score = int(args[0])
        if not 0 <= score <= 100:
            return 'Match score must be between 0 and 100.'
        count = UserProfile.objects.update(minimum_ai_match_score=score)
        return f'Minimum match score set to {score} for {count} profiles.'

    if command == 'setlimit':
        if len(args) != 1 or not args[0].isdigit():
            return 'Usage: /setlimit POSITIVE_INTEGER'
        limit = int(args[0])
        if not 1 <= limit <= 1000:
            return 'Daily limit must be between 1 and 1000.'
        count = UserProfile.objects.update(daily_application_limit=limit)
        return f'Daily application limit set to {limit} for {count} profiles.'

    if command == 'source':
        if not args:
            return 'Usage: /source add NAME | URL or /source disable SOURCE_ID'
        action = args[0].lower()
        if action == 'disable':
            if len(args) != 2 or not args[1].isdigit():
                return 'Usage: /source disable SOURCE_ID'
            try:
                item = _source_by_id(int(args[1]))
            except ValueError as exc:
                return str(exc)
            if item is None:
                return 'Source not found.'
            item.enabled = False
            item.status = 'disabled'
            item.save(update_fields=['enabled', 'status', 'updated_at'])
            from .audit import record_audit_event

            record_audit_event(
                'source_disabled',
                item.pk,
                {'source_type': 'telegram' if isinstance(item, TelegramSource) else 'web'},
            )
            return f'Source {item.pk} disabled.'
        if action == 'add':
            payload = ' '.join(args[1:]).strip()
            parts = [part.strip() for part in payload.split('|', maxsplit=1)]
            if len(parts) != 2 or not parts[0] or not parts[1]:
                return 'Usage: /source add NAME | https://public.example/jobs'
            item = Source(name=parts[0], url=parts[1], source_type='website')
            try:
                item.full_clean()
                item.save()
            except (ValidationError, IntegrityError) as exc:
                return f'Source was not added: {exc}'
            return f'Web source {item.pk} added.'
        return 'Usage: /source add NAME | URL or /source disable SOURCE_ID'

    if command == 'scan':
        if not args:
            web_result = scan_sources_task.delay(20)
            telegram_result = scan_telegram_sources_task.delay(20)
            return (
                f'Web source scan queued: {web_result.id}\n'
                f'Telegram source scan queued: {telegram_result.id}'
            )
        if len(args) == 2 and args[0].lower() in {'web', 'telegram'}:
            source_kind, raw_id = args
        elif len(args) == 1 and args[0].isdigit():
            raw_id = args[0]
            try:
                item = _source_by_id(int(raw_id))
            except ValueError as exc:
                return str(exc)
            if item is None:
                return 'Source not found.'
            source_kind = 'telegram' if isinstance(item, TelegramSource) else 'web'
        else:
            return 'Usage: /scan [SOURCE_ID] or /scan web|telegram SOURCE_ID'
        if not raw_id.isdigit():
            return 'Source ID must be a positive integer.'
        source_id = int(raw_id)
        try:
            if source_kind == 'telegram':
                result = scan_telegram_sources_task.delay(
                    1,
                    telegram_source_ids=[source_id],
                )
            else:
                result = scan_sources_task.delay(1, source_ids=[source_id])
        except ValueError as exc:
            return str(exc)
        return f'{source_kind.title()} source scan queued: {result.id}'

    if command == 'discover':
        result = discover_sources_task.delay()
        return f'Discovery queued: {result.id}'

    if command == 'cycle':
        result = automation_cycle_task.delay()
        return f'Automation cycle queued: {result.id}'

    return 'Unknown command. Use /help.'


def _execute_audited_command(command, args, telegram_user_id, chat_id, natural_language=False):
    from .audit import record_audit_event

    try:
        response = (
            handle_natural_language(command)
            if natural_language
            else _execute_command(command, args)
        )
    except Exception as exc:
        record_audit_event(
            'admin_command_failed',
            'natural_language' if natural_language else command,
            {
                'telegram_user_id': str(telegram_user_id),
                'chat_id': str(chat_id),
                'error_type': type(exc).__name__,
            },
        )
        raise
    record_audit_event(
        'admin_command_executed',
        'natural_language' if natural_language else command,
        {
            'telegram_user_id': str(telegram_user_id),
            'chat_id': str(chat_id),
            'arguments': [] if natural_language else list(args),
        },
    )
    return response


def build_application_bot():
    try:
        from telegram import Update
        from telegram.ext import (
            Application as TGApplication,
            CommandHandler,
            ContextTypes,
            MessageHandler,
            filters,
        )
    except ImportError as exc:
        raise RuntimeError('python-telegram-bot is not installed.') from exc

    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    if not token:
        raise RuntimeError('TELEGRAM_BOT_TOKEN is required.')

    async def command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            if update.effective_message:
                await update.effective_message.reply_text('Unauthorized.')
            return
        command_text = (update.effective_message.text or '').split(maxsplit=1)[0]
        command = command_text[1:].split('@', maxsplit=1)[0].lower()
        response = await sync_to_async(
            _execute_audited_command,
            thread_sensitive=True,
        )(
            command,
            context.args,
            update.effective_user.id,
            update.effective_chat.id,
        )
        await update.effective_message.reply_text(response)

    async def natural_language_handler(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not authorized(update):
            await update.effective_message.reply_text('Unauthorized.')
            return
        response = await sync_to_async(
            _execute_audited_command,
            thread_sensitive=True,
        )(
            update.effective_message.text,
            [],
            update.effective_user.id,
            update.effective_chat.id,
            natural_language=True,
        )
        await update.effective_message.reply_text(response)

    bot = TGApplication.builder().token(token).build()
    for command in COMMANDS:
        bot.add_handler(CommandHandler(command, command_handler))
    bot.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, natural_language_handler),
    )
    return bot
