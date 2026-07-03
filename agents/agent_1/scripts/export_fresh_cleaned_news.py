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

from agent_1.preprocess_worker import ENV_PATH, load_dotenv, preprocess_text, should_filter_non_russian_text


DEFAULT_LIMIT = 20_000
DEFAULT_OUTPUT_DIR = Path("/root/.openclaw/workspace/out")
DEFAULT_FETCH_BATCH_SIZE = 5_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export freshly cleaned latest raw news rows without writing back to DB."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"How many latest raw news rows to reclean. Default: {DEFAULT_LIMIT}.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Where to write export files. Default: {DEFAULT_OUTPUT_DIR}.",
    )
    parser.add_argument(
        "--basename",
        default=None,
        help="Optional explicit basename without extension.",
    )
    parser.add_argument(
        "--target-cleaned",
        type=int,
        default=None,
        help="Stop when this many cleaned rows are written; raw rows are read until the target is reached.",
    )
    return parser.parse_args()


def extract_summary(source_metadata: Any) -> str:
    if isinstance(source_metadata, dict):
        summary = source_metadata.get("summary")
        if isinstance(summary, str):
            return summary
    return ""


def build_processed_row(raw_item: dict[str, Any]) -> dict[str, Any]:
    clean_title, clean_text, language = preprocess_text(raw_item)

    preprocess_status = "cleaned"
    preprocess_reason = ""
    if not clean_text:
        preprocess_status = "failed"
        preprocess_reason = "empty_clean_text"
    elif should_filter_non_russian_text(clean_text, language):
        preprocess_status = "filtered_out"
        preprocess_reason = "non_russian_text"

    return {
        "raw_item_id": raw_item["id"],
        "external_id": raw_item["external_id"],
        "published_at": raw_item["published_at"],
        "source": raw_item["source"],
        "document_type": raw_item["document_type"],
        "url": raw_item["url"],
        "title": raw_item["title"],
        "raw_text": raw_item["raw_text"],
        "summary": extract_summary(raw_item.get("source_metadata")),
        "clean_title": clean_title,
        "clean_text": clean_text,
        "language": language,
        "preprocess_status": preprocess_status,
        "preprocess_reason": preprocess_reason,
    }


def export_latest_news(
    conn: psycopg.Connection[Any],
    *,
    limit: int,
    processed_path: Path,
    cleaned_only_path: Path,
    target_cleaned: int | None = None,
) -> Counter[str]:
    fieldnames = [
        "raw_item_id",
        "external_id",
        "published_at",
        "source",
        "document_type",
        "url",
        "title",
        "raw_text",
        "summary",
        "clean_title",
        "clean_text",
        "language",
        "preprocess_status",
        "preprocess_reason",
    ]

    stats: Counter[str] = Counter()
    processed_path.parent.mkdir(parents=True, exist_ok=True)

    with (
        processed_path.open("w", encoding="utf-8-sig", newline="") as processed_handle,
        cleaned_only_path.open("w", encoding="utf-8-sig", newline="") as cleaned_handle,
        conn.cursor() as cur,
    ):
        processed_writer = csv.DictWriter(processed_handle, fieldnames=fieldnames)
        cleaned_writer = csv.DictWriter(cleaned_handle, fieldnames=fieldnames)
        processed_writer.writeheader()
        cleaned_writer.writeheader()

        offset = 0
        while True:
            remaining_limit = limit - stats["total_rows"]
            if remaining_limit <= 0:
                break

            fetch_size = min(DEFAULT_FETCH_BATCH_SIZE, remaining_limit)
            cur.execute(
                """
                SELECT
                    id,
                    source,
                    document_type,
                    external_id,
                    url,
                    title,
                    raw_text,
                    raw_payload,
                    source_metadata,
                    published_at
                FROM agent_1.raw_items
                WHERE document_type = 'news'
                ORDER BY id DESC
                LIMIT %s
                OFFSET %s
                """,
                (fetch_size, offset),
            )
            batch_rows = cur.fetchall()
            if not batch_rows:
                break

            for raw_item in batch_rows:
                row = build_processed_row(raw_item)
                processed_writer.writerow(row)
                stats["total_rows"] += 1
                stats[f"status:{row['preprocess_status']}"] += 1
                stats[f"language:{row['language'] or '<null>'}"] += 1

                if row["preprocess_status"] == "cleaned":
                    if target_cleaned is None or stats["cleaned_only_rows"] < target_cleaned:
                        cleaned_writer.writerow(row)
                        stats["cleaned_only_rows"] += 1

                    if (
                        target_cleaned is not None
                        and stats["cleaned_only_rows"] >= target_cleaned
                    ):
                        return stats

            offset += len(batch_rows)

    return stats


def gzip_file(path: Path) -> Path:
    gz_path = path.with_suffix(path.suffix + ".gz")
    with path.open("rb") as source_handle, gzip.open(gz_path, "wb") as target_handle:
        target_handle.writelines(source_handle)
    return gz_path


def main() -> int:
    load_dotenv(ENV_PATH)
    args = parse_args()

    conn = psycopg.connect(os.environ["AGENT_1_DB_DSN"], row_factory=dict_row)

    try:
        basename = args.basename or f"agent_1_latest_{args.limit}_fresh_ru_recleaned"
        processed_path = args.output_dir / f"{basename}_processed_news.csv"
        cleaned_only_path = args.output_dir / f"{basename}_cleaned_only.csv"

        stats = export_latest_news(
            conn,
            limit=args.limit,
            processed_path=processed_path,
            cleaned_only_path=cleaned_only_path,
            target_cleaned=args.target_cleaned,
        )
    finally:
        conn.close()

    processed_gz_path = gzip_file(processed_path)
    cleaned_only_gz_path = gzip_file(cleaned_only_path)

    print(f"processed_csv={processed_path}")
    print(f"processed_csv_gz={processed_gz_path}")
    print(f"cleaned_only_csv={cleaned_only_path}")
    print(f"cleaned_only_csv_gz={cleaned_only_gz_path}")
    for key in sorted(stats):
        print(f"{key}={stats[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
