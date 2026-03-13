# Zendesk Google Review Extraction Automation

This repository contains a GitHub Actions automation that:

1. Finds Zendesk tickets created via `via:"google_my_business"` in a date window.
2. Reads each ticket `description` from `/api/v2/tickets/{id}.json`.
3. Extracts text before the 5-star block (`\u2605\u2605\u2605\u2605\u2605`, `\u2605\u2605\u2606\u2606\u2606`, etc.).
4. Writes that extracted text to custom field `34603570445085`.
5. Clears the field (`null`) when no review text is present.

## Why this handles large manual periods

The script uses `GET /api/v2/search/export` with cursor pagination. This avoids the regular search endpoint returned-results cap (1,000 results / 10 pages) and is the recommended approach for large result sets.

## Required GitHub Secrets

- `ZENDESK_SUBDOMAIN` (example: `yourcompany`)
- `ZENDESK_EMAIL` (Zendesk user email used for API token auth)
- `ZENDESK_API_TOKEN` (Zendesk API token)

## Workflow

File: `.github/workflows/zendesk_google_review_sync.yml`

- Daily schedule: `02:15 UTC`
- Manual run: `workflow_dispatch` with optional:
  - `from_date` (`YYYY-MM-DD`, inclusive)
  - `to_date` (`YYYY-MM-DD`, inclusive)
  - `dry_run` (`true/false`)

If no dates are provided, the script processes yesterday (UTC).

## Local run

```bash
export ZENDESK_SUBDOMAIN=your_subdomain
export ZENDESK_EMAIL=you@example.com
export ZENDESK_API_TOKEN=your_token
python scripts/sync_google_reviews.py --from-date 2026-01-01 --to-date 2026-01-31 --dry-run
```

## Notes on extraction logic

- Regex anchor: first `\s[\u2605\u2606]{5}\s*` in `description`
- Extracted value: trimmed text before that marker
- If extracted value is empty, the target field is set to `null`
