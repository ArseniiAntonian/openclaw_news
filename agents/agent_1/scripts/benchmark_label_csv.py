#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import psycopg

from agent_1.label_kr_worker import (
    build_entity_tonality_prompt,
    build_evidence_text,
    build_impact_prompt,
    build_relevance_prompt,
    build_sber_paid_news_prompt,
    call_agent_1,
    parse_agent_label_payload,
    validate_entity_tonality_payload,
    validate_impact_payload,
    validate_relevance_payload,
    validate_sber_paid_news_payload,
)


DEFAULT_INPUT_GLOB = "/root/news_goal_*"
DEFAULT_OUTPUT_DIR = "/root/agent1_prompt_benchmark_out"
DEFAULT_REPORT_EVERY = 25
DEFAULT_MAX_ATTEMPTS = 3

INFLUENCE_COLUMN = "влияние"
DIRECTION_COLUMN = "направленность влияния на достижение КР"
SIGNAL_COLUMN = "сила сигнала"

IMPACT_TO_DIRECTION = {
    "neutral": "0",
    "positive": "1",
    "negative": "2",
}

SIGNAL_TO_TEXT = {
    "direct": "прямое",
    "indirect": "косвенное",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark CSV labeling with the same prompts used by agent_1."
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        default=[DEFAULT_INPUT_GLOB],
        help="Input files or glob patterns. Default: /root/news_goal_*",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for predicted CSV files. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--cache-file",
        default="doc_cache.jsonl",
        help="Cache file name inside output-dir. Default: doc_cache.jsonl",
    )
    parser.add_argument(
        "--openclaw-cmd",
        default=os.getenv("AGENT_1_OPENCLAW_CMD", "openclaw"),
        help="OpenClaw executable or command prefix.",
    )
    parser.add_argument(
        "--agent-id",
        default=os.getenv("AGENT_1_LABEL_AGENT_ID", "agent_1"),
        help="OpenClaw agent id. Default: agent_1",
    )
    parser.add_argument(
        "--agent-timeout",
        type=int,
        default=int(os.getenv("AGENT_1_LABEL_AGENT_TIMEOUT_SECONDS", "600")),
        help="Per-call timeout in seconds. Default: 600",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("AGENT_1_LABEL_MODEL"),
        help="Optional model override.",
    )
    parser.add_argument(
        "--thinking",
        default=os.getenv("AGENT_1_LABEL_THINKING"),
        help="Optional thinking override.",
    )
    parser.add_argument(
        "--session-key-prefix",
        default="benchmark-csv",
        help="Session key prefix for OpenClaw calls.",
    )
    parser.add_argument(
        "--max-rows-per-file",
        type=int,
        default=0,
        help="Optional limit for smoke runs. 0 means all rows.",
    )
    parser.add_argument(
        "--report-every",
        type=int,
        default=DEFAULT_REPORT_EVERY,
        help=f"Progress reporting cadence. Default: {DEFAULT_REPORT_EVERY}",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help=f"Retries for malformed agent replies. Default: {DEFAULT_MAX_ATTEMPTS}",
    )
    return parser.parse_args(argv)


def load_simple_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def expand_inputs(patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        candidate = Path(pattern)
        matched = [candidate] if candidate.exists() else sorted(Path("/").glob(pattern.lstrip("/")))
        for path in matched:
            resolved = path.resolve()
            if resolved.is_file() and resolved not in seen:
                seen.add(resolved)
                paths.append(resolved)
    return sorted(paths)


def load_active_krs(dsn: str) -> dict[int, dict[str, Any]]:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, description, enrichment
                FROM agent_1.key_results
                WHERE active
                ORDER BY id
                """
            )
            columns = [desc.name for desc in cur.description]
            return {
                int(row[0]): dict(zip(columns, row))
                for row in cur.fetchall()
            }


def extract_target_kr_id(path: Path) -> int:
    match = re.search(r"news_goal_(\d+)_", path.name)
    if match is None:
        raise ValueError(f"cannot infer KR id from file name: {path}")
    return int(match.group(1))


def read_csv(path: Path, *, max_rows: int) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        fieldnames = list(reader.fieldnames or [])
        rows: list[dict[str, str]] = []
        for index, row in enumerate(reader, start=1):
            rows.append({key: value or "" for key, value in row.items()})
            if max_rows and index >= max_rows:
                break
    return fieldnames, rows


def fingerprint_row(row: dict[str, str]) -> str:
    title = row.get("title", "").strip()
    raw_text = row.get("raw_text", "").strip()
    payload = json.dumps([title, raw_text], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def build_clean_item(row: dict[str, str], synthetic_id: int) -> dict[str, Any]:
    return {
        "id": synthetic_id,
        "clean_title": row.get("title", "").strip(),
        "raw_title": row.get("title", "").strip(),
        "clean_text": row.get("raw_text", "").strip(),
        "source": "",
        "url": "",
        "source_metadata": {},
        "raw_payload": {},
    }


def load_cache(path: Path) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return cache
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            record = json.loads(line)
            fingerprint = record.get("fingerprint")
            result = record.get("result")
            if isinstance(fingerprint, str) and isinstance(result, dict):
                cache[fingerprint] = result
    return cache


def append_cache_record(path: Path, fingerprint: str, result: dict[str, Any]) -> None:
    record = {
        "fingerprint": fingerprint,
        "result": result,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False))
        handle.write("\n")


def append_retry_log(
    path: Path,
    *,
    step_name: str,
    job_id: int,
    attempt: int,
    error: Exception,
    raw_output: str,
) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "step_name": step_name,
                    "job_id": job_id,
                    "attempt": attempt,
                    "error": str(error),
                    "raw_output": raw_output,
                },
                ensure_ascii=False,
            )
        )
        handle.write("\n")


def call_and_parse(
    *,
    prompt: str,
    clean_item: dict[str, Any],
    job_id: int,
    step_name: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    retry_log_path = Path(args.output_dir) / "retry_failures.jsonl"
    last_error: Exception | None = None

    for attempt in range(1, max(1, args.max_attempts) + 1):
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
        try:
            return parse_agent_label_payload(raw_output)
        except Exception as exc:
            last_error = exc
            append_retry_log(
                retry_log_path,
                step_name=step_name,
                job_id=job_id,
                attempt=attempt,
                error=exc,
                raw_output=raw_output,
            )
            if attempt >= max(1, args.max_attempts):
                break
            print(
                f"retry step={step_name} job_id={job_id} attempt={attempt} error={exc}",
                flush=True,
            )

    assert last_error is not None
    raise last_error


def classify_document(
    *,
    fingerprint: str,
    clean_item: dict[str, Any],
    active_krs: dict[int, dict[str, Any]],
    job_id: int,
    args: argparse.Namespace,
) -> dict[str, Any]:
    ordered_krs = [active_krs[kr_id] for kr_id in sorted(active_krs)]
    evidence_text = build_evidence_text(clean_item)

    relevance_payload = call_and_parse(
        prompt=build_relevance_prompt(clean_item, ordered_krs),
        clean_item=clean_item,
        job_id=job_id,
        step_name="relevance",
        args=args,
    )
    relevance_matches = validate_relevance_payload(
        relevance_payload,
        evidence_text=evidence_text,
        candidate_kr_ids=set(active_krs),
    )

    labels_by_kr: dict[str, dict[str, Any]] = {}
    for match in relevance_matches:
        kr_id = match.kr_id
        impact_payload = call_and_parse(
            prompt=build_impact_prompt(clean_item, active_krs[kr_id]),
            clean_item=clean_item,
            job_id=job_id,
            step_name=f"impact-kr-{kr_id}",
            args=args,
        )
        label = validate_impact_payload(
            impact_payload,
            evidence_text=evidence_text,
            kr_id=kr_id,
        )

        label_result: dict[str, Any] = {
            "impact": label.impact,
            "signal_strength": label.signal_strength,
            "confidence": label.confidence,
            "theme": label.theme,
            "dashboard_description": label.dashboard_description,
            "why_for_goal": label.why_for_goal,
            "evidence": list(label.evidence),
            "reasoning_steps": list(label.reasoning_steps),
            "uncertainty": label.uncertainty,
            "prompt1_payload": impact_payload,
        }

        if label.impact in {"positive", "negative"}:
            paid_payload = call_and_parse(
                prompt=build_sber_paid_news_prompt(clean_item),
                clean_item=clean_item,
                job_id=job_id,
                step_name=f"sber-paid-kr-{kr_id}",
                args=args,
            )
            entity_payload = call_and_parse(
                prompt=build_entity_tonality_prompt(clean_item),
                clean_item=clean_item,
                job_id=job_id,
                step_name=f"entity-tonality-kr-{kr_id}",
                args=args,
            )
            label_result["is_sber_paid_news"] = validate_sber_paid_news_payload(paid_payload)
            label_result["prompt2_payload"] = paid_payload
            label_result["prompt3_payload"] = validate_entity_tonality_payload(
                entity_payload,
                evidence_text=evidence_text,
            )

        labels_by_kr[str(kr_id)] = label_result

    return {
        "fingerprint": fingerprint,
        "relevant_kr_ids": [match.kr_id for match in relevance_matches],
        "labels_by_kr": labels_by_kr,
    }


def predicted_columns(
    doc_result: dict[str, Any],
    *,
    target_kr_id: int,
) -> tuple[str, str, str]:
    label = doc_result.get("labels_by_kr", {}).get(str(target_kr_id))
    if not isinstance(label, dict):
        return "0", "0", "0"

    impact = str(label.get("impact", "")).strip()
    signal = str(label.get("signal_strength", "")).strip()
    return (
        "1",
        IMPACT_TO_DIRECTION.get(impact, "0"),
        SIGNAL_TO_TEXT.get(signal, "0"),
    )


def ensure_row_fields(row: dict[str, str], fieldnames: list[str]) -> dict[str, str]:
    normalized = dict(row)
    for fieldname in fieldnames:
        normalized.setdefault(fieldname, "")
    return normalized


def write_predictions(
    *,
    source_path: Path,
    output_path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
    target_kr_id: int,
    cache: dict[str, dict[str, Any]],
    active_krs: dict[int, dict[str, Any]],
    args: argparse.Namespace,
    next_doc_id: int,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    created_docs = 0
    started_at = time.time()
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8-sig",
            newline="",
            delete=False,
            dir=str(output_path.parent),
            prefix=output_path.name + ".",
            suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";", lineterminator="\n")
            writer.writeheader()

            for index, row in enumerate(rows, start=1):
                fingerprint = fingerprint_row(row)
                doc_result = cache.get(fingerprint)
                if doc_result is None:
                    clean_item = build_clean_item(row, next_doc_id + created_docs)
                    doc_result = classify_document(
                        fingerprint=fingerprint,
                        clean_item=clean_item,
                        active_krs=active_krs,
                        job_id=10_000_000 + next_doc_id + created_docs,
                        args=args,
                    )
                    cache[fingerprint] = doc_result
                    append_cache_record(
                        Path(args.output_dir) / args.cache_file,
                        fingerprint,
                        doc_result,
                    )
                    created_docs += 1

                influence, direction, signal = predicted_columns(
                    doc_result,
                    target_kr_id=target_kr_id,
                )
                out_row = ensure_row_fields(row, fieldnames)
                out_row[INFLUENCE_COLUMN] = influence
                out_row[DIRECTION_COLUMN] = direction
                out_row[SIGNAL_COLUMN] = signal
                writer.writerow(out_row)

                if args.report_every and (index % args.report_every == 0 or index == len(rows)):
                    elapsed = time.time() - started_at
                    print(
                        (
                            f"[{source_path.name}] rows={index}/{len(rows)} "
                            f"new_docs={created_docs} cache_size={len(cache)} elapsed={elapsed:.1f}s"
                        ),
                        flush=True,
                    )
        os.replace(temp_path, output_path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    return created_docs


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    script_path = Path(__file__).resolve()
    agent_root = script_path.parents[1]
    load_simple_dotenv(agent_root / ".env")

    dsn = os.getenv("AGENT_1_DB_DSN", "").strip()
    if not dsn:
        print("AGENT_1_DB_DSN is not set", file=sys.stderr)
        return 2

    input_paths = expand_inputs(args.inputs)
    if not input_paths:
        print("No input files found", file=sys.stderr)
        return 2

    active_krs = load_active_krs(dsn)
    if not active_krs:
        print("No active KR rows found in agent_1.key_results", file=sys.stderr)
        return 2

    for path in input_paths:
        kr_id = extract_target_kr_id(path)
        if kr_id not in active_krs:
            print(f"KR {kr_id} from {path.name} is absent in DB", file=sys.stderr)
            return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / args.cache_file
    cache = load_cache(cache_path)

    total_rows = 0
    rows_by_path: dict[Path, tuple[list[str], list[dict[str, str]]]] = {}
    for path in input_paths:
        fieldnames, rows = read_csv(path, max_rows=args.max_rows_per_file)
        rows_by_path[path] = (fieldnames, rows)
        total_rows += len(rows)

    print(
        f"files={len(input_paths)} rows={total_rows} cache_loaded={len(cache)} output_dir={output_dir}",
        flush=True,
    )

    next_doc_id = len(cache) + 1
    total_new_docs = 0
    for path in input_paths:
        fieldnames, rows = rows_by_path[path]
        target_kr_id = extract_target_kr_id(path)
        output_path = output_dir / path.name
        print(
            f"processing file={path.name} target_kr_id={target_kr_id} rows={len(rows)}",
            flush=True,
        )
        created_docs = write_predictions(
            source_path=path,
            output_path=output_path,
            fieldnames=fieldnames,
            rows=rows,
            target_kr_id=target_kr_id,
            cache=cache,
            active_krs=active_krs,
            args=args,
            next_doc_id=next_doc_id + total_new_docs,
        )
        total_new_docs += created_docs

    print(
        f"done files={len(input_paths)} rows={total_rows} new_docs={total_new_docs} cache_size={len(cache)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
