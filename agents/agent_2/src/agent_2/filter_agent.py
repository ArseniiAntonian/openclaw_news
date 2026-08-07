#!/usr/bin/env python3
"""Агент 2: отбор документов под объект наблюдения ретривом.

Перенесено из `experiments/retrieval_eval/filter_agent.py` (tasks.md 4.2),
адаптировано под каталог в БД и LLM-порог вместо `adaptive_cutoff()`
(design D3 -- отсечка по разрыву в распределении отклонена, заменена на
LLM-рубрику 0-10 с порогом 7.5).

Конструкция (design.md, D1-D10):

1. Лексический канал -- ключевые слова каталога (`channels.keyword_hits`).
2. Векторный канал -- эмбеддинг `search_description`, top-500
   (`channels.vector_scored`).
3. Объединение (union, не пересечение) -- `channels.union_candidates`.
4. LLM-оценка 0-10 по рубрике через OpenClaw, с кэшем
   (`llm_scoring.score_candidates`).
5. Порог 7.5.
6. Негатив-фильтр каталога как вето -- после порога.
7. Запись прошедших в `agent_1_v5.agent_2_relevant_documents`
   (M:N объект<->документ, specs/agent_2-filtering/spec.md).

    python -m agent_2.filter_agent --object 1
    python -m agent_2.filter_agent --all
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_2 import catalog, channels
from agent_2.db import SCHEMA, load_dotenv, set_ef_search
from agent_2.llm_scoring import THRESHOLD, ScoringConfig, resolve_default_openclaw_cmd, score_candidates

DEFAULT_ENV = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_AGENT_ID = "agent_2"


def fetch_candidate_texts(conn, doc_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not doc_ids:
        return {}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT c.id_clean_post, coalesce(r.title, '') AS title,
                   coalesce(c.clean_content, '') AS text
            FROM {SCHEMA}.clean_posts c
            JOIN {SCHEMA}.raw_posts r ON r.id_raw_post = c.id_raw_post
            WHERE c.id_clean_post = ANY(%s)
            """,
            (doc_ids,),
        )
        return {row["id_clean_post"]: row for row in cur.fetchall()}


def write_relevant_documents(
    conn, id_object: int, selected: dict[int, float]
) -> None:
    """Запись итога в agent_2_relevant_documents -- только прошедшие порог
    и негатив-фильтр (specs/agent_2-filtering/spec.md, «Итог отбора
    пишется в agent_2_relevant_documents»)."""
    with conn.cursor() as cur:
        for id_clean_post, llm_score in selected.items():
            cur.execute(
                f"""
                INSERT INTO {SCHEMA}.agent_2_relevant_documents
                    (id_object, id_clean_post, llm_score)
                VALUES (%s, %s, %s)
                ON CONFLICT (id_object, id_clean_post)
                DO UPDATE SET llm_score = EXCLUDED.llm_score, selected_at = now()
                """,
                (id_object, id_clean_post, llm_score),
            )
    conn.commit()


def filter_object(
    conn,
    obj: catalog.ObservationObject,
    *,
    candidates_depth: int,
    scoring_config: ScoringConfig,
    api_key: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Прогнать полный конвейер для одного объекта. Возвращает отчёт."""
    # 1. Лексический канал.
    kw_hits = channels.keyword_hits(conn, obj.keywords)

    # 2. Векторный канал.
    query_vec = catalog.ensure_query_embedding(conn, obj, api_key=api_key)
    from agent_1 import embed_v5  # noqa: E402  -- см. catalog.py про sys.path

    literal = embed_v5.vector_literal(query_vec)
    vec_scored = channels.vector_scored(conn, literal, candidates_depth)

    # 3. Объединение (union, design D1).
    candidate_ids = channels.union_candidates(vec_scored, kw_hits)

    # 4. LLM-оценка по рубрике, с кэшем.
    texts = fetch_candidate_texts(conn, sorted(candidate_ids))
    candidates_payload = [
        {"id_clean_post": doc_id, "title": row["title"], "text": row["text"]}
        for doc_id, row in texts.items()
    ]
    scores = score_candidates(
        conn,
        id_object=obj.id_object,
        label=obj.label,
        search_description=obj.search_description,
        candidates=candidates_payload,
        config=scoring_config,
    )

    # 5. Порог 7.5.
    above_threshold = {doc_id: score for doc_id, score in scores.items() if score >= THRESHOLD}

    # 6. Негатив-фильтр как вето -- после порога, не до (design D4).
    vetoed = channels.negative_hits(conn, obj.negative_filter, sorted(above_threshold))
    selected = {doc_id: score for doc_id, score in above_threshold.items() if doc_id not in vetoed}

    # 7. Запись.
    if not dry_run:
        write_relevant_documents(conn, obj.id_object, selected)

    return {
        "id_object": obj.id_object,
        "label": obj.label,
        "keyword_hits": len(kw_hits),
        "vector_candidates": len(vec_scored),
        "union_candidates": len(candidate_ids),
        "above_threshold": len(above_threshold),
        "vetoed": len(vetoed),
        "selected": len(selected),
        # Сами id, не только счётчик -- нужны регрессионному тесту
        # (scripts/run_regression.py) для честного recall против эталона.
        "selected_ids": set(selected.keys()),
    }


def main(argv: list[str] | None = None) -> int:
    # См. run_regression.py: без этого nohup/редирект в файл блочно
    # буферизует вывод, и при внешнем убийстве процесса лог остаётся
    # пустым несмотря на реально сделанную работу.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    ap = argparse.ArgumentParser(description="Агент 2: отбор документов под объект наблюдения")
    ap.add_argument("--object", type=int, default=None, help="id_object для одиночного прогона")
    ap.add_argument("--all", action="store_true", help="прогнать все объекты каталога")
    ap.add_argument("--candidates", type=int, default=channels.DEFAULT_CANDIDATES_DEPTH)
    ap.add_argument("--dry-run", action="store_true", help="не писать в agent_2_relevant_documents")
    ap.add_argument("--openclaw-cmd", default=os.getenv("AGENT_2_OPENCLAW_CMD", resolve_default_openclaw_cmd()))
    ap.add_argument("--agent-id", default=os.getenv("AGENT_2_SCORING_AGENT_ID", DEFAULT_AGENT_ID))
    ap.add_argument("--model", default=os.getenv("AGENT_2_SCORING_MODEL"))
    ap.add_argument("--thinking", default=os.getenv("AGENT_2_SCORING_THINKING"))
    ap.add_argument("--rate-limit-per-minute", type=float, default=20.0)
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    ap.add_argument("--dsn-var", default="AGENT_1_DB_DSN")
    args = ap.parse_args(argv)

    if not args.all and args.object is None:
        print("ERROR: нужен --object N или --all", file=sys.stderr)
        return 1

    load_dotenv(args.env_file)
    dsn = os.environ.get(args.dsn_var)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not dsn:
        print(f"ERROR: {args.dsn_var} не найден", file=sys.stderr)
        return 1

    scoring_config = ScoringConfig(
        openclaw_cmd=args.openclaw_cmd,
        agent_id=args.agent_id,
        model=args.model,
        thinking=args.thinking,
        rate_limit_per_minute=args.rate_limit_per_minute,
    )

    with psycopg.connect(dsn) as conn:
        set_ef_search(conn, max(args.candidates * 2, 100))

        targets = [catalog.fetch_object(conn, args.object)] if args.object is not None \
            else catalog.fetch_all_objects(conn)

        for obj in targets:
            print(f"--- Объект {obj.id_object}: {obj.label}")
            report = filter_object(
                conn, obj,
                candidates_depth=args.candidates,
                scoring_config=scoring_config,
                api_key=api_key,
                dry_run=args.dry_run,
            )
            print(f"    ключевые слова: {report['keyword_hits']}   "
                  f"вектор: {report['vector_candidates']}   "
                  f"объединение: {report['union_candidates']}")
            print(f"    выше порога {THRESHOLD}: {report['above_threshold']}   "
                  f"вето: {report['vetoed']}   ИТОГО: {report['selected']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())