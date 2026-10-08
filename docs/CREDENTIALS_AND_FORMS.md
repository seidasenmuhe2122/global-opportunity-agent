# Website Credentials & Custom Application Forms

## Credentials
Users can save credentials for sites they are authorized to use. Passwords/secrets are encrypted at rest using `CREDENTIAL_ENCRYPTION_KEY` (or Django `SECRET_KEY` as fallback). Plaintext passwords are never shown in admin. Keep the encryption key stable across deploys and backups; changing it makes previously encrypted values unreadable.

Users sign in with either their account email or their existing username. New registrations require unique email addresses.

CVs and generated application forms use the configured default Django storage. CV uploads must be valid PDF or DOCX files no larger than 10 MB. Production web and worker services must share an S3-compatible bucket configured for private objects with bucket-level public access blocked. A CV is downloaded only through the authenticated, permission-checked profile download view; do not publish a media route or expose a CV's storage URL. A verified provider adapter can upload the CV to an application form when its `cv_upload_selector` is configured.

## Provider configuration
`ProviderAdapter.config` supports: `allowed_domains`, `verified`, `allow_submit`, `public_application_page`, `login.username_selector`, `login.password_selector`, `login.submit_selector`, `application_fields`, `cv_upload_selector`, `submit_selector`, `confirmation_selector`, `confirmation_words`, `headless`, and `timeout_ms`. Automatic submission is disabled unless both `verified` and `allow_submit` are explicitly true. Set `public_application_page` only for a verified application flow that does not require login.

Example:
```json
{
  "allowed_domains": ["example.org"],
  "verified": true,
  "allow_submit": true,
  "public_application_page": true,
  "login": {"username_selector":"#email","password_selector":"#password","submit_selector":"button[type=submit]"},
  "application_fields": {"#full-name":"full_name","#phone":"phone","#cover":"cover_letter"},
  "cv_upload_selector":"input[type=file]",
  "submit_selector":"button[type=submit]",
  "confirmation_selector":".success-message",
  "headless": true
}
```

## PDF forms
`ApplicationFormTemplate` can store a downloadable/fixed PDF and a JSON field map such as `{"Full Name":"full_name","Email":"email"}`. Only fillable AcroForm PDFs can be filled automatically; scanned/non-fillable PDFs become Needs Review.

## Safety
The system does not bypass CAPTCHA, MFA, anti-bot controls, access restrictions, or security challenges. Those cases are recorded as Needs Review.


## Automated workflow
1. Add the website domain to `ProviderAdapter.allowed_domains`.
2. Add the user's authorized login in `SiteCredential`; the password is entered once and encrypted.
3. Configure login selectors and application field selectors.
4. If the organization provides a fillable PDF, add an `ApplicationFormTemplate` with its URL/file and field map.
5. The worker logs in when an authorized credential is configured, fills the profile data and known answers, prepares the custom form, uploads the CV/form where configured, and verifies required fields before submitting.
6. The application is marked Submitted only after a confirmation selector or success message appears and a result URL is saved. CAPTCHA/MFA/security verification, unidentified required fields, missing confirmation, or uncertain post-submit results become `Needs Review`; the worker does not attempt to defeat the control.


## Organization-specific forms
The project includes disabled starter adapter records for UN Inspira/Careers and African Union Careers. They are intentionally not auto-submitting until an administrator configures and verifies the site's selectors and the user supplies their own authorized credentials. The UN applicant guide describes an offline application template that can be loaded into Inspira, while AU job pages explicitly require the AU CV template for many vacancies. The system can fill configured PDF/DOCX templates and upload them where the provider adapter is configured.
\n\n## Email-backed registration\nUsers can configure an authorized mailbox with a dedicated app password. The app password is encrypted at rest. A site credential can point to that mailbox and enable `auto_register`. The generic registration agent detects common email/username/password fields, creates a strong site password, stores the site credential encrypted, and can follow a same-domain email verification/activation link. Common IMAP hosts are auto-detected for Gmail, Outlook/Hotmail, and Yahoo; custom domains must provide IMAP host/port.\n\nThe system does **not** automate or bypass CAPTCHA, MFA/2FA, security challenges, access controls, or verification codes. If a registration/login flow requires those controls, it becomes Needs Review. Email activation links are supported when they are ordinary account-verification links and remain on the configured site domain.\n