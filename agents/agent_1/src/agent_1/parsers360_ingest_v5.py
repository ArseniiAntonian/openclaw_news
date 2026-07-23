"""v5 ingestion: Parsers360 -> agent_1_v5.source / agent_1_v5.raw_posts.

rework-agent-1-v5 task 6.7. **Not wired up to run anywhere** (no cron/systemd)
-- written and ready, deployment deliberately deferred per explicit decision
(2026-07-23): nothing currently ingests on a schedule, so there's no
accumulating gap to catch up on. This file existing doesn't change any running
behavior; `parsers360_ingest.py` (the v1 script, writing to `agent_1.raw_items`)
is untouched and still what actually runs, if anything does.

Reuses `parsers360_ingest`'s fetch_page/parse_published_at (pure, vendor-facing,
no schema dependency) and `migrate_v5`'s resolve_name_source (already validated
against the real corpus during the data migration). Only the insert side
changes, for the v5 schema:

- `raw_items.source` was always the literal parser name "parsers360" -- that's
  `raw_posts.parser` now. The real outlet name (`item.get("source")`) resolves
  through `resolve_name_source` into `agent_1_v5.source` (upsert-by-name, same
  pattern as the migration's `upsert_sources`, cached per-run since the same
  handful of sources repeats across every page).
- `raw_items.raw_payload` (the full vendor item) is NOT copied into
  `raw_posts.metadata` -- v5 only keeps the same five vendor-quirk fields the
  migration kept (summary, companies, source, is_duplicated, original_id) plus
  `external_id`, matching the `agent_1-ingestion` spec ("Обработка вендорских
  особенностей ответа").
- `raw_posts.url` / `content` / `time_post` are NOT NULL in v5 (nullable in
  v1). An item missing any of these can't be represented -- skipped and
  counted, never guessed at (same principle the data migration used).
- `raw_posts.collected_at` is `now()` at actual insert time -- unlike the
  historical migration, which had no real ingestion timestamp to use and fell
  back to `published_at` as an approximation, live ingestion has the real
  thing.
- No `document_type` column in v5 at all -- Parsers360 is a pure news feed,
  and `preprocess_v5.compute_verdict` already hardcodes `document_type="news"`
  for the junk filter. There's nothing else this pipeline ingests to
  distinguish.
- No DB trigger creates a "pending" marker on insert (unlike v1's
  `enqueue_preprocess_job` trigger) -- v5 doesn't need one. Under the
  anti-join claim (design D9), a fresh `raw_posts` row with no `clean_posts`
  row already *is* pending; `preprocess_v5` picks it up on its own.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from agent_1 import parsers360_ingest as v1  # noqa: E402
from agent_1.migrate_v5 import resolve_name_source  # noqa: E402

SCHEMA = "agent_1_v5"
DEFAULT_LOG_FILE = v1.ENV_PATH.parent / "logs" / "parsers360_v5.log"
LOG_FILE = os.getenv("PARSERS360_V5_LOG_FILE", str(DEFAULT_LOG_FILE))
DB_DSN = v1.DB_DSN


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log_line(level: str, message: str, *, stderr: bool = False) -> None:
    line = f"{utc_now_text()} {level} {message}"
    print(line, file=sys.stderr if stderr else sys.stdout)
    log_file = Path(LOG_FILE)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


def resolve_source_id(
    cur: psycopg.Cursor[Any], cache: dict[str, int], name: str
) -> int:
    key = name.casefold()
    cached = cache.get(key)
    if cached is not None:
        return cached
    cur.execute(
        f"""
        INSERT INTO {SCHEMA}.source (name_source, type)
        VALUES (%s, 'unknown')
        ON CONFLICT (name_source) DO UPDATE SET name_source = EXCLUDED.name_source
        RETURNING id_source
        """,
        (name,),
    )
    id_source = cur.fetchone()[0]
    cache[key] = id_source
    return id_source


def insert_raw_post(
    cur: psycopg.Cursor[Any], item: dict[str, Any], source_cache: dict[str, int]
) -> str:
    """Returns 'inserted' or a skip reason ('no_url', 'no_content', 'no_time_post')."""
    url = item.get("url")
    content = item.get("content")
    time_post = v1.parse_published_at(item)

    if not url:
        return "no_url"
    if not content:
        return "no_content"
    if time_post is None:
        return "no_time_post"

    source_metadata = {
        "summary": item.get("summary"),
        "companies": item.get("companies"),
        "source": item.get("source"),
        "is_duplicated": item.get("is_duplicated"),
        "original_id": item.get("original_id"),
    }
    name = resolve_name_source(source_metadata, url)
    id_source = resolve_source_id(cur, source_cache, name)

    metadata = dict(source_metadata)
    metadata["external_id"] = str(item["id"])

    cur.execute(
        f"""
        INSERT INTO {SCHEMA}.raw_posts (
            id_source, parser, title, url, content, time_post, metadata, collected_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, now())
        ON CONFLICT DO NOTHING
        """,
        (id_source, "parsers360", item.get("title"), url, content, time_post,
         json.dumps(metadata, ensure_ascii=False)),
    )
    return "inserted"


def main() -> int:
    start_at = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    log_line(
        "INFO",
        f"Starting v5 ingest start_at={start_at} limit={v1.LIMIT} "
        f"api_url={v1.API_URL} (target schema: {SCHEMA})",
    )

    conn = psycopg.connect(DB_DSN, autocommit=False)
    source_cache: dict[str, int] = {}
    counts = {"inserted": 0, "no_url": 0, "no_content": 0, "no_time_post": 0}
    page = 1

    try:
        with conn.cursor() as cur:
            while True:
                log_line("INFO", f"Fetching Parsers360 start_at={start_at} page={page} limit={v1.LIMIT}")
                items = v1.fetch_page(page, start_at)
                if not items:
                    break

                for item in items:
                    result = insert_raw_post(cur, item, source_cache)
                    counts[result] = counts.get(result, 0) + 1

                conn.commit()
                log_line("INFO", f"page={page} received={len(items)} counts={counts}")

                if len(items) < v1.LIMIT:
                    break
                page += 1

        log_line("INFO", f"done, counts={counts}, sources_resolved={len(source_cache)}")
    except Exception as exc:
        conn.rollback()
        log_line("ERROR", f"v5 ingest failed: {exc}", stderr=True)
        raise
    finally:
        conn.close()
        log_line("INFO", "Postgres connection closed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())