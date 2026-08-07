"""Лексический и векторный каналы отбора кандидатов.

Перенесено из `experiments/retrieval_eval/filter_agent.py` (tasks.md 4.2,
5.1, 5.2, 6.1-6.3) без изменения логики каналов -- замер это уже закрыл
(`docs/architecture/retrieval_research_report.md`, разделы 4.1-4.2).
Отличие от прототипа: регекс и вектор запроса берутся из каталога
(`agent_2.catalog`), не из константы `OBJECT_PATTERNS`/`QUERIES`.
"""

from __future__ import annotations

from typing import Any

from agent_2.db import SCHEMA, to_postgres

DEFAULT_CANDIDATES_DEPTH = 500  # design D2: @500 recall 76.9% против @100 50.7%


def keyword_hits(conn, pattern: str | None, universe: list[int] | None = None) -> set[int]:
    """Лексический канал: ключевые слова каталога по заголовку и тексту."""
    if not pattern:
        return set()
    sql = f"""
        SELECT c.id_clean_post
        FROM {SCHEMA}.clean_posts c
        JOIN {SCHEMA}.raw_posts r ON r.id_raw_post = c.id_raw_post
        WHERE c.drop_reason IS NULL AND c.is_duplicate = FALSE
          AND (coalesce(r.title,'') || ' ' || coalesce(c.clean_content,'')) ~* %s
    """
    params: list[Any] = [to_postgres(pattern)]
    if universe is not None:
        sql += "  AND c.id_clean_post = ANY(%s)\n"
        params.append(universe)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return {row[0] for row in cur.fetchall()}


def negative_hits(conn, negative: str | None, ids: list[int]) -> set[int]:
    """Негатив-фильтр каталога: что нужно вычесть из результата (вето)."""
    if not negative or not ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT c.id_clean_post
            FROM {SCHEMA}.clean_posts c
            JOIN {SCHEMA}.raw_posts r ON r.id_raw_post = c.id_raw_post
            WHERE c.id_clean_post = ANY(%s)
              AND (coalesce(r.title,'') || ' ' || coalesce(c.clean_content,'')) ~* %s
            """,
            (ids, to_postgres(negative)),
        )
        return {row[0] for row in cur.fetchall()}


def vector_scored(
    conn,
    vector_literal: str,
    limit: int = DEFAULT_CANDIDATES_DEPTH,
    *,
    universe: list[int] | None = None,
) -> list[tuple[int, float]]:
    """Векторный канал с оценками (1 - косинусное расстояние), не только id.

    Глубина по умолчанию 500 (design D2). Вызывающий код обязан поднять
    `hnsw.ef_search` (`agent_2.db.set_ef_search`) не меньше `limit` ДО
    вызова -- иначе HNSW тихо режет кандидатов на защитном ef_search=40 по
    умолчанию (грабля брифа).
    """
    sql = f"""
        SELECT c.id_clean_post, 1 - (c.embedding <=> %s::vector)
        FROM {SCHEMA}.clean_posts c
        WHERE c.drop_reason IS NULL AND c.is_duplicate = FALSE
          AND c.embedding IS NOT NULL
          AND (%s::bigint[] IS NULL OR c.id_clean_post = ANY(%s))
        ORDER BY c.embedding <=> %s::vector
        LIMIT %s
    """
    params = (vector_literal, universe, universe, vector_literal, limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [(row[0], float(row[1])) for row in cur.fetchall()]


def union_candidates(vector_scored_docs: list[tuple[int, float]], keyword_docs: set[int]) -> set[int]:
    """Объединение (union), не пересечение -- design D1.

    Цепочка "И" (ключевые слова после вектора) проиграла ключевым словам
    на 8 объектах из 8; не оставлять как опцию даже за флагом.
    """
    return {doc_id for doc_id, _ in vector_scored_docs} | keyword_docs