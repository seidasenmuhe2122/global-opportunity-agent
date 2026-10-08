# Architecture

`Django` is the control plane. `Celery + Redis` run the continuous background engine.

## Relational model

The built-in Django user is linked one-to-one to `UserProfile`. `Source` and `TelegramSource` are distinct collection inputs; `Opportunity` can point to either source. `Match` stores one user/opportunity score. `Application` also has a database-enforced unique user/opportunity pair and owns its `ApplicationAttempt` and generated `ApplicationArtifact` records. `TelegramDestination` is output-only. `ProviderAdapter`, `SystemSetting`, `AutomationRun`, and `AuditLog` hold configuration, execution history, and administrative traceability. Foreign keys, unique constraints, range checks, and query indexes are declared in the models and migrations.

Pipeline:

1. Source discovery and source-manager validation
2. Website/RSS/API/Telegram collection
3. Candidate extraction and contact/application-link detection
4. AI structured extraction and classification
5. URL/title/organization fingerprint deduplication
6. Opportunity storage and deadline expiration
7. User matching with explainable scores and Match records
8. Application queue creation for eligible auto-apply users
9. CV/profile validation, daily-limit and duplicate guards
10. Cover-letter preparation
11. Provider-specific adapter execution or Needs Review
12. Audit logs and Telegram notifications
13. Admin analytics and health monitoring

Periodic work is declared in `CELERY_BEAT_SCHEDULE` in Django settings. Run one Beat scheduler and one or more workers. The cycle isolates failures by stage and per source/application. Retries of failed or rejected applications are explicit task requests; uncertain submissions are routed to review and are never blindly resubmitted.

The system is intentionally independent from AFRIJOB.
