"""rework-agent-1-v5 task 6.1: equivalence check, v5 vs v1 preprocessing.

Read-only against agent_1_v5 -- does NOT write anything to the database.

Context: the v1 worker only ever rendered a verdict for 1233 of the 57000
raw documents (the rest were an untouched backlog, per the stage-1 data
migration report). Those 1233 clean_posts rows came from migrate_v5.py
copying the historical v1 verdict verbatim; the anti-join backfill (stage 3)
skipped them (they already had a clean_posts row), so v5's own logic was
never actually exercised on them. There is no v1 verdict at all for the
other 55767 -- a full-corpus "v5 vs v1" comparison as originally scoped in
design.md isn't literally possible; the only apples-to-apples data is these
1233.

This script identifies that set (via the cleaned_at gap between the
migration run and the backfill run -- see MIGRATION_CUTOFF below), then
recomputes each one's verdict with the SAME pure logic the real worker uses
(preprocess_v5.compute_verdict), fed a dedup cache built from everything
EXCEPT these 1233 (so a document isn't just "matching its own prior self").
No row is deleted or written; the comparison is entirely in memory.

Usage:
    PYTHONPATH=src python scripts/check_v1_equivalence.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_1 import preprocess_worker as pw  # noqa: E402
from agent_1.preprocess_v5 import (  # noqa: E402
    MIN_TOKEN_COUNT,
    DedupEntry,
    DedupState,
    build_junk_state_from_rows,
    compute_verdict,
    content_hash_for,
    dedup_document,
)

SCHEMA = "agent_1_v5"
# The stage-1 migration wrote cleaned_at as its own run time (2026-07-20); the
# stage-3 backfill ran 2026-07-22. Anything strictly before this cutoff is a
# migrated v1 verdict, not a v5-computed one.
MIGRATION_CUTOFF = "2026-07-21"

pw.load_dotenv(Path(__file__).resolve().parents[1] / ".env")
DB_DSN = os.environ.get("AGENT_1_DB_DSN", "")


def fetch_baseline(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    """The 1233 clean_posts rows migrated from v1, with their v1-era verdict."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT id_raw_post, drop_reason, is_duplicate
            FROM {SCHEMA}.clean_posts
            WHERE cleaned_at < %s::timestamptz
            ORDER BY id_raw_post
            """,
            (MIGRATION_CUTOFF,),
        )
        return cur.fetchall()


def fetch_raw_rows(conn: psycopg.Connection[Any], ids: list[int]) -> dict[int, dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT id_raw_post, time_post, title, content
            FROM {SCHEMA}.raw_posts
            WHERE id_raw_post = ANY(%s)
            ORDER BY id_raw_post
            """,
            (ids,),
        )
        return {row["id_raw_post"]: row for row in cur.fetchall()}


def load_dedup_state_excluding(
    conn: psycopg.Connection[Any], excluded_ids: set[int]
) -> DedupState:
    """Same shape as preprocess_v5.load_dedup_state, minus the baseline set --
    so a baseline doc doesn't just match its own already-cached self."""
    dedup = DedupState()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT id_clean_post, id_raw_post, content_hash, clean_content
            FROM {SCHEMA}.clean_posts
            WHERE drop_reason IS NULL AND is_duplicate = FALSE
            ORDER BY id_clean_post
            """
        )
        for row in cur.fetchall():
            if row["id_raw_post"] in excluded_ids:
                continue
            clean_content = row["clean_content"] or ""
            near_doc = dedup_document(clean_content)
            shingles = pw.build_shingles(near_doc)
            band_keys: frozenset = frozenset()
            if len(near_doc.split()) >= MIN_TOKEN_COUNT and shingles:
                signature = pw.build_minhash_signature_from_shingles(shingles)
                band_keys = pw.build_lsh_band_keys(signature)
            chash = row["content_hash"] or content_hash_for(None, clean_content)
            dedup.add(
                DedupEntry(
                    id_raw_post=row["id_raw_post"],
                    id_clean_post=row["id_clean_post"],
                    content_hash=chash,
                    near_doc=near_doc,
                    band_keys=band_keys,
                )
            )
    return dedup


def category(drop_reason: str | None, is_duplicate: bool) -> str:
    if is_duplicate:
        return "duplicate"
    if drop_reason is None:
        return "kept"
    if drop_reason.startswith("junk:"):
        return "junk"
    return drop_reason


def main() -> int:
    conn = psycopg.connect(DB_DSN, row_factory=dict_row, autocommit=False)
    try:
        baseline = fetch_baseline(conn)
        print(f"Baseline (migrated v1 verdicts): {len(baseline)} rows "
              f"(expected ~1233; if this is far off, stop and check MIGRATION_CUTOFF).")
        baseline_by_id = {row["id_raw_post"]: row for row in baseline}
        ids = list(baseline_by_id.keys())

        raws = fetch_raw_rows(conn, ids)
        junk_patterns, guard_re = build_junk_state_from_rows(_load_junk_rows(conn))
        dedup = load_dedup_state_excluding(conn, set(ids))
        print(f"Dedup cache (excluding baseline): {len(dedup.exact)} entries.")

        mismatches = []
        matches = 0
        for raw_id in ids:
            raw = raws.get(raw_id)
            if raw is None:
                mismatches.append((raw_id, "MISSING_RAW", None, None))
                continue
            verdict = compute_verdict(raw, junk_patterns=junk_patterns, guard_re=guard_re, dedup=dedup)
            v5_cat = category(verdict.drop_reason, verdict.is_duplicate)
            v1_row = baseline_by_id[raw_id]
            v1_cat = category(v1_row["drop_reason"], v1_row["is_duplicate"])
            if v5_cat == v1_cat:
                matches += 1
            else:
                mismatches.append((raw_id, v1_cat, v5_cat, verdict.drop_reason))

        total = len(ids)
        rate = len(mismatches) / total * 100 if total else 0.0
        print(f"\nAgreement: {matches}/{total} ({100 - rate:.2f}%)")
        print(f"Mismatches: {len(mismatches)} ({rate:.2f}%) -- tolerance is <=0.5%")
        if mismatches:
            print("\nid_raw_post | v1_verdict -> v5_verdict (v5_drop_reason)")
            for raw_id, v1_cat, v5_cat, v5_reason in mismatches[:50]:
                print(f"  {raw_id:>10} | {v1_cat} -> {v5_cat} ({v5_reason})")
            if len(mismatches) > 50:
                print(f"  ... and {len(mismatches) - 50} more")

        conn.rollback()  # read-only; nothing was written, but be explicit
        return 0 if rate <= 0.5 else 1
    finally:
        conn.close()


def _load_junk_rows(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT category, patterns, is_business_guard "
            f"FROM {SCHEMA}.junk_categories WHERE is_active ORDER BY category"
        )
        return cur.fetchall()


if __name__ == "__main__":
    raise SystemExit(main())