from datetime import timedelta
import json
from xml.etree.ElementTree import Element, SubElement, tostring

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.models import Group
from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Count, Q
from django.core.exceptions import PermissionDenied
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseNotFound,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from pathlib import Path

from .forms import UserProfileForm, SignUpForm, SiteCredentialForm, EmailMailboxForm
from .models import (
    Application,
    AIConversation,
    AIMessage,
    AuditLog,
    AutomationRun,
    Match,
    Opportunity,
    PrivateAccessToken,
    REGISTRATION_MODES,
    Source,
    UserProfile,
    SiteCredential,
    EmailMailbox,
    get_registration_mode,
    get_website_visibility,
    set_registration_mode,
    set_website_visibility,
    validate_private_access_token,
)
from .services.matching import (
    compute_match_score,
    opportunity_match_data,
    profile_match_data,
    refresh_user_matches,
    save_match,
)
from .services.agent_conversations import process_user_message
from .services.audit import record_audit_event
from .services.source_ingestion import is_listing_opportunity


def _public_opportunities(queryset):
    return [
        opportunity for opportunity in queryset
        if not is_listing_opportunity(opportunity)
    ]


def _admin(request):
    return (
        request.user.is_authenticated
        and request.user.is_staff
        and (
            request.user.is_superuser
            or (
                request.user.has_perm('opportunity_agent.view_source')
                and request.user.has_perm('opportunity_agent.view_application')
            )
        )
    )


def _require_permission(user, permission):
    if not user.has_perm(permission):
        raise PermissionDenied


def _safe_redirect_target(request, target):
    if not target or not isinstance(target, str):
        return ''
    candidate = target.strip()
    if not candidate or candidate.startswith('//'):
        return ''
    if candidate.startswith('/') or candidate.startswith('?'):
        return candidate
    if url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()}):
        return candidate
    return ''


@login_required
def ai_chat(request):
    conversations = AIConversation.objects.filter(user=request.user)
    current_id = request.GET.get('conversation')
    current_conversation = None
    if current_id and current_id.isdigit():
        current_conversation = conversations.filter(pk=int(current_id)).first()
    if current_conversation is None:
        current_conversation = conversations.first()
    current_messages = (
        current_conversation.messages.all()
        if current_conversation
        else AIMessage.objects.none()
    )
    return render(request, 'opportunity_agent/ai_chat.html', {
        'conversations': conversations[:50],
        'current_conversation': current_conversation,
        'current_messages': current_messages,
    })


@login_required
def ai_chat_new(request):
    if request.method != 'POST':
        return redirect('ai_chat')
    conversation = AIConversation.objects.create(
        user=request.user,
        title='New conversation',
    )
    record_audit_event(
        'ai_conversation_created',
        conversation.pk,
        {},
        actor=request.user,
    )
    return redirect(f'{reverse("ai_chat")}?conversation={conversation.pk}')


@login_required
def ai_chat_send(request, conversation_id):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'POST is required.'}, status=405)
    try:
        payload = json.loads(request.body or b'{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'success': False, 'message': 'Request body must be valid JSON.'}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({'success': False, 'message': 'Request body must be a JSON object.'}, status=400)
    conversation = get_object_or_404(
        AIConversation,
        pk=conversation_id,
        user=request.user,
    )
    text = payload.get('message')
    confirmation_token = payload.get('confirmation_token', '')
    if not isinstance(confirmation_token, str):
        return JsonResponse({'success': False, 'message': 'Invalid confirmation token.'}, status=400)
    try:
        result = process_user_message(
            request.user,
            conversation,
            text,
            confirmation_token=confirmation_token,
        )
    except PermissionError:
        return JsonResponse({'success': False, 'message': 'Conversation not found.'}, status=404)
    except ValueError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=400)
    return JsonResponse({
        'success': True,
        'result': result,
        'assistant_message': result.get('message', ''),
    })

def signup(request):
    if request.user.is_authenticated:
        return redirect('user_dashboard')

    registration_mode = get_registration_mode()
    if request.method == 'POST':
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_active = registration_mode == 'open'
            user.save()
            user.groups.add(Group.objects.get(name='USER'))
            profile, profile_created = UserProfile.objects.get_or_create(
                user=user,
                defaults={'full_name': user.get_full_name()},
            )
            profile.registration_status = 'active' if registration_mode == 'open' else 'pending'
            profile.registered_at = profile.registered_at or timezone.now()
            profile.registration_submitted_at = timezone.now() if registration_mode == 'admin_approval' else profile.registration_submitted_at
            profile.registration_approved_at = timezone.now() if registration_mode == 'open' else profile.registration_approved_at
            profile.rejection_reason = ''
            profile.save(update_fields=['registration_status', 'registered_at', 'registration_submitted_at', 'registration_approved_at', 'rejection_reason', 'updated_at'])

            from .services.audit import record_audit_event

            record_audit_event(
                'REGISTRATION_SUBMITTED' if registration_mode == 'admin_approval' else 'user_created',
                user.pk,
                {'registration_mode': registration_mode, 'registration': 'web'},
                actor=user,
            )
            if profile_created:
                record_audit_event(
                    'profile_created',
                    profile.pk,
                    {'user_id': user.pk},
                    actor=user,
                )

            if registration_mode == 'open':
                login(request, user, backend='opportunity_agent.authentication.EmailOrUsernameModelBackend')
                messages.success(request, 'Account created. Complete your profile to improve matches.')
                return redirect('user_dashboard')

            messages.success(
                request,
                'Registration submitted successfully. Your account is waiting for administrator approval.',
            )
            return render(request, 'registration/signup.html', {'form': SignUpForm(), 'registration_pending': True, 'registration_mode': registration_mode})
    else:
        form = SignUpForm()
    return render(request, 'registration/signup.html', {'form': form, 'registration_mode': registration_mode})

def home(request):
    opportunities = _public_opportunities(
        Opportunity.objects.filter(status='active').order_by('-created_at')
    )
    return render(request, 'opportunity_agent/home.html', {
        'title': 'Global Opportunity Agent',
        'featured': opportunities[:8],
        'opportunity_count': len(opportunities),
    })


def private_access(request, token=None):
    if token is None:
        token = request.GET.get('token') or request.POST.get('token') or ''
    if request.method == 'POST' and not token:
        token = request.POST.get('token', '').strip()

    next_url = _safe_redirect_target(request, request.GET.get('next') or request.POST.get('next') or '')
    valid_token = validate_private_access_token(token)
    if valid_token and valid_token.consume():
        request.session['private_access_granted_until'] = (timezone.now() + timedelta(hours=12)).isoformat()
        request.session['private_access_granted'] = True
        request.session['private_access_registration_allowed'] = valid_token.allowed_registration
        return redirect(next_url or 'home')

    if token:
        return render(request, 'opportunity_agent/private_access.html', {
            'error': 'This private access link is invalid or no longer active.',
            'website_visibility': get_website_visibility(),
            'next': next_url,
        })

    return render(request, 'opportunity_agent/private_access.html', {
        'website_visibility': get_website_visibility(),
        'next': next_url,
    })


def _can_manage_website_settings(user):
    return user.is_authenticated and (
        user.is_superuser
        or user.has_perm('opportunity_agent.manage_security_settings')
    )


def website_visibility_toggle(request):
    if not _can_manage_website_settings(request.user):
        return HttpResponseForbidden('Website settings access required.')
    if request.method != 'POST':
        return redirect('admin_dashboard')
    visibility = request.POST.get('visibility', '').strip().lower()
    if visibility not in {'public', 'private'}:
        return HttpResponseForbidden('Unsupported website visibility option.')
    set_website_visibility(visibility, actor=request.user)
    return redirect('admin_dashboard')


def registration_mode_toggle(request):
    if not _can_manage_website_settings(request.user):
        return HttpResponseForbidden('Website settings access required.')
    if request.method != 'POST':
        return redirect('admin_dashboard')
    mode = (request.POST.get('registration_mode') or '').strip().lower()
    if mode not in {'open', 'admin_approval'}:
        return HttpResponseForbidden('Unsupported registration mode.')
    set_registration_mode(mode, actor=request.user)
    return redirect('admin_dashboard')


def robots_txt(request):
    if get_website_visibility() == 'private':
        content = 'User-agent: *\nDisallow: /\n'
    else:
        sitemap_url = request.build_absolute_uri(reverse('sitemap'))
        content = f'User-agent: *\nAllow: /\nSitemap: {sitemap_url}\n'
    return HttpResponse(content, content_type='text/plain; charset=utf-8')


def sitemap(request):
    if get_website_visibility() == 'private':
        return HttpResponseNotFound('Not found.')

    namespace = 'http://www.sitemaps.org/schemas/sitemap/0.9'
    urlset = Element('urlset', xmlns=namespace)
    for route_name in ('home', 'opportunity_list'):
        url_element = SubElement(urlset, f'{{{namespace}}}url')
        SubElement(url_element, f'{{{namespace}}}loc').text = request.build_absolute_uri(
            reverse(route_name)
        )
    for opportunity in _public_opportunities(
        Opportunity.objects.filter(status='active').order_by('pk')
    )[:50000]:
        url_element = SubElement(urlset, f'{{{namespace}}}url')
        SubElement(url_element, f'{{{namespace}}}loc').text = request.build_absolute_uri(
            reverse('opportunity_detail', args=[opportunity.pk])
        )
    return HttpResponse(
        tostring(urlset, encoding='utf-8', xml_declaration=True),
        content_type='application/xml; charset=utf-8',
    )


@login_required
def user_dashboard(request):
    _require_permission(request.user, 'opportunity_agent.view_userprofile')
    _require_permission(request.user, 'opportunity_agent.view_application')
    _require_permission(request.user, 'opportunity_agent.view_match')
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    apps = Application.objects.filter(user=request.user)
    active_opportunities = _public_opportunities(
        Opportunity.objects.filter(status='active').order_by('-created_at')
    )
    user_matches = refresh_user_matches(profile, active_opportunities)
    matches = sorted(
        (
            match for match in user_matches.values()
            if match.eligible
            and match.score >= profile.minimum_ai_match_score
        ),
        key=lambda match: (match.score, match.updated_at),
        reverse=True,
    )[:8]
    saved_opportunities = [
        match for match in user_matches.values()
        if match.is_saved and match.opportunity.status == 'active'
    ]
    saved_opportunities.sort(
        key=lambda match: (match.updated_at, match.pk),
        reverse=True,
    )
    today = timezone.localdate()
    daily_count = apps.filter(
        Q(attempts_log__created_at__date=today)
        | Q(
            status='submitted',
            submission_time__date=today,
        )
        | Q(
            status='submitted',
            submission_time__isnull=True,
            updated_at__date=today,
        )
    ).distinct().count()
    upcoming_deadlines = _public_opportunities(Opportunity.objects.filter(
        status='active',
        deadline__gte=timezone.now(),
        deadline__lte=timezone.now() + timedelta(days=30),
    ).order_by('deadline'))[:8]
    applications_by_status = {
        'applied_opportunities': apps.filter(status='submitted')
        .select_related('opportunity').order_by('-submission_time', '-updated_at')[:8],
        'rejected_applications': apps.filter(status='rejected')
        .select_related('opportunity').order_by('-updated_at')[:8],
        'failed_applications': apps.filter(status='failed')
        .select_related('opportunity').order_by('-updated_at')[:8],
        'review_applications': apps.filter(status='needs_review')
        .select_related('opportunity').order_by('-updated_at')[:8],
    }
    return render(request, 'opportunity_agent/dashboard.html', {
        'profile': profile, 'applications': apps.select_related('opportunity').order_by('-created_at')[:10],
        'application_count': apps.count(), 'in_progress_count': apps.filter(status__in=['queued','matching','prepared','pending']).count(),
        'submitted_count': apps.filter(status='submitted').count(), 'rejected_count': apps.filter(status='rejected').count(),
        'failed_count': apps.filter(status='failed').count(),
        'needs_review_count': apps.filter(status='needs_review').count(),
        'active_opportunity_count': len(active_opportunities),
        'daily_count': daily_count, 'daily_limit': profile.daily_application_limit,
        'matches': matches, 'saved_opportunities': saved_opportunities[:8],
        'fallback_opportunities': active_opportunities[:8] if not matches else [],
        'upcoming_deadlines': upcoming_deadlines,
        **applications_by_status,
        'minimum_match_score': profile.minimum_ai_match_score,
    })

def opportunity_list(request):
    qs = Opportunity.objects.filter(status='active').order_by('-created_at')
    q = request.GET.get('q', '').strip()
    country = request.GET.get('country', '').strip()
    kind = request.GET.get('type', '').strip()
    mode = request.GET.get('mode', '').strip()
    remote = request.GET.get('remote')
    qualification = request.GET.get('qualification', '').strip()
    deadline = request.GET.get('deadline', '').strip()
    minimum_score = request.GET.get('minimum_score', '').strip()
    if q: qs=qs.filter(Q(title__icontains=q) | Q(organization__icontains=q) | Q(description__icontains=q))
    if country: qs=qs.filter(country__icontains=country)
    if kind: qs=qs.filter(opportunity_type=kind)
    if mode in dict(Opportunity.WORK_MODE_CHOICES):
        if mode == 'remote':
            qs=qs.filter(Q(work_mode='remote') | Q(remote_worldwide=True))
        else:
            qs=qs.filter(work_mode=mode)
    if remote == '1': qs=qs.filter(remote_worldwide=True)
    if qualification:
        qs = qs.filter(
            Q(education_requirements__icontains=qualification)
            | Q(qualifications__icontains=qualification)
            | Q(requirements__icontains=qualification)
        )
    if deadline == 'upcoming':
        qs = qs.filter(deadline__gte=timezone.now())
    elif deadline == 'none':
        qs = qs.filter(deadline__isnull=True)
    opportunities = _public_opportunities(qs)[:100]
    profile = None
    if request.user.is_authenticated:
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        matches = refresh_user_matches(profile, opportunities)
        for opportunity in opportunities:
            opportunity.user_match = matches.get(opportunity.pk)
        if minimum_score.isdigit():
            score_floor = min(100, int(minimum_score))
            opportunities = [
                opportunity for opportunity in opportunities
                if opportunity.user_match
                and opportunity.user_match.score >= score_floor
            ]
        opportunities.sort(
            key=lambda opportunity: (
                opportunity.user_match.score if opportunity.user_match else 0,
                opportunity.created_at,
            ),
            reverse=True,
        )
    return render(request, 'opportunity_agent/opportunity_list.html', {
        'opportunities': opportunities,
        'types': Opportunity.OPPORTUNITY_TYPES,
        'work_modes': Opportunity.WORK_MODE_CHOICES,
        'q': q,
        'country': country,
        'kind': kind,
        'mode': mode,
        'remote': remote,
        'qualification': qualification,
        'deadline_filter': deadline,
        'minimum_score': minimum_score,
        'profile': profile,
    })

def opportunity_detail(request, pk):
    opportunity=get_object_or_404(Opportunity,pk=pk,status='active')
    if is_listing_opportunity(opportunity):
        raise Http404
    match=Match.objects.filter(user=request.user,opportunity=opportunity).first() if request.user.is_authenticated else None
    match_result = None
    if request.user.is_authenticated:
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        match_result = compute_match_score(
            profile_match_data(profile),
            opportunity_match_data(opportunity),
        )
        match, _ = save_match(request.user, opportunity, match_result)
    return render(request,'opportunity_agent/opportunity_detail.html',{'opportunity':opportunity,'match':match,'match_result':match_result})


@login_required
def toggle_saved_opportunity(request, pk):
    if request.method != 'POST':
        return HttpResponse('POST required.', status=405)
    _require_permission(request.user, 'opportunity_agent.add_match')
    action = request.POST.get('action')
    if action not in {'save', 'unsave'}:
        return HttpResponse('Invalid save action.', status=400)
    opportunity = get_object_or_404(Opportunity, pk=pk, status='active')
    if is_listing_opportunity(opportunity):
        raise Http404
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    result = compute_match_score(
        profile_match_data(profile),
        opportunity_match_data(opportunity),
    )
    match, _ = save_match(request.user, opportunity, result)
    match.is_saved = action == 'save'
    match.save(update_fields=['is_saved', 'updated_at'])
    messages.success(
        request,
        'Opportunity saved to your list.'
        if match.is_saved else 'Opportunity removed from your saved list.',
    )
    return redirect('opportunity_detail', pk=pk)

@login_required
def apply_opportunity(request, pk):
    if request.method != 'POST': return redirect('opportunity_detail',pk=pk)
    _require_permission(request.user, 'opportunity_agent.add_application')
    _require_permission(request.user, 'opportunity_agent.add_match')
    opportunity=get_object_or_404(Opportunity,pk=pk,status='active')
    if is_listing_opportunity(opportunity):
        raise Http404
    if opportunity.is_expired():
        messages.error(request, 'This opportunity has expired.')
        return redirect('opportunity_detail',pk=pk)
    profile,_=UserProfile.objects.get_or_create(user=request.user)
    result=compute_match_score(profile_to_dict(profile), opportunity_to_dict(opportunity))
    override_requested = request.POST.get('match_override') == '1'
    if override_requested and not result['manual_override_allowed']:
        messages.error(request, 'The score threshold can only be overridden when your location and work-mode preferences match.')
        return redirect('opportunity_detail',pk=pk)
    manual_override = override_requested and result['manual_override_allowed']
    match, match_created=Match.objects.update_or_create(
        user=request.user,
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
    if match_created:
        from .services.audit import record_audit_event

        record_audit_event(
            'opportunity_matched',
            match.pk,
            {
                'user_id': request.user.pk,
                'opportunity_id': opportunity.pk,
                'score': result['score'],
                'eligible': result['eligible'],
            },
            actor=request.user,
        )
    app,created=Application.objects.get_or_create(
        user=request.user,
        opportunity=opportunity,
        defaults={
            'match_score': result['score'],
            'status': 'queued' if result['eligible'] or manual_override else 'needs_review',
            'match_override': manual_override,
        },
    )
    can_override_needs_review = (
        manual_override
        and app.status == 'needs_review'
        and 'below your threshold' in app.error_message.casefold()
    )
    if not created and app.status not in ['cancelled','failed','rejected'] and not can_override_needs_review:
        messages.info(request,'You already have an application for this opportunity.')
        return redirect('opportunity_detail',pk=pk)
    if not created:
        app.match_score = result['score']
        app.status = 'queued' if result['eligible'] or manual_override else 'needs_review'
        app.match_override = manual_override
    if manual_override:
        app.audit_history = list(app.audit_history or []) + [{
            'action': 'match_threshold_override',
            'actor_id': request.user.pk,
            'score': result['score'],
            'threshold': result['minimum_score'],
            'timestamp': timezone.now().isoformat(),
        }]
    app.error_message = '' if result['eligible'] or manual_override else (
        f"Match score {result['score']} is below your threshold of {result['minimum_score']}."
        if not result['threshold_met']
        else 'Location or work-mode preferences do not match.'
    )
    app.save()
    if result['eligible'] or manual_override:
        messages.success(
            request,
            'Match threshold override recorded. Application added to the queue.'
            if manual_override
            else 'Application added to the queue. The automation engine will prepare it safely.',
        )
    else:
        messages.warning(request,'This opportunity was sent to Needs Review because it does not meet your match criteria.')
    if created or manual_override:
        AuditLog.objects.create(
            actor=request.user,
            action='match_threshold_override' if manual_override else 'application_queued',
            target=str(app.pk),
            details={
                'opportunity_id': opportunity.pk,
                'match_score': result['score'],
                'minimum_score': result['minimum_score'],
                'manual_override': manual_override,
            },
        )
    return redirect('opportunity_detail',pk=pk)

@login_required
def credentials(request):
    _require_permission(request.user, 'opportunity_agent.view_sitecredential')
    _require_permission(request.user, 'opportunity_agent.view_emailmailbox')
    credentials_qs = SiteCredential.objects.filter(user=request.user).select_related('email_mailbox').order_by('-updated_at')
    mailboxes = EmailMailbox.objects.filter(user=request.user).order_by('-updated_at')
    if request.method == 'POST':
        action = request.POST.get('action','site')
        if action == 'mailbox':
            _require_permission(request.user, 'opportunity_agent.add_emailmailbox')
            form = EmailMailboxForm(request.POST)
            if form.is_valid():
                obj = form.save(commit=False); obj.user = request.user; obj.save()
                messages.success(request, 'Email mailbox saved securely. The app password is encrypted.')
                return redirect('credentials')
            site_form = SiteCredentialForm(user=request.user)
        else:
            _require_permission(request.user, 'opportunity_agent.add_sitecredential')
            site_form = SiteCredentialForm(request.POST, user=request.user)
            form = EmailMailboxForm()
            if site_form.is_valid():
                obj = site_form.save(commit=False); obj.user = request.user; obj.save()
                messages.success(request, 'Website account saved securely. The password is encrypted.')
                return redirect('credentials')
    else:
        site_form = SiteCredentialForm(user=request.user); form = EmailMailboxForm()
    return render(request, 'opportunity_agent/credentials.html', {'form': site_form, 'mailbox_form': form, 'credentials': credentials_qs, 'mailboxes': mailboxes})

@login_required
def profile_edit(request):
    _require_permission(
        request.user,
        'opportunity_agent.change_userprofile' if request.method == 'POST'
        else 'opportunity_agent.view_userprofile',
    )
    profile,_=UserProfile.objects.get_or_create(user=request.user)
    if request.method=='POST':
        form=UserProfileForm(request.POST,request.FILES,instance=profile)
        if form.is_valid():
            changed_fields = list(form.changed_data)
            profile = form.save()
            from .services.audit import record_audit_event

            record_audit_event(
                'profile_updated',
                profile.pk,
                {'changed_fields': changed_fields},
                actor=request.user,
            )
            messages.success(request,'Profile updated successfully.')
            return redirect('profile_edit')
    else: form=UserProfileForm(instance=profile)
    return render(request,'opportunity_agent/profile.html',{'form':form,'profile':profile})

@login_required
def profile_cv_download(request, user_id):
    profile = get_object_or_404(UserProfile.objects.select_related('user'), user_id=user_id)
    if request.user.pk == profile.user_id:
        _require_permission(request.user, 'opportunity_agent.view_userprofile')
    else:
        profile_permission = request.user.has_perm('opportunity_agent.view_userprofile')
        review_permission = (
            request.user.is_staff
            and request.user.has_perm('opportunity_agent.review_application')
            and Application.objects.filter(
                user_id=profile.user_id,
                status__in=['pending', 'needs_review', 'failed', 'rejected'],
            ).exists()
        )
        if not request.user.is_staff or not (profile_permission or review_permission):
            raise Http404
    if not profile.cv:
        raise Http404

    response = FileResponse(
        profile.cv.open('rb'),
        as_attachment=True,
        filename=Path(profile.cv.name).name,
        content_type='application/octet-stream',
    )
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response

def admin_dashboard(request):
    if not _admin(request): return HttpResponseForbidden('Admin access required.')
    return _analytics_context(request,'opportunity_agent/admin_dashboard.html')

def analytics_dashboard(request):
    if not _admin(request): return HttpResponseForbidden('Admin access required.')
    return _analytics_context(request,'opportunity_agent/analytics.html')

def _analytics_context(request, template):
    from django.contrib.auth import get_user_model
    User=get_user_model(); now=timezone.now(); last14=[]
    for i in range(13,-1,-1):
        day=(now-timedelta(days=i)).date(); last14.append({'label':day.strftime('%b %d'),'users':User.objects.filter(date_joined__date=day).count(),'opportunities':Opportunity.objects.filter(created_at__date=day).count(),'applications':Application.objects.filter(created_at__date=day).count()})
    apps=Application.objects.all(); total=apps.count() or 1
    status_data=[]
    for key,label in Application.STATUS_CHOICES:
        n=apps.filter(status=key).count(); status_data.append({'key':key,'label':label,'count':n,'pct':round(n/total*100)})
    source_rows=list(Source.objects.values('status').annotate(n=Count('id')).order_by('-n'))
    country_rows=list(Opportunity.objects.filter(status='active').exclude(country='').values('country').annotate(n=Count('id')).order_by('-n')[:8])
    type_rows=[]
    for key,label in Opportunity.OPPORTUNITY_TYPES:
        n=Opportunity.objects.filter(status='active',opportunity_type=key).count()
        if n:type_rows.append({'label':label,'count':n})
    registration_counts = {
        'pending': UserProfile.objects.filter(registration_status='pending').count(),
        'active': UserProfile.objects.filter(registration_status='active').count(),
        'rejected': UserProfile.objects.filter(registration_status='rejected').count(),
        'suspended': UserProfile.objects.filter(registration_status='suspended').count(),
    }
    registration_mode = get_registration_mode()
    return render(request,template,{
        'user_count':User.objects.count(),'active_user_count':User.objects.filter(is_active=True).count(),'source_count':Source.objects.count(),'active_source_count':Source.objects.filter(enabled=True,status='active').count(),
        'opportunity_count':Opportunity.objects.count(),'active_opportunity_count':Opportunity.objects.filter(status='active').count(),'expired_count':Opportunity.objects.filter(deadline__lt=now).count(),
        'application_count':apps.count(),'submitted_count':apps.filter(status='submitted').count(),'rejected_count':apps.filter(status='rejected').count(),'failed_count':apps.filter(status='failed').count(),'review_count':apps.filter(status='needs_review').count(),
        'avg_match':round(Match.objects.aggregate(v=Avg('score')).get('v') or 0,1),'profiles_complete':round(sum(1 for p in UserProfile.objects.all() if p.profile_completeness>=80)/max(UserProfile.objects.count(),1)*100),
        'deadline_week':Opportunity.objects.filter(status='active',deadline__isnull=False,deadline__gte=now,deadline__lte=now+timedelta(days=7)).count(),
        'status_data':status_data,'country_rows':country_rows,'type_rows':type_rows,'source_rows':source_rows,'series':last14,
        'registration_counts': registration_counts,
        'website_visibility': get_website_visibility(),
        'registration_mode': registration_mode,
        'registration_mode_display': REGISTRATION_MODES[registration_mode],
        'can_manage_website_settings': _can_manage_website_settings(request.user),
        'recent_applications':apps.select_related('user','opportunity').order_by('-created_at')[:10],'recent_runs':AutomationRun.objects.order_by('-started_at')[:8],
    })

def profile_to_dict(profile):
    return profile_match_data(profile)

def opportunity_to_dict(o):
    return opportunity_match_data(o)
