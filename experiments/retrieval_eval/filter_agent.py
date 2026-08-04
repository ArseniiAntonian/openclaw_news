#!/usr/bin/env python3
"""Фильтратор: отбор документов под объект наблюдения.

Не замер, а реализация. Замеры отвечали на вопрос «сколько нужного попадает
в первые K»; здесь надо выдать КОНЕЧНЫЙ НАБОР документов, то есть провести
границу между нужным и ненужным.

## Конструкция и откуда она взялась

Два канала параллельно, потом объединение:

- **лексический** -- ключевые слова каталога. На объектах-строках
  (GigaChat, OpenAI, open-source) он один даёт 75-92% полноты;
- **векторный** -- эмбеддинг ОПИСАНИЯ объекта, не названия. Замер:
  описание даёт +12 пунктов recall@100 против названия, и это не свойство
  энкодера -- на асимметричной модели разрыв сохранился. Описание просто
  несёт больше информации о том, что искать.

**Объединение, а не пересечение.** Архитектурная схема банка ставит
ключевые слова фильтром ПОСЛЕ вектора. Тогда полнота всей цепочки
ограничена полнотой ключевых слов, а на понятийных объектах она 5-14%.
Замер на восьми объектах: цепочка через «И» проиграла ключевым словам
восемь раз из восьми, на объекте «Инциденты» дала 1.4% против 61.4% у
вектора.

## Как проводится граница

Порог не может быть константой: оценки не откалиброваны между объектами,
0.35 у «Доверия к ИИ» и у «GPU» означают разное. Поэтому отсечка
адаптивная -- по разрыву в распределении оценок. Это же делает heiDS
(ArchEHR-QA 2025) через статистику экстремальных значений: сложный запрос
сам берёт больше документов, простой -- меньше.

Три яруса:

1. **Согласие каналов.** Документ, который поймали и ключевые слова, и
   вектор, принимается безусловно. По ручной выгрузке такая корзина у
   объекта 7 дала 11 событий и 4 упоминания из 15 при нуле нерелевантных.
2. **Защитный минимум.** Первые `--min-keep` документов проходят
   независимо от оценки: ошибка реранкера не должна отрезать правильное.
   Приём из практики продовых RAG.
3. **Адаптивный порог.** Дальше берём, пока не наткнёмся на самый большой
   провал в оценках, но не больше `--max-keep`.

В конце -- негатив-фильтр каталога как вето.

## Что здесь доказано, а что нет

Каналы, объединение и форма запроса подтверждены замерами. **Правило
отсечки не подтверждено ничем** -- оно из общих соображений и литературы.
Поэтому оно параметр, а не константа, и скрипт печатает, сколько отрезал и
по какому правилу. С `--evaluate` считает полноту и точность по эталону,
чтобы правило можно было настраивать, не переписывая конструкцию.

    python filter_agent.py --object 5 --evaluate --labels data/labels_v2.jsonl
    python filter_agent.py --all --alt-model jina-embeddings-v3 --evaluate \\
        --labels data/labels_v2.jsonl --out data/filter_report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/root/.openclaw/workspace/agents/agent_1/src")

from patterns import OBJECT_PATTERNS, to_postgres  # noqa: E402
from queries import QUERIES  # noqa: E402
from run_eval import ALT_SCHEMA, SCHEMA, load_dotenv, load_truth, set_ef_search  # noqa: E402

DEFAULT_ENV = Path("/root/.openclaw/workspace/agents/agent_1/.env")


def keyword_hits(conn, pattern: str, universe: list[int] | None) -> set[int]:
    """Лексический канал: ключевые слова каталога по заголовку и тексту."""
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


def negative_hits(conn, negative: str, ids: list[int]) -> set[int]:
    """Негатив-фильтр каталога: что нужно вычесть из результата."""
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


def vector_scored(conn, vector_literal: str, limit: int, *, alt_model: str | None,
                  universe: list[int] | None) -> list[tuple[int, float]]:
    """Векторный канал с ОЦЕНКАМИ, а не только идентификаторами.

    Оценки нужны для отсечки: без них можно резать только по позиции, то
    есть фиксированным K, а это и есть то, от чего мы уходим. Возвращаем
    близость (1 - косинусное расстояние), чтобы больше означало лучше.
    """
    if alt_model:
        sql = f"""
            SELECT d.id_clean_post, 1 - (d.embedding <=> %s::vector)
            FROM {ALT_SCHEMA}.doc_embeddings d
            JOIN {SCHEMA}.clean_posts c ON c.id_clean_post = d.id_clean_post
            WHERE d.model = %s
              AND c.drop_reason IS NULL AND c.is_duplicate = FALSE
              AND (%s::bigint[] IS NULL OR d.id_clean_post = ANY(%s))
            ORDER BY d.embedding <=> %s::vector
            LIMIT %s
        """
        params = (vector_literal, alt_model, universe, universe, vector_literal, limit)
    else:
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


def adaptive_cutoff(scores: list[float], *, min_keep: int, max_keep: int,
                    min_ratio: float = 3.0) -> int:
    """Сколько документов оставить: по самому большому провалу в оценках.

    Фиксированный порог невозможен -- оценки не сравнимы между объектами.
    Зато СТРУКТУРА распределения сравнима: у релевантных документов оценки
    кучкуются высоко, дальше идёт обрыв. Ищем его.

    `min_keep` -- защитный минимум: столько документов проходит независимо
    от оценок. Нужен потому, что ошибка ранжирования не должна отрезать
    правильный документ, а на плоском распределении «самый большой провал»
    может оказаться где угодно.

    `max_keep` -- потолок: на объекте, где релевантно почти всё, обрыва не
    будет вовсе, и без потолка мы отдадим весь список.

    `min_ratio` -- во сколько раз провал должен превышать обычный шаг, чтобы
    считаться границей. Без этого условия на ровном распределении отсечка
    срабатывала на первом же месте и отдавала минимум -- то есть ровно там,
    где мы меньше всего уверены, отдавала меньше всего документов.
    """
    if not scores:
        return 0
    lo = min(min_keep, len(scores))
    hi = min(max_keep, len(scores))
    if hi <= lo:
        return lo

    drops = [(scores[i - 1] - scores[i], i) for i in range(lo, hi)]
    if not drops:
        return lo
    best_drop, best_index = max(drops)

    # Обрыв засчитывается, только если он ЗАМЕТНО больше обычного шага. На
    # плоском распределении «самый большой провал» -- это шум, и резать по
    # нему нельзя: раз границы не видно, надо брать больше, а не меньше.
    # Потери полноты необратимы, лишнее отсеется дальше по пайплайну.
    ordered = sorted(d for d, _ in drops)
    typical = ordered[len(ordered) // 2]
    if typical > 0 and best_drop < min_ratio * typical:
        return hi
    return best_index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Фильтратор документов под объект")
    ap.add_argument("--object", type=int, default=None)
    ap.add_argument("--all", action="store_true", help="все объекты каталога")
    ap.add_argument("--exclude-objects", type=int, nargs="*", default=[])
    ap.add_argument("--candidates", type=int, default=500,
                    help="глубина векторного канала. Замер: @100 даёт 51%%, "
                         "@500 -- 77%%, поэтому по умолчанию глубоко")
    ap.add_argument("--min-keep", type=int, default=20,
                    help="защитный минимум: проходит независимо от оценок")
    ap.add_argument("--max-keep", type=int, default=200, help="потолок набора")
    ap.add_argument("--min-ratio", type=float, default=3.0,
                    help="во сколько раз провал должен превышать обычный шаг, "
                         "чтобы считаться границей; иначе берём до потолка")
    ap.add_argument("--alt-model", default=None,
                    help="модель из retrieval_eval.doc_embeddings; без неё -- "
                         "вектора из clean_posts")
    ap.add_argument("--universe-model", default=None,
                    help="ограничить вселенную документами этой модели")
    ap.add_argument("--evaluate", action="store_true",
                    help="посчитать полноту и точность по эталону")
    ap.add_argument("--labels", type=Path, default=None)
    ap.add_argument("--relation", choices=("event", "any"), default="event")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    ap.add_argument("--dsn-var", default="AGENT_1_DB_DSN")
    args = ap.parse_args(argv)

    if not args.all and args.object is None:
        print("ERROR: нужен --object N или --all", file=sys.stderr)
        return 1
    if args.evaluate and not args.labels:
        print("ERROR: --evaluate требует --labels", file=sys.stderr)
        return 1

    load_dotenv(args.env_file)
    dsn = os.environ.get(args.dsn_var)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not dsn:
        print(f"ERROR: {args.dsn_var} не найден", file=sys.stderr)
        return 1

    from agent_1 import embed_v5  # noqa: E402

    truth: dict[int, set[int]] = {}
    if args.evaluate:
        truth, _ = load_truth(args.labels, 1, args.relation)

    targets = [p.object_id for p in OBJECT_PATTERNS] if args.all else [args.object]
    targets = [o for o in targets if o not in args.exclude_objects]
    by_id = {p.object_id: p for p in OBJECT_PATTERNS}

    report: dict[str, Any] = {"candidates": args.candidates, "min_keep": args.min_keep,
                              "max_keep": args.max_keep, "alt_model": args.alt_model,
                              "objects": {}}

    with psycopg.connect(dsn) as conn:
        set_ef_search(conn, max(args.candidates * 2, 100))

        universe = None
        if args.universe_model:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT id_clean_post FROM {ALT_SCHEMA}.doc_embeddings WHERE model = %s",
                    (args.universe_model,),
                )
                universe = [row[0] for row in cur.fetchall()]
            print(f"Вселенная: {len(universe)} документов\n")

        for object_id in targets:
            pattern = by_id.get(object_id)
            if pattern is None:
                continue
            print(f"--- Объект {object_id}: {pattern.label}")

            # 1. Лексический канал.
            kw = keyword_hits(conn, pattern.regex, universe)

            # 2. Векторный канал по ОПИСАНИЮ объекта.
            if args.alt_model:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""SELECT embedding FROM {ALT_SCHEMA}.query_embeddings
                            WHERE object_id = %s AND form = 'description' AND model = %s""",
                        (object_id, args.alt_model),
                    )
                    row = cur.fetchone()
                if row is None:
                    print(f"    нет вектора запроса для {args.alt_model}, пропуск\n")
                    continue
                literal = row[0]
            else:
                if not api_key:
                    print("ERROR: нет OPENROUTER_API_KEY", file=sys.stderr)
                    return 1
                vec = embed_v5.openrouter_embed(
                    [QUERIES[object_id]["description"]], api_key=api_key,
                    model=os.environ.get("EMBED_MODEL", embed_v5.DEFAULT_MODEL),
                    base_url=os.environ.get("OPENROUTER_BASE_URL", embed_v5.DEFAULT_BASE_URL),
                )[0]
                literal = embed_v5.vector_literal(vec)

            scored = vector_scored(conn, literal, args.candidates,
                                   alt_model=args.alt_model, universe=universe)

            # 3. Ярусы.
            both = [d for d, _ in scored if d in kw]          # согласие каналов
            cut = adaptive_cutoff([s for _, s in scored], min_keep=args.min_keep,
                                  max_keep=args.max_keep, min_ratio=args.min_ratio)
            by_score = [d for d, _ in scored[:cut]]

            # Ключевые слова -- собственное правило включения у банка, поэтому
            # их попадания входят в набор целиком, а не только те, что вектор
            # тоже поднял.
            selected = set(by_score) | kw | set(both)

            # 4. Негатив-фильтр как вето.
            vetoed = negative_hits(conn, pattern.negative or "", sorted(selected))
            final = selected - vetoed

            print(f"    ключевые слова: {len(kw)}   вектор@{args.candidates}: {len(scored)}"
                  f"   согласие: {len(both)}")
            print(f"    отсечка по разрыву: {cut} из {len(scored)}"
                  f"   (минимум {args.min_keep}, потолок {args.max_keep})")
            print(f"    негатив-фильтр убрал: {len(vetoed)}")
            print(f"    ИТОГО отобрано: {len(final)}")

            row_out: dict[str, Any] = {
                "label": pattern.label, "keywords": len(kw), "vector": len(scored),
                "agreed": len(both), "cutoff": cut, "vetoed": len(vetoed),
                "selected": len(final),
            }

            if args.evaluate and object_id in truth:
                positives = truth[object_id]
                found = final & positives
                recall = len(found) / len(positives) if positives else 0.0
                precision = len(found) / len(final) if final else 0.0
                # Потолок: сколько положительных вообще было среди кандидатов.
                reachable = (set(d for d, _ in scored) | kw) & positives
                ceiling = len(reachable) / len(positives) if positives else 0.0
                row_out |= {"recall": recall, "precision": precision,
                            "ceiling": ceiling, "positives": len(positives)}
                print(f"    полнота {recall:.1%}   точность {precision:.1%}"
                      f"   (потолок кандидатов {ceiling:.1%}, "
                      f"положительных {len(positives)})")
            print()
            report["objects"][str(object_id)] = row_out

    if args.evaluate and report["objects"]:
        rows = [r for r in report["objects"].values() if "recall" in r]
        if rows:
            print("=== Сводка ===")
            print(f"{'об':>3} {'отобрано':>9} {'полнота':>9} {'точность':>9} {'потолок':>9}")
            for oid, r in sorted(report["objects"].items(), key=lambda kv: int(kv[0])):
                if "recall" not in r:
                    continue
                print(f"{oid:>3} {r['selected']:>9} {r['recall']:>9.1%} "
                      f"{r['precision']:>9.1%} {r['ceiling']:>9.1%}")
            print(f"{'':>3} {sum(r['selected'] for r in rows)//len(rows):>9} "
                  f"{sum(r['recall'] for r in rows)/len(rows):>9.1%} "
                  f"{sum(r['precision'] for r in rows)/len(rows):>9.1%} "
                  f"{sum(r['ceiling'] for r in rows)/len(rows):>9.1%}   среднее")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"\nОтчёт: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())