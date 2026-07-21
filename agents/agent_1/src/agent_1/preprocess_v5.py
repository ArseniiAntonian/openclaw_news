"""v5 preprocessing worker: agent_1_v5.raw_posts -> agent_1_v5.clean_posts.

rework-agent-1-v5 stage 3 (tasks 3.2 / 3.4 / 3.5). Reuses every pure text/dedup
function from `preprocess_worker` (normalization, language, shingling, the
vectorized MinHash, LSH banding) and only replaces the I/O + orchestration layer
for the v5 schema. The old worker stays as a legacy reference.

Design (see openspec/changes/rework-agent-1-v5/design.md):

- **Claim = anti-join, no queue (D9).** "Pending" is a `raw_posts` row with no
  `clean_posts` row. A batch is one transaction: claim (FOR UPDATE OF raw_posts
  SKIP LOCKED, no intermediate commit) -> process in memory -> one bulk write ->
  commit. Crash before commit just rolls back and the rows get re-claimed; no
  lease/timeout. `UNIQUE(id_raw_post)` on clean_posts is the dup backstop.

- **Verdict per raw (D1).** Every processed raw gets exactly one clean_posts row:
  kept (drop_reason NULL, is_duplicate false), dropped (drop_reason set:
  non_russian / junk:<cat> / empty_clean_text), or duplicate (is_duplicate true,
  id_canonical_post set). Only kept rows carry clean_content + content_hash and
  are eligible for embedding later.

- **Two-phase write for the self-FK.** id_canonical_post references
  clean_posts.id_clean_post, which is assigned at insert time. So kept rows are
  inserted first (RETURNING id), then duplicates are inserted referencing the
  canonical's id via an id_raw_post -> id_clean_post map (existing canonicals
  carry their id from the startup cache; within-batch canonicals come from the
  RETURNING map). Same two-pass idea as the data migration.

- **Junk from DB (3.5).** Categories load from agent_1_v5.junk_categories once at
  startup and feed the shared classify_junk_topic (identical matching logic).

- **Dedup cache (D8/D10).** In-memory LSH built from existing kept clean_posts on
  startup and grown as we go. Candidate shingles are recomputed on a band hit
  rather than cached (the ~1.7 GB full cache isn't worth it; see D8). Single
  process for now -- multiprocessing (3.6) is gated on the 3.7 re-measure, since
  one core already clears the throughput target after the MinHash fix (3.3).

- **Dedup document (deviation from v1, documented).** v1 built near-dup text from
  title + vendor summary + content[:4000]. v5 has no separate summary column and
  stores a combined clean_content, so near-dup uses clean_content (title + body)
  uniformly for both new docs and the startup rebuild -- symmetric and simpler.
  Exact dedup is unchanged: sha256 of build_exact_dedup_key(title, text), the
  same summary-independent key + hash the migration wrote, enforced by the
  partial UNIQUE index on content_hash. Equivalence is validated by acceptance
  6.1 within the 0.5% tolerance.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_1 import preprocess_worker as pw  # noqa: E402

BandKey = pw.BandKey
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_BATCH_SIZE = 100
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
NEAR_DUPLICATE_THRESHOLD = pw.DEFAULT_NEAR_DUPLICATE_THRESHOLD
DEDUP_CONTENT_LIMIT = pw.DEFAULT_DEDUP_CONTENT_LIMIT
MIN_TOKEN_COUNT = pw.DEFAULT_MIN_TOKEN_COUNT

SCHEMA = "agent_1_v5"


# --------------------------------------------------------------------------- #
# Pure helpers (no DB) -- unit tested in tests/test_preprocess_v5.py
# --------------------------------------------------------------------------- #

def content_hash_for(clean_title: str | None, clean_text: str) -> str:
    """sha256 of the summary-independent exact-dedup key. Matches the hash the
    data migration wrote, so migrated and freshly-cleaned rows are comparable."""
    key = pw.build_exact_dedup_key(clean_title, clean_text)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def build_clean_content(clean_title: str | None, clean_text: str) -> str:
    return f"{clean_title}\n\n{clean_text}" if clean_title else clean_text


def dedup_document(clean_content: str) -> str:
    """Normalized near-dup document from the stored clean_content. Used
    identically for new docs and for rebuilding cache entries at startup."""
    return pw.build_dedup_document_text(
        clean_title=None,
        clean_text=clean_content,
        summary=None,
        content_limit=DEDUP_CONTENT_LIMIT,
    )


def build_junk_state_from_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[tuple[str, re.Pattern[str]]], re.Pattern[str]]:
    """Turn agent_1_v5.junk_categories rows into (junk_patterns, guard_re),
    compiled with the same boundary wrapper the code path uses."""
    junk_patterns: list[tuple[str, re.Pattern[str]]] = []
    guard_re: re.Pattern[str] | None = None
    for row in rows:
        pattern = pw.compile_pattern(list(row["patterns"]))
        if row["is_business_guard"]:
            guard_re = pattern
        else:
            junk_patterns.append((row["category"], pattern))
    if guard_re is None:
        # No guard row loaded -- fall back to the hardcoded one so we never run
        # the junk filter without its business-context safety net.
        guard_re = pw.PROTECTED_BUSINESS_CONTEXT_RE
    return junk_patterns, guard_re


@dataclass
class DedupEntry:
    id_raw_post: int
    id_clean_post: int | None  # None until the kept row is inserted this batch
    content_hash: str
    near_doc: str
    band_keys: frozenset


@dataclass
class DedupState:
    exact: dict[str, DedupEntry] = field(default_factory=dict)
    bands: dict[BandKey, list[DedupEntry]] = field(default_factory=dict)

    def add(self, entry: DedupEntry) -> None:
        self.exact.setdefault(entry.content_hash, entry)
        for band_key in entry.band_keys:
            self.bands.setdefault(band_key, []).append(entry)

    def find_exact(self, content_hash: str) -> DedupEntry | None:
        return self.exact.get(content_hash)

    def find_near(
        self, query_shingles: frozenset[int], band_keys: frozenset, threshold: float
    ) -> tuple[DedupEntry, float] | None:
        candidates: dict[int, DedupEntry] = {}
        for band_key in band_keys:
            for cand in self.bands.get(band_key, []):
                candidates[id(cand)] = cand
        best: DedupEntry | None = None
        best_score = 0.0
        for cand in candidates.values():
            cand_shingles = pw.build_shingles(cand.near_doc)  # recompute (D8)
            score = pw.jaccard_similarity(query_shingles, cand_shingles)
            if score >= threshold and score > best_score:
                best, best_score = cand, score
        return (best, best_score) if best is not None else None


@dataclass
class Verdict:
    id_raw_post: int
    time_post: Any
    drop_reason: str | None = None
    is_duplicate: bool = False
    clean_content: str | None = None
    content_hash: str | None = None
    dup_score: float | None = None
    canonical: DedupEntry | None = None  # for duplicates: the canonical entry


def compute_verdict(
    raw: dict[str, Any],
    *,
    junk_patterns: list[tuple[str, re.Pattern[str]]],
    guard_re: re.Pattern[str],
    dedup: DedupState,
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
) -> Verdict:
    """Full cleanup + dedup decision for one raw_posts row. Pure over `dedup`
    except that a kept doc is added to it so later docs in the same batch dedup
    against it (its id_clean_post is filled in after the batch insert)."""
    id_raw_post = raw["id_raw_post"]
    time_post = raw["time_post"]

    clean_title = pw.normalize_title(raw.get("title"))
    clean_text = pw.normalize_text(raw.get("content") or "")

    if not clean_text:
        return Verdict(id_raw_post, time_post, drop_reason="empty_clean_text")

    language = pw.detect_language(clean_text)
    if pw.should_filter_non_russian_text(clean_text, language):
        return Verdict(id_raw_post, time_post, drop_reason="non_russian")

    junk = pw.classify_junk_topic(
        clean_title=clean_title,
        clean_text=clean_text,
        document_type="news",
        junk_patterns=junk_patterns,
        guard_re=guard_re,
    )
    if junk is not None:
        return Verdict(
            id_raw_post, time_post, drop_reason=f"junk:{junk['category']}"
        )

    clean_content = build_clean_content(clean_title, clean_text)
    chash = content_hash_for(clean_title, clean_text)

    exact = dedup.find_exact(chash)
    if exact is not None:
        return Verdict(
            id_raw_post, time_post, drop_reason="duplicate",
            is_duplicate=True, dup_score=1.0, canonical=exact,
        )

    near_doc = dedup_document(clean_content)
    shingles = pw.build_shingles(near_doc)
    if len(near_doc.split()) >= MIN_TOKEN_COUNT and shingles:
        signature = pw.build_minhash_signature_from_shingles(shingles)
        band_keys = pw.build_lsh_band_keys(signature)
        if band_keys:
            near = dedup.find_near(shingles, band_keys, threshold)
            if near is not None:
                canonical, score = near
                return Verdict(
                    id_raw_post, time_post, drop_reason="duplicate",
                    is_duplicate=True, dup_score=round(score, 3), canonical=canonical,
                )
            entry = DedupEntry(
                id_raw_post=id_raw_post, id_clean_post=None,
                content_hash=chash, near_doc=near_doc, band_keys=band_keys,
            )
            dedup.add(entry)
            return Verdict(
                id_raw_post, time_post, clean_content=clean_content,
                content_hash=chash,
            )

    # Kept but too short for near-dup indexing: still register for exact dedup.
    entry = DedupEntry(
        id_raw_post=id_raw_post, id_clean_post=None,
        content_hash=chash, near_doc=near_doc, band_keys=frozenset(),
    )
    dedup.add(entry)
    return Verdict(
        id_raw_post, time_post, clean_content=clean_content, content_hash=chash,
    )


def process_batch(
    rows: list[dict[str, Any]],
    *,
    junk_patterns: list[tuple[str, re.Pattern[str]]],
    guard_re: re.Pattern[str],
    dedup: DedupState,
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
) -> list[Verdict]:
    return [
        compute_verdict(
            raw, junk_patterns=junk_patterns, guard_re=guard_re,
            dedup=dedup, threshold=threshold,
        )
        for raw in rows
    ]


# --------------------------------------------------------------------------- #
# DB layer
# --------------------------------------------------------------------------- #

def load_junk_state(conn: psycopg.Connection[Any]):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT category, patterns, is_business_guard
            FROM {SCHEMA}.junk_categories
            WHERE is_active
            ORDER BY category
            """
        )
        rows = cur.fetchall()
    return build_junk_state_from_rows(rows)


def load_dedup_state(conn: psycopg.Connection[Any]) -> DedupState:
    """Rebuild the in-memory dedup index from existing kept clean_posts. Joins
    raw_posts for the title so the near-dup document matches new-doc processing."""
    dedup = DedupState()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT c.id_clean_post, c.id_raw_post, c.content_hash, c.clean_content
            FROM {SCHEMA}.clean_posts c
            WHERE c.drop_reason IS NULL AND c.is_duplicate = FALSE
            ORDER BY c.id_clean_post
            """
        )
        for row in cur.fetchall():
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


def claim_batch(
    conn: psycopg.Connection[Any], batch_size: int
) -> list[dict[str, Any]]:
    """Anti-join claim. Locks the raw rows; the lock is released by the caller's
    commit after the clean_posts write. No commit here -- claim and write share
    one transaction (D9)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT r.id_raw_post, r.time_post, r.title, r.content
            FROM {SCHEMA}.raw_posts r
            WHERE NOT EXISTS (
                SELECT 1 FROM {SCHEMA}.clean_posts c
                WHERE c.id_raw_post = r.id_raw_post
            )
            ORDER BY r.id_raw_post
            FOR UPDATE OF r SKIP LOCKED
            LIMIT %s
            """,
            (batch_size,),
        )
        return cur.fetchall()


def _bulk_insert(cur, columns: list[str], rows: list[tuple], *, returning: str | None):
    placeholder = "(" + ",".join(["%s"] * len(columns)) + ")"
    values_sql = ",".join([placeholder] * len(rows))
    flat: list[Any] = [value for row in rows for value in row]
    tail = f" RETURNING {returning}" if returning else ""
    cur.execute(
        f"INSERT INTO {SCHEMA}.clean_posts ({','.join(columns)}) VALUES {values_sql}{tail}",
        flat,
    )
    return cur.fetchall() if returning else None


def write_verdicts(cur, verdicts: list[Verdict]) -> dict[int, int]:
    """Two-phase write. Returns id_raw_post -> id_clean_post for kept rows."""
    kept = [v for v in verdicts if v.drop_reason is None and not v.is_duplicate]
    dropped = [v for v in verdicts if v.drop_reason is not None and not v.is_duplicate]
    dups = [v for v in verdicts if v.is_duplicate]

    kept_ids: dict[int, int] = {}
    if kept:
        returned = _bulk_insert(
            cur,
            ["id_raw_post", "time_post", "clean_content", "content_hash"],
            [(v.id_raw_post, v.time_post, v.clean_content, v.content_hash) for v in kept],
            returning="id_raw_post, id_clean_post",
        )
        for row in returned:
            kept_ids[row[0]] = row[1]

    if dropped:
        _bulk_insert(
            cur,
            ["id_raw_post", "time_post", "drop_reason"],
            [(v.id_raw_post, v.time_post, v.drop_reason) for v in dropped],
            returning=None,
        )

    if dups:
        rows = []
        for v in dups:
            canonical = v.canonical
            canon_id = (
                canonical.id_clean_post
                if canonical is not None and canonical.id_clean_post is not None
                else kept_ids.get(canonical.id_raw_post) if canonical else None
            )
            rows.append(
                (v.id_raw_post, v.time_post, canon_id, "duplicate", True, v.dup_score)
            )
        _bulk_insert(
            cur,
            ["id_raw_post", "time_post", "id_canonical_post", "drop_reason",
             "is_duplicate", "dup_score"],
            rows,
            returning=None,
        )

    return kept_ids


def finalize_dedup_ids(dedup: DedupState, kept_ids: dict[int, int]) -> None:
    """After the kept rows are inserted, backfill their id_clean_post in the
    in-memory cache so later batches can reference them as canonicals."""
    for entry in dedup.exact.values():
        if entry.id_clean_post is None and entry.id_raw_post in kept_ids:
            entry.id_clean_post = kept_ids[entry.id_raw_post]


def utc_now_text() -> str:
    return pw.utc_now_text()


def run(
    conn: psycopg.Connection[Any],
    *,
    batch_size: int,
    once: bool,
    poll_interval: float,
    max_docs: int | None,
    log,
) -> int:
    junk_patterns, guard_re = load_junk_state(conn)
    log(f"Loaded {len(junk_patterns)} junk categories + business guard.")
    dedup = load_dedup_state(conn)
    log(f"Loaded {len(dedup.exact)} existing clean_posts into the dedup cache.")

    processed = 0
    while True:
        batch = claim_batch(conn, batch_size)
        if not batch:
            conn.rollback()  # release the (empty) transaction
            if once:
                break
            time.sleep(poll_interval)
            continue

        verdicts = process_batch(
            batch, junk_patterns=junk_patterns, guard_re=guard_re, dedup=dedup
        )
        with conn.cursor() as cur:
            kept_ids = write_verdicts(cur, verdicts)
        conn.commit()  # releases the FOR UPDATE locks
        finalize_dedup_ids(dedup, kept_ids)

        kept = sum(1 for v in verdicts if v.drop_reason is None and not v.is_duplicate)
        dups = sum(1 for v in verdicts if v.is_duplicate)
        dropped = len(verdicts) - kept - dups
        processed += len(verdicts)
        log(f"batch={len(verdicts)} kept={kept} dropped={dropped} dup={dups} total={processed}")

        if max_docs is not None and processed >= max_docs:
            log(f"Reached max_docs={max_docs}.")
            break
        if once:
            break
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="v5 preprocessing worker")
    ap.add_argument("--once", action="store_true", help="process one batch and exit")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    ap.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    ap.add_argument("--max-docs", type=int, default=None)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    def log(message: str) -> None:
        print(f"{utc_now_text()} INFO {message}")

    conn = psycopg.connect(DB_DSN, autocommit=False)
    try:
        log(
            f"Starting v5 preprocess worker batch_size={args.batch_size} "
            f"once={args.once} threshold={NEAR_DUPLICATE_THRESHOLD}"
        )
        return run(
            conn,
            batch_size=args.batch_size,
            once=args.once,
            poll_interval=args.poll_interval,
            max_docs=args.max_docs,
            log=log,
        )
    finally:
        conn.close()
        print(f"{utc_now_text()} INFO v5 preprocess worker stopped.")


pw.load_dotenv(ENV_PATH)
DB_DSN = os.environ.get("AGENT_1_DB_DSN", "")


if __name__ == "__main__":
    raise SystemExit(main())