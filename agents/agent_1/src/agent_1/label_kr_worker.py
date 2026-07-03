from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row

from agent_1.label_prompts import (
    ENTITY_TONALITY_PROMPT_TEMPLATE,
    IMPACT_PROMPT_TEMPLATE,
    RELEVANCE_PROMPT_TEMPLATE,
    SBER_PAID_NEWS_PROMPT_TEMPLATE,
)


ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_LOG_FILE = ENV_PATH.parent / "logs" / "label_kr_worker.log"
DEFAULT_BATCH_SIZE = 1
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_AGENT_ID = "agent_1"
DEFAULT_AGENT_TIMEOUT_SECONDS = 600
DEFAULT_SESSION_KEY_PREFIX = "label-kr"
DEFAULT_RATE_LIMIT_PER_MINUTE = 20.0
SOURCE_RANKING_KINDS = {"source", "domain", "source_type"}
KR_STEP_RELEVANCE = "relevance"
KR_STEP_IMPACT = "impact"
KR_STEP_SBER_PAID = "sber_paid_news"
KR_STEP_ENTITY_TONALITY = "entity_tonality"
WRAPPING_QUOTE_CHARS = "\"'`«»“”„‟‚‛‘’"


class AgentInvocationError(RuntimeError):
    pass


class AgentCapacityError(AgentInvocationError):
    pass


class LabelValidationError(ValueError):
    pass


@dataclass(frozen=True)
class KrLabel:
    kr_id: int
    impact: str
    signal_strength: str
    theme: str
    dashboard_description: str
    why_for_goal: str
    evidence: tuple[str, ...]
    reasoning_steps: tuple[str, ...]
    uncertainty: str
    confidence: float
    prompt1_payload: dict[str, Any]
    is_sber_paid_news: int | None = None
    prompt2_payload: dict[str, Any] | None = None
    prompt3_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class RelevanceMatch:
    kr_id: int
    why_related: str
    evidence: tuple[str, ...]


AgentRunner = Callable[[str, dict[str, Any], int, str], str]


@dataclass
class LabelRunResult:
    labels: list[KrLabel]
    skipped_kr_ids: list[int]
    source_ranking_decisions: dict[int, dict[str, Any]]
    relevant_kr_ids: list[int]
    irrelevant_kr_ids: list[int]
    relevance_matches: list[RelevanceMatch]

    @property
    def processed_kr_ids(self) -> list[int]:
        return [label.kr_id for label in self.labels]

    @property
    def candidate_kr_ids(self) -> list[int]:
        return [*self.relevant_kr_ids, *self.irrelevant_kr_ids]


class RateLimiter:
    def __init__(self, calls_per_minute: float) -> None:
        self.calls_per_minute = calls_per_minute
        self.next_allowed_at = 0.0

    def wait(self) -> None:
        if self.calls_per_minute <= 0:
            return

        interval_seconds = 60.0 / self.calls_per_minute
        now = time.monotonic()
        if now < self.next_allowed_at:
            time.sleep(self.next_allowed_at - now)
            now = time.monotonic()
        self.next_allowed_at = now + interval_seconds


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


def is_agent_capacity_error_detail(detail: str) -> bool:
    lowered = detail.lower()
    return any(
        marker in lowered
        for marker in (
            "subscription usage limit",
            "usage limit",
            "next reset",
            "rate limit",
            "quota",
        )
    )


def resolve_default_openclaw_cmd() -> str:
    bundled = Path("/root/.hermes/node/bin/openclaw")
    if bundled.exists():
        return str(bundled)
    return "openclaw"


def parse_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def ignore_source_type_rankings_enabled() -> bool:
    return parse_bool_env(
        "AGENT_1_LABEL_IGNORE_SOURCE_TYPE_RANKINGS",
        IGNORE_SOURCE_TYPE_RANKINGS,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process agent_1 label_kr jobs from PostgreSQL queue."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one claimed batch and exit.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=parse_int_env("AGENT_1_LABEL_BATCH_SIZE", DEFAULT_BATCH_SIZE),
        help=f"How many pending label_kr jobs to claim at once. Default: {DEFAULT_BATCH_SIZE}.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=(
            "How many seconds to sleep when no jobs are available. "
            f"Default: {DEFAULT_POLL_INTERVAL_SECONDS}."
        ),
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="Optional upper bound on processed jobs before exit.",
    )
    parser.add_argument(
        "--stop-when-empty",
        action="store_true",
        help="Exit instead of polling when no matching jobs are available.",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        default=os.getenv("AGENT_1_LABEL_RETRY_FAILED", "false").lower() == "true",
        help="Claim failed label_kr jobs too, reusing saved per-step checkpoints.",
    )
    parser.add_argument(
        "--job-id-min",
        type=int,
        default=parse_int_env("AGENT_1_LABEL_JOB_ID_MIN", 0),
        help="Optional inclusive lower bound for processing_jobs.id. Default: disabled.",
    )
    parser.add_argument(
        "--job-id-max",
        type=int,
        default=parse_int_env("AGENT_1_LABEL_JOB_ID_MAX", 0),
        help="Optional inclusive upper bound for processing_jobs.id. Default: disabled.",
    )
    parser.add_argument(
        "--clean-item-id-min",
        type=int,
        default=parse_int_env("AGENT_1_LABEL_CLEAN_ITEM_ID_MIN", 0),
        help="Optional inclusive lower bound for clean_items.id. Default: disabled.",
    )
    parser.add_argument(
        "--clean-item-id-max",
        type=int,
        default=parse_int_env("AGENT_1_LABEL_CLEAN_ITEM_ID_MAX", 0),
        help="Optional inclusive upper bound for clean_items.id. Default: disabled.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore saved label_kr step checkpoints and call the agent again.",
    )
    parser.add_argument(
        "--rate-limit-per-minute",
        type=float,
        default=parse_float_env(
            "AGENT_1_LABEL_RATE_LIMIT_PER_MINUTE",
            DEFAULT_RATE_LIMIT_PER_MINUTE,
        ),
        help=(
            "Maximum OpenClaw calls per minute for this worker. "
            f"Default: {DEFAULT_RATE_LIMIT_PER_MINUTE}. Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--openclaw-cmd",
        default=os.getenv("AGENT_1_OPENCLAW_CMD", resolve_default_openclaw_cmd()),
        help="OpenClaw executable or command prefix used to call agent_1.",
    )
    parser.add_argument(
        "--agent-id",
        default=os.getenv("AGENT_1_LABEL_AGENT_ID", DEFAULT_AGENT_ID),
        help=f"OpenClaw agent id to invoke. Default: {DEFAULT_AGENT_ID}.",
    )
    parser.add_argument(
        "--agent-timeout",
        type=int,
        default=int(os.getenv("AGENT_1_LABEL_AGENT_TIMEOUT_SECONDS", DEFAULT_AGENT_TIMEOUT_SECONDS)),
        help=(
            "OpenClaw agent timeout in seconds. "
            f"Default: {DEFAULT_AGENT_TIMEOUT_SECONDS}. Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("AGENT_1_LABEL_MODEL"),
        help="Optional OpenClaw model override.",
    )
    parser.add_argument(
        "--thinking",
        default=os.getenv("AGENT_1_LABEL_THINKING"),
        help="Optional OpenClaw thinking level override.",
    )
    parser.add_argument(
        "--session-key-prefix",
        default=os.getenv("AGENT_1_LABEL_SESSION_KEY_PREFIX", DEFAULT_SESSION_KEY_PREFIX),
        help=f"Session-key suffix prefix for labeling calls. Default: {DEFAULT_SESSION_KEY_PREFIX}.",
    )
    parser.add_argument(
        "--ignore-source-type-rankings",
        action="store_true",
        default=parse_bool_env("AGENT_1_LABEL_IGNORE_SOURCE_TYPE_RANKINGS", True),
        help=(
            "Fail-open for KR source-type rankings when documents do not carry an "
            "explicit source_type."
        ),
    )
    parser.add_argument(
        "--log-file",
        default=os.getenv("AGENT_1_LABEL_LOG_FILE", str(DEFAULT_LOG_FILE)),
        help="Optional log file path.",
    )
    return parser.parse_args(argv)


def claim_jobs(
    conn: psycopg.Connection[Any],
    batch_size: int,
    *,
    retry_failed: bool = False,
    job_id_min: int | None = None,
    job_id_max: int | None = None,
    clean_item_id_min: int | None = None,
    clean_item_id_max: int | None = None,
) -> list[tuple[int, int]]:
    statuses = ["pending", "failed"] if retry_failed else ["pending"]
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH next_jobs AS (
                SELECT id
                FROM agent_1.processing_jobs
                WHERE job_type = 'label_kr'
                  AND entity_type = 'clean_item'
                  AND status = ANY(%s)
                  AND (%s::bigint IS NULL OR id >= %s::bigint)
                  AND (%s::bigint IS NULL OR id <= %s::bigint)
                  AND (%s::bigint IS NULL OR entity_id >= %s::bigint)
                  AND (%s::bigint IS NULL OR entity_id <= %s::bigint)
                ORDER BY id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE agent_1.processing_jobs AS jobs
            SET status = 'processing'
            FROM next_jobs
            WHERE jobs.id = next_jobs.id
            RETURNING jobs.id, jobs.entity_id
            """,
            (
                statuses,
                job_id_min,
                job_id_min,
                job_id_max,
                job_id_max,
                clean_item_id_min,
                clean_item_id_min,
                clean_item_id_max,
                clean_item_id_max,
                batch_size,
            ),
        )
        rows = cur.fetchall()
    conn.commit()
    return [(row["id"], row["entity_id"]) for row in rows]


def fetch_clean_item(conn: psycopg.Connection[Any], clean_item_id: int) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                clean.id,
                clean.raw_item_id,
                clean.clean_title,
                clean.clean_text,
                clean.language,
                raw.source,
                raw.document_type,
                raw.title AS raw_title,
                raw.url,
                raw.raw_payload,
                raw.source_metadata,
                raw.published_at
            FROM agent_1.clean_items AS clean
            JOIN agent_1.raw_items AS raw
              ON raw.id = clean.raw_item_id
            WHERE clean.id = %s
            """,
            (clean_item_id,),
        )
        return cur.fetchone()


def fetch_active_key_results(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title, description, enrichment
            FROM agent_1.key_results
            WHERE active IS TRUE
            ORDER BY id
            """
        )
        key_results = list(cur.fetchall())

    return key_results


def count_active_key_results(conn: psycopg.Connection[Any]) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS count
            FROM agent_1.key_results
            WHERE active IS TRUE
            """
        )
        row = cur.fetchone()
    return row["count"]


def mark_job_status(conn: psycopg.Connection[Any], job_id: int, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE agent_1.processing_jobs
            SET status = %s
            WHERE id = %s
            """,
            (status, job_id),
        )


def merge_raw_label_metadata(
    conn: psycopg.Connection[Any],
    raw_item_id: int,
    patch: dict[str, Any],
) -> None:
    payload = {"label_kr": {**patch, "updated_at": utc_now_text()}}
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE agent_1.raw_items
            SET source_metadata = COALESCE(source_metadata, '{}'::jsonb) || %s::jsonb
            WHERE id = %s
            """,
            (json.dumps(payload, ensure_ascii=False), raw_item_id),
        )


def fetch_label_checkpoint(
    conn: psycopg.Connection[Any],
    *,
    clean_item_id: int,
    kr_id: int,
    step_name: str,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT payload
            FROM agent_1.label_kr_step_checkpoints
            WHERE clean_item_id = %s
              AND kr_id = %s
              AND step_name = %s
              AND status = 'done'
            """,
            (clean_item_id, kr_id, step_name),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return coerce_json_object(row["payload"])


def save_label_checkpoint(
    conn: psycopg.Connection[Any],
    *,
    clean_item_id: int,
    kr_id: int,
    step_name: str,
    payload: dict[str, Any],
    raw_output: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_1.label_kr_step_checkpoints (
                clean_item_id,
                kr_id,
                step_name,
                status,
                payload,
                raw_output,
                updated_at
            )
            VALUES (%s, %s, %s, 'done', %s::jsonb, %s, now())
            ON CONFLICT (clean_item_id, kr_id, step_name)
            DO UPDATE SET
                status = EXCLUDED.status,
                payload = EXCLUDED.payload,
                raw_output = EXCLUDED.raw_output,
                updated_at = now()
            """,
            (
                clean_item_id,
                kr_id,
                step_name,
                json.dumps(payload, ensure_ascii=False),
                raw_output,
            ),
        )
    conn.commit()


def parse_kr_id_from_step_name(step_name: str) -> int | None:
    match = re.search(r"-kr-(\d+)$", step_name)
    if not match:
        return None
    return int(match.group(1))


def extract_usage_summary(raw_output: str, *, prompt_chars: int) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "prompt_chars": prompt_chars,
        "output_chars": len(raw_output),
    }
    try:
        outer = parse_json_object(raw_output)
    except Exception:
        return summary

    matches: list[dict[str, Any]] = []

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = key.lower()
                child_path = f"{path}.{key}" if path else key
                if any(marker in lowered for marker in ("usage", "token", "cost")):
                    if isinstance(item, (dict, list, int, float, str)):
                        matches.append({"path": child_path, "value": item})
                walk(item, child_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(outer)
    if matches:
        summary["openclaw_usage_fields"] = matches[:20]
    return summary


def log_llm_call(
    conn: psycopg.Connection[Any],
    *,
    worker: str,
    clean_item_id: int,
    kr_id: int | None,
    step_name: str,
    job_id: int,
    model: str | None,
    session_key: str | None,
    started_at: datetime,
    finished_at: datetime,
    success: bool,
    prompt: str,
    raw_output: str | None,
    error: str | None,
) -> None:
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    usage = extract_usage_summary(raw_output or "", prompt_chars=len(prompt))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_1.llm_call_logs (
                worker,
                clean_item_id,
                kr_id,
                step_name,
                job_id,
                model,
                session_key,
                started_at,
                finished_at,
                duration_ms,
                success,
                prompt_chars,
                output_chars,
                usage,
                error
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
            )
            """,
            (
                worker,
                clean_item_id,
                kr_id,
                step_name,
                job_id,
                model,
                session_key,
                started_at,
                finished_at,
                duration_ms,
                success,
                len(prompt),
                len(raw_output) if raw_output is not None else None,
                json.dumps(usage, ensure_ascii=False),
                error,
            ),
        )
    conn.commit()


def enqueue_extract_semantics_job(conn: psycopg.Connection[Any], clean_item_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_1.processing_jobs (
                job_type,
                entity_type,
                entity_id,
                status
            )
            VALUES (
                'extract_semantics',
                'clean_item',
                %s,
                'pending'
            )
            ON CONFLICT (job_type, entity_type, entity_id)
            WHERE status IN ('pending', 'processing')
            DO NOTHING
            """,
            (clean_item_id,),
        )


def replace_document_kr_labels(
    conn: psycopg.Connection[Any],
    clean_item_id: int,
    labels: list[KrLabel],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM agent_1.document_kr_labels
            WHERE clean_item_id = %s
            """,
            (clean_item_id,),
        )
        for label in labels:
            cur.execute(
                """
                INSERT INTO agent_1.document_kr_labels (
                    clean_item_id,
                    kr_id,
                    impact,
                    signal_strength,
                    theme,
                    dashboard_description,
                    why_for_goal,
                    evidence,
                    reasoning_steps,
                    uncertainty,
                    confidence,
                    is_sber_paid_news,
                    prompt1_payload,
                    prompt2_payload,
                    prompt3_payload
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s::jsonb,
                    %s,
                    %s,
                    %s,
                    %s::jsonb,
                    %s::jsonb,
                    %s::jsonb
                )
                """,
                (
                    clean_item_id,
                    label.kr_id,
                    label.impact,
                    label.signal_strength,
                    label.theme,
                    label.dashboard_description,
                    label.why_for_goal,
                    list(label.evidence),
                    json.dumps(list(label.reasoning_steps), ensure_ascii=False),
                    label.uncertainty,
                    label.confidence,
                    label.is_sber_paid_news,
                    json.dumps(label.prompt1_payload, ensure_ascii=False),
                    json.dumps(label.prompt2_payload, ensure_ascii=False)
                    if label.prompt2_payload is not None
                    else None,
                    json.dumps(label.prompt3_payload, ensure_ascii=False)
                    if label.prompt3_payload is not None
                    else None,
                ),
            )


def coerce_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def first_non_empty_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def get_url_domain(url: Any) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    parsed = urlparse(url.strip())
    return parsed.netloc.lower()


def get_news_title(clean_item: dict[str, Any]) -> str:
    return first_non_empty_string(clean_item.get("clean_title"), clean_item.get("raw_title"))


def get_news_source(clean_item: dict[str, Any]) -> str:
    source_metadata = coerce_json_object(clean_item.get("source_metadata"))
    raw_payload = coerce_json_object(clean_item.get("raw_payload"))
    return first_non_empty_string(
        source_metadata.get("source"),
        raw_payload.get("source"),
        clean_item.get("source"),
    )


def normalize_source_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value.strip().casefold())


def unique_non_empty(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return tuple(result)


def normalize_domain_key(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""

    raw = value.strip()
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    domain = parsed.hostname or raw
    domain = domain.strip().strip(".").casefold()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def normalize_source_kind(value: Any) -> str:
    normalized = normalize_source_key(value).replace("-", "_")
    aliases = {
        "источник": "source",
        "домен": "domain",
        "url_domain": "domain",
        "host": "domain",
        "тип источника": "source_type",
        "тип_источника": "source_type",
        "source type": "source_type",
        "source_type": "source_type",
        "type": "source_type",
    }
    kind = aliases.get(normalized, normalized)
    return kind if kind in SOURCE_RANKING_KINDS else ""


def normalize_source_type_key(value: Any) -> str:
    normalized = normalize_source_key(value)
    aliases = {
        "сми": "сми",
        "блог": "блог",
        "блоги": "блог",
        "blog": "блог",
        "blogs": "блог",
        "мессенджер": "мессенджеры",
        "мессенджеры": "мессенджеры",
        "соц сеть": "соц сети",
        "соц сети": "соц сети",
        "соцсеть": "соц сети",
        "соцсети": "соц сети",
        "социальная сеть": "соц сети",
        "социальные сети": "соц сети",
        "видеохостинг": "видеохостинг",
        "видеохостинги": "видеохостинг",
        "форум": "форумы",
        "форумы": "форумы",
        "отзыв": "отзывы",
        "отзывы": "отзывы",
        "микроблог": "микроблог",
        "микроблоги": "микроблог",
    }
    return aliases.get(normalized, normalized)


def source_type_candidates_from_payloads(
    raw_payload: dict[str, Any],
    source_metadata: dict[str, Any],
) -> list[str]:
    keys = (
        "source_type",
        "sourceType",
        "source_kind",
        "sourceKind",
        "media_type",
        "mediaType",
        "category",
        "type",
    )

    candidates: list[str] = []
    for payload in (source_metadata, raw_payload):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
    return candidates


def get_document_source_keys(clean_item: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    raw_payload = coerce_json_object(clean_item.get("raw_payload"))
    source_metadata = coerce_json_object(clean_item.get("source_metadata"))

    source_keys = unique_non_empty(
        [
            normalize_source_key(source_metadata.get("source")),
            normalize_source_key(raw_payload.get("source")),
            normalize_source_key(clean_item.get("source")),
        ]
    )
    domain_keys = unique_non_empty(
        [
            normalize_domain_key(clean_item.get("url")),
            normalize_domain_key(raw_payload.get("url")),
            normalize_domain_key(source_metadata.get("url")),
        ]
    )
    source_type_keys = unique_non_empty(
        [
            normalize_source_type_key(candidate)
            for candidate in source_type_candidates_from_payloads(raw_payload, source_metadata)
        ]
    )

    return {
        kind: values
        for kind, values in (
            ("source", source_keys),
            ("domain", domain_keys),
            ("source_type", source_type_keys),
        )
        if values
    }


def normalize_ranking_source_key(source_kind: str, value: Any) -> str:
    if source_kind == "domain":
        return normalize_domain_key(value)
    if source_kind == "source_type":
        return normalize_source_type_key(value)
    return normalize_source_key(value)


def parse_importance(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        if match:
            return int(match.group(0))
    return None


def first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def ranking_from_enriched_source(source: Any, index: int) -> dict[str, Any] | None:
    if isinstance(source, str):
        source_kind = "source_type"
        source_value = source
        importance = None
        reason = None
        include = True
    elif isinstance(source, dict):
        source_kind = normalize_source_kind(
            first_present(
                source,
                (
                    "source_kind",
                    "kind",
                    "тип_ключа",
                    "тип ключа",
                ),
            )
        ) or "source_type"
        source_value = first_present(
            source,
            (
                "тип",
                "source_value",
                "source",
                "source_type",
                "type",
                "name",
                "value",
                "домен",
            ),
        )
        importance = parse_importance(
            first_present(source, ("важность", "importance", "rank", "priority", "score"))
        )
        reason = first_present(source, ("причина", "reason", "why"))
        raw_include = first_present(source, ("include", "included", "use"))
        include = bool(raw_include) if isinstance(raw_include, bool) else (importance is None or importance >= 2)
    else:
        return None

    if not isinstance(source_value, str) or not source_value.strip():
        return None
    source_key = normalize_ranking_source_key(source_kind, source_value)
    if not source_key:
        return None
    return {
        "source_kind": source_kind,
        "source_value": source_value.strip(),
        "source_key": source_key,
        "rank_score": importance,
        "rank_position": index + 1,
        "include": include,
        "reason": reason,
    }


def extract_kr_source_rankings(kr: dict[str, Any]) -> list[dict[str, Any]]:
    direct_rankings = kr.get("source_rankings")
    if isinstance(direct_rankings, list):
        return [ranking for ranking in direct_rankings if isinstance(ranking, dict)]

    enrichment = coerce_json_object(kr.get("enrichment"))
    sources = first_present(
        enrichment,
        (
            "типы_источников",
            "source_types",
            "source_rankings",
            "sources",
        ),
    )
    if not isinstance(sources, list):
        return []

    rankings: list[dict[str, Any]] = []
    for index, source in enumerate(sources):
        ranking = ranking_from_enriched_source(source, index)
        if ranking is not None:
            rankings.append(ranking)
    return rankings


def source_ranking_metadata(ranking: dict[str, Any] | None) -> dict[str, Any] | None:
    if ranking is None:
        return None
    rank_score = ranking.get("rank_score")
    if isinstance(rank_score, Decimal):
        rank_score = float(rank_score)
    return {
        "source_kind": ranking.get("source_kind"),
        "source_value": ranking.get("source_value"),
        "source_key": ranking.get("source_key"),
        "rank_score": rank_score,
        "rank_position": ranking.get("rank_position"),
        "include": bool(ranking.get("include")),
        "reason": ranking.get("reason"),
    }


def source_rankings_allow_label(
    clean_item: dict[str, Any],
    rankings: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
) -> dict[str, Any]:
    document_keys = get_document_source_keys(clean_item)
    if not rankings:
        return {
            "allowed": True,
            "reason": "no_rankings_for_kr",
            "document_keys": document_keys,
            "matched_ranking": None,
        }

    if (
        ignore_source_type_rankings_enabled()
        and not document_keys.get("source_type")
        and all(normalize_source_kind(ranking.get("source_kind")) == "source_type" for ranking in rankings)
    ):
        return {
            "allowed": True,
            "reason": "ignored_source_type_rankings_without_document_source_type",
            "document_keys": document_keys,
            "matched_ranking": None,
        }

    matches: list[dict[str, Any]] = []
    for ranking in rankings:
        source_kind = normalize_source_kind(ranking.get("source_kind"))
        if not source_kind:
            continue
        ranking_key = normalize_ranking_source_key(
            source_kind,
            ranking.get("source_key") or ranking.get("source_value"),
        )
        if ranking_key and ranking_key in document_keys.get(source_kind, ()):
            matches.append(ranking)

    excluded = next((ranking for ranking in matches if not bool(ranking.get("include"))), None)
    if excluded is not None:
        return {
            "allowed": False,
            "reason": "excluded_by_source_ranking",
            "document_keys": document_keys,
            "matched_ranking": source_ranking_metadata(excluded),
        }

    included = next((ranking for ranking in matches if bool(ranking.get("include"))), None)
    if included is not None:
        return {
            "allowed": True,
            "reason": "included_by_source_ranking",
            "document_keys": document_keys,
            "matched_ranking": source_ranking_metadata(included),
        }

    return {
        "allowed": False,
        "reason": "no_matching_source_ranking",
        "document_keys": document_keys,
        "matched_ranking": None,
    }


def get_goal_text(kr: dict[str, Any]) -> str:
    return "\n\n".join(
        part
        for part in (
            first_non_empty_string(kr.get("title")),
            first_non_empty_string(kr.get("description")),
        )
        if part
    )


def collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def extract_kr_keywords(kr: dict[str, Any]) -> tuple[str, ...]:
    enrichment = coerce_json_object(kr.get("enrichment"))
    raw_keywords = enrichment.get("ключевые_слова")
    if not isinstance(raw_keywords, list):
        raw_keywords = enrichment.get("keywords")

    keywords: list[str] = []
    if isinstance(raw_keywords, list):
        for item in raw_keywords:
            if not isinstance(item, str):
                continue
            keyword = collapse_whitespace(item)
            if keyword and keyword not in keywords:
                keywords.append(keyword)
    return tuple(keywords[:12])


def build_relevance_goal_catalog(candidate_krs: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for kr in candidate_krs:
        goal_text = collapse_whitespace(get_goal_text(kr))
        keywords = extract_kr_keywords(kr)
        lines = [
            f"- KR_ID: {int(kr['id'])}",
            f"  GOAL: {goal_text}",
        ]
        if keywords:
            lines.append(f"  KEYWORDS: {', '.join(keywords)}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def render_prompt(template: str, replacements: dict[str, Any]) -> str:
    prompt = template
    for key, value in replacements.items():
        prompt = prompt.replace("{" + key + "}", "" if value is None else str(value))
    return prompt


def build_relevance_prompt(
    clean_item: dict[str, Any],
    candidate_krs: list[dict[str, Any]],
) -> str:
    return render_prompt(
        RELEVANCE_PROMPT_TEMPLATE,
        {
            "goal_catalog": build_relevance_goal_catalog(candidate_krs),
            "source": get_news_source(clean_item),
            "url_domain": get_url_domain(clean_item.get("url")),
            "title": get_news_title(clean_item),
            "text": clean_item["clean_text"],
        },
    )


def build_impact_prompt(clean_item: dict[str, Any], kr: dict[str, Any]) -> str:
    return render_prompt(
        IMPACT_PROMPT_TEMPLATE,
        {
            "goal_text": get_goal_text(kr),
            "source": get_news_source(clean_item),
            "url_domain": get_url_domain(clean_item.get("url")),
            "title": get_news_title(clean_item),
            "text": clean_item["clean_text"],
        },
    )


def build_sber_paid_news_prompt(clean_item: dict[str, Any]) -> str:
    return render_prompt(
        SBER_PAID_NEWS_PROMPT_TEMPLATE,
        {
            "source": get_news_source(clean_item),
            "url_domain": get_url_domain(clean_item.get("url")),
            "title": get_news_title(clean_item),
            "text": clean_item["clean_text"],
        },
    )


def build_entity_tonality_prompt(clean_item: dict[str, Any]) -> str:
    return render_prompt(
        ENTITY_TONALITY_PROMPT_TEMPLATE,
        {"text": clean_item["clean_text"]},
    )


def build_session_key(
    agent_id: str,
    prefix: str,
    clean_item_id: int,
    job_id: int,
    step_name: str,
) -> str:
    safe_prefix = "".join(
        char if char.isalnum() or char in {"-", "_"} else "-" for char in prefix.strip()
    ).strip("-_")
    if not safe_prefix:
        safe_prefix = DEFAULT_SESSION_KEY_PREFIX
    safe_step = "".join(
        char if char.isalnum() or char in {"-", "_"} else "-" for char in step_name.strip()
    ).strip("-_")
    if not safe_step:
        safe_step = "step"
    return f"agent:{agent_id}:{safe_prefix}-clean-{clean_item_id}-job-{job_id}-{safe_step}"


def extract_json_object_text(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise LabelValidationError("agent output is empty")

    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LabelValidationError("agent output does not contain a JSON object")
    return text[start : end + 1]


def parse_json_object(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = json.loads(extract_json_object_text(raw))

    if not isinstance(parsed, dict):
        raise LabelValidationError("agent output JSON must be an object")
    return parsed


def collect_payload_text(payloads: Any) -> str | None:
    if not isinstance(payloads, list):
        return None
    parts: list[str] = []
    for payload in payloads:
        if isinstance(payload, dict) and isinstance(payload.get("text"), str):
            parts.append(payload["text"].strip())
    text = "\n".join(part for part in parts if part)
    return text or None


def is_direct_agent_payload(value: dict[str, Any]) -> bool:
    return any(key in value for key in ("matches", "impact", "is_sber_paid_news", "mentions"))


def extract_agent_reply_text(agent_response: dict[str, Any]) -> str:
    if is_direct_agent_payload(agent_response):
        return json.dumps(agent_response, ensure_ascii=False)

    result = agent_response.get("result")
    if isinstance(result, dict):
        meta = result.get("meta")
        if isinstance(meta, dict):
            for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
                value = meta.get(key)
                if isinstance(value, str) and value.strip():
                    return value

        payload_text = collect_payload_text(result.get("payloads"))
        if payload_text:
            return payload_text

    meta = agent_response.get("meta")
    if isinstance(meta, dict):
        for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                return value

    payload_text = collect_payload_text(agent_response.get("payloads"))
    if payload_text:
        return payload_text

    for key in ("text", "reply", "output"):
        value = agent_response.get(key)
        if isinstance(value, str) and value.strip():
            return value

    raise LabelValidationError("OpenClaw response does not contain assistant text")


def parse_agent_label_payload(raw_output: str) -> dict[str, Any]:
    outer = parse_json_object(raw_output)
    if is_direct_agent_payload(outer):
        return outer

    reply_text = extract_agent_reply_text(outer)
    return parse_json_object(reply_text)


def require_non_empty_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LabelValidationError(f"{key} must be a non-empty string")
    return value.strip()


def normalize_string_list(
    value: Any,
    key: str,
    *,
    containing_text: str | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise LabelValidationError(f"{key} must be a non-empty list")

    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise LabelValidationError(f"{key}[{index}] must be a string")
        fragment = item.strip()
        if not fragment:
            raise LabelValidationError(f"{key}[{index}] is empty")
        if containing_text is not None:
            grounded_fragment = ground_fragment_in_text(fragment, containing_text)
            if grounded_fragment is None:
                raise LabelValidationError(f"{key}[{index}] is not present in input text")
            fragment = grounded_fragment
        if fragment not in result:
            result.append(fragment)
    return tuple(result)


def normalize_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LabelValidationError("confidence must be a number")

    confidence = float(value)
    tenths = round(confidence * 10)
    if abs(confidence * 10 - tenths) > 1e-9:
        raise LabelValidationError("confidence must use a 0.1 step")
    if tenths < 5 or tenths > 10:
        raise LabelValidationError("confidence must be between 0.5 and 1.0")
    return tenths / 10


def normalize_kr_id(value: Any, *, key: str) -> int:
    if isinstance(value, bool):
        raise LabelValidationError(f"{key} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise LabelValidationError(f"{key} must be an integer")


def validate_relevance_payload(
    payload: dict[str, Any],
    *,
    evidence_text: str,
    candidate_kr_ids: set[int],
) -> list[RelevanceMatch]:
    matches = payload.get("matches")
    if not isinstance(matches, list):
        raise LabelValidationError("matches must be a list")

    normalized_matches: list[RelevanceMatch] = []
    seen_kr_ids: set[int] = set()
    for index, item in enumerate(matches):
        if not isinstance(item, dict):
            raise LabelValidationError(f"matches[{index}] must be an object")

        kr_id = normalize_kr_id(item.get("kr_id"), key=f"matches[{index}].kr_id")
        if kr_id not in candidate_kr_ids:
            raise LabelValidationError(f"matches[{index}].kr_id is not in candidate KR set")
        if kr_id in seen_kr_ids:
            raise LabelValidationError(f"matches[{index}].kr_id is duplicated")

        evidence = normalize_string_list(
            item.get("evidence"),
            f"matches[{index}].evidence",
            containing_text=evidence_text,
        )
        normalized_matches.append(
            RelevanceMatch(
                kr_id=kr_id,
                why_related=require_non_empty_string(item, "why_related"),
                evidence=evidence,
            )
        )
        seen_kr_ids.add(kr_id)

    return normalized_matches


def validate_impact_payload(
    payload: dict[str, Any],
    *,
    evidence_text: str,
    kr_id: int,
) -> KrLabel:
    impact = require_non_empty_string(payload, "impact")
    if impact not in {"positive", "negative", "neutral"}:
        raise LabelValidationError("impact must be positive, negative, or neutral")

    signal_strength = require_non_empty_string(payload, "signal_strength")
    if signal_strength not in {"direct", "indirect"}:
        raise LabelValidationError("signal_strength must be direct or indirect")

    evidence = normalize_string_list(
        payload.get("evidence"),
        "evidence",
        containing_text=evidence_text,
    )
    reasoning_steps = normalize_string_list(payload.get("reasoning_steps"), "reasoning_steps")

    return KrLabel(
        kr_id=kr_id,
        impact=impact,
        signal_strength=signal_strength,
        theme=require_non_empty_string(payload, "theme"),
        dashboard_description=require_non_empty_string(payload, "dashboard_description"),
        why_for_goal=require_non_empty_string(payload, "why_for_goal"),
        evidence=evidence,
        reasoning_steps=reasoning_steps,
        uncertainty=require_non_empty_string(payload, "uncertainty"),
        confidence=normalize_confidence(payload.get("confidence")),
        prompt1_payload=payload,
    )


def validate_sber_paid_news_payload(payload: dict[str, Any]) -> int:
    value = payload.get("is_sber_paid_news")
    if isinstance(value, bool):
        raise LabelValidationError("is_sber_paid_news must be 0 or 1")
    if value in (0, 1):
        return int(value)
    raise LabelValidationError("is_sber_paid_news must be 0 or 1")


def normalize_entity_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LabelValidationError("mention confidence must be a number")

    confidence = float(value)
    if confidence < 0.0 or confidence > 1.0:
        raise LabelValidationError("mention confidence must be between 0.0 and 1.0")
    return confidence


def strip_wrapping_quotes(value: str) -> str:
    stripped = value.strip()
    while stripped and stripped[0] in WRAPPING_QUOTE_CHARS:
        stripped = stripped[1:].lstrip()
    while stripped and stripped[-1] in WRAPPING_QUOTE_CHARS:
        stripped = stripped[:-1].rstrip()
    return stripped


def ground_fragment_in_text(fragment: str, containing_text: str) -> str | None:
    candidates: list[str] = []

    def add_candidate(value: str) -> None:
        candidate = value.strip()
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    add_candidate(fragment)
    add_candidate(strip_wrapping_quotes(fragment))
    for candidate in tuple(candidates):
        add_candidate(re.sub(r"\s+", " ", candidate))

    for candidate in candidates:
        if candidate in containing_text:
            return candidate

    for candidate in candidates:
        tokens = candidate.split()
        if not tokens:
            continue
        pattern = r"\s+".join(re.escape(token) for token in tokens)
        match = re.search(pattern, containing_text)
        if match is not None:
            return containing_text[match.start() : match.end()]

    return None


def validate_entity_tonality_payload(
    payload: dict[str, Any],
    *,
    evidence_text: str,
) -> dict[str, Any]:
    mentions = payload.get("mentions")
    if not isinstance(mentions, list):
        raise LabelValidationError("mentions must be a list")

    normalized_mentions: list[dict[str, Any]] = []
    for index, mention in enumerate(mentions):
        if not isinstance(mention, dict):
            raise LabelValidationError(f"mentions[{index}] must be an object")

        text = require_non_empty_string(mention, "text")
        grounded_text = ground_fragment_in_text(text, evidence_text)
        if grounded_text is None:
            raise LabelValidationError(f"mentions[{index}].text is not present in input text")

        sentiment = require_non_empty_string(mention, "sentiment")
        if sentiment not in {"positive", "negative", "neutral"}:
            raise LabelValidationError(
                f"mentions[{index}].sentiment must be positive, negative, or neutral"
            )

        normalized_mentions.append(
            {
                "text": grounded_text,
                "sentiment": sentiment,
                "justification": require_non_empty_string(mention, "justification"),
                "confidence": normalize_entity_confidence(mention.get("confidence")),
            }
        )

    return {"mentions": normalized_mentions}


def call_agent_1(
    prompt: str,
    clean_item: dict[str, Any],
    job_id: int,
    step_name: str,
    *,
    openclaw_cmd: str,
    agent_id: str,
    agent_timeout: int,
    model: str | None,
    thinking: str | None,
    session_key_prefix: str,
) -> str:
    cmd = shlex.split(openclaw_cmd)
    if not cmd:
        raise AgentInvocationError("openclaw command is empty")

    session_key = build_session_key(
        agent_id,
        session_key_prefix,
        clean_item["id"],
        job_id,
        step_name,
    )
    args = [
        *cmd,
        "agent",
        "--agent",
        agent_id,
        "--session-key",
        session_key,
        "--message",
        prompt,
        "--json",
        "--timeout",
        str(agent_timeout),
    ]
    if model:
        args.extend(["--model", model])
    if thinking:
        args.extend(["--thinking", thinking])

    timeout = None if agent_timeout == 0 else agent_timeout + 60
    completed = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        detail = stderr or stdout or f"exit code {completed.returncode}"
        if is_agent_capacity_error_detail(detail):
            raise AgentCapacityError(f"agent_1 capacity blocked: {detail}")
        raise AgentInvocationError(f"agent_1 call failed: {detail}")

    return completed.stdout


def build_evidence_text(clean_item: dict[str, Any]) -> str:
    return "\n".join(
        part
        for part in (get_news_title(clean_item), clean_item["clean_text"])
        if isinstance(part, str) and part
    )


def run_label_step(
    *,
    conn: psycopg.Connection[Any] | None,
    resume: bool,
    clean_item: dict[str, Any],
    kr_id: int,
    checkpoint_step_name: str,
    agent_step_name: str,
    prompt: str,
    job_id: int,
    agent_runner: AgentRunner,
) -> dict[str, Any]:
    clean_item_id = int(clean_item["id"])
    if conn is not None and resume:
        checkpoint = fetch_label_checkpoint(
            conn,
            clean_item_id=clean_item_id,
            kr_id=kr_id,
            step_name=checkpoint_step_name,
        )
        if checkpoint is not None:
            log_line(
                "INFO",
                (
                    f"clean_item_id={clean_item_id} kr_id={kr_id} "
                    f"step={checkpoint_step_name} checkpoint=hit"
                ),
            )
            return checkpoint

    raw_output = agent_runner(prompt, clean_item, job_id, agent_step_name)
    payload = parse_agent_label_payload(raw_output)
    if conn is not None:
        save_label_checkpoint(
            conn,
            clean_item_id=clean_item_id,
            kr_id=kr_id,
            step_name=checkpoint_step_name,
            payload=payload,
            raw_output=raw_output,
        )
    return payload


def label_clean_item(
    clean_item: dict[str, Any],
    active_krs: list[dict[str, Any]],
    job_id: int,
    *,
    agent_runner: AgentRunner,
    conn: psycopg.Connection[Any] | None = None,
    resume: bool = True,
) -> LabelRunResult:
    labels: list[KrLabel] = []
    skipped_kr_ids: list[int] = []
    source_ranking_decisions: dict[int, dict[str, Any]] = {}
    evidence_text = build_evidence_text(clean_item)
    candidate_krs: list[dict[str, Any]] = []

    for kr in active_krs:
        kr_id = int(kr["id"])
        source_decision = source_rankings_allow_label(
            clean_item,
            extract_kr_source_rankings(kr),
        )
        source_ranking_decisions[kr_id] = source_decision
        if not source_decision["allowed"]:
            skipped_kr_ids.append(kr_id)
            log_line(
                "INFO",
                (
                    f"clean_item_id={clean_item['id']} kr_id={kr_id} "
                    f"source_ranking=skip reason={source_decision['reason']}"
                ),
            )
            continue
        candidate_krs.append(kr)

    if not candidate_krs:
        return LabelRunResult(
            labels=labels,
            skipped_kr_ids=skipped_kr_ids,
            source_ranking_decisions=source_ranking_decisions,
            relevant_kr_ids=[],
            irrelevant_kr_ids=[],
            relevance_matches=[],
        )

    relevance_payload = parse_agent_label_payload(
        agent_runner(
            build_relevance_prompt(clean_item, candidate_krs),
            clean_item,
            job_id,
            KR_STEP_RELEVANCE,
        )
    )
    relevance_matches = validate_relevance_payload(
        relevance_payload,
        evidence_text=evidence_text,
        candidate_kr_ids={int(kr["id"]) for kr in candidate_krs},
    )
    relevance_matches_by_kr = {match.kr_id: match for match in relevance_matches}
    relevant_kr_ids = [
        int(kr["id"]) for kr in candidate_krs if int(kr["id"]) in relevance_matches_by_kr
    ]
    irrelevant_kr_ids = [
        int(kr["id"]) for kr in candidate_krs if int(kr["id"]) not in relevance_matches_by_kr
    ]
    log_line(
        "INFO",
        (
            f"clean_item_id={clean_item['id']} relevance matched={len(relevant_kr_ids)} "
            f"filtered={len(irrelevant_kr_ids)}"
        ),
    )

    for kr in candidate_krs:
        kr_id = int(kr["id"])
        if kr_id not in relevance_matches_by_kr:
            continue

        impact_payload = run_label_step(
            conn=conn,
            resume=resume,
            clean_item=clean_item,
            kr_id=kr_id,
            checkpoint_step_name=KR_STEP_IMPACT,
            agent_step_name=f"impact-kr-{kr_id}",
            prompt=build_impact_prompt(clean_item, kr),
            job_id=job_id,
            agent_runner=agent_runner,
        )
        label = validate_impact_payload(
            impact_payload,
            evidence_text=evidence_text,
            kr_id=kr_id,
        )

        if label.impact in {"positive", "negative"}:
            paid_payload = run_label_step(
                conn=conn,
                resume=resume,
                clean_item=clean_item,
                kr_id=kr_id,
                checkpoint_step_name=KR_STEP_SBER_PAID,
                agent_step_name=f"sber-paid-kr-{kr_id}",
                prompt=build_sber_paid_news_prompt(clean_item),
                job_id=job_id,
                agent_runner=agent_runner,
            )
            is_sber_paid_news = validate_sber_paid_news_payload(paid_payload)

            entity_payload = run_label_step(
                conn=conn,
                resume=resume,
                clean_item=clean_item,
                kr_id=kr_id,
                checkpoint_step_name=KR_STEP_ENTITY_TONALITY,
                agent_step_name=f"entity-tonality-kr-{kr_id}",
                prompt=build_entity_tonality_prompt(clean_item),
                job_id=job_id,
                agent_runner=agent_runner,
            )
            entity_payload = validate_entity_tonality_payload(
                entity_payload,
                evidence_text=evidence_text,
            )

            label = KrLabel(
                kr_id=label.kr_id,
                impact=label.impact,
                signal_strength=label.signal_strength,
                theme=label.theme,
                dashboard_description=label.dashboard_description,
                why_for_goal=label.why_for_goal,
                evidence=label.evidence,
                reasoning_steps=label.reasoning_steps,
                uncertainty=label.uncertainty,
                confidence=label.confidence,
                prompt1_payload=label.prompt1_payload,
                is_sber_paid_news=is_sber_paid_news,
                prompt2_payload=paid_payload,
                prompt3_payload=entity_payload,
            )

        labels.append(label)

    return LabelRunResult(
        labels=labels,
        skipped_kr_ids=skipped_kr_ids,
        source_ranking_decisions=source_ranking_decisions,
        relevant_kr_ids=relevant_kr_ids,
        irrelevant_kr_ids=irrelevant_kr_ids,
        relevance_matches=relevance_matches,
    )


def process_job(
    conn: psycopg.Connection[Any],
    job_id: int,
    clean_item_id: int,
    *,
    agent_runner: AgentRunner,
    resume: bool,
) -> str:
    clean_item = fetch_clean_item(conn, clean_item_id)
    if clean_item is None:
        mark_job_status(conn, job_id, "failed")
        conn.commit()
        return "failed_missing_clean_item"

    active_krs = fetch_active_key_results(conn)

    if not active_krs:
        mark_job_status(conn, job_id, "pending")
        conn.commit()
        return "deferred_no_active_krs"

    label_result = label_clean_item(
        clean_item,
        active_krs,
        job_id,
        agent_runner=agent_runner,
        conn=conn,
        resume=resume,
    )
    labels = label_result.labels
    replace_document_kr_labels(conn, clean_item_id, labels)

    source_filter_metadata = {
        "mode": "agent_2_key_result_enrichment",
        "document_keys": get_document_source_keys(clean_item),
        "processed_kr_ids": label_result.processed_kr_ids,
        "candidate_kr_ids": label_result.candidate_kr_ids,
        "relevant_kr_ids": label_result.relevant_kr_ids,
        "irrelevant_kr_ids": label_result.irrelevant_kr_ids,
        "skipped_kr_ids": label_result.skipped_kr_ids,
        "relevance_matches": [
            {
                "kr_id": match.kr_id,
                "why_related": match.why_related,
                "evidence": list(match.evidence),
            }
            for match in label_result.relevance_matches
        ],
        "skipped_decisions": {
            str(kr_id): label_result.source_ranking_decisions[kr_id]
            for kr_id in label_result.skipped_kr_ids
        },
    }

    if not labels:
        skip_reason = "source_rankings_filtered_all_krs"
        if label_result.candidate_kr_ids:
            skip_reason = "relevance_filtered_all_krs"
        merge_raw_label_metadata(
            conn,
            clean_item["raw_item_id"],
            {
                "status": "skipped",
                "reason": skip_reason,
                "source_filter": source_filter_metadata,
            },
        )
        mark_job_status(conn, job_id, "done")
        conn.commit()
        if skip_reason == "relevance_filtered_all_krs":
            return (
                f"skipped_relevance candidate_krs={len(label_result.candidate_kr_ids)} "
                f"filtered={len(label_result.irrelevant_kr_ids)}"
            )
        return f"skipped_source_rankings krs={len(label_result.skipped_kr_ids)}"

    enqueue_extract_semantics_job(conn, clean_item_id)
    merge_raw_label_metadata(
        conn,
        clean_item["raw_item_id"],
        {
            "status": "labeled",
            "source_filter": source_filter_metadata,
            "kr_count": len(labels),
            "relevant_kr_count": len(label_result.relevant_kr_ids),
            "irrelevant_kr_count": len(label_result.irrelevant_kr_ids),
            "skipped_kr_count": len(label_result.skipped_kr_ids),
            "positive_count": sum(1 for label in labels if label.impact == "positive"),
            "negative_count": sum(1 for label in labels if label.impact == "negative"),
            "neutral_count": sum(1 for label in labels if label.impact == "neutral"),
        },
    )
    mark_job_status(conn, job_id, "done")
    conn.commit()
    positives = sum(1 for label in labels if label.impact == "positive")
    negatives = sum(1 for label in labels if label.impact == "negative")
    return (
        f"labeled krs={len(labels)} skipped_by_source_rankings="
        f"{len(label_result.skipped_kr_ids)} skipped_by_relevance="
        f"{len(label_result.irrelevant_kr_ids)} positive={positives} negative={negatives}"
    )


def open_connection() -> psycopg.Connection[Any]:
    return psycopg.connect(DB_DSN, row_factory=dict_row)


def main(argv: list[str] | None = None) -> int:
    global IGNORE_SOURCE_TYPE_RANKINGS, LOG_FILE

    args = parse_args(argv)
    LOG_FILE = args.log_file
    IGNORE_SOURCE_TYPE_RANKINGS = args.ignore_source_type_rankings
    job_id_min = args.job_id_min or None
    job_id_max = args.job_id_max or None
    clean_item_id_min = args.clean_item_id_min or None
    clean_item_id_max = args.clean_item_id_max or None

    processed_jobs = 0
    conn = open_connection()
    rate_limiter = RateLimiter(args.rate_limit_per_minute)
    resume = not args.no_resume

    def agent_runner(
        prompt: str,
        clean_item: dict[str, Any],
        job_id: int,
        step_name: str,
    ) -> str:
        clean_item_id = int(clean_item["id"])
        kr_id = parse_kr_id_from_step_name(step_name)
        session_key = build_session_key(
            args.agent_id,
            args.session_key_prefix,
            clean_item_id,
            job_id,
            step_name,
        )
        started_at = datetime.now(timezone.utc)
        raw_output: str | None = None
        try:
            rate_limiter.wait()
            raw_output = call_agent_1(
                prompt,
                clean_item,
                job_id,
                step_name,
                openclaw_cmd=args.openclaw_cmd,
                agent_id=args.agent_id,
                agent_timeout=args.agent_timeout,
                model=args.model,
                thinking=args.thinking,
                session_key_prefix=args.session_key_prefix,
            )
        except Exception as exc:
            finished_at = datetime.now(timezone.utc)
            log_llm_call(
                conn,
                worker="label_kr_worker",
                clean_item_id=clean_item_id,
                kr_id=kr_id,
                step_name=step_name,
                job_id=job_id,
                model=args.model,
                session_key=session_key,
                started_at=started_at,
                finished_at=finished_at,
                success=False,
                prompt=prompt,
                raw_output=raw_output,
                error=str(exc),
            )
            raise

        finished_at = datetime.now(timezone.utc)
        log_llm_call(
            conn,
            worker="label_kr_worker",
            clean_item_id=clean_item_id,
            kr_id=kr_id,
            step_name=step_name,
            job_id=job_id,
            model=args.model,
            session_key=session_key,
            started_at=started_at,
            finished_at=finished_at,
            success=True,
            prompt=prompt,
            raw_output=raw_output,
            error=None,
        )
        return raw_output

    log_line(
        "INFO",
        (
            "Starting label_kr worker "
            f"batch_size={args.batch_size} once={args.once} "
            f"poll_interval={args.poll_interval} agent_id={args.agent_id} "
            f"rate_limit_per_minute={args.rate_limit_per_minute} "
            f"resume={resume} retry_failed={args.retry_failed} "
            f"job_id_min={job_id_min} job_id_max={job_id_max} "
            f"clean_item_id_min={clean_item_id_min} "
            f"clean_item_id_max={clean_item_id_max}"
        ),
    )

    try:
        while True:
            if count_active_key_results(conn) == 0:
                log_line("WARNING", "No active key_results; label_kr jobs are left pending.")
                if args.once:
                    break
                time.sleep(args.poll_interval)
                continue

            claimed_jobs = claim_jobs(
                conn,
                args.batch_size,
                retry_failed=args.retry_failed,
                job_id_min=job_id_min,
                job_id_max=job_id_max,
                clean_item_id_min=clean_item_id_min,
                clean_item_id_max=clean_item_id_max,
            )
            if not claimed_jobs:
                if args.once or args.stop_when_empty:
                    break
                time.sleep(args.poll_interval)
                continue

            for job_id, clean_item_id in claimed_jobs:
                try:
                    result = process_job(
                        conn,
                        job_id,
                        clean_item_id,
                        agent_runner=agent_runner,
                        resume=resume,
                    )
                    log_line(
                        "INFO",
                        f"job_id={job_id} clean_item_id={clean_item_id} result={result}",
                    )
                except Exception as exc:
                    conn.rollback()
                    if isinstance(exc, AgentCapacityError):
                        mark_job_status(conn, job_id, "pending")
                        conn.commit()
                        log_line(
                            "WARNING",
                            (
                                f"job_id={job_id} clean_item_id={clean_item_id} "
                                f"capacity_blocked requeued_pending error={exc}"
                            ),
                            stderr=True,
                        )
                        return 0

                    mark_job_status(conn, job_id, "failed")
                    conn.commit()
                    log_line(
                        "ERROR",
                        (
                            f"job_id={job_id} clean_item_id={clean_item_id} "
                            f"failed error={exc}"
                        ),
                        stderr=True,
                    )

                processed_jobs += 1
                if args.max_jobs is not None and processed_jobs >= args.max_jobs:
                    log_line("INFO", f"Reached max_jobs={args.max_jobs}.")
                    return 0

            if args.once:
                break
    finally:
        conn.close()
        log_line("INFO", "label_kr worker stopped.")

    return 0


load_dotenv(ENV_PATH)
DB_DSN = os.environ["AGENT_1_DB_DSN"]
LOG_FILE = os.getenv("AGENT_1_LABEL_LOG_FILE", str(DEFAULT_LOG_FILE))
# Default to fail-open here because current news inputs often do not expose an
# explicit source_type, and otherwise the worker can skip every KR without ever
# invoking agent_1.
IGNORE_SOURCE_TYPE_RANKINGS = parse_bool_env("AGENT_1_LABEL_IGNORE_SOURCE_TYPE_RANKINGS", True)


if __name__ == "__main__":
    raise SystemExit(main())
