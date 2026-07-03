from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_1.label_kr_worker import (
    LabelValidationError,
    collect_payload_text,
    extract_json_object_text,
    is_direct_agent_payload,
    load_dotenv,
    parse_int_env,
    resolve_default_openclaw_cmd,
)


ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_AGENT_ID = "agent_2"
DEFAULT_AGENT_TIMEOUT_SECONDS = 120
DEFAULT_SESSION_KEY_PREFIX = "kr-enrich"
ALLOWED_SOURCE_TYPES = (
    "СМИ",
    "Блоги",
    "Мессенджеры",
    "Соц сети",
    "Видеохостинг",
    "Форумы",
    "Отзывы",
    "Микроблог",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Call agent_2 and persist KR enrichment into agent_1.key_results."
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--only-missing",
        action="store_true",
        help=(
            "Process active KRs with missing enrichment plus rows still marked "
            "as agent_2_test_run."
        ),
    )
    scope.add_argument(
        "--force",
        action="store_true",
        help="Recompute all active KRs regardless of existing enrichment.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Call agent_2 but do not write results to PostgreSQL.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=parse_int_env("AGENT_2_SYNC_LIMIT", 0),
        help="Optional limit on processed KRs. Default: disabled.",
    )
    parser.add_argument(
        "--openclaw-cmd",
        default=os.getenv("AGENT_1_OPENCLAW_CMD", resolve_default_openclaw_cmd()),
        help="OpenClaw executable or command prefix used to call agent_2.",
    )
    parser.add_argument(
        "--agent-id",
        default=os.getenv("AGENT_2_SYNC_AGENT_ID", DEFAULT_AGENT_ID),
        help=f"OpenClaw agent id to invoke. Default: {DEFAULT_AGENT_ID}.",
    )
    parser.add_argument(
        "--agent-timeout",
        type=int,
        default=int(
            os.getenv(
                "AGENT_2_SYNC_AGENT_TIMEOUT_SECONDS",
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
        default=os.getenv("AGENT_2_SYNC_MODEL"),
        help="Optional OpenClaw model override.",
    )
    parser.add_argument(
        "--thinking",
        default=os.getenv("AGENT_2_SYNC_THINKING"),
        help="Optional OpenClaw thinking level override.",
    )
    parser.add_argument(
        "--session-key-prefix",
        default=os.getenv("AGENT_2_SYNC_SESSION_KEY_PREFIX", DEFAULT_SESSION_KEY_PREFIX),
        help=(
            "Session-key suffix prefix for agent_2 calls. "
            f"Default: {DEFAULT_SESSION_KEY_PREFIX}."
        ),
    )
    return parser.parse_args(argv)


def parse_json_object(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = json.loads(extract_json_object_text(raw))

    if not isinstance(parsed, dict):
        raise LabelValidationError("agent_2 output JSON must be an object")
    return parsed


def extract_agent_reply_text(agent_response: dict[str, Any]) -> str:
    if is_direct_agent_payload(agent_response) or "тема" in agent_response:
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


def parse_agent_2_payload(raw: str) -> dict[str, Any]:
    outer = parse_json_object(raw)
    if "тема" in outer:
        return outer
    reply_text = extract_agent_reply_text(outer)
    return parse_json_object(reply_text)


def build_goal_text(row: dict[str, Any]) -> str:
    return "\n\n".join(
        part.strip()
        for part in (row.get("title"), row.get("description"))
        if isinstance(part, str) and part.strip()
    )


def build_session_key(prefix: str, kr_id: int) -> str:
    safe_prefix = "".join(
        char if char.isalnum() or char in {"-", "_"} else "-" for char in prefix.strip()
    ).strip("-_")
    if not safe_prefix:
        safe_prefix = DEFAULT_SESSION_KEY_PREFIX
    return f"agent:agent_2:{safe_prefix}-kr-{kr_id}"


def call_agent_2(
    goal_text: str,
    *,
    kr_id: int,
    openclaw_cmd: str,
    agent_id: str,
    agent_timeout: int,
    model: str | None,
    thinking: str | None,
    session_key_prefix: str,
) -> dict[str, Any]:
    cmd = shlex.split(openclaw_cmd)
    if not cmd:
        raise RuntimeError("openclaw command is empty")

    args = [
        *cmd,
        "agent",
        "--agent",
        agent_id,
        "--session-key",
        build_session_key(session_key_prefix, kr_id),
        "--message",
        goal_text,
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
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise RuntimeError(f"openclaw agent_2 failed: {detail}")
    return parse_agent_2_payload(completed.stdout)


def normalize_keywords(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise LabelValidationError("ключевые_слова must be a non-empty list")
    normalized: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise LabelValidationError(f"ключевые_слова[{index}] must be a non-empty string")
        keyword = item.strip()
        if keyword not in normalized:
            normalized.append(keyword)
    return normalized


def parse_importance(value: Any, index: int) -> int:
    if isinstance(value, bool):
        raise LabelValidationError(f"типы_источников[{index}].важность must be 1, 2, or 3")
    if isinstance(value, int):
        importance = value
    elif isinstance(value, str) and value.strip().isdigit():
        importance = int(value.strip())
    else:
        raise LabelValidationError(f"типы_источников[{index}].важность must be 1, 2, or 3")
    if importance not in {1, 2, 3}:
        raise LabelValidationError(f"типы_источников[{index}].важность must be 1, 2, or 3")
    return importance


def normalize_source_types(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise LabelValidationError("типы_источников must be a non-empty list")

    normalized: list[dict[str, Any]] = []
    seen_types: set[str] = set()
    count_importance_3 = 0
    count_importance_2 = 0
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise LabelValidationError(f"типы_источников[{index}] must be an object")
        source_type = item.get("тип")
        if not isinstance(source_type, str) or source_type.strip() not in ALLOWED_SOURCE_TYPES:
            raise LabelValidationError(
                f"типы_источников[{index}].тип must be one of: {', '.join(ALLOWED_SOURCE_TYPES)}"
            )
        normalized_type = source_type.strip()
        if normalized_type in seen_types:
            raise LabelValidationError(f"duplicate типы_источников.тип: {normalized_type}")
        seen_types.add(normalized_type)

        importance = parse_importance(item.get("важность"), index)
        if importance == 3:
            count_importance_3 += 1
        if importance == 2:
            count_importance_2 += 1
        reason = item.get("причина")
        if not isinstance(reason, str) or not reason.strip():
            raise LabelValidationError(f"типы_источников[{index}].причина must be a non-empty string")
        normalized.append(
            {
                "тип": normalized_type,
                "важность": importance,
                "причина": reason.strip(),
            }
        )

    if count_importance_3 > 2:
        raise LabelValidationError("типы_источников may contain at most two sources with важность=3")
    if count_importance_2 > 3:
        raise LabelValidationError("типы_источников may contain at most three sources with важность=2")
    return normalized


def validate_enrichment_payload(payload: dict[str, Any]) -> dict[str, Any]:
    topic = payload.get("тема")
    if not isinstance(topic, str) or not topic.strip():
        raise LabelValidationError("тема must be a non-empty string")

    return {
        "тема": topic.strip(),
        "ключевые_слова": normalize_keywords(payload.get("ключевые_слова")),
        "типы_источников": normalize_source_types(payload.get("типы_источников")),
    }


def fetch_key_results(
    conn: psycopg.Connection[Any],
    *,
    only_missing: bool,
    limit: int | None,
) -> list[dict[str, Any]]:
    where = (
        "(enrichment IS NULL OR enriched_by = 'agent_2_test_run')"
        if only_missing
        else "TRUE"
    )
    limit_sql = "LIMIT %s" if limit is not None else ""
    params: list[Any] = []
    if limit is not None:
        params.append(limit)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, title, description, enrichment, enriched_by
            FROM agent_1.key_results
            WHERE active IS TRUE
              AND {where}
            ORDER BY id
            {limit_sql}
            """,
            params,
        )
        return list(cur.fetchall())


def update_key_result(
    conn: psycopg.Connection[Any],
    *,
    kr_id: int,
    enrichment: dict[str, Any],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE agent_1.key_results
            SET enrichment = %s::jsonb,
                enriched_at = %s,
                enriched_by = 'agent_2'
            WHERE id = %s
            """,
            (
                json.dumps(enrichment, ensure_ascii=False),
                datetime.now(timezone.utc),
                kr_id,
            ),
        )


def open_connection() -> psycopg.Connection[Any]:
    return psycopg.connect(DB_DSN, row_factory=dict_row)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    limit = args.limit or None
    conn = open_connection()
    try:
        rows = fetch_key_results(conn, only_missing=args.only_missing, limit=limit)
        print(f"к обработке целей: {len(rows)}")
        ok = 0
        fail = 0

        for row in rows:
            goal_text = build_goal_text(row)
            try:
                payload = call_agent_2(
                    goal_text,
                    kr_id=row["id"],
                    openclaw_cmd=args.openclaw_cmd,
                    agent_id=args.agent_id,
                    agent_timeout=args.agent_timeout,
                    model=args.model,
                    thinking=args.thinking,
                    session_key_prefix=args.session_key_prefix,
                )
                enrichment = validate_enrichment_payload(payload)
                if args.dry_run:
                    print(
                        "[dry] kr={kr_id} -> {payload}".format(
                            kr_id=row["id"],
                            payload=json.dumps(enrichment, ensure_ascii=False),
                        )
                    )
                else:
                    update_key_result(conn, kr_id=row["id"], enrichment=enrichment)
                    conn.commit()
                    print(
                        "[ok] kr={kr_id} old_by={old_by} new_by=agent_2".format(
                            kr_id=row["id"],
                            old_by=row.get("enriched_by"),
                        )
                    )
                ok += 1
            except Exception as exc:
                conn.rollback()
                fail += 1
                print(f"[fail] kr={row['id']}: {exc}", file=sys.stderr)

        print(f"готово: ok={ok} fail={fail}")
        return 0 if fail == 0 else 1
    finally:
        conn.close()


load_dotenv(ENV_PATH)
DB_DSN = os.environ["AGENT_1_DB_DSN"]


if __name__ == "__main__":
    raise SystemExit(main())
