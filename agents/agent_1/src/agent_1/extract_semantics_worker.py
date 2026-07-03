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
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row

from agent_1.label_kr_worker import (
    AgentCapacityError,
    AgentInvocationError,
    LabelValidationError,
    build_session_key,
    coerce_json_object,
    collect_payload_text,
    extract_usage_summary,
    extract_json_object_text,
    first_non_empty_string,
    get_news_title,
    is_agent_capacity_error_detail,
    load_dotenv,
    parse_float_env,
    parse_int_env,
    resolve_default_openclaw_cmd,
    utc_now_text,
)
from agent_1.label_prompts import (
    EVENTS_PROMPT_TEMPLATE,
    ENTITIES_PROMPT_TEMPLATE,
)


ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_LOG_FILE = ENV_PATH.parent / "logs" / "extract_semantics_worker.log"
DEFAULT_BATCH_SIZE = 1
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_AGENT_ID = "agent_1"
DEFAULT_AGENT_TIMEOUT_SECONDS = 600
DEFAULT_SESSION_KEY_PREFIX = "extract-semantics"
DEFAULT_RATE_LIMIT_PER_MINUTE = 20.0
SEMANTICS_STEP_ENTITIES = "entities"
SEMANTICS_STEP_EVENTS = "events"
ENTITY_GROUPS = ("companies", "products", "people", "locations", "technologies")
WRAPPING_QUOTE_CHARS = "\"'`«»“”„‟‚‛‘’"


@dataclass
class SemanticsResult:
    entities: dict[str, list[str]]
    events: list[dict[str, Any]]
    label_count: int


AgentRunner = Callable[[str, dict[str, Any], int, str], str]


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


def log_line(level: str, message: str, *, stderr: bool = False) -> None:
    line = f"{utc_now_text()} {level} {message}"
    print(line, file=sys.stderr if stderr else sys.stdout)

    log_file = Path(LOG_FILE)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


def parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process agent_1 extract_semantics jobs from PostgreSQL queue."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one claimed batch and exit.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=parse_int_env("AGENT_1_SEMANTICS_BATCH_SIZE", DEFAULT_BATCH_SIZE),
        help=f"How many pending extract_semantics jobs to claim at once. Default: {DEFAULT_BATCH_SIZE}.",
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
        default=parse_bool_env("AGENT_1_SEMANTICS_RETRY_FAILED", False),
        help="Claim failed extract_semantics jobs too, reusing saved per-step checkpoints.",
    )
    parser.add_argument(
        "--clean-item-id-min",
        type=int,
        default=parse_int_env("AGENT_1_SEMANTICS_CLEAN_ITEM_ID_MIN", 0),
        help="Optional inclusive lower bound for clean_items.id. Default: disabled.",
    )
    parser.add_argument(
        "--clean-item-id-max",
        type=int,
        default=parse_int_env("AGENT_1_SEMANTICS_CLEAN_ITEM_ID_MAX", 0),
        help="Optional inclusive upper bound for clean_items.id. Default: disabled.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore saved extract_semantics step checkpoints and call the agent again.",
    )
    parser.add_argument(
        "--rate-limit-per-minute",
        type=float,
        default=parse_float_env(
            "AGENT_1_SEMANTICS_RATE_LIMIT_PER_MINUTE",
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
        default=os.getenv("AGENT_1_SEMANTICS_AGENT_ID", DEFAULT_AGENT_ID),
        help=f"OpenClaw agent id to invoke. Default: {DEFAULT_AGENT_ID}.",
    )
    parser.add_argument(
        "--agent-timeout",
        type=int,
        default=int(
            os.getenv(
                "AGENT_1_SEMANTICS_AGENT_TIMEOUT_SECONDS",
                DEFAULT_AGENT_TIMEOUT_SECONDS,
            )
        ),
        help=(
            "OpenClaw agent timeout in seconds. "
            f"Default: {DEFAULT_AGENT_TIMEOUT_SECONDS}. Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("AGENT_1_SEMANTICS_MODEL"),
        help="Optional OpenClaw model override.",
    )
    parser.add_argument(
        "--thinking",
        default=os.getenv("AGENT_1_SEMANTICS_THINKING"),
        help="Optional OpenClaw thinking level override.",
    )
    parser.add_argument(
        "--session-key-prefix",
        default=os.getenv(
            "AGENT_1_SEMANTICS_SESSION_KEY_PREFIX",
            DEFAULT_SESSION_KEY_PREFIX,
        ),
        help=(
            "Session-key suffix prefix for extract_semantics calls. "
            f"Default: {DEFAULT_SESSION_KEY_PREFIX}."
        ),
    )
    parser.add_argument(
        "--log-file",
        default=os.getenv("AGENT_1_SEMANTICS_LOG_FILE", str(DEFAULT_LOG_FILE)),
        help="Optional log file path.",
    )
    return parser.parse_args(argv)


def claim_jobs(
    conn: psycopg.Connection[Any],
    batch_size: int,
    *,
    retry_failed: bool = False,
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
                WHERE job_type = 'extract_semantics'
                  AND entity_type = 'clean_item'
                  AND status = ANY(%s)
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


def fetch_document_labels(
    conn: psycopg.Connection[Any], clean_item_id: int
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                labels.kr_id,
                labels.impact,
                labels.theme,
                labels.dashboard_description,
                kr.title AS kr_title
            FROM agent_1.document_kr_labels AS labels
            JOIN agent_1.key_results AS kr
              ON kr.id = labels.kr_id
            WHERE labels.clean_item_id = %s
            ORDER BY labels.kr_id
            """,
            (clean_item_id,),
        )
        return list(cur.fetchall())


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


def merge_raw_semantics_metadata(
    conn: psycopg.Connection[Any],
    raw_item_id: int,
    patch: dict[str, Any],
) -> None:
    payload = {"extract_semantics": {**patch, "updated_at": utc_now_text()}}
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE agent_1.raw_items
            SET source_metadata = COALESCE(source_metadata, '{}'::jsonb) || %s::jsonb
            WHERE id = %s
            """,
            (json.dumps(payload, ensure_ascii=False), raw_item_id),
        )


def fetch_step_checkpoint(
    conn: psycopg.Connection[Any],
    *,
    clean_item_id: int,
    step_name: str,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT payload
            FROM agent_1.extract_semantics_step_checkpoints
            WHERE clean_item_id = %s
              AND step_name = %s
              AND status = 'done'
            """,
            (clean_item_id, step_name),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return coerce_json_object(row["payload"])


def save_step_checkpoint(
    conn: psycopg.Connection[Any],
    *,
    clean_item_id: int,
    step_name: str,
    payload: dict[str, Any],
    raw_output: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_1.extract_semantics_step_checkpoints (
                clean_item_id,
                step_name,
                status,
                payload,
                raw_output,
                updated_at
            )
            VALUES (%s, %s, 'done', %s::jsonb, %s, now())
            ON CONFLICT (clean_item_id, step_name)
            DO UPDATE SET
                status = EXCLUDED.status,
                payload = EXCLUDED.payload,
                raw_output = EXCLUDED.raw_output,
                updated_at = now()
            """,
            (
                clean_item_id,
                step_name,
                json.dumps(payload, ensure_ascii=False),
                raw_output,
            ),
        )
    conn.commit()


def log_llm_call(
    conn: psycopg.Connection[Any],
    *,
    worker: str,
    clean_item_id: int,
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
                %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
            )
            """,
            (
                worker,
                clean_item_id,
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


def render_prompt(template: str, replacements: dict[str, Any]) -> str:
    prompt = template
    for key, value in replacements.items():
        prompt = prompt.replace("{" + key + "}", "" if value is None else str(value))
    return prompt


def format_label_context(labels: list[dict[str, Any]]) -> str:
    if not labels:
        return "[]"

    lines = []
    for label in labels:
        lines.append(
            "- KR {kr_id}: impact={impact}; theme={theme}; summary={summary}; goal={goal}".format(
                kr_id=label["kr_id"],
                impact=label["impact"],
                theme=label["theme"],
                summary=label["dashboard_description"],
                goal=label["kr_title"],
            )
        )
    return "\n".join(lines)


def build_entities_prompt(clean_item: dict[str, Any], labels: list[dict[str, Any]]) -> str:
    return render_prompt(
        ENTITIES_PROMPT_TEMPLATE,
        {
            "title": get_news_title(clean_item),
            "text": clean_item["clean_text"],
            "label_context": format_label_context(labels),
        },
    )


def build_events_prompt(clean_item: dict[str, Any], labels: list[dict[str, Any]]) -> str:
    return render_prompt(
        EVENTS_PROMPT_TEMPLATE,
        {
            "title": get_news_title(clean_item),
            "text": clean_item["clean_text"],
            "label_context": format_label_context(labels),
        },
    )


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


def extract_agent_reply_text(agent_response: dict[str, Any]) -> str:
    if any(key in agent_response for key in ("entities", "events")):
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


def parse_json_object(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = json.loads(extract_json_object_text(raw))

    if not isinstance(parsed, dict):
        raise LabelValidationError("agent output JSON must be an object")
    return parsed


def parse_agent_payload(raw_output: str) -> dict[str, Any]:
    outer = parse_json_object(raw_output)
    if any(key in outer for key in ("entities", "events")):
        return outer
    reply_text = extract_agent_reply_text(outer)
    return parse_json_object(reply_text)


def require_non_empty_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LabelValidationError(f"{key} must be a non-empty string")
    return value.strip()


def normalize_unique_grounded_list(
    values: Any,
    key: str,
    *,
    containing_text: str,
) -> list[str]:
    if not isinstance(values, list):
        raise LabelValidationError(f"{key} must be a list")

    normalized: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise LabelValidationError(f"{key}[{index}] must be a string")
        grounded = ground_fragment_in_text(value, containing_text)
        if grounded is None:
            raise LabelValidationError(f"{key}[{index}] is not present in input text")
        if grounded not in normalized:
            normalized.append(grounded)
    return normalized


def validate_entities_payload(
    payload: dict[str, Any],
    *,
    evidence_text: str,
) -> dict[str, list[str]]:
    entities = payload.get("entities")
    if not isinstance(entities, dict):
        raise LabelValidationError("entities must be an object")

    normalized: dict[str, list[str]] = {}
    for group in ENTITY_GROUPS:
        values = entities.get(group, [])
        if values is None:
            values = []
        normalized[group] = normalize_unique_grounded_list(
            values,
            f"entities.{group}",
            containing_text=evidence_text,
        )
    return normalized


def validate_events_payload(
    payload: dict[str, Any],
    *,
    evidence_text: str,
) -> list[dict[str, Any]]:
    events = payload.get("events")
    if not isinstance(events, list):
        raise LabelValidationError("events must be a list")

    normalized_events: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise LabelValidationError(f"events[{index}] must be an object")

        participants = event.get("participants", [])
        if participants is None:
            participants = []
        if not isinstance(participants, list):
            raise LabelValidationError(f"events[{index}].participants must be a list")

        normalized_participants: list[str] = []
        for participant_index, participant in enumerate(participants):
            if not isinstance(participant, str) or not participant.strip():
                raise LabelValidationError(
                    f"events[{index}].participants[{participant_index}] must be a non-empty string"
                )
            value = participant.strip()
            if value not in normalized_participants:
                normalized_participants.append(value)

        event_time = event.get("event_time")
        if event_time is not None and (not isinstance(event_time, str) or not event_time.strip()):
            raise LabelValidationError(f"events[{index}].event_time must be a non-empty string or null")

        normalized_events.append(
            {
                "event_type": require_non_empty_string(event, "event_type"),
                "summary": require_non_empty_string(event, "summary"),
                "participants": normalized_participants,
                "event_time": event_time.strip() if isinstance(event_time, str) else None,
                "evidence": normalize_unique_grounded_list(
                    event.get("evidence"),
                    f"events[{index}].evidence",
                    containing_text=evidence_text,
                ),
            }
        )
    return normalized_events


def call_agent(
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


def run_step(
    *,
    conn: psycopg.Connection[Any] | None,
    resume: bool,
    clean_item: dict[str, Any],
    step_name: str,
    prompt: str,
    job_id: int,
    agent_runner: AgentRunner,
) -> dict[str, Any]:
    clean_item_id = int(clean_item["id"])
    if conn is not None and resume:
        checkpoint = fetch_step_checkpoint(
            conn,
            clean_item_id=clean_item_id,
            step_name=step_name,
        )
        if checkpoint is not None:
            log_line(
                "INFO",
                f"clean_item_id={clean_item_id} step={step_name} checkpoint=hit",
            )
            return checkpoint

    raw_output = agent_runner(prompt, clean_item, job_id, step_name)
    payload = parse_agent_payload(raw_output)
    if conn is not None:
        save_step_checkpoint(
            conn,
            clean_item_id=clean_item_id,
            step_name=step_name,
            payload=payload,
            raw_output=raw_output,
        )
    return payload


def upsert_document_enrichment(
    conn: psycopg.Connection[Any],
    *,
    clean_item_id: int,
    entities: dict[str, list[str]],
    events: list[dict[str, Any]],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_1.document_enrichments (
                clean_item_id,
                entities,
                events
            )
            VALUES (%s, %s::jsonb, %s::jsonb)
            ON CONFLICT (clean_item_id)
            DO UPDATE SET
                entities = EXCLUDED.entities,
                events = EXCLUDED.events
            """,
            (
                clean_item_id,
                json.dumps(entities, ensure_ascii=False),
                json.dumps(events, ensure_ascii=False),
            ),
        )


def extract_semantics(
    clean_item: dict[str, Any],
    labels: list[dict[str, Any]],
    job_id: int,
    *,
    agent_runner: AgentRunner,
    conn: psycopg.Connection[Any] | None = None,
    resume: bool = True,
) -> SemanticsResult:
    evidence_text = build_evidence_text(clean_item)
    entities_payload = run_step(
        conn=conn,
        resume=resume,
        clean_item=clean_item,
        step_name=SEMANTICS_STEP_ENTITIES,
        prompt=build_entities_prompt(clean_item, labels),
        job_id=job_id,
        agent_runner=agent_runner,
    )
    events_payload = run_step(
        conn=conn,
        resume=resume,
        clean_item=clean_item,
        step_name=SEMANTICS_STEP_EVENTS,
        prompt=build_events_prompt(clean_item, labels),
        job_id=job_id,
        agent_runner=agent_runner,
    )
    return SemanticsResult(
        entities=validate_entities_payload(entities_payload, evidence_text=evidence_text),
        events=validate_events_payload(events_payload, evidence_text=evidence_text),
        label_count=len(labels),
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

    labels = fetch_document_labels(conn, clean_item_id)
    if not labels:
        merge_raw_semantics_metadata(
            conn,
            clean_item["raw_item_id"],
            {
                "status": "failed",
                "reason": "missing_document_kr_labels",
            },
        )
        mark_job_status(conn, job_id, "failed")
        conn.commit()
        return "failed_missing_document_kr_labels"

    result = extract_semantics(
        clean_item,
        labels,
        job_id,
        agent_runner=agent_runner,
        conn=conn,
        resume=resume,
    )
    upsert_document_enrichment(
        conn,
        clean_item_id=clean_item_id,
        entities=result.entities,
        events=result.events,
    )
    merge_raw_semantics_metadata(
        conn,
        clean_item["raw_item_id"],
        {
            "status": "enriched",
            "label_count": result.label_count,
            "entity_counts": {
                group: len(result.entities[group]) for group in ENTITY_GROUPS
            },
            "event_count": len(result.events),
        },
    )
    mark_job_status(conn, job_id, "done")
    conn.commit()
    return (
        f"enriched label_count={result.label_count} "
        f"entities={sum(len(values) for values in result.entities.values())} "
        f"events={len(result.events)}"
    )


def open_connection() -> psycopg.Connection[Any]:
    return psycopg.connect(DB_DSN, row_factory=dict_row)


def main(argv: list[str] | None = None) -> int:
    global LOG_FILE

    args = parse_args(argv)
    LOG_FILE = args.log_file
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
            raw_output = call_agent(
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
                worker="extract_semantics_worker",
                clean_item_id=clean_item_id,
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
            worker="extract_semantics_worker",
            clean_item_id=clean_item_id,
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
            "Starting extract_semantics worker "
            f"batch_size={args.batch_size} once={args.once} "
            f"poll_interval={args.poll_interval} agent_id={args.agent_id} "
            f"rate_limit_per_minute={args.rate_limit_per_minute} "
            f"resume={resume} retry_failed={args.retry_failed} "
            f"clean_item_id_min={clean_item_id_min} "
            f"clean_item_id_max={clean_item_id_max}"
        ),
    )

    try:
        while True:
            claimed_jobs = claim_jobs(
                conn,
                args.batch_size,
                retry_failed=args.retry_failed,
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
        log_line("INFO", "extract_semantics worker stopped.")

    return 0


load_dotenv(ENV_PATH)
DB_DSN = os.environ["AGENT_1_DB_DSN"]
LOG_FILE = os.getenv("AGENT_1_SEMANTICS_LOG_FILE", str(DEFAULT_LOG_FILE))


if __name__ == "__main__":
    raise SystemExit(main())
