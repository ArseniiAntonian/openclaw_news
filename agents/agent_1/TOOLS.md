# TOOLS.md - Local Notes

## Current Focus

- External news parsers
- Raw document storage
- PostgreSQL-backed job queue
- Cleaning and normalization workers
- Deduplication and event extraction

## Local Rules

- Keep source-specific quirks here once real parsers appear.
- Record which sources have stable `external_id` and which do not.
- Record cleaning and dedup edge cases here when discovered.
- Prefer one clear processing contract per stage.

## Parsers360 quirks

- Access pattern: `POST https://parsers360.ru:10443/enablers-api/api/v2/parametrized` with query params by default; override via `--api-url` or `PARSERS360_API_URL` when the vendor changes the route.
- Required params now include `service=parser`, `start_at`, `end_at`, `limit`, and `page`.
- The vendor now expects HTTP Basic Auth in addition to the query token.
- The parser accepts both `PARSERS360_BASIC_USER` / `PARSERS360_BASIC_PASSWORD` and legacy `PARSERS360_USER` / `PARSERS360_PASSWORD`.
- Date filters are still coarse, so rolling 24h pulls need post-filtering by `created_at`.
- `created_at` may arrive either as ISO datetime or unix epoch in milliseconds.
- `companies` may arrive as stringified JSON.
- Prefer `original_id` over `id` for stable deduplication when both are present; keep the raw parser item id in metadata.
- Large pulls should paginate until a short page arrives.
- The default CLI timeout is 300 seconds; raise `--timeout` further only for vendor-side stalls.
- TLS verification is disabled by default for the `:10443` endpoint because its certificate chain currently does not validate cleanly.

## Design Reminders

- `raw_items` stores provenance, not interpretation.
- `processing_jobs` is a queue and control plane, not a document store.
- `clean_items` stores normalized text and dedup metadata.
- `enriched_documents` and `events` should stay separate.
