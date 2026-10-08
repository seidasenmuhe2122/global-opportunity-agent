# Opportunity sources

Website, job-board, scholarship, university, NGO, government, company, RSS, public API and discovered sources use the `Source` model. Public Telegram channels use the separate `TelegramSource` model; do not add a Telegram channel as a website `Source`. Notification groups are separate again: configure those as `TelegramDestination` records (see [TELEGRAM.md](./TELEGRAM.md)).

## Add and manage sources

Staff with the appropriate source permissions can create and edit sources in Django admin under **Opportunity agent → Sources**. Configure a descriptive name, public HTTP(S) URL, source type, optional country and opportunity types, trust score, scan frequency, and enabled state. Scan frequencies are manual, hourly, every six hours, daily, or weekly. `Manual only` sources are not included in scheduled scans. CSV imports require `name,url`; optional fields and validation rules are described in the root [README](../README.md).

The scanner accepts public web URLs only. It rejects private/local IP addresses and redirects rather than following a URL to another host. Configure the final public URL directly. A failed source increments its own error counter and is logged without preventing other sources from scanning.

## Collection and extraction

The worker fetches due enabled sources, extracts candidates according to the source type, and uses structured AI extraction when configured. Explicit source values are retained; unsupported or unsubstantiated facts remain unknown. Extracted opportunity URLs and content fingerprints are deduplicated before records are created or updated. See [ARCHITECTURE.md](./ARCHITECTURE.md) for the pipeline and [SECURITY.md](./SECURITY.md) for network safeguards.

Source discovery runs periodically and is best-effort. The curated candidate list is always available; optional web discovery can be enabled with `ENABLE_WEB_SOURCE_DISCOVERY=1`. Discovery examines public pages only, skips barriers and irrelevant pages, and does not authenticate or bypass access controls.

## Telegram channels

Manage public channels under **Opportunity agent → Telegram sources**, not **Sources**. Public-channel collection uses Telethon and requires `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and an authorized `TELEGRAM_SESSION`. Configure that session out of band; the collector refuses to initiate account login. Only public channel username URLs are supported, not private channels or invite links. See [TELEGRAM.md](./TELEGRAM.md) for configuration and output destinations.

## Scheduling

Celery Beat launches the automation cycle every 15 minutes. It scans sources whose configured frequency is due (web and Telegram), then matches users and processes the application queue. Public source discovery runs every six hours. See [DEPLOYMENT.md](./DEPLOYMENT.md) to run the worker and the single Beat scheduler.
