from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_1.kr_enrichment_sync import call_agent_2, validate_enrichment_payload  # noqa: E402
from agent_1.label_kr_worker import (  # noqa: E402
    build_impact_prompt,
    build_sber_paid_news_prompt,
    call_agent_1,
    extract_usage_summary,
    get_document_source_keys,
    label_clean_item,
    load_dotenv,
    parse_agent_label_payload,
    resolve_default_openclaw_cmd,
    validate_entity_tonality_payload,
    validate_impact_payload,
    validate_sber_paid_news_payload,
)


ENV_PATH = ROOT_DIR / ".env"
TRACE_LOG = Path(__file__).with_name("run_trace.log")
DB_DSN_ENV = "AGENT_1_DB_DSN"
DEFAULT_LIMIT = 25


@dataclass
class StepUsage:
    llm_calls: int = 0
    tokens: int = 0
    seconds: float = 0.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run isolated demo trace over schema demo.")
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--openclaw-cmd", default=os.getenv("AGENT_1_OPENCLAW_CMD", resolve_default_openclaw_cmd()))
    parser.add_argument("--agent-1", default=os.getenv("AGENT_1_LABEL_AGENT_ID", "agent_1"))
    parser.add_argument("--agent-2", default=os.getenv("AGENT_2_SYNC_AGENT_ID", "agent_2"))
    parser.add_argument("--agent-timeout", type=int, default=600)
    parser.add_argument("--model", default=os.getenv("AGENT_1_LABEL_MODEL"))
    parser.add_argument("--thinking", default=os.getenv("AGENT_1_LABEL_THINKING"))
    return parser.parse_args(argv)


def connect() -> psycopg.Connection[Any]:
    load_dotenv(ENV_PATH)
    return psycopg.connect(os.environ[DB_DSN_ENV], row_factory=dict_row)


def now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def append_trace(text: str) -> None:
    TRACE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TRACE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def print_block(step_no: int, name: str, inp: str, did: str, total: str, billing: str, examples: list[str]) -> None:
    block = "\n".join(
        [
            f"── ШАГ {step_no}: {name} ──",
            f"вход: {inp}",
            f"сделал: {did}",
            f"итог: {total}",
            f"биллинг: {billing}",
            "примеры: " + (" | ".join(examples) if examples else "—"),
        ]
    )
    print(block)
    append_trace(f"[{now_text()}]\n{block}\n")


def estimate_tokens(usage: dict[str, Any]) -> int:
    total = 0
    for item in usage.get("openclaw_usage_fields", []):
        value = item.get("value")
        if isinstance(value, int):
            total += value
        elif isinstance(value, dict):
            for nested in value.values():
                if isinstance(nested, int):
                    total += nested
    return total


def insert_kr(conn: psycopg.Connection[Any], text: str, enrichment: dict[str, Any]) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO demo.kr (text, enrichment, enriched_at)
            VALUES (%s, %s::jsonb, now())
            RETURNING id
            """,
            (text, json.dumps(enrichment, ensure_ascii=False)),
        )
        row = cur.fetchone()
    conn.commit()
    return int(row["id"])


def enrich_query(conn: psycopg.Connection[Any], args: argparse.Namespace) -> tuple[int, dict[str, Any], StepUsage]:
    started = time.monotonic()
    raw = call_agent_2(
        args.query,
        kr_id=0,
        openclaw_cmd=args.openclaw_cmd,
        agent_id=args.agent_2,
        agent_timeout=args.agent_timeout,
        model=args.model,
        thinking=args.thinking,
        session_key_prefix="demo-enrich",
    )
    enrichment = validate_enrichment_payload(raw)
    kr_id = insert_kr(conn, args.query, enrichment)
    elapsed = time.monotonic() - started
    usage = StepUsage(llm_calls=1, seconds=elapsed)
    return kr_id, enrichment, usage


def fetch_corpus(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, raw_id, url, title, created_at, source, content
            FROM demo.clean_items
            WHERE is_duplicate IS FALSE
            ORDER BY created_at, id
            """
        )
        return list(cur.fetchall())


def normalize_source_type(value: str) -> str:
    mapping = {
        "сми": "сми",
        "блог": "блог",
        "блоги": "блог",
        "мессенджеры": "мессенджеры",
        "соц сети": "соц сети",
        "видеохостинг": "видеохостинг",
        "форумы": "форумы",
        "отзывы": "отзывы",
        "микроблог": "микроблог",
    }
    key = value.strip().casefold()
    return mapping.get(key, key)


def filter_corpus(items: list[dict[str, Any]], enrichment: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    keywords = [keyword.casefold() for keyword in enrichment.get("ключевые_слова", []) if isinstance(keyword, str)]
    ranked_source_types = {
        normalize_source_type(item["тип"])
        for item in enrichment.get("типы_источников", [])
        if isinstance(item, dict) and int(item.get("важность", 0)) >= 2 and isinstance(item.get("тип"), str)
    }
    passed: list[dict[str, Any]] = []
    dropped = {"keyword_miss": 0, "source_type_miss": 0}

    for item in items:
        haystack = f"{item.get('title') or ''}\n{item['content']}".casefold()
        keyword_match = any(keyword in haystack for keyword in keywords) if keywords else True
        doc_keys = get_document_source_keys(
            {
                "source": item["source"],
                "source_metadata": {"source": item["source"]},
                "raw_payload": {},
                "url": item["url"],
            }
        )
        doc_source_types = set(doc_keys.get("source_type", ()))
        source_match = (not ranked_source_types) or bool(doc_source_types & ranked_source_types) or not doc_source_types

        if keyword_match and source_match:
            passed.append(item)
        else:
            if not keyword_match:
                dropped["keyword_miss"] += 1
            elif not source_match:
                dropped["source_type_miss"] += 1

    return passed, dropped


def summarize_items(items: list[dict[str, Any]]) -> tuple[str, list[str]]:
    by_source = Counter(item["source"] for item in items)
    by_day = Counter(item["created_at"].date().isoformat() for item in items)
    total = f"сколько={len(items)} | source={dict(by_source)} | days={dict(sorted(by_day.items()))}"
    examples = [f"[{item['id']}] {item.get('title') or '(без заголовка)'}" for item in items[:3]]
    return total, examples


def insert_label(
    conn: psycopg.Connection[Any],
    *,
    clean_item_id: int,
    kr_id: int,
    impact: str,
    sber_paid_news: int | None,
    entity_tonality: dict[str, Any] | None,
    raw_json: dict[str, Any],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO demo.doc_labels (
                clean_item_id, kr_id, impact, sber_paid_news, entity_tonality, raw_json, created_at
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, now())
            ON CONFLICT (clean_item_id, kr_id) DO UPDATE SET
                impact = EXCLUDED.impact,
                sber_paid_news = EXCLUDED.sber_paid_news,
                entity_tonality = EXCLUDED.entity_tonality,
                raw_json = EXCLUDED.raw_json,
                created_at = now()
            """,
            (
                clean_item_id,
                kr_id,
                impact,
                sber_paid_news,
                json.dumps(entity_tonality, ensure_ascii=False) if entity_tonality is not None else None,
                json.dumps(raw_json, ensure_ascii=False),
            ),
        )
    conn.commit()


def run_labeling(
    conn: psycopg.Connection[Any],
    items: list[dict[str, Any]],
    kr_id: int,
    query: str,
    args: argparse.Namespace,
) -> tuple[list[str], str, StepUsage]:
    usage = StepUsage()
    lines: list[str] = []
    impact_counter = Counter()
    paid_counter = 0
    step_times: list[float] = []
    for item in items:
        clean_item = {
            "id": item["id"],
            "raw_item_id": item["raw_id"],
            "clean_title": item.get("title"),
            "clean_text": item["content"],
            "source": item["source"],
            "source_metadata": {"source": item["source"]},
            "url": item["url"],
        }

        def runner(prompt: str, _item: dict[str, Any], job_id: int, step_name: str) -> str:
            started = time.monotonic()
            output = call_agent_1(
                prompt,
                clean_item,
                job_id,
                step_name,
                openclaw_cmd=args.openclaw_cmd,
                agent_id=args.agent_1,
                agent_timeout=args.agent_timeout,
                model=args.model,
                thinking=args.thinking,
                session_key_prefix="demo-label",
            )
            elapsed = time.monotonic() - started
            usage.llm_calls += 1
            usage.seconds += elapsed
            usage.tokens += estimate_tokens(extract_usage_summary(output, prompt_chars=len(prompt)))
            step_times.append(elapsed)
            return output

        result = label_clean_item(
            clean_item,
            [{"id": kr_id, "title": query, "description": "", "enrichment": None}],
            job_id=item["id"],
            agent_runner=runner,
            conn=None,
            resume=False,
        )
        label = result.labels[0]
        impact_counter[label.impact] += 1
        paid = label.is_sber_paid_news
        if paid == 1:
            paid_counter += 1
        insert_label(
            conn,
            clean_item_id=item["id"],
            kr_id=kr_id,
            impact=label.impact,
            sber_paid_news=label.is_sber_paid_news,
            entity_tonality=label.prompt3_payload,
            raw_json={
                "impact": label.prompt1_payload,
                "sber_paid_news": label.prompt2_payload,
                "entity_tonality": label.prompt3_payload,
            },
        )
        extra = ""
        if label.impact in {"positive", "negative"}:
            extra = f" +sber_paid={label.is_sber_paid_news} tonality_mentions={len((label.prompt3_payload or {}).get('mentions', []))}"
        line = f"[{item['id']}] impact={label.impact}{extra}"
        print(line)
        append_trace(line)
        lines.append(line)

    avg = sum(step_times) / len(step_times) if step_times else 0.0
    summary = (
        f"размечено={len(items)} | impact={dict(impact_counter)} | "
        f"paid_news={paid_counter} | avg_step_time={avg:.2f}s"
    )
    return lines, summary, usage


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    conn = connect()
    try:
        append_trace(f"\n===== demo_run start {now_text()} query={args.query!r} =====")
        kr_id, enrichment, enrich_usage = enrich_query(conn, args)
        print_block(
            5,
            "ОБОГАЩЕНИЕ",
            args.query,
            "вызвал agent_2 и сохранил enrichment в demo.kr",
            f"kr_id={kr_id} | тема={enrichment['тема']} | ключевые={len(enrichment['ключевые_слова'])} | типы={len(enrichment['типы_источников'])}",
            f"время {enrich_usage.seconds:.2f}s | вызовов LLM {enrich_usage.llm_calls} | токены {enrich_usage.tokens}",
            [
                f"тема={enrichment['тема']}",
                "keywords=" + ", ".join(enrichment["ключевые_слова"][:5]),
                "types=" + ", ".join(f"{row['тип']}:{row['важность']}" for row in enrichment["типы_источников"][:3]),
            ],
        )

        corpus = fetch_corpus(conn)
        filtered, dropped = filter_corpus(corpus, enrichment)
        total_text, examples = summarize_items(filtered)
        print_block(
            6,
            "ФИЛЬТР КОРПУСА",
            f"demo.clean_items: всего={len(corpus)}",
            "отфильтровал по ключевым словам в title/content и по ranked source types при наличии",
            total_text + f" | отсев={len(corpus) - len(filtered)} reason={dropped}",
            "время 0.00s | вызовов LLM 0 | токены 0",
            examples,
        )

        limited = filtered[: args.limit]
        lines, summary, label_usage = run_labeling(conn, limited, kr_id, args.query, args)
        print_block(
            7,
            "РАЗМЕТКА",
            f"на вход прошло={len(filtered)} | лимит={args.limit}",
            "разметил документы через agent_1 в 3 шага: impact, затем paid-news и entity tonality для pos/neg",
            summary + f" | неразмечено_из_остатка={max(0, len(filtered) - len(limited))}",
            f"время {label_usage.seconds:.2f}s | вызовов LLM {label_usage.llm_calls} | токены {label_usage.tokens}",
            lines[:3],
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
