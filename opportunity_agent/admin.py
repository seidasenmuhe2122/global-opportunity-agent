from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import Group
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import timezone
import csv
import io

from .models import Application, ApplicationAttempt, ApplicationArtifact, ApplicationFormTemplate, AuditLog, AutomationRun, Match, Opportunity, PrivateAccessToken, ProviderAdapter, SiteCredential, Source, SystemSetting, TelegramDestination, TelegramSource, UserProfile, EmailMailbox
from .forms import CVUploadValidationMixin, PrivateProfileFileInput, SourceCSVImportForm
from .services.audit import record_audit_event
from .tasks import MAX_TASK_BATCH_SIZE, discover_sources_task, process_application_queue_task, retry_applications_task, scan_sources_task, scan_telegram_sources_task

admin.site.site_header='Opportunity Hub • Command Center'
admin.site.site_title='Opportunity Hub Admin'
admin.site.index_title='Automation & Intelligence Center'

admin.site.unregister(get_user_model())


@admin.action(description='Approve selected registrations')
def approve_user_registrations(modeladmin, request, queryset):
    if not request.user.has_perm('auth.change_user'):
        raise PermissionDenied
    for user in queryset:
        if user.pk == request.user.pk:
            modeladmin.message_user(request, 'You cannot approve your own registration.', messages.ERROR)
            continue
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.set_registration_status('active', actor=request.user, reason='Approved by admin action')
        record_audit_event(
            'REGISTRATION_APPROVED',
            user.pk,
            {'admin_id': request.user.pk, 'status': 'active'},
            actor=request.user,
        )
    modeladmin.message_user(request, 'Selected registrations were approved.', messages.SUCCESS)


@admin.action(description='Reject selected registrations')
def reject_user_registrations(modeladmin, request, queryset):
    if not request.user.has_perm('auth.change_user'):
        raise PermissionDenied
    for user in queryset:
        if user.pk == request.user.pk:
            modeladmin.message_user(request, 'You cannot reject your own registration.', messages.ERROR)
            continue
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.set_registration_status('rejected', actor=request.user, reason='Rejected by admin action')
        record_audit_event(
            'REGISTRATION_REJECTED',
            user.pk,
            {'admin_id': request.user.pk, 'status': 'rejected'},
            actor=request.user,
        )
    modeladmin.message_user(request, 'Selected registrations were rejected.', messages.SUCCESS)


@admin.action(description='Suspend selected registrations')
def suspend_user_registrations(modeladmin, request, queryset):
    if not request.user.has_perm('auth.change_user'):
        raise PermissionDenied
    for user in queryset:
        if user.pk == request.user.pk:
            modeladmin.message_user(request, 'You cannot suspend your own registration.', messages.ERROR)
            continue
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.set_registration_status('suspended', actor=request.user, reason='Suspended by admin action')
        record_audit_event(
            'ACCOUNT_SUSPENDED',
            user.pk,
            {'admin_id': request.user.pk, 'status': 'suspended'},
            actor=request.user,
        )
    modeladmin.message_user(request, 'Selected registrations were suspended.', messages.SUCCESS)


@admin.action(description='Activate selected registrations')
def activate_user_registrations(modeladmin, request, queryset):
    if not request.user.has_perm('auth.change_user'):
        raise PermissionDenied
    for user in queryset:
        if user.pk == request.user.pk:
            modeladmin.message_user(request, 'You cannot activate your own registration.', messages.ERROR)
            continue
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.set_registration_status('active', actor=request.user, reason='Activated by admin action')
        record_audit_event(
            'ACCOUNT_ACTIVATED',
            user.pk,
            {'admin_id': request.user.pk, 'status': 'active'},
            actor=request.user,
        )
    modeladmin.message_user(request, 'Selected registrations were activated.', messages.SUCCESS)


@admin.register(get_user_model())
class OpportunityUserAdmin(UserAdmin):
    filter_horizontal = ('groups', 'user_permissions')
    actions = [
        approve_user_registrations,
        reject_user_registrations,
        suspend_user_registrations,
        activate_user_registrations,
    ]

    def _can_manage_privileged_accounts(self, request):
        return request.user.is_superuser or request.user.has_perm('auth.change_permission')

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if not self._can_manage_privileged_accounts(request):
            queryset = queryset.filter(is_staff=False, is_superuser=False)
        return queryset

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if self._can_manage_privileged_accounts(request):
            return fieldsets
        restricted_fields = {'is_staff', 'is_superuser', 'groups', 'user_permissions'}
        return tuple(
            (name, {
                **options,
                'fields': tuple(
                    field for field in options.get('fields', ())
                    if field not in restricted_fields
                ),
            })
            for name, options in fieldsets
        )

    def formfield_for_manytomany(self, db_field, request=None, **kwargs):
        if db_field.name == 'user_permissions':
            queryset = kwargs.get('queryset', db_field.remote_field.model.objects)
            kwargs['queryset'] = queryset.select_related('content_type')
        return super().formfield_for_manytomany(db_field, request=request, **kwargs)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change and not obj.is_staff and not obj.is_superuser:
            user_group, _ = Group.objects.get_or_create(name='USER')
            obj.groups.add(user_group)
        record_audit_event(
            'user_updated' if change else 'user_created',
            obj.pk,
            {'changed_fields': list(form.changed_data)},
            actor=request.user,
        )

    def has_delete_permission(self, request, obj=None):
        if obj is not None and (
            obj.is_staff or obj.is_superuser
        ) and not self._can_manage_privileged_accounts(request):
            return False
        return super().has_delete_permission(request, obj)


class ReadOnlyDates(admin.ModelAdmin):
    readonly_fields=('created_at','updated_at')


class UserProfileAdminForm(CVUploadValidationMixin, forms.ModelForm):
    cv = forms.FileField(
        required=False,
        widget=PrivateProfileFileInput(attrs={'accept': '.pdf,.docx'}),
        help_text='Upload a PDF or DOCX CV, up to 10 MB. Access remains private.',
    )

    class Meta:
        model = UserProfile
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.user_id:
            self.fields['cv'].widget.download_url = reverse(
                'profile_cv_download',
                kwargs={'user_id': self.instance.user_id},
            )


@admin.action(description='Scan selected sources now')
def scan_now(modeladmin, request, queryset):
    if not request.user.has_perm('opportunity_agent.scan_sources'):
        raise PermissionDenied
    source_ids = list(
        queryset.filter(enabled=True).exclude(status='disabled').order_by('pk')
        .values_list('pk', flat=True)[:MAX_TASK_BATCH_SIZE + 1]
    )
    if not source_ids:
        modeladmin.message_user(request, 'No enabled sources were selected.', messages.INFO)
        return
    truncated = len(source_ids) > MAX_TASK_BATCH_SIZE
    source_ids = source_ids[:MAX_TASK_BATCH_SIZE]
    scan_sources_task.delay(limit=len(source_ids), source_ids=source_ids)
    message = f'{len(source_ids)} source scan(s) queued.'
    if truncated:
        message += f' Only the first {MAX_TASK_BATCH_SIZE} enabled sources were queued; filter and repeat for the rest.'
    modeladmin.message_user(request, message, messages.WARNING if truncated else messages.SUCCESS)


@admin.action(description='Scan selected Telegram channels now')
def scan_telegram_now(modeladmin, request, queryset):
    if not request.user.has_perm('opportunity_agent.scan_telegram_sources'):
        raise PermissionDenied
    source_ids = list(
        queryset.filter(enabled=True)
        .exclude(status='disabled')
        .order_by('pk')
        .values_list('pk', flat=True)[:MAX_TASK_BATCH_SIZE + 1]
    )
    if not source_ids:
        modeladmin.message_user(
            request,
            'No enabled Telegram opportunity sources were selected.',
            messages.INFO,
        )
        return
    truncated = len(source_ids) > MAX_TASK_BATCH_SIZE
    source_ids = source_ids[:MAX_TASK_BATCH_SIZE]
    scan_telegram_sources_task.delay(
        limit=len(source_ids),
        telegram_source_ids=source_ids,
    )
    message = f'{len(source_ids)} Telegram source scan(s) queued.'
    if truncated:
        message += (
            f' Only the first {MAX_TASK_BATCH_SIZE} enabled sources were queued; '
            'filter and repeat for the rest.'
        )
    modeladmin.message_user(
        request,
        message,
        messages.WARNING if truncated else messages.SUCCESS,
    )


@admin.action(description='Discover public sources now')
def discover_sources(modeladmin, request, queryset):
    if not request.user.has_perm('opportunity_agent.add_source'):
        raise PermissionDenied
    task = discover_sources_task.delay()
    modeladmin.message_user(
        request,
        f'Public source discovery queued (task {task.id}).',
        messages.SUCCESS,
    )


@admin.action(description='Enable selected sources')
def enable_sources(modeladmin, request, queryset):
    if not request.user.has_perm('opportunity_agent.change_source'):
        raise PermissionDenied
    old_states = list(queryset.values_list('pk', 'enabled', 'status'))
    queryset.filter(status='disabled').update(status='pending')
    count = queryset.update(enabled=True)
    for source_id, was_enabled, previous_status in old_states:
        if not was_enabled or previous_status == 'disabled':
            record_audit_event(
                'source_enabled',
                source_id,
                {'source_model': modeladmin.model._meta.label},
                actor=request.user,
            )
    modeladmin.message_user(request, f'{count} source(s) enabled.', messages.SUCCESS)


@admin.action(description='Disable selected sources')
def disable_sources(modeladmin, request, queryset):
    if not request.user.has_perm('opportunity_agent.change_source'):
        raise PermissionDenied
    sources = list(queryset.only('pk', 'enabled', 'status'))
    count = queryset.update(enabled=False, status='disabled')
    for source in sources:
        if source.enabled or source.status != 'disabled':
            record_audit_event(
                'source_disabled',
                source.pk,
                {'source_model': modeladmin.model._meta.label},
                actor=request.user,
            )
    modeladmin.message_user(request, f'{count} source(s) disabled.', messages.SUCCESS)


@admin.action(description='Queue selected applications for processing')
def queue_apps(modeladmin, request, queryset):
    if not request.user.has_perm('opportunity_agent.change_application'):
        raise PermissionDenied
    applications = list(queryset.filter(
        status__in=['queued', 'prepared', 'pending', 'cancelled'],
    ))
    for application in applications:
        application.status = 'queued'
        application.error_message = ''
        application.save(update_fields=['status', 'error_message', 'updated_at'])
    count = len(applications)
    if count:
        process_application_queue_task.delay(min(count, 20))
    modeladmin.message_user(request, f'{count} application(s) queued.', messages.SUCCESS)


@admin.action(description='Retry selected failed applications')
def retry_failed_apps(modeladmin, request, queryset):
    if not request.user.has_perm('opportunity_agent.retry_application'):
        raise PermissionDenied
    application_ids = list(
        queryset.filter(status='failed').values_list('pk', flat=True)[:21]
    )
    if not application_ids:
        modeladmin.message_user(request, 'No failed applications were selected.', messages.INFO)
        return
    truncated = len(application_ids) > 20
    application_ids = application_ids[:20]
    retry_applications_task.delay(
        application_ids=application_ids,
        status='failed',
        actor_id=request.user.pk,
    )
    count = len(application_ids)
    modeladmin.message_user(
        request,
        f'Retry requested for {count} failed application(s); a worker will validate and queue them.' +
        (' Only the first 20 were queued; filter and repeat for the rest.' if truncated else ''),
        messages.WARNING if truncated else messages.SUCCESS,
    )


@admin.action(description='Approve selected applications for processing')
def approve_applications(modeladmin, request, queryset):
    if not request.user.has_perm('opportunity_agent.review_application'):
        raise PermissionDenied
    applications = list(queryset.filter(status__in=['pending', 'needs_review']))
    for application in applications:
        previous_status = application.status
        application.status = 'queued'
        application.error_message = ''
        application.save(update_fields=['status', 'error_message', 'updated_at'])
        AuditLog.objects.create(
            actor=request.user,
            action='application_approved',
            target=str(application.pk),
            details={'previous_status': previous_status},
        )
    if applications:
        process_application_queue_task.delay(min(len(applications), 20))
    modeladmin.message_user(
        request,
        f'{len(applications)} application(s) approved and queued.',
        messages.SUCCESS,
    )


@admin.action(description='Reject selected applications')
def reject_applications(modeladmin, request, queryset):
    if not request.user.has_perm('opportunity_agent.review_application'):
        raise PermissionDenied
    applications = list(queryset.filter(
        status__in=['pending', 'needs_review', 'failed', 'rejected'],
    ))
    for application in applications:
        previous_status = application.status
        application.status = 'rejected'
        application.rejection_reason = 'Rejected during review.'
        application.save(update_fields=['status', 'rejection_reason', 'updated_at'])
        AuditLog.objects.create(
            actor=request.user,
            action='application_rejected',
            target=str(application.pk),
            details={'previous_status': previous_status},
        )
    modeladmin.message_user(
        request,
        f'{len(applications)} application(s) rejected.',
        messages.SUCCESS,
    )


@admin.register(UserProfile)
class UserProfileAdmin(ReadOnlyDates):
    form=UserProfileAdminForm
    readonly_fields=(
        'created_at',
        'updated_at',
        'registration_status',
        'registration_submitted_at',
        'registration_approved_at',
        'registration_rejected_at',
        'suspended_at',
        'last_status_changed_by',
    )
    list_display=('full_name','user','current_country','registration_status','registration_submitted_at','profile_score','auto_apply','minimum_ai_match_score','daily_application_limit','updated_at')
    list_filter=('registration_status','auto_apply','worldwide_preference','visa_sponsorship_preference','current_country')
    search_fields=('full_name','user__username','user__email','current_country','degree')
    autocomplete_fields=('user',)
    fieldsets=(('Identity',{'fields':('user','full_name','phone','current_country')}),('Registration',{'fields':('registration_status','registration_submitted_at','registration_approved_at','registration_rejected_at','suspended_at','rejection_reason','last_status_changed_by')}),('Targeting',{'fields':('target_countries','worldwide_preference','preferred_opportunity_types','preferred_work_modes','visa_sponsorship_preference','salary_stipend_preference')}),('Skills & CV',{'fields':('skills','education','degree','work_experience','languages','certifications','cv','portfolio_url','linkedin_url','github_url','other_links')}),('Automation',{'fields':('minimum_ai_match_score','auto_apply','daily_application_limit','notification_preferences')}),('Timestamps',{'fields':('created_at','updated_at')}))
    def profile_score(self,obj): return f'{obj.profile_completeness}%'
    profile_score.short_description='Complete'

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        record_audit_event(
            'profile_updated' if change else 'profile_created',
            obj.pk,
            {'user_id': obj.user_id, 'changed_fields': list(form.changed_data)},
            actor=request.user,
        )

@admin.register(Source)
class SourceAdmin(ReadOnlyDates):
    list_display=('name','source_type','country','health','enabled','trust_score','scan_frequency','last_scan','last_successful_scan','error_count','auto_discovered')
    list_filter=('status','enabled','source_type','country','scan_frequency','auto_discovered')
    search_fields=('name','url','country','notes')
    list_editable=('enabled','trust_score')
    actions=[scan_now,discover_sources,enable_sources,disable_sources]
    date_hierarchy='created_at'
    list_per_page=100
    show_full_result_count=False
    change_list_template='admin/opportunity_agent/source/change_list.html'

    def save_model(self, request, obj, form, change):
        previous = (
            Source.objects.filter(pk=obj.pk).values('enabled', 'status').first()
            if change else None
        )
        super().save_model(request, obj, form, change)
        if not change:
            action = 'source_added'
        elif previous['enabled'] != obj.enabled and not obj.enabled:
            action = 'source_disabled'
        elif previous['enabled'] != obj.enabled and obj.enabled:
            action = 'source_enabled'
        else:
            action = 'source_updated'
        record_audit_event(
            action,
            obj.pk,
            {
                'name': obj.name,
                'url': obj.url,
                'previous_status': previous['status'] if change else '',
                'status': obj.status,
            },
            actor=request.user,
        )

    def get_urls(self):
        from django.urls import path

        return [
            path(
                'import-csv/',
                self.admin_site.admin_view(self.import_csv_view),
                name='opportunity_agent_source_import_csv',
            ),
        ] + super().get_urls()

    def import_csv_view(self, request):
        if not request.user.has_perm('opportunity_agent.add_source'):
            raise PermissionDenied

        form = SourceCSVImportForm(request.POST or None, request.FILES or None)
        errors = []
        if request.method == 'POST' and form.is_valid():
            uploaded = form.cleaned_data['file']
            try:
                text = uploaded.read().decode('utf-8-sig')
            except UnicodeDecodeError:
                form.add_error('file', 'CSV must use UTF-8 encoding.')
            else:
                reader = csv.DictReader(io.StringIO(text))
                required = {'name', 'url'}
                headers = {
                    (header or '').strip().lower()
                    for header in (reader.fieldnames or [])
                }
                if not required.issubset(headers):
                    form.add_error('file', 'CSV must contain name and url columns.')
                else:
                    rows = list(enumerate(reader, start=2))
                    if len(rows) > 5000:
                        errors.append('CSV exceeds the 5,000-row limit.')
                    submitted_urls = {
                        (row.get('url') or '').strip()
                        for _, row in rows
                        if row and row.get('url')
                    }
                    existing_urls = set()
                    submitted_url_list = list(submitted_urls)
                    for offset in range(0, len(submitted_url_list), 500):
                        existing_urls.update(
                            Source.objects.filter(
                                url__in=submitted_url_list[offset:offset + 500],
                            ).values_list('url', flat=True)
                        )
                    pending = []
                    seen_urls = set()
                    skipped = 0
                    for row_number, raw_row in rows[:5000]:
                        if None in raw_row:
                            errors.append(f'Row {row_number}: too many columns.')
                            continue
                        row = {
                            (key or '').strip().lower(): (value or '').strip()
                            for key, value in raw_row.items()
                        }
                        url = row.get('url', '')
                        if not row.get('name') or not url:
                            errors.append(f'Row {row_number}: name and url are required.')
                            continue
                        if url in seen_urls:
                            skipped += 1
                            continue
                        seen_urls.add(url)
                        if url in existing_urls:
                            skipped += 1
                            continue

                        opportunity_types = [
                            value.strip()
                            for value in row.get('opportunity_types', '').split('|')
                            if value.strip()
                        ]
                        enabled_value = row.get('enabled', 'true').lower()
                        if enabled_value not in {'true', 'false', '1', '0', 'yes', 'no'}:
                            errors.append(f'Row {row_number}: enabled must be true or false.')
                            continue
                        try:
                            source = Source(
                                name=row['name'],
                                url=url,
                                source_type=row.get('source_type') or 'website',
                                country=row.get('country', ''),
                                opportunity_types=opportunity_types,
                                enabled=enabled_value in {'true', '1', 'yes'},
                                trust_score=float(row.get('trust_score') or 0),
                                scan_frequency=row.get('scan_frequency') or 'daily',
                                notes=row.get('notes', ''),
                                auto_discovered=False,
                                status='pending' if enabled_value in {'true', '1', 'yes'} else 'disabled',
                            )
                            allowed_types = {value for value, _ in Opportunity.OPPORTUNITY_TYPES}
                            unknown_types = set(opportunity_types) - allowed_types
                            if unknown_types:
                                raise ValueError(
                                    'Unknown opportunity_types: ' + ', '.join(sorted(unknown_types))
                                )
                            source.full_clean()
                        except (ValidationError, ValueError) as exc:
                            detail = exc.message_dict if isinstance(exc, ValidationError) and hasattr(exc, 'message_dict') else str(exc)
                            errors.append(f'Row {row_number}: {detail}')
                            continue
                        pending.append(source)

                    if not errors:
                        with transaction.atomic():
                            created_sources = Source.objects.bulk_create(pending, batch_size=500)
                            for source in created_sources:
                                record_audit_event(
                                    'source_added',
                                    source.pk,
                                    {
                                        'name': source.name,
                                        'url': source.url,
                                        'imported_from_csv': True,
                                    },
                                    actor=request.user,
                                )
                        self.message_user(
                            request,
                            f'Imported {len(pending)} source(s); skipped {skipped} existing or duplicate URL(s).',
                            messages.SUCCESS,
                        )
                        return HttpResponseRedirect(reverse('admin:opportunity_agent_source_changelist'))

        return render(
            request,
            'admin/opportunity_agent/source/import_csv.html',
            self.admin_site.each_context(request) | {
                'opts': self.model._meta,
                'title': 'Import sources from CSV',
                'form': form,
                'errors': errors,
            },
        )

    def health(self,obj): return obj.get_status_display()


@admin.register(TelegramSource)
class TelegramSourceAdmin(ReadOnlyDates):
    list_display=(
        'name','channel_url','country','health','enabled','trust_score',
        'scan_frequency','last_scan','last_successful_scan','error_count',
        'auto_discovered',
    )
    list_filter=('status','enabled','country','scan_frequency','auto_discovered')
    search_fields=('name','channel_url','country','notes')
    list_editable=('enabled','trust_score')
    actions=[scan_telegram_now,enable_sources,disable_sources]
    date_hierarchy='created_at'
    list_per_page=100

    def save_model(self, request, obj, form, change):
        previous_enabled = obj.enabled
        previous_status = obj.status
        super().save_model(request, obj, form, change)
        if not change:
            action = 'telegram_source_added'
        elif previous_enabled != obj.enabled and not obj.enabled:
            action = 'telegram_source_disabled'
        elif previous_enabled != obj.enabled and obj.enabled:
            action = 'telegram_source_enabled'
        else:
            action = 'telegram_source_updated'
        record_audit_event(
            action,
            obj.pk,
            {
                'name': obj.name,
                'channel_url': obj.channel_url,
                'previous_status': previous_status if change else '',
                'status': obj.status,
            },
            actor=request.user,
        )

    def save_model(self, request, obj, form, change):
        previous = (
            TelegramSource.objects.filter(pk=obj.pk).values('enabled', 'status').first()
            if change else None
        )
        super().save_model(request, obj, form, change)
        if not change:
            action = 'telegram_source_added'
        elif previous['enabled'] != obj.enabled and not obj.enabled:
            action = 'telegram_source_disabled'
        elif previous['enabled'] != obj.enabled and obj.enabled:
            action = 'telegram_source_enabled'
        else:
            action = 'telegram_source_updated'
        record_audit_event(
            action,
            obj.pk,
            {
                'name': obj.name,
                'channel_url': obj.channel_url,
                'previous_status': previous['status'] if change else '',
                'status': obj.status,
            },
            actor=request.user,
        )

    def health(self,obj):
        return obj.get_status_display()


@admin.register(Opportunity)
class OpportunityAdmin(ReadOnlyDates):
    list_display=('title','organization','opportunity_type','country','work_mode','remote_worldwide','status','deadline','deadline_category','created_at')
    list_filter=('status','opportunity_type','country','work_mode','remote_worldwide','visa_sponsorship')
    search_fields=('title','organization','country','description','requirements','application_url')
    autocomplete_fields=('source',)
    date_hierarchy='created_at'
    list_per_page=50

    @admin.display(description='Deadline status', ordering='deadline')
    def deadline_category(self, obj):
        return obj.deadline_status_label

    def save_model(self, request, obj, form, change):
        previous_status = (
            Opportunity.objects.filter(pk=obj.pk).values_list('status', flat=True).first()
            if change else None
        )
        super().save_model(request, obj, form, change)
        if not change:
            action = 'opportunity_added'
        elif previous_status != 'rejected' and obj.status == 'rejected':
            action = 'opportunity_rejected'
        else:
            action = 'opportunity_updated'
        record_audit_event(
            action,
            obj.pk,
            {
                'changed_fields': list(form.changed_data),
                'previous_status': previous_status,
                'status': obj.status,
            },
            actor=request.user,
        )

@admin.register(Match)
class MatchAdmin(ReadOnlyDates):
    list_display=('user','opportunity','score','eligible','updated_at')
    list_filter=('eligible','score')
    search_fields=('user__username','opportunity__title','opportunity__organization')
    autocomplete_fields=('user','opportunity')

@admin.register(Application)
class ApplicationAdmin(ReadOnlyDates):
    list_display=('opportunity','user','match_score','match_override','status','attempts','created_at','submission_time')
    list_filter=('status','created_at','submission_time')
    search_fields=('user__username','user__email','opportunity__title','opportunity__organization','error_message')
    autocomplete_fields=('user','opportunity')
    date_hierarchy='created_at'
    readonly_fields=('created_at','updated_at','submission_time','audit_history','match_override','workflow_state')
    actions=[queue_apps,retry_failed_apps,approve_applications,reject_applications]

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if (
            request.user.has_perm('opportunity_agent.review_application')
            and not request.user.has_perm('opportunity_agent.change_application')
        ):
            queryset = queryset.filter(status__in=['pending', 'needs_review', 'failed', 'rejected'])
        return queryset

    def has_change_permission(self, request, obj=None):
        if request.user.has_perm('opportunity_agent.change_application'):
            return True
        if not request.user.has_perm('opportunity_agent.review_application'):
            return False
        return obj is None or obj.status in {'pending', 'needs_review', 'failed', 'rejected'}

    def has_delete_permission(self, request, obj=None):
        if request.user.has_perm('opportunity_agent.review_application'):
            return request.user.has_perm('opportunity_agent.delete_application')
        return super().has_delete_permission(request, obj)

    def get_readonly_fields(self, request, obj=None):
        if (
            request.user.has_perm('opportunity_agent.review_application')
            and not request.user.has_perm('opportunity_agent.change_application')
        ):
            return tuple(field.name for field in self.model._meta.fields)
        return super().get_readonly_fields(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.has_perm('opportunity_agent.change_application'):
            actions.pop('queue_apps', None)
        if not request.user.has_perm('opportunity_agent.retry_application'):
            actions.pop('retry_failed_apps', None)
        if not request.user.has_perm('opportunity_agent.review_application'):
            actions.pop('approve_applications', None)
            actions.pop('reject_applications', None)
        return actions

@admin.register(ApplicationAttempt)
class ApplicationAttemptAdmin(admin.ModelAdmin):
    list_display=('application','attempt_number','status','created_at')
    list_filter=('status','created_at')
    search_fields=('application__user__username','application__opportunity__title','error_message')
    readonly_fields=('created_at',)

@admin.register(TelegramDestination)
class TelegramDestinationAdmin(admin.ModelAdmin):
    list_display=('name','type','chat_id','enabled','description','created_at')
    list_filter=('type','enabled')
    search_fields=('name','chat_id','description')
    list_editable=('enabled',)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        record_audit_event(
            'telegram_destination_updated' if change else 'telegram_destination_added',
            obj.pk,
            {'name': obj.name, 'type': obj.type, 'enabled': obj.enabled},
            actor=request.user,
        )

    def delete_model(self, request, obj):
        record_audit_event(
            'telegram_destination_deleted',
            obj.pk,
            {'name': obj.name, 'type': obj.type},
            actor=request.user,
        )
        super().delete_model(request, obj)

@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display=('action','actor','target','timestamp')
    list_filter=('action','timestamp')
    search_fields=('action','target','actor__username')
    readonly_fields=('actor','action','target','timestamp','details')
    date_hierarchy='timestamp'
    def has_add_permission(self,request): return False
    def has_change_permission(self,request,obj=None): return False

@admin.register(ProviderAdapter)
class ProviderAdapterAdmin(admin.ModelAdmin):
    list_display=('name','adapter_type','enabled','verified','allow_submit','created_at')
    list_filter=('enabled','adapter_type')
    search_fields=('name','adapter_type')
    readonly_fields=('created_at',)
    @admin.display(boolean=True)
    def verified(self,obj):
        return bool((obj.config or {}).get('verified'))
    @admin.display(boolean=True)
    def allow_submit(self,obj):
        config = obj.config or {}
        return bool(config.get('allow_submit'))

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        record_audit_event(
            'provider_adapter_updated' if change else 'provider_adapter_added',
            obj.pk,
            {'name': obj.name, 'adapter_type': obj.adapter_type, 'changed_fields': list(form.changed_data)},
            actor=request.user,
        )

@admin.register(AutomationRun)
class AutomationRunAdmin(admin.ModelAdmin):
    list_display=('status','started_at','finished_at')
    list_filter=('status','started_at')
    readonly_fields=('started_at','finished_at','details')
    date_hierarchy='started_at'

@admin.register(SystemSetting)
class SystemSettingAdmin(admin.ModelAdmin):
    class Form(forms.ModelForm):
        MODE_CHOICES = {
            'website_visibility': (
                ('public', 'Public / Active'),
                ('private', 'Private / Hidden'),
            ),
            'registration_mode': (
                ('open', 'Open registration'),
                ('admin_approval', 'Admin approval required'),
            ),
        }

        class Meta:
            model = SystemSetting
            fields = '__all__'

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            choices = self.MODE_CHOICES.get(self.instance.key)
            if choices:
                self.fields['value'] = forms.ChoiceField(
                    choices=choices,
                    initial=self.instance.value,
                )

        def clean(self):
            cleaned_data = super().clean()
            choices = self.MODE_CHOICES.get(cleaned_data.get('key'))
            value = cleaned_data.get('value')
            if choices and value not in {choice[0] for choice in choices}:
                self.add_error('value', 'Select a supported setting value.')
            return cleaned_data

    form = Form
    list_display=('key','value','updated_at')
    search_fields=('key',)
    readonly_fields=('updated_at',)

    def has_module_permission(self, request):
        return request.user.is_superuser or request.user.has_perm('opportunity_agent.manage_security_settings')

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser or request.user.has_perm('opportunity_agent.manage_security_settings')

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        record_audit_event(
            'settings_changed',
            obj.key,
            {'changed_fields': list(form.changed_data)},
            actor=request.user,
        )


@admin.register(PrivateAccessToken)
class PrivateAccessTokenAdmin(admin.ModelAdmin):
    list_display=(
        'label',
        'status_display',
        'recipient_email',
        'expires_at',
        'used_count',
        'max_uses',
        'created_at',
    )
    list_filter=('active','allowed_registration')
    search_fields=('label','recipient_email','notes','description')
    readonly_fields=('used_count','last_used_at','created_at','updated_at','active','revoked_at','disabled_at')
    actions=('disable_links','enable_links','revoke_links')
    change_list_template='admin/opportunity_agent/privateaccesstoken/change_list.html'
    fieldsets=(
        ('General', {'fields': ('label', 'description', 'created_by', 'recipient_email', 'active', 'allowed_registration')}),
        ('Limits', {'fields': ('expires_at', 'max_uses', 'used_count', 'last_used_at')}),
        ('Status', {'fields': ('revoked_at', 'disabled_at', 'notes')}),
    )

    @admin.display(description='Status')
    def status_display(self, obj):
        if obj.is_revoked:
            return 'Revoked'
        if obj.is_expired:
            return 'Expired'
        if obj.used_count >= obj.max_uses:
            return 'Exhausted'
        if obj.is_disabled:
            return 'Disabled'
        return 'Active'

    def has_add_permission(self, request):
        return request.user.is_superuser or request.user.has_perm(
            'opportunity_agent.add_privateaccesstoken'
        )

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser or request.user.has_perm(
            'opportunity_agent.change_privateaccesstoken'
        )

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not self.has_change_permission(request):
            actions.clear()
        return actions

    @admin.action(description='Disable selected private access links')
    def disable_links(self, request, queryset):
        for token in queryset:
            token.active = False
            token.disabled_at = timezone.now()
            token.save(update_fields=('active', 'disabled_at', 'updated_at'))
            record_audit_event(
                'PRIVATE_ACCESS_LINK_DISABLED',
                token.pk,
                {'label': token.label},
                actor=request.user,
            )
        self.message_user(request, 'Selected private access links were disabled.')

    @admin.action(description='Enable selected private access links')
    def enable_links(self, request, queryset):
        enabled_count = 0
        for token in queryset:
            if token.is_revoked or token.is_expired or token.used_count >= token.max_uses:
                continue
            token.active = True
            token.disabled_at = None
            token.save(update_fields=('active', 'disabled_at', 'updated_at'))
            enabled_count += 1
            record_audit_event(
                'PRIVATE_ACCESS_LINK_ENABLED',
                token.pk,
                {'label': token.label},
                actor=request.user,
            )
        self.message_user(
            request,
            f'{enabled_count} private access link(s) enabled; revoked, expired, or exhausted links were not changed.',
        )

    @admin.action(description='Revoke selected private access links')
    def revoke_links(self, request, queryset):
        for token in queryset:
            if token.revoked_at is not None:
                continue
            token.revoked_at = timezone.now()
            token.active = False
            token.save(update_fields=('revoked_at', 'active', 'updated_at'))
            record_audit_event(
                'PRIVATE_ACCESS_LINK_REVOKED',
                token.pk,
                {'label': token.label},
                actor=request.user,
            )
        self.message_user(request, 'Selected private access links were revoked.')

    def get_urls(self):
        custom_urls = [
            path(
                'issue-link/',
                self.admin_site.admin_view(self.issue_link_view),
                name='opportunity_agent_privateaccesstoken_issue_link',
            ),
        ]
        return custom_urls + super().get_urls()

    def add_view(self, request, form_url='', extra_context=None):
        return redirect('admin:opportunity_agent_privateaccesstoken_issue_link')

    def issue_link_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied

        issued_link = request.session.pop('_private_access_issued_link', None)
        form = PrivateAccessTokenIssueForm(request.POST or None)
        if request.method == 'POST' and form.is_valid():
            token, raw_token = PrivateAccessToken.issue_token(
                created_by=request.user,
                **form.cleaned_data,
            )
            request.session['_private_access_issued_link'] = {
                'label': token.label,
                'url': request.build_absolute_uri(
                    reverse('private_access_token', args=[raw_token])
                ),
            }
            record_audit_event(
                'PRIVATE_ACCESS_LINK_CREATED',
                token.pk,
                {
                    'label': token.label,
                    'recipient_email': token.recipient_email,
                    'expires_at': token.expires_at.isoformat() if token.expires_at else None,
                    'max_uses': token.max_uses,
                    'allowed_registration': token.allowed_registration,
                },
                actor=request.user,
            )
            return redirect('admin:opportunity_agent_privateaccesstoken_issue_link')

        context = {
            **self.admin_site.each_context(request),
            'opts': self.model._meta,
            'title': 'Issue private access link',
            'form': form,
            'issued_link': issued_link,
            'changelist_url': reverse(
                'admin:opportunity_agent_privateaccesstoken_changelist'
            ),
        }
        return render(
            request,
            'admin/opportunity_agent/privateaccesstoken/issue_link.html',
            context,
        )


class PrivateAccessTokenIssueForm(forms.Form):
    label = forms.CharField(max_length=100, required=False)
    description = forms.CharField(required=False, widget=forms.Textarea)
    recipient_email = forms.EmailField(required=False)
    expires_at = forms.DateTimeField(
        required=False,
        input_formats=('%Y-%m-%dT%H:%M',),
        widget=forms.DateTimeInput(
            format='%Y-%m-%dT%H:%M',
            attrs={'type': 'datetime-local'},
        ),
    )
    max_uses = forms.IntegerField(min_value=1, initial=1)
    allowed_registration = forms.BooleanField(required=False, initial=True)


class SiteCredentialAdminForm(forms.ModelForm):
    password = forms.CharField(required=False, widget=forms.PasswordInput(render_value=False), help_text='Enter to set/rotate the encrypted password. It is never displayed after saving.')
    secret = forms.CharField(required=False, widget=forms.PasswordInput(render_value=False), help_text='Optional token/secret; stored encrypted.')
    class Meta:
        model = SiteCredential
        fields = '__all__'

    def save(self, commit=True):
        obj = super().save(commit=False)
        password = self.cleaned_data.get('password')
        secret = self.cleaned_data.get('secret')
        if password:
            obj.set_password(password)
        if secret:
            obj.set_secret(secret)
        if commit:
            obj.save()
        return obj


class EmailMailboxAdminForm(forms.ModelForm):
    app_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text='Use a dedicated mailbox app password. It is encrypted and never displayed after saving.',
    )

    class Meta:
        model = EmailMailbox
        fields = '__all__'

    def save(self, commit=True):
        obj = super().save(commit=False)
        app_password = self.cleaned_data.get('app_password')
        if app_password:
            obj.set_app_password(app_password)
        if commit:
            obj.save()
        return obj


@admin.register(EmailMailbox)
class EmailMailboxAdmin(admin.ModelAdmin):
    form = EmailMailboxAdminForm
    list_display=('name','user','email','imap_host','enabled','last_checked_at','updated_at')
    list_filter=('enabled','imap_ssl')
    search_fields=('name','email','user__username')
    readonly_fields=('encrypted_app_password','last_checked_at','last_error','created_at','updated_at')
    fieldsets=(('Mailbox',{'fields':('user','name','email','imap_host','imap_port','imap_ssl','enabled')}),('Encrypted app password',{'fields':('app_password','encrypted_app_password'),'description':'Use a dedicated mailbox app password. The stored value is encrypted and never displayed.'}),('Health',{'fields':('last_checked_at','last_error','created_at','updated_at')}))

@admin.register(SiteCredential)
class SiteCredentialAdmin(admin.ModelAdmin):
    form=SiteCredentialAdminForm
    list_display=('name','user','domain','auth_type','enabled','last_used_at','updated_at')
    list_filter=('enabled','auth_type')
    search_fields=('name','domain','user__username','user__email')
    readonly_fields=('encrypted_password','encrypted_secret','last_used_at','created_at','updated_at')
    fieldsets=(('Account',{'fields':('user','name','domain','login_url','registration_url','auto_register','email_mailbox','account_status','registration_error','last_registration_at','auth_type','username','enabled')}),('Encrypted secrets',{'fields':('password','secret','encrypted_password','encrypted_secret'),'description':'Password/token inputs are write-only. Stored values are encrypted at rest and never displayed in plaintext.'}),('Metadata',{'fields':('metadata','last_used_at','created_at','updated_at')}))

@admin.register(ApplicationFormTemplate)
class ApplicationFormTemplateAdmin(admin.ModelAdmin):
    list_display=('name','form_type','enabled','updated_at')
    list_filter=('form_type','enabled')
    search_fields=('name',)
    readonly_fields=('created_at','updated_at')

@admin.register(ApplicationArtifact)
class ApplicationArtifactAdmin(admin.ModelAdmin):
    list_display=('application','kind','label','created_at')
    list_filter=('kind','created_at')
    search_fields=('application__user__username','application__opportunity__title','label')
    readonly_fields=('created_at',)
