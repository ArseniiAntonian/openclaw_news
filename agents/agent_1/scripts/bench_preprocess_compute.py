"""Read-only compute profile for the preprocessing hot path.

rework-agent-1-v5 task 3.1 (the "до" baseline). Pulls N real documents from
agent_1_v5.raw_posts and times the pure-compute pipeline the preprocess worker
runs per document -- normalize -> language -> junk -> shingles -> MinHash
signature -- with NO writes and NO per-document DB round-trips. Purpose:

- confirm the ~22 ms/doc MinHash-signature cost on the REAL corpus rather than
  synthetic text (the local synthetic run gave ~250 ms/doc; the real number
  decides whether the D5 scheme change is load-bearing);
- get the shingle-count distribution to size the D8 shingle-cache memory
  decision (cache all shingles vs bounded);
- show the cProfile breakdown of where compute time actually goes.

This profiles the CURRENT (v1) functions as-is, unchanged -- that is the point
of a "before" baseline. It reads real text out of the already-migrated
agent_1_v5.raw_posts; it does not touch the agent_1 schema and writes nothing.

The end-to-end I/O-vs-compute split (the 94%-I/O claim) is measured separately
by running the existing worker under cProfile -- see the instructions that ship
with this task, not this script. This one is compute-only, read-only.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import os
import pstats
import statistics
import sys
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_1 import preprocess_worker as pw  # noqa: E402

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
pw.load_dotenv(ENV_PATH)
DB_DSN = os.environ["AGENT_1_DB_DSN"]


def fetch_docs(conn: psycopg.Connection, n: int) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT title, content
            FROM agent_1_v5.raw_posts
            WHERE content IS NOT NULL AND btrim(content) <> ''
            ORDER BY id_raw_post
            LIMIT %s
            """,
            (n,),
        )
        return cur.fetchall()


def compute_pipeline(title, content) -> tuple[str, frozenset[int], tuple[int, ...]]:
    """Mirror preprocess_text + the news near-dup compute path. No DB, no writes.
    summary is passed as None (a timing benchmark; the extra summary text is
    negligible and raw_posts keeps it in metadata, not a column)."""
    clean_title = pw.normalize_title(title)
    clean_text = pw.normalize_text(content or "")
    pw.detect_language(clean_text)
    pw.classify_junk_topic(
        clean_title=clean_title, clean_text=clean_text, document_type="news"
    )
    dedup_text = pw.build_dedup_document_text(
        clean_title=clean_title, clean_text=clean_text, summary=None
    )
    shingles = pw.build_shingles(dedup_text)
    signature = pw.build_minhash_signature_from_shingles(shingles)
    pw.build_lsh_band_keys(signature)
    return dedup_text, shingles, signature


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--reps", type=int, default=5, help="repeats for micro-benchmarks")
    args = ap.parse_args()

    conn = psycopg.connect(DB_DSN, row_factory=dict_row)
    try:
        docs = fetch_docs(conn, args.limit)
    finally:
        conn.close()
    print(f"Loaded {len(docs)} docs from agent_1_v5.raw_posts.\n")
    if not docs:
        print("No documents found -- nothing to profile.")
        return

    # Full compute pipeline, once per doc, timed end to end.
    t0 = time.perf_counter()
    precomputed: list[tuple[str, frozenset[int]]] = []
    shingle_counts: list[int] = []
    for d in docs:
        dedup_text, shingles, _sig = compute_pipeline(d["title"], d["content"])
        precomputed.append((dedup_text, shingles))
        shingle_counts.append(len(shingles))
    t1 = time.perf_counter()
    print(f"Full compute pipeline : {(t1 - t0) / len(docs) * 1000:8.3f} ms/doc")

    # Isolated MinHash signature (shingles precomputed) -- verify the ~22 ms claim.
    t0 = time.perf_counter()
    for _ in range(args.reps):
        for _text, shingles in precomputed:
            pw.build_minhash_signature_from_shingles(shingles)
    t1 = time.perf_counter()
    print(f"MinHash signature only: {(t1 - t0) / args.reps / len(docs) * 1000:8.3f} ms/doc")

    # Isolated shingle construction (informs D8: how much near-dup recompute costs).
    t0 = time.perf_counter()
    for _ in range(args.reps):
        for text, _shingles in precomputed:
            pw.build_shingles(text)
    t1 = time.perf_counter()
    print(f"build_shingles only   : {(t1 - t0) / args.reps / len(docs) * 1000:8.3f} ms/doc")

    # Shingle-count distribution -> D8 memory sizing.
    sc = sorted(shingle_counts)
    mean = statistics.mean(sc)
    med = statistics.median(sc)
    print(
        f"\nshingles/doc: min={sc[0]} median={med} mean={mean:.0f} max={sc[-1]}"
    )
    # Rough lower bound: mean int64 count * 8 bytes * 50k news docs. Real Python
    # frozenset overhead is ~2-3x this, so treat it as a floor.
    est_gb = mean * 8 * 50_000 / (1024**3)
    print(
        f"D8 shingle-cache floor @50k news docs: ~{est_gb:.2f} GB of raw int64s "
        f"(frozenset overhead ~2-3x -> real ~{est_gb * 2.5:.1f} GB)"
    )

    # cProfile of the full pipeline, sorted by self-time.
    print("\n=== cProfile (tottime, top 15) ===")
    pr = cProfile.Profile()
    pr.enable()
    for d in docs:
        compute_pipeline(d["title"], d["content"])
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(15)
    print(s.getvalue())


if __name__ == "__main__":
    main()