"""v5 embedding worker: fills agent_1_v5.clean_posts.embedding via OpenRouter.

rework-agent-1-v5 task 4.1. Model `openai/text-embedding-3-small` at 1024
dims (design D4), pulled through OpenRouter's OpenAI-compatible /embeddings
endpoint.

Dimensions handling: OpenRouter's embeddings docs do NOT document the
`dimensions` param (only `encoding_format`), so we don't depend on it.
text-embedding-3-small is Matryoshka-trained, so taking the first 1024
components and L2-renormalizing client-side is exactly what OpenAI's
`dimensions=1024` does server-side. We send `dimensions=1024` (best case
OpenRouter forwards it -> less bandwidth) but defensively truncate+normalize
to 1024 whatever comes back. Result is always a unit-norm vector(1024).

Only clean, non-duplicate rows without an embedding are claimed
(`drop_reason IS NULL AND is_duplicate = false AND embedding IS NULL`), so a
re-run does zero API calls once the corpus is embedded (idempotency = money).
Claim uses FOR UPDATE SKIP LOCKED so several embed workers can run in
parallel (this stage is API-bound, not CPU-bound, so parallelism is fine
here even though preprocessing stays single-process for now).

The HNSW index (vector_cosine_ops) is built once AFTER the bulk load, by a
separate SQL step (db/v5, task 4.2) -- not here.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

import psycopg
import requests
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_1 import preprocess_worker as pw  # noqa: E402

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
SCHEMA = "agent_1_v5"

EMBED_DIMS = 1024
DEFAULT_MODEL = "openai/text-embedding-3-small"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_BATCH_SIZE = 64
# Cap each input well under the model's 8191-token limit. Russian is ~2-3
# chars/token, so 8000 chars stays safely inside the limit while still
# capturing the substance of a news article.
DEFAULT_MAX_INPUT_CHARS = 8000
REQUEST_TIMEOUT_SECONDS = 60
REQUEST_RETRIES = 5
RETRY_SLEEP_SECONDS = 5


# --------------------------------------------------------------------------- #
# Pure helpers (no network / DB) -- unit tested
# --------------------------------------------------------------------------- #

def truncate_normalize(vector: list[float], dims: int = EMBED_DIMS) -> list[float]:
    """First `dims` components, L2-renormalized. Equivalent to OpenAI's
    server-side `dimensions` truncation for Matryoshka text-embedding-3."""
    head = vector[:dims]
    norm = math.sqrt(sum(value * value for value in head))
    if norm == 0.0:
        return head
    return [value / norm for value in head]


def vector_literal(vector: list[float]) -> str:
    """pgvector text input format: '[v1,v2,...]'. Avoids needing the
    pgvector-python adapter; the SQL casts it with ::vector."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def cap_text(text: str, max_chars: int = DEFAULT_MAX_INPUT_CHARS) -> str:
    return text[:max_chars] if text else ""


def parse_embeddings_response(payload: dict[str, Any], expected: int) -> list[list[float]]:
    """Extract embeddings in input order, tolerating providers that don't
    return `index` (fall back to response order)."""
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected:
        raise ValueError(
            f"embeddings response has {len(data) if isinstance(data, list) else 'no'} "
            f"items, expected {expected}"
        )
    if all(isinstance(item.get("index"), int) for item in data):
        data = sorted(data, key=lambda item: item["index"])
    vectors = [item["embedding"] for item in data]
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise ValueError("embeddings response contains an empty vector")
    return vectors


# --------------------------------------------------------------------------- #
# OpenRouter call
# --------------------------------------------------------------------------- #

def openrouter_embed(
    texts: list[str], *, api_key: str, model: str, base_url: str
) -> list[list[float]]:
    url = f"{base_url.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"model": model, "input": texts, "dimensions": EMBED_DIMS}

    last_exc: Exception | None = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            resp = requests.post(
                url, headers=headers, json=body, timeout=REQUEST_TIMEOUT_SECONDS
            )
            resp.raise_for_status()
            vectors = parse_embeddings_response(resp.json(), len(texts))
            return [truncate_normalize(vector) for vector in vectors]
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt >= REQUEST_RETRIES:
                break
            time.sleep(RETRY_SLEEP_SECONDS)
    raise RuntimeError(f"OpenRouter embeddings failed after {REQUEST_RETRIES} tries: {last_exc}")


# --------------------------------------------------------------------------- #
# DB layer
# --------------------------------------------------------------------------- #

def claim_batch(conn: psycopg.Connection[Any], batch_size: int) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT id_clean_post, clean_content
            FROM {SCHEMA}.clean_posts
            WHERE drop_reason IS NULL
              AND is_duplicate = FALSE
              AND embedding IS NULL
              AND clean_content IS NOT NULL
            ORDER BY id_clean_post
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            (batch_size,),
        )
        return cur.fetchall()


def write_embeddings(cur, pairs: list[tuple[int, list[float]]]) -> None:
    for id_clean_post, vector in pairs:
        cur.execute(
            f"UPDATE {SCHEMA}.clean_posts SET embedding = %s::vector WHERE id_clean_post = %s",
            (vector_literal(vector), id_clean_post),
        )


def run(
    conn: psycopg.Connection[Any],
    embed_fn: Callable[[list[str]], list[list[float]]],
    *,
    batch_size: int,
    max_input_chars: int,
    once: bool,
    poll_interval: float,
    max_docs: int | None,
    log,
) -> int:
    processed = 0
    while True:
        batch = claim_batch(conn, batch_size)
        if not batch:
            conn.rollback()
            if once:
                break
            time.sleep(poll_interval)
            continue

        texts = [cap_text(row["clean_content"], max_input_chars) for row in batch]
        vectors = embed_fn(texts)
        if len(vectors) != len(batch):
            conn.rollback()
            raise RuntimeError(f"embed_fn returned {len(vectors)} vectors for {len(batch)} inputs")

        with conn.cursor() as cur:
            write_embeddings(cur, [(row["id_clean_post"], vec) for row, vec in zip(batch, vectors)])
        conn.commit()

        processed += len(batch)
        log(f"embedded batch={len(batch)} total={processed}")
        if max_docs is not None and processed >= max_docs:
            log(f"Reached max_docs={max_docs}.")
            break
        if once:
            break
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="v5 embedding worker (OpenRouter)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    ap.add_argument("--max-input-chars", type=int, default=DEFAULT_MAX_INPUT_CHARS)
    ap.add_argument("--poll-interval", type=float, default=5.0)
    ap.add_argument("--max-docs", type=int, default=None)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    def log(message: str) -> None:
        print(f"{pw.utc_now_text()} INFO {message}")

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 2
    model = os.environ.get("EMBED_MODEL", DEFAULT_MODEL)
    base_url = os.environ.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL)

    def embed_fn(texts: list[str]) -> list[list[float]]:
        return openrouter_embed(texts, api_key=api_key, model=model, base_url=base_url)

    conn = psycopg.connect(DB_DSN, autocommit=False)
    try:
        log(f"Starting v5 embed worker model={model} dims={EMBED_DIMS} batch={args.batch_size}")
        return run(
            conn, embed_fn,
            batch_size=args.batch_size,
            max_input_chars=args.max_input_chars,
            once=args.once,
            poll_interval=args.poll_interval,
            max_docs=args.max_docs,
            log=log,
        )
    finally:
        conn.close()
        print(f"{pw.utc_now_text()} INFO v5 embed worker stopped.")


pw.load_dotenv(ENV_PATH)
DB_DSN = os.environ.get("AGENT_1_DB_DSN", "")


if __name__ == "__main__":
    raise SystemExit(main())