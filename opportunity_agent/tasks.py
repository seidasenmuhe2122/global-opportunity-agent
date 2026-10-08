from __future__ import annotations
import ipaddress, socket, asyncio
import logging
from datetime import timedelta
from urllib.parse import urlsplit
from celery import shared_task
from bs4 import BeautifulSoup
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import Application, ApplicationAttempt, ApplicationFormTemplate, SiteCredential, AuditLog, AutomationRun, Match, Opportunity, Source, TelegramSource, UserProfile, ProviderAdapter
from .services.ai_engine import AIClient, AIProviderError
from .services.application_guard import ApplicationGuard
from .services.deduplication import deduplicate_and_save_opportunity
from .services.matching import compute_match_score
from .services.source_discovery import discover_public_sources
from .services.source_ingestion import (
    basic_extract,
    extract_candidates,
    fetch_public_source,
    is_listing_page,
)
from .services.provider_adapters import adapter_for
from .services.document_forms import download_form, fill_pdf
from .services.telegram import TelegramNotifier
import requests

logger = logging.getLogger(__name__)
MAX_TASK_BATCH_SIZE=100; SOURCE_REQUEST_TIMEOUT=15

def _validate_public_source_url(url):
    p=urlsplit(url)
    if p.scheme not in {'http','https'} or not p.hostname or p.username or p.password: raise ValueError('Source URL must be a public HTTP or HTTPS URL.')
    if p.hostname.lower().endswith(('.localhost','.local')): raise ValueError('Local network source URLs are not allowed.')
    try: addresses=socket.getaddrinfo(p.hostname,p.port or (443 if p.scheme=='https' else 80))
    except OSError as e: raise ValueError(f'Source hostname could not be resolved: {e}') from e
    ips={ipaddress.ip_address(x[4][0]) for x in addresses}
    if not ips or any(not ip.is_global for ip in ips): raise ValueError('Source URL must resolve only to public IP addresses.')

def _validate_limit(limit):
    if not isinstance(limit,int) or isinstance(limit,bool) or not 1<=limit<=MAX_TASK_BATCH_SIZE: raise ValueError(f'limit must be an integer between 1 and {MAX_TASK_BATCH_SIZE}')
    return limit


def _sources_due_for_scan(now=None):
    now = now or timezone.now()
    enabled_sources = Source.objects.filter(enabled=True).exclude(status='disabled')
    due = Q()
    for frequency, interval in (
        ('hourly', timedelta(hours=1)),
        ('every_6_hours', timedelta(hours=6)),
        ('daily', timedelta(days=1)),
        ('weekly', timedelta(days=7)),
    ):
        due |= Q(
            scan_frequency=frequency,
        ) & (
            Q(last_scan__isnull=True)
            | Q(last_scan__lte=now - interval)
        )
    return enabled_sources.filter(due).order_by('last_scan', 'pk')


def _telegram_sources_due_for_scan(now=None):
    now = now or timezone.now()
    due = Q()
    for frequency, interval in (
        ('hourly', timedelta(hours=1)),
        ('every_6_hours', timedelta(hours=6)),
        ('daily', timedelta(days=1)),
        ('weekly', timedelta(days=7)),
    ):
        due |= Q(scan_frequency=frequency) & (
            Q(last_scan__isnull=True)
            | Q(last_scan__lte=now - interval)
        )
    return TelegramSource.objects.filter(
        enabled=True,
        status__in=['active', 'pending', 'error', 'scanning'],
    ).filter(due).order_by('last_scan', 'pk')

def _profile_dict(p):
    return {'skills':p.skills,'current_country':p.current_country,'target_countries':p.target_countries,'worldwide_preference':p.worldwide_preference,'preferred_opportunity_types':p.preferred_opportunity_types,'preferred_work_modes':p.preferred_work_modes,'visa_sponsorship_preference':p.visa_sponsorship_preference,'salary_stipend_preference':p.salary_stipend_preference,'minimum_ai_match_score':p.minimum_ai_match_score,'auto_apply':p.auto_apply,'education':p.education,'degree':p.degree,'certifications':p.certifications,'work_experience':p.work_experience,'languages':p.languages}

def _opp_dict(o):
    return {'skills':o.skills,'country':o.country,'work_mode':o.work_mode,'remote_worldwide':o.remote_worldwide,'opportunity_type':o.opportunity_type,'visa_sponsorship':o.visa_sponsorship,'education_requirements':o.education_requirements,'experience_requirements':o.experience_requirements,'languages':o.languages,'salary_stipend':o.salary_stipend,'qualifications':o.qualifications,'requirements':o.requirements}

def _save_match(user,opportunity,result):
    match, created = Match.objects.update_or_create(
        user=user,
        opportunity=opportunity,
        defaults={
            'score': result['score'],
            'eligible': result['eligible'],
            'reasons': result['reasons'],
            'strong_matches': result.get('strong_matches', []),
            'missing_requirements': result['missing'],
            'risk_factors': result.get('risks', []),
            'recommended_action': result.get('recommended_action', ''),
        },
    )
    if created:
        from .services.audit import record_audit_event

        record_audit_event(
            'opportunity_matched',
            match.pk,
            {
                'user_id': user.pk,
                'opportunity_id': opportunity.pk,
                'score': result['score'],
                'eligible': result['eligible'],
            },
        )
    return match, created

def _upsert_opportunity(source,data):
    return deduplicate_and_save_opportunity(
        source,
        data,
        raw_content=data.get('raw_source_content') or data.get('description') or '',
    )

@shared_task
def scan_sources_task(limit=10, source_ids=None):
    limit=_validate_limit(limit); successful=0; errors=[]; candidate_errors=[]; new_opportunities=0
    if source_ids is None:
        sources=list(_sources_due_for_scan()[:limit])
    else:
        if (
            not isinstance(source_ids, (list, tuple))
            or not source_ids
            or len(source_ids) > MAX_TASK_BATCH_SIZE
            or any(not isinstance(source_id, int) or isinstance(source_id, bool) or source_id < 1 for source_id in source_ids)
            or len(set(source_ids)) != len(source_ids)
        ):
            raise ValueError(f'source_ids must contain between 1 and {MAX_TASK_BATCH_SIZE} positive integers')
        requested_ids=set(source_ids)
        sources=list(
            Source.objects.filter(pk__in=requested_ids, enabled=True)
            .exclude(status='disabled')
            .exclude(source_type='telegram')
            .order_by('pk')
        )
        if len(sources) != len(requested_ids):
            raise ValueError('Every requested source must exist and be enabled.')
        if len(sources) > limit:
            raise ValueError('limit cannot be smaller than the number of requested source_ids.')
    source_ids=[source.pk for source in sources]
    ai=AIClient()
    for source in sources:
        now=timezone.now()
        source.status='scanning'
        source.last_scan=now
        source.save(update_fields=['status','last_scan','updated_at'])
        try:
            _validate_public_source_url(source.url)
            final_url,body,_=fetch_public_source(source.url,SOURCE_REQUEST_TIMEOUT)
            candidates=list(extract_candidates(source,final_url,body))[:50]
            for candidate in candidates:
                try:
                    if candidate.get('fetch_detail_page'):
                        candidate_url = candidate.get('url', '')
                        _validate_public_source_url(candidate_url)
                        detail_url, detail_body, _ = fetch_public_source(
                            candidate_url,
                            SOURCE_REQUEST_TIMEOUT,
                        )
                        detail_soup = BeautifulSoup(detail_body, 'html.parser')
                        detail_title = detail_soup.find('h1') or detail_soup.title
                        if detail_title:
                            candidate['title'] = detail_title.get_text(' ', strip=True)
                        candidate['url'] = detail_url
                        candidate['text'] = detail_soup.get_text('\n', strip=True)
                        candidate['html'] = detail_body
                        candidate['raw_source_content'] = detail_body

                    if source.source_type == 'website' and is_listing_page(candidate):
                        logger.info(
                            'Skipping listing/category page for source %s: %s',
                            source.pk,
                            candidate.get('url', ''),
                        )
                        continue

                    data=basic_extract(candidate,source)
                    evidence_text = '\n'.join(filter(None, (
                        candidate.get('text', ''),
                        'URL:' + candidate.get('url', '') if candidate.get('url') else '',
                    )))
                    try:
                        ai_data=ai.extract_opportunity(
                            evidence_text,
                            candidate.get('source_landing_url') or candidate.get('url',''),
                            source.name,
                        )
                    except AIProviderError:
                        logger.warning(
                            'AI extraction unavailable for source %s candidate %s; using explicit source fields only.',
                            source.pk,
                            candidate.get('url', ''),
                            exc_info=True,
                        )
                        ai_data = {}
                    if ai_data:
                        data.update({
                            key: value
                            for key, value in ai_data.items()
                            if value not in ('', None, [], {})
                            and key not in {'source_url', 'raw_source_content', 'application_url'}
                            and (key != 'description' or not data.get('description'))
                        })
                    data['raw_source_content'] = (
                        candidate.get('raw_source_content')
                        or candidate.get('html')
                        or candidate.get('text')
                        or ''
                    )
                    opp,created=_upsert_opportunity(source,data)
                    if opp:
                        new_opportunities += int(created)
                        for profile in UserProfile.objects.select_related('user').filter(user__is_active=True):
                            result=compute_match_score(_profile_dict(profile),_opp_dict(opp)); _save_match(profile.user,opp,result)
                            if result['eligible'] and profile.auto_apply:
                                Application.objects.get_or_create(user=profile.user,opportunity=opp,defaults={'match_score':result['score'],'status':'queued'})
                except Exception as exc:
                    candidate_errors.append({
                        'source_id': source.pk,
                        'url': candidate.get('url', ''),
                        'error': str(exc),
                    })
                    logger.exception(
                        'Opportunity candidate processing failed for source %s (%s); continuing.',
                        source.pk,
                        candidate.get('url', ''),
                    )
            source.status='active'; source.error_count=0; source.last_scan=now; source.last_successful_scan=now
            source.save(update_fields=['status','error_count','last_scan','last_successful_scan','updated_at']); successful += 1
        except Exception as exc:
            source.status='error'; source.error_count+=1; source.save(update_fields=['status','error_count','updated_at']); errors.append({'source_id':source.pk,'error':str(exc)})
            logger.exception('Opportunity source scan failed for source %s (%s).', source.pk, source.url)
            from .services.audit import record_audit_event

            try:
                record_audit_event(
                    'source_scan_failed',
                    source.pk,
                    {'source_type': source.source_type, 'error_count': source.error_count, 'error': str(exc)[:1000]},
                )
            except Exception:
                logger.exception('Could not audit failed scan for source %s.', source.pk)
    return {
        'scanned': len(source_ids),
        'successful': successful,
        'source_ids': source_ids,
        'new_opportunities': new_opportunities,
        'errors': errors,
        'candidate_errors': candidate_errors,
    }


@shared_task
def scan_telegram_sources_task(limit=10, telegram_source_ids=None):
    limit = _validate_limit(limit)
    if telegram_source_ids is None:
        sources = list(_telegram_sources_due_for_scan()[:limit])
    else:
        if (
            not isinstance(telegram_source_ids, (list, tuple))
            or not telegram_source_ids
            or len(telegram_source_ids) > MAX_TASK_BATCH_SIZE
            or any(
                not isinstance(source_id, int)
                or isinstance(source_id, bool)
                or source_id < 1
                for source_id in telegram_source_ids
            )
            or len(set(telegram_source_ids)) != len(telegram_source_ids)
        ):
            raise ValueError(
                f'telegram_source_ids must contain between 1 and '
                f'{MAX_TASK_BATCH_SIZE} positive integers'
            )
        requested_ids = set(telegram_source_ids)
        sources = list(
            TelegramSource.objects.filter(pk__in=requested_ids, enabled=True)
            .exclude(status='disabled')
            .order_by('pk')
        )
        if len(sources) != len(requested_ids):
            raise ValueError('Every requested Telegram source must exist and be enabled.')
        if len(sources) > limit:
            raise ValueError(
                'limit cannot be smaller than the number of requested telegram_source_ids.'
            )

    successful = 0
    new_opportunities = 0
    errors = []
    ai = AIClient()
    for source in sources:
        now = timezone.now()
        source.status = 'scanning'
        source.last_scan = now
        source.save(update_fields=['status', 'last_scan', 'updated_at'])
        try:
            from .services.telegram_collector import collect_public_channel

            candidates = asyncio.run(collect_public_channel(source, limit=50))
            for candidate in candidates:
                data = basic_extract(candidate, source)
                try:
                    evidence_text = '\n'.join(filter(None, (
                        candidate.get('text', ''),
                        'URL:' + candidate.get('url', '') if candidate.get('url') else '',
                    )))
                    ai_data = ai.extract_opportunity(
                        evidence_text,
                        candidate.get('url', ''),
                        source.name,
                    )
                    if ai_data:
                        data.update({
                            key: value
                            for key, value in ai_data.items()
                            if value not in ('', None, [], {})
                            and key not in {'source_url', 'raw_source_content', 'application_url'}
                            and (key != 'description' or not data.get('description'))
                        })
                except AIProviderError:
                    logger.warning(
                        'AI extraction unavailable for Telegram source %s candidate %s; '
                        'using explicit source fields only.',
                        source.pk,
                        candidate.get('url', ''),
                        exc_info=True,
                    )
                data['raw_source_content'] = (
                    candidate.get('raw_source_content')
                    or candidate.get('text')
                    or ''
                )
                opportunity, created = _upsert_opportunity(source, data)
                if opportunity:
                    new_opportunities += int(created)
                    for profile in UserProfile.objects.select_related('user').filter(
                        user__is_active=True,
                    ):
                        result = compute_match_score(
                            _profile_dict(profile),
                            _opp_dict(opportunity),
                        )
                        _save_match(profile.user, opportunity, result)
                        if result['eligible'] and profile.auto_apply:
                            Application.objects.get_or_create(
                                user=profile.user,
                                opportunity=opportunity,
                                defaults={
                                    'match_score': result['score'],
                                    'status': 'queued',
                                },
                            )
            source.status = 'active'
            source.error_count = 0
            source.last_successful_scan = now
            source.save(update_fields=[
                'status',
                'error_count',
                'last_successful_scan',
                'updated_at',
            ])
            successful += 1
        except Exception as exc:
            source.status = 'error'
            source.error_count += 1
            source.save(update_fields=['status', 'error_count', 'updated_at'])
            errors.append({'telegram_source_id': source.pk, 'error': str(exc)})
            logger.exception(
                'Telegram opportunity source scan failed for source %s (%s).',
                source.pk,
                source.channel_url,
            )
            from .services.audit import record_audit_event

            try:
                record_audit_event(
                    'telegram_source_scan_failed',
                    source.pk,
                    {'error_count': source.error_count, 'error': str(exc)[:1000]},
                )
            except Exception:
                logger.exception('Could not audit failed scan for Telegram source %s.', source.pk)
    return {
        'scanned': len(sources),
        'successful': successful,
        'telegram_source_ids': [source.pk for source in sources],
        'new_opportunities': new_opportunities,
        'errors': errors,
    }


@shared_task
def discover_sources_task():
    discovered=discover_public_sources(); created=[]; errors=[]
    for item in discovered:
        try:
            source,was_created=Source.objects.get_or_create(
                url=item['url'],
                defaults={**item, 'status':'pending'},
            )
            if was_created:
                created.append(source.pk)
                from .services.audit import record_audit_event

                try:
                    record_audit_event(
                        'source_added',
                        source.pk,
                        {'name': source.name, 'url': source.url, 'auto_discovered': True},
                    )
                except Exception:
                    logger.exception('Could not audit discovered source %s.', source.pk)
        except Exception as exc:
            logger.exception('Could not save discovered source %r.', item.get('url'))
            errors.append({'url':item.get('url'), 'error':str(exc)})
    return {'discovered':len(discovered),'created':len(created),'source_ids':created,'errors':errors}

@shared_task
def refresh_matches_task(limit=100):
    count=0
    for profile in UserProfile.objects.select_related('user').filter(user__is_active=True):
        for opp in Opportunity.objects.filter(status='active').order_by('-created_at')[:limit]:
            result=compute_match_score(_profile_dict(profile),_opp_dict(opp)); _save_match(profile.user,opp,result); count+=1
    return {'matches_refreshed':count}

@shared_task
def process_application_queue_task(limit=20):
    limit=_validate_limit(limit); processed=[]
    queue=Application.objects.filter(status__in=['queued','matching','prepared']).select_related('user','opportunity').order_by('created_at','pk')[:limit]
    for app in queue:
        try:
            if app.status != 'matching':
                app.status = 'matching'
                app.save(update_fields=['status','updated_at'])
            profile=UserProfile.objects.filter(user_id=app.user_id).first(); opp=app.opportunity
            if not profile or opp.status!='active' or opp.is_expired(): app.status='needs_review'; app.error_message='Profile missing or opportunity is no longer active.'; app.save(update_fields=['status','error_message','updated_at']); continue
            result=compute_match_score(_profile_dict(profile),_opp_dict(opp)); match,_=_save_match(app.user,opp,result); app.match_score=result['score']
            threshold_override = app.match_override and result['manual_override_allowed']
            if not result['eligible'] and not threshold_override:
                app.status='needs_review'
                try:
                    rejection = AIClient().explain_rejection(
                        _profile_dict(profile),
                        _opp_dict(opp),
                        result,
                    )
                    app.rejection_reason = rejection['summary']
                except AIProviderError:
                    logger.exception(
                        'AI rejection explanation failed for application %s; using deterministic match reasons.',
                        app.pk,
                    )
                    app.rejection_reason = ' '.join(
                        result.get('missing', []) + result.get('risks', [])
                    ) or 'Match score is below the configured threshold.'
                app.error_message=app.rejection_reason
                app.save(update_fields=['status','match_score','rejection_reason','error_message','updated_at'])
                continue
            if not app.cover_letter:
                try:
                    app.cover_letter=AIClient().generate_cover_letter(
                        _profile_dict(profile),
                        _opp_dict(opp),
                    )
                except AIProviderError:
                    logger.exception('AI cover-letter generation failed for application %s.', app.pk)
                    app.status='needs_review'
                    app.error_message='Cover-letter generation failed; review and prepare this application manually.'
                    app.save(update_fields=['status','match_score','error_message','updated_at'])
                    continue
            app.status='prepared'; app.error_message=''
            app.save(update_fields=['status','match_score','cover_letter','error_message','updated_at']); processed.append(app.pk)
        except Exception as exc:
            logger.exception('Application preparation failed for application %s; continuing with the queue.', app.pk)
            app.status = 'failed'
            app.error_message = f'Application preparation failed: {exc}'[:4000]
            app.save(update_fields=['status', 'error_message', 'updated_at'])
    return {'processed':len(processed),'application_ids':processed}

@shared_task
def execute_application_queue_task(limit=10):
    limit = _validate_limit(limit)
    done = []
    application_ids = list(
        Application.objects.filter(status='prepared')
        .order_by('created_at', 'pk')
        .values_list('pk', flat=True)[:limit]
    )
    for application_id in application_ids:
        try:
            if _execute_one_application(application_id):
                done.append(application_id)
        except Exception as exc:
            logger.exception(
                'Application execution failed for application %s; continuing with the batch.',
                application_id,
            )
            try:
                app = Application.objects.select_related('opportunity').get(pk=application_id)
                if app.status in {'queued', 'matching', 'prepared'}:
                    app.status = 'failed'
                    app.error_message = f'Application execution failed before submission: {exc}'[:4000]
                elif app.status == 'pending':
                    app.status = 'needs_review'
                    app.error_message = (
                        'Application execution stopped after an attempt was reserved; '
                        f'the submission outcome may be uncertain: {exc}'
                    )[:4000]
                else:
                    continue
                app.save(update_fields=['status', 'error_message', 'updated_at'])
                ApplicationAttempt.objects.filter(
                    application=app,
                    attempt_number=app.attempts,
                    status='pending',
                ).update(status=app.status, error_message=app.error_message)
            except Exception:
                logger.exception(
                    'Could not persist isolated execution failure for application %s.',
                    application_id,
                )
    return {'submitted':len(done),'application_ids':done}


@shared_task
def retry_applications_task(application_ids, status='failed', actor_id=None):
    if status not in {'failed', 'rejected'}:
        raise ValueError('Only failed or rejected applications can be retried.')
    if (
        not isinstance(application_ids, (list, tuple))
        or not application_ids
        or len(application_ids) > MAX_TASK_BATCH_SIZE
        or any(
            not isinstance(application_id, int)
            or isinstance(application_id, bool)
            or application_id < 1
            for application_id in application_ids
        )
        or len(set(application_ids)) != len(application_ids)
    ):
        raise ValueError(
            f'application_ids must contain between 1 and '
            f'{MAX_TASK_BATCH_SIZE} unique positive integers'
        )

    from .services.audit import record_audit_event

    actor = None
    if actor_id is not None:
        from django.contrib.auth import get_user_model

        actor = get_user_model().objects.filter(pk=actor_id).first()

    queued_ids = []
    for application_id in application_ids:
        try:
            with transaction.atomic():
                application = Application.objects.select_for_update().get(
                    pk=application_id,
                )
                if application.status != status:
                    continue
                application.status = 'queued'
                application.error_message = ''
                application.rejection_reason = ''
                application.save(update_fields=[
                    'status',
                    'error_message',
                    'rejection_reason',
                    'updated_at',
                ])
                record_audit_event(
                    'application_retry_queued',
                    application.pk,
                    {'previous_status': status},
                    actor=actor,
                )
                queued_ids.append(application.pk)
        except Application.DoesNotExist:
            logger.info(
                'Application %s no longer exists; skipping requested retry.',
                application_id,
            )
        except Exception:
            logger.exception(
                'Could not queue retry for application %s; continuing the retry batch.',
                application_id,
            )

    if queued_ids:
        transaction.on_commit(
            lambda: process_application_queue_task.delay(
                min(len(queued_ids), MAX_TASK_BATCH_SIZE),
            )
        )
    return {'queued': len(queued_ids), 'application_ids': queued_ids}


def _execute_one_application(application_id):
    with transaction.atomic():
        locked_app = Application.objects.select_for_update().select_related(
            'user',
            'opportunity',
        ).get(pk=application_id)
        if locked_app.status != 'prepared':
            return False
        locked_profile = UserProfile.objects.select_for_update().filter(
            user_id=locked_app.user_id,
        ).first()
        if not locked_profile:
            locked_app.status = 'needs_review'
            locked_app.error_message = 'User profile is missing.'
            locked_app.save(update_fields=['status', 'error_message', 'updated_at'])
            return False
        current_match = compute_match_score(
            _profile_dict(locked_profile),
            _opp_dict(locked_app.opportunity),
        )
        locked_match, _ = _save_match(
            locked_app.user,
            locked_app.opportunity,
            current_match,
        )
        domain = (urlsplit(locked_app.opportunity.application_url).hostname or '').lower()
        adapter_record = None
        if domain:
            for candidate in ProviderAdapter.objects.filter(enabled=True):
                allowed = [
                    str(value).lower()
                    for value in (candidate.config or {}).get('allowed_domains', [])
                ]
                if domain in allowed or any(
                    domain.endswith('.' + value) for value in allowed
                ):
                    adapter_record = candidate
                    break
        credential = None
        if domain:
            credential = SiteCredential.objects.filter(
                user=locked_app.user,
                enabled=True,
                domain__iexact=domain,
            ).order_by('-updated_at').first()
        form_template = None
        for candidate_template in ApplicationFormTemplate.objects.filter(enabled=True):
            if candidate_template.matches_domain(domain):
                form_template = candidate_template
                break
        adapter = adapter_for(
            locked_app.opportunity,
            adapter_record,
            credential,
            form_template,
        )
        allowed, safety_reason = ApplicationGuard.can_submit(
            locked_app.user,
            locked_app.opportunity,
            locked_profile,
            locked_match,
            application=locked_app,
            adapter=adapter,
        )
        if not allowed:
            locked_app.status = 'needs_review'
            locked_app.error_message = safety_reason
            locked_app.save(update_fields=['status','error_message','updated_at'])
            return False
        locked_app.attempts += 1
        locked_app.status = 'pending'
        locked_app.save(update_fields=['attempts','status','updated_at'])
        attempt = ApplicationAttempt.objects.create(
            application=locked_app,
            attempt_number=locked_app.attempts,
            status='pending',
        )

    app = locked_app
    try:
        adapter_result = adapter.submit(app)
    except Exception as exc:
        logger.exception('Application provider adapter failed for application %s.', app.pk)
        app.status = 'needs_review'
        app.error_message = (
            'Provider adapter raised an error after an attempt was reserved; '
            f'the submission outcome may be uncertain: {exc}'
        )[:4000]
        app.save(update_fields=['status','error_message','updated_at'])
        adapter_result = False
    app.refresh_from_db()
    result_url = urlsplit(app.result_url)
    if adapter_result and (
        app.status != 'submitted'
        or app.submission_time is None
        or result_url.scheme not in {'http', 'https'}
        or not result_url.netloc
    ):
        app.status = 'needs_review'
        app.error_message = (
            'Submission result could not be verified: adapter did not save a valid '
            'confirmed submission status, submission time, and result URL.'
        )
        app.save(update_fields=['status','error_message','updated_at'])
    elif not adapter_result and app.status == 'submitted':
        app.status = 'needs_review'
        app.error_message = (
            'Submission outcome is uncertain: provider adapter reported failure '
            'after setting a submitted status.'
        )
        app.save(update_fields=['status','error_message','updated_at'])
    elif not adapter_result and app.status == 'pending':
        app.status = 'needs_review'
        app.error_message = (
            'Provider adapter did not return a confirmed result; manual review is required.'
        )
        app.save(update_fields=['status','error_message','updated_at'])
    attempt.status = app.status
    attempt.error_message = app.error_message
    attempt.save(update_fields=['status','error_message'])
    return app.status == 'submitted'

@shared_task
def expire_deadlines_task():
    qs = Opportunity.objects.filter(
        deadline__lt=timezone.now(),
        status__in=['active', 'needs_review', 'expired'],
    )
    count = qs.update(status='inactive')
    return {'expired': count}

@shared_task
def health_check_task():
    now=timezone.now()
    enabled_sources=Source.objects.filter(enabled=True)
    enabled_telegram_sources=TelegramSource.objects.filter(enabled=True)
    stale=enabled_sources.filter(
        Q(last_successful_scan__isnull=True)
        | Q(last_successful_scan__lt=now-timedelta(hours=48))
    ).count()
    stale += enabled_telegram_sources.filter(
        Q(last_successful_scan__isnull=True)
        | Q(last_successful_scan__lt=now-timedelta(hours=48))
    ).count()
    failed=enabled_sources.filter(status='error').count()
    failed += enabled_telegram_sources.filter(status='error').count()
    scanning=enabled_sources.filter(status='scanning').count()
    scanning += enabled_telegram_sources.filter(status='scanning').count()
    return {
        'stale_sources':stale,
        'failed_sources':failed,
        'scanning_sources':scanning,
        'time':now.isoformat(),
    }

@shared_task
def automation_cycle_task():
    run=AutomationRun.objects.create(status='running',details={}); started=timezone.now()
    details={}; stage_errors=[]
    stages=(
        ('scan', scan_sources_task.run, ()),
        ('telegram_scan', scan_telegram_sources_task.run, ()),
        ('matches', refresh_matches_task.run, ()),
        ('queue', process_application_queue_task.run, ()),
        ('execution', execute_application_queue_task.run, ()),
        ('expired', expire_deadlines_task.run, ()),
    )
    for name, task, args in stages:
        try:
            details[name]=task(*args, **({'limit':50} if name in {'scan','telegram_scan','matches','queue'} else {'limit':20} if name == 'execution' else {}))
            if details[name].get('errors'):
                stage_errors.append({'stage':name,'errors':details[name]['errors']})
        except Exception as exc:
            logger.exception('Automation cycle stage %s failed; continuing with remaining stages.', name)
            error={'stage':name,'error':str(exc)}
            details[name]=error
            stage_errors.append(error)
    if stage_errors:
        details['stage_errors']=stage_errors
    run.status='failed' if stage_errors else 'success'
    run.finished_at=timezone.now(); run.details=details; run.save(update_fields=['status','finished_at','details'])
    return {'run_id':run.pk,'duration_seconds':round((timezone.now()-started).total_seconds(),2),**details}

@shared_task
def send_notification_task(chat_id,message): return {'chat_id':chat_id,'sent':TelegramNotifier().send_message(chat_id,message)}


@shared_task
def send_telegram_system_alert_task(message):
    return {
        'type': 'system_alert',
        'results': TelegramNotifier().send_to_enabled_destinations(
            'system_alert',
            message,
        ),
    }


@shared_task
def send_telegram_application_status_task(application_id, status):
    from .models import TelegramDestination

    status_types = {
        'submitted': 'applied',
        'rejected': 'rejected',
        'failed': 'failed',
        'needs_review': 'needs_review',
    }
    destination_type = status_types.get(status)
    if destination_type is None:
        return {'sent': 0, 'reason': 'No Telegram destination type for this application status.'}

    application = Application.objects.select_related('opportunity', 'user').filter(
        pk=application_id,
    ).first()
    if application is None or application.status != status:
        return {'sent': 0, 'reason': 'Application status changed before notification delivery.'}

    if not TelegramDestination.objects.filter(
        enabled=True,
        type=destination_type,
    ).exists():
        return {'sent': 0, 'reason': 'No enabled Telegram destinations for this status.'}

    opportunity = application.opportunity
    profile_name = UserProfile.objects.filter(
        user_id=application.user_id,
    ).values_list('full_name', flat=True).first()
    user_label = (
        profile_name
        or application.user.get_full_name()
        or application.user.get_username()
    )
    match_score = f'{application.match_score}%'
    if status == 'submitted':
        submitted_at = (
            timezone.localtime(application.submission_time).strftime('%Y-%m-%d %H:%M %Z')
            if application.submission_time
            else 'Not recorded'
        )
        message = (
            'APPLICATION SUBMITTED\n'
            f'User: {user_label}\n'
            f'Opportunity: {opportunity.title}\n'
            f'Organization: {opportunity.organization or "Not provided"}\n'
            f'Match Score: {match_score}\n'
            f'Application URL: {application.result_url or opportunity.application_url or "Not provided"}\n'
            f'Submission Time: {submitted_at}'
        )
    elif status == 'rejected':
        message = (
            'APPLICATION REJECTED\n'
            f'Opportunity: {opportunity.title}\n'
            f'Reason: {application.rejection_reason or application.error_message or "Not provided"}\n'
            f'Match Score: {match_score}\n'
            f'Source URL: {opportunity.source_url or "Not provided"}'
        )
    elif status == 'failed':
        message = (
            'APPLICATION FAILED\n'
            f'Opportunity: {opportunity.title}\n'
            f'Error: {application.error_message or "No error details recorded."}\n'
            f'Attempt Count: {application.attempts}'
        )
    else:
        message = (
            'MANUAL REVIEW REQUIRED\n'
            f'Opportunity: {opportunity.title}\n'
            f'Reason: {application.error_message or application.rejection_reason or "Not provided"}\n'
            'Required Action: Review the application manually and decide whether to proceed, retry, or reject.'
        )
    return {
        'application_id': application.pk,
        'type': destination_type,
        'results': TelegramNotifier().send_to_enabled_destinations(
            destination_type,
            message,
        ),
    }


@shared_task
def write_audit_log_task(actor_id,action,target,details=None):
    log=AuditLog.objects.create(actor_id=actor_id,action=action,target=target,details=details or {}); return {'audit_log_id':log.pk}
