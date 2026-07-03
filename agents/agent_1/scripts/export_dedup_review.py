from __future__ import annotations

import argparse
import csv
import gzip
import os
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_1.preprocess_worker import ENV_PATH, load_dotenv, preprocess_text


DEFAULT_OUTPUT_DIR = Path("/root/.openclaw/workspace/out")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export duplicate-vs-canonical news pairs for manual dedup review."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Where to write export files. Default: {DEFAULT_OUTPUT_DIR}.",
    )
    parser.add_argument(
        "--basename",
        default="agent_1_dedup_review_2026-07-03",
        help="Basename without extension.",
    )
    parser.add_argument(
        "--duplicate-kind",
        choices=("all", "exact", "near"),
        default="all",
        help="Filter exported duplicates by kind. Default: all.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max number of duplicate rows to export.",
    )
    return parser.parse_args()


def extract_summary(source_metadata: Any) -> str:
    if isinstance(source_metadata, dict):
        summary = source_metadata.get("summary")
        if isinstance(summary, str):
            return summary
    return ""


def maybe_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return ""


def gzip_file(path: Path) -> Path:
    gz_path = path.with_suffix(path.suffix + ".gz")
    with path.open("rb") as source_handle, gzip.open(gz_path, "wb") as target_handle:
        target_handle.writelines(source_handle)
    return gz_path


def fetch_duplicate_pairs(
    conn: psycopg.Connection[Any],
    *,
    duplicate_kind: str,
    limit: int | None,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ["dup.source_metadata->'preprocess'->>'status' = 'duplicate'"]
    if duplicate_kind != "all":
        where.append("dup.source_metadata->'preprocess'->>'duplicate_kind' = %s")
        params.append(duplicate_kind)

    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT %s"
        params.append(limit)

    sql = f"""
        SELECT
            dup.id AS duplicate_raw_item_id,
            dup.external_id AS duplicate_external_id,
            dup.published_at AS duplicate_published_at,
            dup.source AS duplicate_source,
            dup.url AS duplicate_url,
            dup.title AS duplicate_title,
            dup.raw_text AS duplicate_raw_text,
            dup.raw_payload AS duplicate_raw_payload,
            dup.source_metadata AS duplicate_source_metadata,
            dup.source_metadata->'preprocess'->>'duplicate_kind' AS duplicate_kind,
            NULLIF(dup.source_metadata->'preprocess'->>'similarity', '')::numeric AS similarity,
            NULLIF(
                dup.source_metadata->'preprocess'->>'minhash_similarity',
                ''
            )::numeric AS minhash_similarity,
            (dup.source_metadata->'preprocess'->>'duplicate_of_raw_item_id')::bigint
                AS canonical_raw_item_id,
            (dup.source_metadata->'preprocess'->>'duplicate_of_clean_item_id')::bigint
                AS canonical_clean_item_id,
            canon_raw.external_id AS canonical_external_id,
            canon_raw.published_at AS canonical_published_at,
            canon_raw.source AS canonical_source,
            canon_raw.url AS canonical_url,
            canon_raw.title AS canonical_title,
            canon_raw.raw_text AS canonical_raw_text,
            canon_raw.source_metadata AS canonical_source_metadata,
            canon_clean.clean_title AS canonical_clean_title,
            canon_clean.clean_text AS canonical_clean_text,
            canon_clean.language AS canonical_language
        FROM agent_1.raw_items AS dup
        LEFT JOIN agent_1.raw_items AS canon_raw
            ON canon_raw.id = (
                dup.source_metadata->'preprocess'->>'duplicate_of_raw_item_id'
            )::bigint
        LEFT JOIN agent_1.clean_items AS canon_clean
            ON canon_clean.id = (
                dup.source_metadata->'preprocess'->>'duplicate_of_clean_item_id'
            )::bigint
        WHERE {" AND ".join(where)}
        ORDER BY
            CASE dup.source_metadata->'preprocess'->>'duplicate_kind'
                WHEN 'near' THEN 0
                ELSE 1
            END,
            NULLIF(dup.source_metadata->'preprocess'->>'similarity', '')::numeric ASC NULLS LAST,
            dup.id DESC
        {limit_sql}
    """

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def export_duplicate_pairs(
    conn: psycopg.Connection[Any],
    *,
    output_path: Path,
    duplicate_kind: str,
    limit: int | None,
) -> Counter[str]:
    fieldnames = [
        "duplicate_kind",
        "similarity",
        "minhash_similarity",
        "duplicate_raw_item_id",
        "duplicate_external_id",
        "duplicate_published_at",
        "duplicate_source",
        "duplicate_url",
        "duplicate_title",
        "duplicate_summary",
        "duplicate_clean_title",
        "duplicate_clean_text",
        "duplicate_clean_language",
        "duplicate_raw_text",
        "canonical_raw_item_id",
        "canonical_clean_item_id",
        "canonical_external_id",
        "canonical_published_at",
        "canonical_source",
        "canonical_url",
        "canonical_title",
        "canonical_summary",
        "canonical_clean_title",
        "canonical_clean_text",
        "canonical_language",
        "canonical_raw_text",
    ]

    stats: Counter[str] = Counter()
    rows = fetch_duplicate_pairs(conn, duplicate_kind=duplicate_kind, limit=limit)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            duplicate_clean_title, duplicate_clean_text, duplicate_language = preprocess_text(
                {
                    "title": row["duplicate_title"],
                    "raw_text": row["duplicate_raw_text"],
                    "raw_payload": row["duplicate_raw_payload"],
                    "source_metadata": row["duplicate_source_metadata"],
                }
            )
            writer.writerow(
                {
                    "duplicate_kind": maybe_text(row["duplicate_kind"]),
                    "similarity": row["similarity"],
                    "minhash_similarity": row["minhash_similarity"],
                    "duplicate_raw_item_id": row["duplicate_raw_item_id"],
                    "duplicate_external_id": maybe_text(row["duplicate_external_id"]),
                    "duplicate_published_at": row["duplicate_published_at"],
                    "duplicate_source": maybe_text(row["duplicate_source"]),
                    "duplicate_url": maybe_text(row["duplicate_url"]),
                    "duplicate_title": maybe_text(row["duplicate_title"]),
                    "duplicate_summary": extract_summary(row["duplicate_source_metadata"]),
                    "duplicate_clean_title": duplicate_clean_title,
                    "duplicate_clean_text": duplicate_clean_text,
                    "duplicate_clean_language": duplicate_language,
                    "duplicate_raw_text": maybe_text(row["duplicate_raw_text"]),
                    "canonical_raw_item_id": row["canonical_raw_item_id"],
                    "canonical_clean_item_id": row["canonical_clean_item_id"],
                    "canonical_external_id": maybe_text(row["canonical_external_id"]),
                    "canonical_published_at": row["canonical_published_at"],
                    "canonical_source": maybe_text(row["canonical_source"]),
                    "canonical_url": maybe_text(row["canonical_url"]),
                    "canonical_title": maybe_text(row["canonical_title"]),
                    "canonical_summary": extract_summary(row["canonical_source_metadata"]),
                    "canonical_clean_title": maybe_text(row["canonical_clean_title"]),
                    "canonical_clean_text": maybe_text(row["canonical_clean_text"]),
                    "canonical_language": maybe_text(row["canonical_language"]),
                    "canonical_raw_text": maybe_text(row["canonical_raw_text"]),
                }
            )
            stats["rows"] += 1
            stats[f"kind:{row['duplicate_kind']}"] += 1

    return stats


def main() -> int:
    load_dotenv(ENV_PATH)
    args = parse_args()

    conn = psycopg.connect(os.environ["AGENT_1_DB_DSN"], row_factory=dict_row)
    try:
        output_path = args.output_dir / f"{args.basename}.csv"
        stats = export_duplicate_pairs(
            conn,
            output_path=output_path,
            duplicate_kind=args.duplicate_kind,
            limit=args.limit,
        )
    finally:
        conn.close()

    gz_path = gzip_file(output_path)

    print(f"csv={output_path}")
    print(f"csv_gz={gz_path}")
    print(f"duplicate_kind={args.duplicate_kind}")
    print(f"limit={args.limit if args.limit is not None else 'all'}")
    for key in sorted(stats):
        print(f"{key}={stats[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
