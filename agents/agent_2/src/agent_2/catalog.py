"""Каталог объектов наблюдения: чтение из `agent_1_v5.observation_objects`.

Каталог приходит готовым (см. `docs/architecture/
observation_objects_context.md`) -- этот модуль его не порождает, только
читает и проверяет обязательные поля (design D2, specs/agent_2/spec.md:
«Контракт объекта наблюдения включает поисковое описание»).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_2.db import SCHEMA

# Эмбеддинг запроса считается через agent_1.embed_v5 -- та же модель и та
# же нормировка, что у корпуса, иначе косинус сравнивает разные
# пространства (урок из experiments/retrieval_eval/run_eval.py).
sys.path.insert(0, "/root/.openclaw/workspace/agents/agent_1/src")


class ContractError(ValueError):
    """Объект каталога нарушает обязательный контракт (например, без
    search_description)."""


@dataclass(frozen=True)
class ObservationObject:
    id_object: int
    label: str
    aliases: list[str]
    keywords: str | None
    negative_filter: str | None
    search_description: str
    query_embedding: list[float] | None


def _row_to_object(row: dict[str, Any]) -> ObservationObject:
    description = row.get("search_description")
    if not isinstance(description, str) or not description.strip():
        raise ContractError(
            f"объект {row.get('id_object')}: пустой search_description -- "
            "ошибка контракта входа, название объекта НЕ подставляется как замена"
        )
    return ObservationObject(
        id_object=row["id_object"],
        label=row["label"],
        aliases=list(row.get("aliases") or []),
        keywords=row.get("keywords"),
        negative_filter=row.get("negative_filter"),
        search_description=description,
        query_embedding=row.get("query_embedding"),
    )


def fetch_object(conn, id_object: int) -> ObservationObject:
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT id_object, label, aliases, keywords, negative_filter,
                   search_description, query_embedding
            FROM {SCHEMA}.observation_objects
            WHERE id_object = %s
            """,
            (id_object,),
        )
        row = cur.fetchone()
    if row is None:
        raise ContractError(f"объект {id_object} не найден в каталоге")
    return _row_to_object(row)


def fetch_all_objects(conn) -> list[ObservationObject]:
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT id_object, label, aliases, keywords, negative_filter,
                   search_description, query_embedding
            FROM {SCHEMA}.observation_objects
            ORDER BY id_object
            """
        )
        rows = cur.fetchall()
    return [_row_to_object(row) for row in rows]


def ensure_query_embedding(conn, obj: ObservationObject, *, api_key: str) -> list[float]:
    """Вернуть вектор запроса объекта, вычислив и сохранив его при первом
    обращении.

    `query_embedding` в каталоге NULL до первого запроса -- миграция сида
    (agents/agent_2/db/002_seed_observation_objects.sql) его не считает,
    вычисление требует вызова OpenRouter. Кэшируется в
    `observation_objects.query_embedding`, а не пересчитывается на каждый
    прогон.
    """
    if obj.query_embedding is not None:
        return obj.query_embedding

    from agent_1 import embed_v5
    import os

    vec = embed_v5.openrouter_embed(
        [obj.search_description],
        api_key=api_key,
        model=os.environ.get("EMBED_MODEL", embed_v5.DEFAULT_MODEL),
        base_url=os.environ.get("OPENROUTER_BASE_URL", embed_v5.DEFAULT_BASE_URL),
    )[0]

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {SCHEMA}.observation_objects
            SET query_embedding = %s::vector, updated_at = now()
            WHERE id_object = %s
            """,
            (embed_v5.vector_literal(vec), obj.id_object),
        )
    conn.commit()
    return vec