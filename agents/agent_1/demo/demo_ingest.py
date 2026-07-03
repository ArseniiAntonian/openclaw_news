from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import requests
import urllib3
from psycopg.rows import dict_row

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_1.parsers360_ingest import (  # noqa: E402
    API_URL,
    AUTH_PASSWORD,
    AUTH_USER,
    LIMIT,
    REQUEST_RETRIES,
    REQUEST_TIMEOUT_SECONDS,
    RETRY_SLEEP_SECONDS,
    TOKEN,
    VERIFY_SSL,
    load_dotenv,
)
from agent_1.preprocess_worker import (  # noqa: E402
    NearDuplicateCache,
    build_dedup_document_text,
    build_lsh_band_keys,
    build_minhash_signature,
    build_near_duplicate_entry,
    extract_text_value,
    index_near_duplicate_entry,
    normalize_text,
    normalize_title,
    signature_similarity,
)


ENV_PATH = ROOT_DIR / ".env"
DB_DSN_ENV = "AGENT_1_DB_DSN"
DEMO_SCHEMA_SQL = Path(__file__).with_name("demo_schema.sql")
DEFAULT_START = "2026-06-01"
DEFAULT_END = "2026-06-30"
NEAR_DUPLICATE_THRESHOLD = 0.7


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Freeze June 2026 demo corpus into schema demo.")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--reset", action="store_true", help="Truncate demo tables before refill.")
    parser.add_argument(
        "--source-mode",
        choices=("agent1_raw_mirror", "api_interval"),
        default="agent1_raw_mirror",
        help="Where demo.raw_items should be seeded from. Default: read-only mirror from agent_1.raw_items.",
    )
    return parser.parse_args(argv)


def connect() -> psycopg.Connection[Any]:
    load_dotenv(ENV_PATH)
    return psycopg.connect(os.environ[DB_DSN_ENV], row_factory=dict_row)


def ensure_schema(conn: psycopg.Connection[Any]) -> None:
    with DEMO_SCHEMA_SQL.open("r", encoding="utf-8") as handle:
        sql = handle.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def maybe_reset(conn: psycopg.Connection[Any], reset: bool) -> None:
    if not reset:
        return
    with conn.cursor() as cur:
        cur.execute("TRUNCATE demo.doc_labels, demo.kr, demo.clean_items, demo.raw_items RESTART IDENTITY CASCADE")
    conn.commit()


def parse_created_at(item: dict[str, Any]) -> datetime | None:
    raw = item.get("created_at")
    if raw in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except Exception:
        return None


def normalize_source(item: dict[str, Any]) -> str:
    source = item.get("source")
    if isinstance(source, str) and source.strip():
        return source.strip()
    return "unknown"


def interval_text(day: str) -> str:
    day_text = datetime.fromisoformat(day).strftime("%d-%m-%Y")
    return f"{day_text}to{day_text}"


def fetch_interval_page(page: int, day: str) -> list[dict[str, Any]]:
    if not VERIFY_SSL:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    last_exc: Exception | None = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = requests.post(
                API_URL,
                params={
                    "service": "parser",
                    "limit": LIMIT,
                    "page": page,
                    "summary": "true",
                    "company": "true",
                    "token": TOKEN,
                    "interval": interval_text(day),
                },
                auth=(AUTH_USER, AUTH_PASSWORD),
                headers={"accept": "application/json"},
                verify=VERIFY_SSL,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, list):
                raise RuntimeError("unexpected Parsers360 payload type")
            return payload
        except Exception as exc:
            last_exc = exc
            if attempt >= REQUEST_RETRIES:
                break
            print(
                f"warn: day={day} page={page} attempt={attempt}/{REQUEST_RETRIES} error={exc}",
                flush=True,
            )
            time.sleep(RETRY_SLEEP_SECONDS)
    if last_exc is None:
        raise RuntimeError("unknown Parsers360 interval fetch failure")
    raise last_exc


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def insert_raw_item(conn: psycopg.Connection[Any], item: dict[str, Any]) -> bool:
    created_at = parse_created_at(item)
    content = normalize_text(extract_text_value(item.get("content")))
    url = (item.get("url") or "").strip()
    if not created_at or not url or not content:
        return False

    title = normalize_title(item.get("title"))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO demo.raw_items (
                url, title, created_at, source, content, content_hash, raw_payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (url, created_at) DO NOTHING
            """,
            (
                url,
                title,
                created_at,
                normalize_source(item),
                content,
                content_hash(content),
                json.dumps(item, ensure_ascii=False),
            ),
        )
        inserted = cur.rowcount > 0
    conn.commit()
    return inserted


def fetch_june_pages(start_at: str, end_at: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    start_day = datetime.fromisoformat(start_at).date()
    end_day = datetime.fromisoformat(end_at).date()
    day = start_day
    while day <= end_day:
        day_text = day.isoformat()
        page = 1
        day_total = 0
        while True:
            payload = fetch_interval_page(page, day_text)
            if not payload:
                break
            for item in payload:
                created_at = parse_created_at(item)
                if created_at is None or created_at.date().isoformat() != day_text:
                    continue
                items.append(item)
                day_total += 1
            print(f"progress: day={day_text} page={page} received={len(payload)} kept={day_total}", flush=True)
            if len(payload) < LIMIT:
                break
            page += 1
        day = day.fromordinal(day.toordinal() + 1)
    return items


def copy_from_agent1_raw(
    conn: psycopg.Connection[Any],
    *,
    start_at: str,
    end_at: str,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO demo.raw_items (
                url, title, created_at, source, content, content_hash, raw_payload
            )
            SELECT
                raw.url,
                raw.title,
                raw.published_at,
                COALESCE(raw.source_metadata ->> 'source', raw.source, 'unknown'),
                raw.raw_text,
                md5(raw.raw_text),
                raw.raw_payload
            FROM agent_1.raw_items AS raw
            WHERE raw.published_at >= %s::timestamptz
              AND raw.published_at < (%s::date + INTERVAL '1 day')
              AND raw.url IS NOT NULL
              AND raw.raw_text IS NOT NULL
              AND btrim(raw.raw_text) <> ''
            ON CONFLICT (url, created_at) DO NOTHING
            """,
            (start_at, end_at),
        )
        inserted = cur.rowcount
    conn.commit()
    return inserted


def exact_duplicate_map(conn: psycopg.Connection[Any]) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT content_hash, min(id) AS keep_raw_id
            FROM demo.raw_items
            GROUP BY content_hash
            """
        )
        return {row["content_hash"]: row["keep_raw_id"] for row in cur.fetchall()}


def ensure_clean_row(
    conn: psycopg.Connection[Any],
    *,
    raw_id: int,
    url: str,
    title: str | None,
    created_at: datetime,
    source: str,
    content: str,
    is_duplicate: bool,
    dup_of: int | None,
    duplicate_kind: str | None,
    duplicate_similarity: float | None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO demo.clean_items (
                raw_id, url, title, created_at, source, content,
                is_duplicate, dup_of, duplicate_kind, duplicate_similarity
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (raw_id) DO UPDATE SET
                url = EXCLUDED.url,
                title = EXCLUDED.title,
                created_at = EXCLUDED.created_at,
                source = EXCLUDED.source,
                content = EXCLUDED.content,
                is_duplicate = EXCLUDED.is_duplicate,
                dup_of = EXCLUDED.dup_of,
                duplicate_kind = EXCLUDED.duplicate_kind,
                duplicate_similarity = EXCLUDED.duplicate_similarity
            RETURNING id
            """,
            (
                raw_id,
                url,
                title,
                created_at,
                source,
                content,
                is_duplicate,
                dup_of,
                duplicate_kind,
                duplicate_similarity,
            ),
        )
        row = cur.fetchone()
    return int(row["id"])


def load_demo_near_duplicate_cache(conn: psycopg.Connection[Any]) -> None:
    import agent_1.preprocess_worker as preprocess_worker  # noqa: E402

    preprocess_worker.near_duplicate_signature_cache = None

    def _load_cache(_: psycopg.Connection[Any]):
        cache = preprocess_worker.NearDuplicateCache(entries=[], bands={}, exact_text_index={})
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id AS clean_item_id, raw_id AS raw_item_id, title, content
                FROM demo.clean_items
                WHERE is_duplicate IS FALSE
                ORDER BY id
                """
            )
            for row in cur.fetchall():
                entry = preprocess_worker.build_near_duplicate_entry(
                    clean_item_id=row["clean_item_id"],
                    raw_item_id=row["raw_item_id"],
                    clean_title=row["title"],
                    clean_text=row["content"],
                    summary=None,
                )
                if entry is not None:
                    preprocess_worker.index_near_duplicate_entry(cache, entry)
        preprocess_worker.near_duplicate_signature_cache = cache
        return cache

    preprocess_worker.load_near_duplicate_signature_cache = _load_cache  # type: ignore[assignment]


def freeze_clean_items(conn: psycopg.Connection[Any]) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE demo.clean_items RESTART IDENTITY CASCADE")
    conn.commit()

    exact_map = exact_duplicate_map(conn)
    counters = {"raw": 0, "kept": 0, "exact": 0, "near": 0}
    rows_to_insert: list[tuple[Any, ...]] = []
    keep_entries: list[dict[str, Any]] = []
    cache = NearDuplicateCache(entries=[], bands={}, exact_text_index={})

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, url, title, created_at, source, content, content_hash
            FROM demo.raw_items
            ORDER BY created_at, id
            """
        )
        raw_rows = cur.fetchall()

    for row in raw_rows:
        counters["raw"] += 1
        keep_raw_id = exact_map[row["content_hash"]]
        if keep_raw_id != row["id"]:
            rows_to_insert.append(
                (
                    row["id"],
                    row["url"],
                    row["title"],
                    row["created_at"],
                    row["source"],
                    row["content"],
                    True,
                    keep_raw_id,
                    "exact",
                    1.0,
                )
            )
            counters["exact"] += 1
            if counters["raw"] % 1000 == 0:
                print(
                    f"dedup: processed={counters['raw']} kept={counters['kept']} exact={counters['exact']} near={counters['near']}",
                    flush=True,
                )
            continue

        normalized_text = build_dedup_document_text(
            clean_title=row["title"],
            clean_text=row["content"],
            summary=None,
        )
        signature = tuple()
        band_keys = frozenset()
        near_match: dict[str, Any] | None = None
        if len(normalized_text.split()) >= 8:
            signature = build_minhash_signature(normalized_text)
            band_keys = build_lsh_band_keys(signature)
            candidates: dict[int, Any] = {}
            for band_key in band_keys:
                for candidate in cache.bands.get(band_key, []):
                    candidates[candidate.raw_item_id] = candidate
            for candidate in candidates.values():
                if not candidate.signature:
                    continue
                similarity = signature_similarity(signature, candidate.signature)
                if similarity >= NEAR_DUPLICATE_THRESHOLD and (
                    near_match is None or similarity > near_match["similarity"]
                ):
                    near_match = {
                        "raw_id": candidate.raw_item_id,
                        "similarity": similarity,
                    }
        if near_match is not None:
            rows_to_insert.append(
                (
                    row["id"],
                    row["url"],
                    row["title"],
                    row["created_at"],
                    row["source"],
                    row["content"],
                    True,
                    near_match["raw_id"],
                    "near",
                    float(near_match["similarity"]),
                )
            )
            counters["near"] += 1
            if counters["raw"] % 1000 == 0:
                print(
                    f"dedup: processed={counters['raw']} kept={counters['kept']} exact={counters['exact']} near={counters['near']}",
                    flush=True,
                )
            continue

        rows_to_insert.append(
            (
                row["id"],
                row["url"],
                row["title"],
                row["created_at"],
                row["source"],
                row["content"],
                False,
                None,
                None,
                None,
            )
        )
        entry = build_near_duplicate_entry(
            clean_item_id=-(counters["kept"] + 1),
            raw_item_id=row["id"],
            clean_title=row["title"],
            clean_text=row["content"],
            summary=None,
        )
        if entry is not None:
            index_near_duplicate_entry(cache, entry)
        counters["kept"] += 1
        if counters["raw"] % 1000 == 0:
            print(
                f"dedup: processed={counters['raw']} kept={counters['kept']} exact={counters['exact']} near={counters['near']}",
                flush=True,
            )

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO demo.clean_items (
                raw_id, url, title, created_at, source, content,
                is_duplicate, dup_of, duplicate_kind, duplicate_similarity
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows_to_insert,
        )
        cur.execute(
            """
            WITH keep_map AS (
                SELECT raw_id, id
                FROM demo.clean_items
                WHERE is_duplicate IS FALSE
            )
            UPDATE demo.clean_items AS dup
            SET dup_of = keep_map.id
            FROM keep_map
            WHERE dup.is_duplicate IS TRUE
              AND dup.dup_of = keep_map.raw_id
            """
        )
    conn.commit()
    return counters


def print_freeze_report(conn: psycopg.Connection[Any], counters: dict[str, int]) -> None:
    print("== DEMO FREEZE REPORT ==")
    print(
        "raw={raw} kept={kept} removed={removed} exact={exact} near={near}".format(
            raw=counters["raw"],
            kept=counters["kept"],
            removed=counters["exact"] + counters["near"],
            exact=counters["exact"],
            near=counters["near"],
        )
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source, count(*) AS total,
                   count(*) FILTER (WHERE is_duplicate IS FALSE) AS kept
            FROM demo.clean_items
            GROUP BY source
            ORDER BY kept DESC, source
            """
        )
        print("-- by source --")
        for row in cur.fetchall():
            print(f"{row['source']}: kept={row['kept']} total={row['total']}")

        cur.execute(
            """
            SELECT created_at::date AS day, count(*) AS total,
                   count(*) FILTER (WHERE is_duplicate IS FALSE) AS kept
            FROM demo.clean_items
            GROUP BY created_at::date
            ORDER BY day
            """
        )
        print("-- by day --")
        for row in cur.fetchall():
            print(f"{row['day']}: kept={row['kept']} total={row['total']}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    conn = connect()
    try:
        ensure_schema(conn)
        maybe_reset(conn, args.reset)
        inserted = 0
        fetched_count: int | None = None
        if args.source_mode == "agent1_raw_mirror":
            inserted = copy_from_agent1_raw(conn, start_at=args.start, end_at=args.end)
        else:
            items = fetch_june_pages(args.start, args.end)
            fetched_count = len(items)
            for item in items:
                if insert_raw_item(conn, item):
                    inserted += 1
        counters = freeze_clean_items(conn)
        if fetched_count is not None:
            print(f"fetched_in_range={fetched_count} inserted_new={inserted}")
        else:
            print(f"source_mode={args.source_mode} inserted_new={inserted}")
        print_freeze_report(conn, counters)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
