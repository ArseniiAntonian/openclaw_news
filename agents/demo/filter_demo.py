#!/usr/bin/env python3
import io
import json
import os
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parent
ENV_FILE = Path("/root/.openclaw/workspace/agents/agent_1/.env")
BATCH_DOCS = 1000


def load_dsn() -> str:
    dsn = os.environ.get("AGENT_1_DB_DSN")
    if dsn:
        return dsn
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("AGENT_1_DB_DSN="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("AGENT_1_DB_DSN is not set")


def normalize_keywords(value) -> list[str]:
    if not isinstance(value, list):
        return []
    seen = set()
    keywords = []
    for item in value:
        keyword = str(item or "").strip().lower()
        if keyword and keyword not in seen:
            seen.add(keyword)
            keywords.append(keyword)
    return keywords


def load_keywords(conn) -> list[tuple[int, list[str]]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, enrichment->'ключевые_слова' AS keywords
            FROM demo.kr
            WHERE enrichment ? 'ключевые_слова'
            ORDER BY id
            """
        )
        rows = cur.fetchall()
    result = []
    for kr_id, keywords_json in rows:
        if isinstance(keywords_json, str):
            keywords_json = json.loads(keywords_json)
        keywords = normalize_keywords(keywords_json)
        if keywords:
            result.append((kr_id, keywords))
    if not result:
        raise RuntimeError("No KR keywords found in demo.kr.enrichment")
    return result


def text_matches(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(keyword in low for keyword in keywords)


def copy_labels(conn, rows: list[tuple[int, int, bool]]) -> None:
    with conn.cursor() as cur:
        with cur.copy(
            "COPY demo.doc_labels (clean_item_id, kr_id, impact, raw_json, relevance) FROM STDIN"
        ) as copy:
            for clean_item_id, kr_id, relevance in rows:
                copy.write_row((clean_item_id, kr_id, "neutral", "{}", relevance))


def main() -> None:
    log_path = ROOT / "filter.log"
    dsn = load_dsn()
    processed = 0
    label_rows = 0

    with log_path.open("a", encoding="utf-8") as log:
        log.write("filter start\n")

    with psycopg.connect(dsn) as read_conn, psycopg.connect(dsn) as write_conn:
        read_conn.execute("SET search_path TO demo, public")
        write_conn.execute("SET search_path TO demo, public")
        write_conn.execute("TRUNCATE TABLE demo.doc_labels")
        write_conn.commit()

        keywords_by_kr = load_keywords(read_conn)
        with read_conn.cursor(name="clean_items_filter") as cur:
            cur.itersize = BATCH_DOCS
            cur.execute(
                """
                SELECT id, COALESCE(title, ''), COALESCE(content, '')
                FROM demo.clean_items
                ORDER BY id
                """
            )
            batch = []
            for clean_item_id, title, content in cur:
                text = f"{title} {content}"
                for kr_id, keywords in keywords_by_kr:
                    batch.append((clean_item_id, kr_id, text_matches(text, keywords)))
                processed += 1
                if processed % BATCH_DOCS == 0:
                    copy_labels(write_conn, batch)
                    label_rows += len(batch)
                    batch.clear()
                    write_conn.commit()
                    with log_path.open("a", encoding="utf-8") as log:
                        log.write(
                            f"processed={processed} labels={label_rows} kr={len(keywords_by_kr)}\n"
                        )
            if batch:
                copy_labels(write_conn, batch)
                label_rows += len(batch)
                write_conn.commit()
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(
                        f"processed={processed} labels={label_rows} kr={len(keywords_by_kr)} done\n"
                    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        with (ROOT / "filter.log").open("a", encoding="utf-8") as log:
            log.write(f"ERROR {type(exc).__name__}: {exc}\n")
        raise
