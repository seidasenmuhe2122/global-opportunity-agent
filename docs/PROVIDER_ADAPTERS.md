# Provider adapters

`ProviderAdapter` records a provider name, adapter type and JSON configuration. `playwright_configured` is available for administrators who have explicit permission to automate a provider.

Example configuration:

```json
{
  "allowed_domains": ["example.org"],
  "verified": true,
  "allow_submit": true,
  "public_application_page": true,
  "application_fields": {
    "#full-name": "full_name",
    "#email": "email",
    "#cover-letter": "cover_letter"
  },
  "cv_upload_selector": "input[type=file]",
  "submit_selector": "button[type=submit]",
  "confirmation_selector": ".application-confirmation",
  "confirmation_words": ["application received", "thank you for applying"]
}
```

Automatic submission requires both `verified` and `allow_submit`. Configure `public_application_page` only for a provider page that genuinely does not require sign-in; otherwise each user needs an enabled, authorized site credential. A detected sign-in barrier without usable credentials goes to Needs Review.

Before submission, the adapter checks for CAPTCHA/MFA/anti-bot controls and verifies that required browser fields have valid values. It only records `Submitted` when the configured confirmation selector or a recognized/configured success message is observed and a valid result URL is saved. A changed URL alone is not treated as success. If submission may have started but confirmation is missing, the outcome is marked Needs Review to prevent an unsafe automatic retry. Unsupported, unverified or preparation-only adapters do not submit.
