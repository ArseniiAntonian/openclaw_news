import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import requests
import urllib3


ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_LOG_FILE = ENV_PATH.parent / "logs" / "parsers360.log"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log_line(level: str, message: str, *, stderr: bool = False) -> None:
    line = f"{utc_now_text()} {level} {message}"
    print(line, file=sys.stderr if stderr else sys.stdout)
    log_file = Path(LOG_FILE)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


load_dotenv(ENV_PATH)

API_URL = os.environ["PARSERS360_API_URL"]
TOKEN = os.environ["PARSERS360_TOKEN"]
AUTH_USER = os.environ["PARSERS360_BASIC_USER"]
AUTH_PASSWORD = os.environ["PARSERS360_BASIC_PASSWORD"]
VERIFY_SSL = os.getenv("PARSERS360_VERIFY_SSL", "true").lower() == "true"
DB_DSN = os.environ["AGENT_1_DB_DSN"]
LOG_FILE = os.getenv("PARSERS360_LOG_FILE", str(DEFAULT_LOG_FILE))

LIMIT = 200
REQUEST_TIMEOUT_SECONDS = 20
REQUEST_RETRIES = 10
RETRY_SLEEP_SECONDS = 10


def fetch_page(page: int, start_at: str):
    if not VERIFY_SSL:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    last_exc = None

    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            r = requests.post(
                API_URL,
                params={
                    "service": "parser",
                    "limit": LIMIT,
                    "page": page,
                    "summary": "true",
                    "company": "true",
                    "token": TOKEN,
                    "start_at": start_at,
                },
                auth=(AUTH_USER, AUTH_PASSWORD),
                headers={"accept": "application/json"},
                verify=VERIFY_SSL,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

            r.raise_for_status()
            payload = r.json()
            if isinstance(payload, str):
                payload = json.loads(payload)
            return payload
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt >= REQUEST_RETRIES:
                break
            log_line(
                "WARNING",
                (
                    f"Fetch failed start_at={start_at} page={page} "
                    f"attempt={attempt}/{REQUEST_RETRIES} retry_in={RETRY_SLEEP_SECONDS}s "
                    f"error={exc}"
                ),
            )
            time.sleep(RETRY_SLEEP_SECONDS)

    raise last_exc


def parse_published_at(item):
    ts = item.get("created_at")

    if not ts:
        return None

    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except Exception:
        return None


def insert_item(cur, item):
    source_metadata = {
        "summary": item.get("summary"),
        "companies": item.get("companies"),
        "source": item.get("source"),
        "is_duplicated": item.get("is_duplicated"),
        "original_id": item.get("original_id"),
    }

    cur.execute(
        """
        INSERT INTO agent_1.raw_items (
            source,
            document_type,
            external_id,
            url,
            title,
            raw_text,
            raw_payload,
            source_metadata,
            published_at
        )
        VALUES (
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s::jsonb,
            %s::jsonb,
            %s
        )
        ON CONFLICT DO NOTHING
        """,
        (
            "parsers360",
            "news",
            str(item["id"]),
            item.get("url"),
            item.get("title"),
            item.get("content"),
            json.dumps(item, ensure_ascii=False),
            json.dumps(source_metadata, ensure_ascii=False),
            parse_published_at(item),
        ),
    )


def main():
    start_at = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).strftime("%Y-%m-%d")

    if not VERIFY_SSL:
        log_line("WARNING", "Parsers360 TLS certificate verification is disabled for this run.")

    log_line(
        "INFO",
        (
            f"Starting ingest start_at={start_at} limit={LIMIT} "
            f"timeout={REQUEST_TIMEOUT_SECONDS} retries={REQUEST_RETRIES} "
            f"retry_sleep={RETRY_SLEEP_SECONDS} api_url={API_URL}"
        ),
    )

    conn = psycopg.connect(DB_DSN)
    conn.autocommit = False

    inserted = 0
    page = 1

    try:
        with conn.cursor() as cur:
            while True:
                log_line(
                    "INFO",
                    f"Fetching Parsers360 start_at={start_at} page={page} limit={LIMIT}",
                )
                items = fetch_page(page, start_at)

                if not items:
                    break

                for item in items:
                    insert_item(cur, item)
                    inserted += 1

                conn.commit()

                log_line(
                    "INFO",
                    f"page={page} received={len(items)} total_insert_attempts={inserted}",
                )

                if len(items) < LIMIT:
                    break

                page += 1

        log_line("INFO", f"done, processed={inserted}")

    except Exception as exc:
        conn.rollback()
        log_line("ERROR", f"ingest failed: {exc}", stderr=True)
        raise
    finally:
        conn.close()
        log_line("INFO", "Postgres connection closed.")


if __name__ == "__main__":
    main()
