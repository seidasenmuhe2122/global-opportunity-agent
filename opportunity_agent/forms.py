import re
import io
import zipfile

from django import forms
from django.forms.widgets import ClearableFileInput
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .models import Opportunity, Source, UserProfile, SiteCredential, EmailMailbox
from .security import clear_auth_failures, is_auth_locked, record_failed_auth_attempt, sanitize_text
from .services.audit import record_audit_event


class PrivateProfileFileInput(ClearableFileInput):
    template_name = 'opportunity_agent/widgets/private_profile_file_input.html'

    def __init__(self, *args, download_url='', **kwargs):
        self.download_url = download_url
        super().__init__(*args, **kwargs)

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context['widget']['download_url'] = self.download_url
        return context


class CVUploadValidationMixin:
    MAX_CV_SIZE = 10 * 1024 * 1024

    def clean_cv(self):
        uploaded = self.cleaned_data.get('cv')
        if not uploaded or not hasattr(uploaded, 'read'):
            return uploaded

        filename = (uploaded.name or '').lower()
        if not filename.endswith(('.pdf', '.docx')):
            raise forms.ValidationError('Upload your CV as a PDF or DOCX file.')
        if uploaded.size > self.MAX_CV_SIZE:
            raise forms.ValidationError('CV files must be 10 MB or smaller.')

        uploaded.seek(0)
        content = uploaded.read(self.MAX_CV_SIZE + 1)
        uploaded.seek(0)
        if not content or len(content) > self.MAX_CV_SIZE:
            raise forms.ValidationError('The CV file is empty or exceeds the 10 MB limit.')

        if filename.endswith('.pdf'):
            if not content.startswith(b'%PDF-'):
                raise forms.ValidationError('The uploaded file is not a valid PDF document.')
            try:
                reader = PdfReader(io.BytesIO(content), strict=True)
            except PdfReadError as exc:
                raise forms.ValidationError('The uploaded PDF could not be read.') from exc
            if not reader.pages:
                raise forms.ValidationError('The PDF does not contain any pages.')
        else:
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as document:
                    members = set(document.namelist())
                    if (
                        '[Content_Types].xml' not in members
                        or 'word/document.xml' not in members
                    ):
                        raise forms.ValidationError(
                            'The uploaded DOCX is missing required document content.'
                        )
            except forms.ValidationError:
                raise
            except (OSError, zipfile.BadZipFile) as exc:
                raise forms.ValidationError('The uploaded file is not a valid DOCX document.') from exc
        return uploaded


class CommaSeparatedListField(forms.CharField):
    def prepare_value(self, value):
        if isinstance(value, (list, tuple)):
            return ', '.join(str(item) for item in value)
        return value

    def to_python(self, value):
        value = super().to_python(value)
        if not value:
            return []
        return list(dict.fromkeys(
            item.strip() for item in value.replace('\n', ',').split(',')
            if item.strip()
        ))


class SanitizedFormMixin:
    def sanitize_cleaned_data(self):
        if not hasattr(self, 'cleaned_data'):
            return None
        for field_name, value in list(self.cleaned_data.items()):
            if isinstance(value, str):
                self.cleaned_data[field_name] = sanitize_text(value)
        return self.cleaned_data


class UserProfileForm(CVUploadValidationMixin, SanitizedFormMixin, forms.ModelForm):
    skills = CommaSeparatedListField(
        required=False,
        label='Skills and career interests',
        help_text='Include skills, job titles, and career interests (for example: Python, software development, management, project coordination).',
        widget=forms.Textarea(attrs={'rows': 3}),
    )
    cv = forms.FileField(
        required=False,
        widget=PrivateProfileFileInput(attrs={'accept': '.pdf,.docx'}),
        help_text='Upload a PDF or DOCX CV, up to 10 MB. Your CV is private and only available to you and authorized reviewers.',
    )
    target_countries = CommaSeparatedListField(
        required=False,
        help_text='Enter one or more countries separated by commas. Leave blank for no country preference.',
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': 'Ethiopia, Germany'}),
    )
    skills = CommaSeparatedListField(
        required=False,
        help_text='Separate skills with commas.',
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': 'Python, Django, Excel'}),
    )
    languages = CommaSeparatedListField(required=False, widget=forms.Textarea(attrs={'rows': 2}))
    certifications = CommaSeparatedListField(required=False, widget=forms.Textarea(attrs={'rows': 2}))
    other_links = CommaSeparatedListField(required=False, widget=forms.Textarea(attrs={'rows': 2}))
    preferred_opportunity_types = forms.MultipleChoiceField(
        required=False,
        choices=Opportunity.OPPORTUNITY_TYPES,
        widget=forms.CheckboxSelectMultiple,
        help_text='Select any types you want to see. Leave all unchecked to include every type.',
    )
    preferred_work_modes = forms.MultipleChoiceField(
        required=False,
        choices=Opportunity.WORK_MODE_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        help_text='Select any work modes you want. Leave all unchecked for any mode.',
    )

    class Meta:
        model = UserProfile
        fields = [
            'full_name', 'phone', 'current_country', 'target_countries', 'worldwide_preference',
            'skills', 'education', 'degree', 'work_experience', 'languages', 'certifications',
            'cv', 'portfolio_url', 'linkedin_url', 'github_url', 'other_links',
            'preferred_opportunity_types', 'preferred_work_modes', 'visa_sponsorship_preference',
            'salary_stipend_preference', 'minimum_ai_match_score', 'auto_apply', 'daily_application_limit',
            'notification_preferences',
        ]
        widgets = {
            'education': forms.Textarea(attrs={'rows': 4}),
            'work_experience': forms.Textarea(attrs={'rows': 5}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user_id = self.instance.user_id
        if user_id:
            from django.urls import reverse
            self.fields['cv'].widget.download_url = reverse(
                'profile_cv_download',
                kwargs={'user_id': user_id},
            )

    def clean(self):
        cleaned_data = super().clean()
        self.sanitize_cleaned_data()
        return cleaned_data


class SourceForm(SanitizedFormMixin, forms.ModelForm):
    class Meta:
        model = Source
        fields = [
            'name', 'url', 'source_type', 'country', 'opportunity_types', 'enabled', 'trust_score',
            'scan_frequency', 'status', 'notes', 'auto_discovered'
        ]

    def clean(self):
        cleaned_data = super().clean()
        self.sanitize_cleaned_data()
        return cleaned_data


class SourceCSVImportForm(forms.Form):
    file = forms.FileField(
        help_text=(
            'Upload a UTF-8 CSV with columns name,url. Optional columns: source_type, '
            'country, opportunity_types (pipe-separated), enabled, trust_score, '
            'scan_frequency, notes. Maximum 5 MB and 5,000 rows.'
        ),
        widget=forms.ClearableFileInput(attrs={'accept': '.csv,text/csv'}),
    )

    def clean_file(self):
        uploaded = self.cleaned_data['file']
        if uploaded.size > 5 * 1024 * 1024:
            raise forms.ValidationError('CSV file must be 5 MB or smaller.')
        return uploaded

from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm


class SignUpForm(SanitizedFormMixin, UserCreationForm):
    email = forms.EmailField(required=True)
    password1 = forms.CharField(
        label='Password',
        strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
        help_text='Use at least 8 characters. Avoid common passwords or personal information.',
    )
    username = forms.CharField(
        required=False,
        label='Username (optional)',
        help_text='Leave blank to use a username based on your email address.',
    )

    class Meta:
        model = get_user_model()
        fields = ('username','email','password1','password2')

    def clean(self):
        cleaned_data = super().clean()
        self.sanitize_cleaned_data()
        return cleaned_data

    def clean_username(self):
        username = (self.cleaned_data.get('username', '') or '').strip()
        if username:
            username = sanitize_text(username)
            self.cleaned_data['username'] = username
            return super().clean_username()

        email = sanitize_text((self.cleaned_data.get('email') or self.data.get('email', '')))
        email_prefix = email.partition('@')[0]
        username_field = get_user_model()._meta.get_field('username')
        base = re.sub(r'[^\w.@+-]', '', email_prefix)[:username_field.max_length] or 'user'
        candidate = base
        suffix = 2
        users = get_user_model()._default_manager
        while users.filter(username__iexact=candidate).exists():
            suffix_text = str(suffix)
            candidate = f'{base[:username_field.max_length - len(suffix_text)]}{suffix_text}'
            suffix += 1
        return self.fields['username'].clean(candidate)

    def clean_email(self):
        email = sanitize_text(self.cleaned_data['email'].strip())
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with this email address already exists.')
        return email


class EmailAuthenticationForm(SanitizedFormMixin, AuthenticationForm):
    username = forms.CharField(
        label='Username or email',
        widget=forms.TextInput(attrs={'autofocus': True, 'autocomplete': 'username'}),
    )

    def clean(self):
        username = sanitize_text(self.data.get('username', '')) if self.data.get('username') else self.data.get('username')
        password = sanitize_text(self.data.get('password', '')) if self.data.get('password') else self.data.get('password')
        request = getattr(self, 'request', None)

        if request is not None and is_auth_locked(request, username):
            raise forms.ValidationError(
                'Too many failed sign-in attempts. Please wait a few minutes before trying again.'
            )

        user = None
        if username and password:
            user_model = get_user_model()
            user = user_model._default_manager.filter(username__iexact=username).first()
            if user is None and '@' in username:
                user = user_model._default_manager.filter(email__iexact=username).first()

            if user is not None and user.check_password(password):
                if request is not None:
                    clear_auth_failures(request, username)
                if not user.is_active:
                    raise forms.ValidationError('This account is inactive.')
                profile = getattr(user, 'profile', None)
                status = getattr(profile, 'registration_status', 'active')
                if status == 'pending':
                    record_audit_event(
                        'LOGIN_BLOCKED_PENDING',
                        user.pk,
                        {'username': username, 'reason': 'pending_registration'},
                    )
                    raise forms.ValidationError(
                        'Your account is awaiting administrator approval. You will be able to sign in after your registration is approved.'
                    )
                if status == 'rejected':
                    record_audit_event(
                        'LOGIN_BLOCKED_REJECTED',
                        user.pk,
                        {'username': username, 'reason': 'rejected_registration'},
                    )
                    raise forms.ValidationError('Your account has been rejected. Please contact an administrator for more information.')
                if status == 'suspended':
                    record_audit_event(
                        'LOGIN_BLOCKED_SUSPENDED',
                        user.pk,
                        {'username': username, 'reason': 'suspended_registration'},
                    )
                    raise forms.ValidationError('Your account is suspended and cannot sign in at this time.')

                self.cleaned_data['username'] = username
                self.cleaned_data['password'] = password
                self.user_cache = user
                user.backend = 'opportunity_agent.authentication.EmailOrUsernameModelBackend'
                self.sanitize_cleaned_data()
                return self.cleaned_data

            if request is not None and (user is not None or username):
                record_failed_auth_attempt(request, username)
                if is_auth_locked(request, username):
                    raise forms.ValidationError(
                        'Too many failed sign-in attempts. Please wait a few minutes before trying again.'
                    )

        cleaned = super().clean()
        self.sanitize_cleaned_data()
        return cleaned


class SiteCredentialForm(SanitizedFormMixin, forms.ModelForm):
    password = forms.CharField(required=False, widget=forms.PasswordInput, help_text='Stored encrypted; it will never be shown again.')
    secret = forms.CharField(required=False, widget=forms.PasswordInput, help_text='Optional token/secret; stored encrypted.')
    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None and 'email_mailbox' in self.fields:
            self.fields['email_mailbox'].queryset = EmailMailbox.objects.filter(user=user, enabled=True).order_by('name')
    class Meta:
        model = SiteCredential
        fields = ['name','domain','login_url','registration_url','auto_register','email_mailbox','username','auth_type','enabled','metadata']

    def clean(self):
        cleaned_data = super().clean()
        self.sanitize_cleaned_data()
        return cleaned_data

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data.get('password'): obj.set_password(self.cleaned_data['password'])
        if self.cleaned_data.get('secret'): obj.set_secret(self.cleaned_data['secret'])
        if commit: obj.save()
        return obj


class EmailMailboxForm(SanitizedFormMixin, forms.ModelForm):
    app_password = forms.CharField(required=False, widget=forms.PasswordInput(render_value=False), help_text='Use a dedicated mailbox app password. It is encrypted and never displayed.')
    class Meta:
        model = EmailMailbox
        fields = ['name','email','imap_host','imap_port','imap_ssl','enabled']

    def clean(self):
        cleaned_data = super().clean()
        self.sanitize_cleaned_data()
        return cleaned_data

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data.get('app_password'):
            obj.set_app_password(self.cleaned_data['app_password'])
        if commit: obj.save()
        return obj
