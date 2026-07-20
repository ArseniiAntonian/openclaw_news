"""One-time backfill: agent_1.raw_items/clean_items -> agent_1_v5.raw_posts/clean_posts.

rework-agent-1-v5 / tasks.md 1.3. See db/v5/DATA_MIGRATION.md for how to run
this. Read-only against the `agent_1` schema; only ever writes to
`agent_1_v5`.

Design notes (see openspec/changes/rework-agent-1-v5/design.md D7):

- `raw_items.source` is always the literal string "parsers360" today (the
  parser name, not the outlet) -- it maps to `raw_posts.parser`. The actual
  outlet name lives in `raw_items.source_metadata->>'source'` (mirrors
  `label_kr_worker.get_news_source`) and resolves to `agent_1_v5.source`.
- `raw_posts.url` / `content` / `time_post` are NOT NULL in v5 but nullable
  in the old schema. Rows that cannot satisfy this (no url, or no
  published_at to use as time_post) are skipped and reported -- never
  guessed at.
- `raw_posts.collected_at` has no real historical equivalent (the old schema
  never recorded ingestion wall-clock time); it is approximated as
  `published_at`. This is a known, documented lossy migration decision, not
  a discovered fact.
- `raw_posts.lang` is backfilled from `source_metadata.preprocess.language`
  (recorded by preprocess_worker for every processed row, pass or fail),
  falling back to `clean_items.language`.
- `clean_posts.content_hash` for kept rows is `sha256(build_exact_dedup_key(
  clean_title, clean_text))`, reusing preprocess_worker's own normalization
  so historically-verified-unique rows hash the same way going forward. A
  collision here would mean the OLD exact-dedup already had a bug -- caught
  per-row via a savepoint and reported, not allowed to abort the run.
- The old schema never created a clean_items row for a duplicate -- only a
  JSON marker on the duplicate's raw_items row pointing at the *canonical*
  clean_item_id. v5 wants one clean_posts row per processed raw_posts row
  (including duplicates), so this script runs in two passes: pass 1 creates
  raw_posts + clean_posts for every "kept" (cleaned) and "dropped"
  (filtered/failed) row and builds an old-clean-item-id -> new-id_clean_post
  map; pass 2 uses that map to create clean_posts rows for duplicates.
- Idempotent / safe to re-run: already-migrated raw_items are recognized via
  `raw_posts.metadata->>'_migrated_from_raw_item_id'` (checked once up
  front); clean_posts inserts use `ON CONFLICT (id_raw_post) DO NOTHING`
  (a real unique constraint already in the DDL).

Transaction handling (worth being explicit about, since it's easy to get
wrong with psycopg3): `conn.transaction()` only becomes a SAVEPOINT if the
connection is already mid-transaction; opened while the connection is idle,
it BEGINs and COMMITs on its own, per-call. This script relies on that: it
never wraps a whole row in `conn.transaction()` (that would silently commit
after every single row, batch boundaries and --dry-run's final rollback
included). Ordinary statements run directly against the ambient
autocommit=False transaction, which is only ever closed by this script's own
explicit `conn.commit()` (per batch) or, for --dry-run, a single
`conn.rollback()` at the very end. `conn.transaction()` is used exactly once,
around the one INSERT that can legitimately fail on data it doesn't
control (a content_hash collision) -- entered while the connection is
already non-idle from the row's own raw_posts INSERT moments earlier, so it
correctly becomes a SAVEPOINT and only that one insert is undone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg import errors
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_1.label_kr_worker import coerce_json_object, normalize_domain_key  # noqa: E402
from agent_1.preprocess_worker import build_exact_dedup_key, load_dotenv  # noqa: E402

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(ENV_PATH)
DB_DSN = os.environ["AGENT_1_DB_DSN"]

OLD_SCHEMA = "agent_1"
NEW_SCHEMA = "agent_1_v5"
DEFAULT_BATCH_SIZE = 500


def content_hash_for(clean_title: str | None, clean_text: str) -> str | None:
    key = build_exact_dedup_key(clean_title, clean_text)
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def resolve_name_source(source_metadata: dict[str, Any], url: str | None) -> str:
    candidate = source_metadata.get("source")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    domain = normalize_domain_key(url) if url else ""
    if domain:
        return domain
    return "unknown_source"


class Report:
    def __init__(self) -> None:
        self.migrated_raw = 0
        self.skipped_already_migrated = 0
        self.skipped_no_url: list[int] = []
        self.skipped_no_time_post: list[int] = []
        self.kept = 0
        self.dropped_by_reason: dict[str, int] = {}
        self.duplicates_linked = 0
        self.duplicates_orphaned: list[int] = []
        self.content_hash_collisions: list[tuple[int, int]] = []
        self.anomalous_status: list[tuple[int, str]] = []
        self.unprocessed = 0

    def print_summary(self) -> None:
        print("\n=== Migration report ===")
        print(f"raw_items migrated to raw_posts: {self.migrated_raw}")
        print(f"raw_items already migrated (skipped, idempotent re-run): {self.skipped_already_migrated}")
        print(f"raw_items skipped, no url: {len(self.skipped_no_url)} {self.skipped_no_url[:20]}")
        print(f"raw_items skipped, no published_at: {len(self.skipped_no_time_post)} {self.skipped_no_time_post[:20]}")
        print(f"clean_posts kept (drop_reason IS NULL): {self.kept}")
        print(f"clean_posts unprocessed (no verdict yet, no row created): {self.unprocessed}")
        for reason, count in sorted(self.dropped_by_reason.items()):
            print(f"clean_posts dropped, reason={reason}: {count}")
        print(f"duplicates linked to a canonical clean_posts row: {self.duplicates_linked}")
        print(f"duplicates orphaned (canonical was itself skipped): {len(self.duplicates_orphaned)} {self.duplicates_orphaned[:20]}")
        print(f"content_hash collisions among 'kept' rows (old dedup bug, need review): {len(self.content_hash_collisions)} {self.content_hash_collisions[:20]}")
        print(f"anomalous preprocess.status values (need review): {len(self.anomalous_status)} {self.anomalous_status[:20]}")


def preload_already_migrated(conn: psycopg.Connection[Any]) -> set[int]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT (metadata->>'_migrated_from_raw_item_id')::bigint AS old_id
            FROM {NEW_SCHEMA}.raw_posts
            WHERE metadata ? '_migrated_from_raw_item_id'
            """
        )
        return {row["old_id"] for row in cur.fetchall()}


def preload_clean_items(conn: psycopg.Connection[Any]) -> dict[int, dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, raw_item_id, clean_title, clean_text, language
            FROM {OLD_SCHEMA}.clean_items
            """
        )
        return {row["raw_item_id"]: row for row in cur.fetchall()}


def fetch_raw_items_batch(
    conn: psycopg.Connection[Any], after_id: int, batch_size: int
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, source, document_type, external_id, url, title,
                   raw_text, raw_payload, source_metadata, published_at
            FROM {OLD_SCHEMA}.raw_items
            WHERE id > %s
            ORDER BY id
            LIMIT %s
            """,
            (after_id, batch_size),
        )
        return cur.fetchall()


def upsert_sources(
    conn: psycopg.Connection[Any], report: Report, *, dry_run: bool
) -> dict[str, int]:
    """Collect every distinct outlet name seen in raw_items and upsert them
    into agent_1_v5.source up front, so raw_posts inserts can resolve
    id_source from an in-memory map instead of one lookup per row."""

    names: dict[str, str] = {}  # casefold key -> first-seen display text
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT url, source_metadata FROM {OLD_SCHEMA}.raw_items"
        )
        for row in cur.fetchall():
            source_metadata = coerce_json_object(row["source_metadata"])
            name = resolve_name_source(source_metadata, row["url"])
            key = name.casefold()
            names.setdefault(key, name)

    name_to_id: dict[str, int] = {}
    with conn.cursor() as cur:
        for key, display_name in names.items():
            cur.execute(
                f"""
                INSERT INTO {NEW_SCHEMA}.source (name_source, type)
                VALUES (%s, 'unknown')
                ON CONFLICT (name_source) DO UPDATE SET name_source = EXCLUDED.name_source
                RETURNING id_source
                """,
                (display_name,),
            )
            name_to_id[key] = cur.fetchone()["id_source"]
    if not dry_run:
        conn.commit()
    print(f"Resolved {len(name_to_id)} distinct sources into agent_1_v5.source.")
    return name_to_id


def insert_raw_post(
    cur: psycopg.Cursor[Any],
    *,
    raw_item: dict[str, Any],
    id_source: int,
    content: str,
    lang: str | None,
) -> tuple[int, Any]:
    source_metadata = coerce_json_object(raw_item["source_metadata"])
    metadata = dict(source_metadata)
    metadata["external_id"] = raw_item["external_id"]
    metadata["_migrated_from_raw_item_id"] = raw_item["id"]

    cur.execute(
        f"""
        INSERT INTO {NEW_SCHEMA}.raw_posts (
            id_source, parser, title, url, content, time_post, lang,
            metadata, collected_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
        RETURNING id_raw_post, time_post
        """,
        (
            id_source,
            raw_item["source"],
            raw_item["title"],
            raw_item["url"],
            content,
            raw_item["published_at"],
            lang,
            json.dumps(metadata, ensure_ascii=False),
            raw_item["published_at"],
        ),
    )
    row = cur.fetchone()
    return row["id_raw_post"], row["time_post"]


def insert_clean_post_kept(
    cur: psycopg.Cursor[Any],
    *,
    id_raw_post: int,
    time_post: Any,
    clean_title: str | None,
    clean_text: str,
) -> int | None:
    content_hash = content_hash_for(clean_title, clean_text)
    clean_content = f"{clean_title}\n\n{clean_text}" if clean_title else clean_text
    cur.execute(
        f"""
        INSERT INTO {NEW_SCHEMA}.clean_posts (
            id_raw_post, time_post, clean_content, content_hash,
            is_duplicate, cleaned_at
        )
        VALUES (%s, %s, %s, %s, FALSE, now())
        ON CONFLICT (id_raw_post) DO NOTHING
        RETURNING id_clean_post
        """,
        (id_raw_post, time_post, clean_content, content_hash),
    )
    row = cur.fetchone()
    return row["id_clean_post"] if row else None


def insert_clean_post_dropped(
    cur: psycopg.Cursor[Any],
    *,
    id_raw_post: int,
    time_post: Any,
    drop_reason: str,
) -> None:
    cur.execute(
        f"""
        INSERT INTO {NEW_SCHEMA}.clean_posts (
            id_raw_post, time_post, drop_reason, is_duplicate, cleaned_at
        )
        VALUES (%s, %s, %s, FALSE, now())
        ON CONFLICT (id_raw_post) DO NOTHING
        """,
        (id_raw_post, time_post, drop_reason),
    )


def run_pass_one(
    conn: psycopg.Connection[Any],
    *,
    name_to_id: dict[str, int],
    clean_items_by_raw_id: dict[int, dict[str, Any]],
    already_migrated: set[int],
    batch_size: int,
    limit: int | None,
    dry_run: bool,
    report: Report,
) -> tuple[dict[int, int], list[tuple[int, Any, int, float | None]]]:
    """Returns (old_clean_item_id -> new_id_clean_post map,
    deferred duplicate records for pass 2:
    [(new_id_raw_post, new_time_post, old_duplicate_of_clean_item_id, dup_score), ...])
    """

    old_clean_to_new: dict[int, int] = {}
    deferred_duplicates: list[tuple[int, Any, int, float | None]] = []

    after_id = 0
    processed = 0
    while True:
        batch = fetch_raw_items_batch(conn, after_id, batch_size)
        if not batch:
            break
        after_id = batch[-1]["id"]

        for raw_item in batch:
            if limit is not None and processed >= limit:
                break
            processed += 1

            if raw_item["id"] in already_migrated:
                report.skipped_already_migrated += 1
                continue

            if not raw_item["url"]:
                report.skipped_no_url.append(raw_item["id"])
                continue
            if not raw_item["published_at"]:
                report.skipped_no_time_post.append(raw_item["id"])
                continue

            content = raw_item["raw_text"]
            if not content:
                from agent_1.preprocess_worker import extract_text_value

                content = extract_text_value(raw_item["raw_payload"])

            source_metadata = coerce_json_object(raw_item["source_metadata"])
            name = resolve_name_source(source_metadata, raw_item["url"])
            id_source = name_to_id[name.casefold()]

            preprocess_meta = coerce_json_object(source_metadata.get("preprocess"))
            lang = preprocess_meta.get("language")
            clean_row = clean_items_by_raw_id.get(raw_item["id"])
            if lang is None and clean_row is not None:
                lang = clean_row.get("language")

            # No outer conn.transaction() here on purpose: opened while the
            # connection is idle it would BEGIN+COMMIT on its own, silently
            # committing after every single row (see module docstring).
            # These statements just run in the ambient autocommit=False
            # transaction, closed only by this loop's own conn.commit()
            # below (or never, for --dry-run, until the final rollback).
            with conn.cursor(row_factory=dict_row) as cur:
                id_raw_post, time_post = insert_raw_post(
                    cur,
                    raw_item=raw_item,
                    id_source=id_source,
                    content=content,
                    lang=lang,
                )
                report.migrated_raw += 1

                if clean_row is not None:
                    try:
                        # The connection is already non-idle (insert_raw_post
                        # just ran above), so this correctly becomes a
                        # SAVEPOINT, not another outer auto-committing
                        # transaction -- only this insert is undone on
                        # conflict, not the raw_posts row already inserted.
                        with conn.transaction():
                            new_id = insert_clean_post_kept(
                                cur,
                                id_raw_post=id_raw_post,
                                time_post=time_post,
                                clean_title=clean_row["clean_title"],
                                clean_text=clean_row["clean_text"],
                            )
                    except errors.UniqueViolation:
                        report.content_hash_collisions.append(
                            (raw_item["id"], clean_row["id"])
                        )
                    else:
                        if new_id is not None:
                            old_clean_to_new[clean_row["id"]] = new_id
                            report.kept += 1
                    continue

                status = preprocess_meta.get("status")
                if status is None:
                    report.unprocessed += 1
                elif status == "duplicate":
                    deferred_duplicates.append(
                        (
                            id_raw_post,
                            time_post,
                            preprocess_meta.get("duplicate_of_clean_item_id"),
                            preprocess_meta.get("similarity"),
                        )
                    )
                elif status == "filtered_out" and preprocess_meta.get("reason") == "non_russian_text":
                    insert_clean_post_dropped(
                        cur, id_raw_post=id_raw_post, time_post=time_post,
                        drop_reason="non_russian",
                    )
                    report.dropped_by_reason["non_russian"] = (
                        report.dropped_by_reason.get("non_russian", 0) + 1
                    )
                elif status == "filtered_out" and preprocess_meta.get("reason") == "junk_topic_regex":
                    category = preprocess_meta.get("junk_category", "unknown")
                    reason = f"junk:{category}"
                    insert_clean_post_dropped(
                        cur, id_raw_post=id_raw_post, time_post=time_post,
                        drop_reason=reason,
                    )
                    report.dropped_by_reason[reason] = (
                        report.dropped_by_reason.get(reason, 0) + 1
                    )
                elif status == "failed" and preprocess_meta.get("reason") == "empty_clean_text":
                    insert_clean_post_dropped(
                        cur, id_raw_post=id_raw_post, time_post=time_post,
                        drop_reason="empty_clean_text",
                    )
                    report.dropped_by_reason["empty_clean_text"] = (
                        report.dropped_by_reason.get("empty_clean_text", 0) + 1
                    )
                else:
                    report.anomalous_status.append((raw_item["id"], str(status)))

        if not dry_run:
            conn.commit()
        if limit is not None and processed >= limit:
            break

    return old_clean_to_new, deferred_duplicates


def run_pass_two(
    conn: psycopg.Connection[Any],
    *,
    deferred_duplicates: list[tuple[int, Any, int, float | None]],
    old_clean_to_new: dict[int, int],
    batch_size: int,
    dry_run: bool,
    report: Report,
) -> None:
    for start in range(0, len(deferred_duplicates), batch_size):
        chunk = deferred_duplicates[start : start + batch_size]
        with conn.cursor(row_factory=dict_row) as cur:
            for id_raw_post, time_post, old_canonical_id, dup_score in chunk:
                new_canonical_id = (
                    old_clean_to_new.get(old_canonical_id)
                    if old_canonical_id is not None
                    else None
                )
                if new_canonical_id is None:
                    report.duplicates_orphaned.append(id_raw_post)
                    continue
                cur.execute(
                    f"""
                    INSERT INTO {NEW_SCHEMA}.clean_posts (
                        id_raw_post, time_post, id_canonical_post, drop_reason,
                        is_duplicate, dup_score, cleaned_at
                    )
                    VALUES (%s, %s, %s, 'duplicate', TRUE, %s, now())
                    ON CONFLICT (id_raw_post) DO NOTHING
                    """,
                    (id_raw_post, time_post, new_canonical_id, dup_score),
                )
                report.duplicates_linked += 1
        if not dry_run:
            conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only the first N raw_items rows (by id) -- for a trial run.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run everything, print the report, then roll back all writes.",
    )
    args = parser.parse_args()

    report = Report()
    conn = psycopg.connect(DB_DSN, row_factory=dict_row, autocommit=False)
    try:
        already_migrated = preload_already_migrated(conn)
        clean_items_by_raw_id = preload_clean_items(conn)
        name_to_id = upsert_sources(conn, report, dry_run=args.dry_run)

        old_clean_to_new, deferred_duplicates = run_pass_one(
            conn,
            name_to_id=name_to_id,
            clean_items_by_raw_id=clean_items_by_raw_id,
            already_migrated=already_migrated,
            batch_size=args.batch_size,
            limit=args.limit,
            dry_run=args.dry_run,
            report=report,
        )
        run_pass_two(
            conn,
            deferred_duplicates=deferred_duplicates,
            old_clean_to_new=old_clean_to_new,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            report=report,
        )

        if args.dry_run:
            conn.rollback()
            print("\n[DRY RUN] all writes rolled back.")
        else:
            conn.commit()
    finally:
        conn.close()

    report.print_summary()


if __name__ == "__main__":
    main()
