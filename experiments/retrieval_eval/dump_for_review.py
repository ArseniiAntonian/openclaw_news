#!/usr/bin/env python3
"""Выгрузка текстов для ручного чтения: что на самом деле поднимает каждый метод.

Замер (`run_eval.py`) считает только recall. Precision он не считает
намеренно: релевантность документов за пределами размеченной очереди
неизвестна. Но из-за этого по его цифрам нельзя судить, чем именно метод
наполняет выдачу -- «регекс 71%» ничего не говорит о том, что регекс
попутно нагрёб.

Вторая, более серьёзная дыра: эталон размечен только внутри пула, а пул
собирался как «широкий невод по ИИ-лексике ∪ ключевые слова каталога».
Значит документ, который ловит регекс, гарантированно мог получить метку,
а релевантный по смыслу документ без ИИ-лексики в пул не попадал вовсе --
и вектор, найдя его, не получает за это ничего. Обе дыры закрываются
только чтением текстов.

Скрипт работает **по всему живому корпусу**, а не по пулу, и раскладывает
выдачу на три корзины:

  · только регекс   -- поймали ключевые слова, вектор не поднял;
  · только вектор   -- поднял вектор, ключевые слова молчат;
  · оба             -- согласие методов.

У каждого документа помечено, был ли он в пуле и что сказала про него
модель. Документ «вне пула» -- это ровно тот случай, который наша метрика
не видит в принципе: он не мог быть размечен, поэтому для recall его не
существует.

Читать надо корзину «только вектор» (правда ли это релевантные новости,
которых мы недосчитались) и корзину «только регекс» (правда ли это
релевантные новости, или ключевые слова нагребают мусор, раздувая свой
recall за счёт собственных ложных срабатываний).

Только чтение БД. Пишет один markdown-файл.

    python dump_for_review.py --object 5 --out review_obj5.md
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/root/.openclaw/workspace/agents/agent_1/src")

from patterns import OBJECT_PATTERNS, to_postgres  # noqa: E402
from run_eval import QUERIES, load_dotenv  # noqa: E402

SCHEMA = "agent_1_v5"
DEFAULT_ENV = Path("/root/.openclaw/workspace/agents/agent_1/.env")


def fetch_docs(conn, ids: list[int], snippet: int) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT c.id_clean_post, c.time_post, s.name_source,
                   coalesce(r.title, ''), left(coalesce(c.clean_content, ''), %s),
                   length(coalesce(c.clean_content, ''))
            FROM {SCHEMA}.clean_posts c
            JOIN {SCHEMA}.raw_posts r ON r.id_raw_post = c.id_raw_post
            JOIN {SCHEMA}.source   s ON s.id_source   = r.id_source
            WHERE c.id_clean_post = ANY(%s)
            """,
            (snippet, ids),
        )
        return {
            row[0]: {"time": row[1], "source": row[2], "title": row[3],
                     "text": row[4], "len": row[5]}
            for row in cur.fetchall()
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Выгрузка текстов на ручную проверку")
    ap.add_argument("--object", type=int, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--form", choices=("name", "aliases", "description"),
                    default="description", help="форма запроса для векторного поиска")
    ap.add_argument("--top", type=int, default=100, help="глубина векторной выдачи")
    ap.add_argument("--sample", type=int, default=15, help="документов на корзину")
    ap.add_argument("--snippet", type=int, default=500, help="знаков текста на документ")
    ap.add_argument("--pool", type=Path, default=Path("data/pool.jsonl"))
    ap.add_argument("--labels", type=Path, default=Path("data/labels_v2.jsonl"))
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    ap.add_argument("--dsn-var", default="AGENT_1_DB_DSN")
    ap.add_argument("--seed", type=int, default=17)
    args = ap.parse_args(argv)

    load_dotenv(args.env_file)
    dsn = os.environ.get(args.dsn_var)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not dsn or not api_key:
        print("ERROR: нет AGENT_1_DB_DSN или OPENROUTER_API_KEY", file=sys.stderr)
        return 1

    from agent_1 import embed_v5  # noqa: E402

    pattern = next((p for p in OBJECT_PATTERNS if p.object_id == args.object), None)
    if pattern is None:
        print(f"ERROR: объекта {args.object} нет в каталоге", file=sys.stderr)
        return 1

    in_pool = set()
    if args.pool.is_file():
        for line in args.pool.read_text(encoding="utf-8").splitlines():
            if line.strip():
                in_pool.add(json.loads(line)["id_clean_post"])

    labels: dict[int, dict[str, Any]] = {}
    if args.labels.is_file():
        for line in args.labels.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                labels[row["id_clean_post"]] = row

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('hnsw.ef_search', %s, false)", (str(max(args.top * 2, 100)),)
            )

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT c.id_clean_post
                FROM {SCHEMA}.clean_posts c
                JOIN {SCHEMA}.raw_posts r ON r.id_raw_post = c.id_raw_post
                WHERE c.drop_reason IS NULL AND c.is_duplicate = FALSE
                  AND (coalesce(r.title,'') || ' ' || coalesce(c.clean_content,'')) ~* %s
                """,
                (to_postgres(pattern.regex),),
            )
            regex_ids = {row[0] for row in cur.fetchall()}

        vector = embed_v5.openrouter_embed(
            [QUERIES[args.object][args.form]], api_key=api_key,
            model=os.environ.get("EMBED_MODEL", embed_v5.DEFAULT_MODEL),
            base_url=os.environ.get("OPENROUTER_BASE_URL", embed_v5.DEFAULT_BASE_URL),
        )[0]
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT c.id_clean_post
                FROM {SCHEMA}.clean_posts c
                WHERE c.drop_reason IS NULL AND c.is_duplicate = FALSE
                  AND c.embedding IS NOT NULL
                ORDER BY c.embedding <=> %s::vector
                LIMIT %s
                """,
                (embed_v5.vector_literal(vector), args.top),
            )
            vector_ids = [row[0] for row in cur.fetchall()]

        vector_set = set(vector_ids)
        buckets = {
            "только вектор": [i for i in vector_ids if i not in regex_ids],
            "только регекс": sorted(regex_ids - vector_set),
            "оба метода": [i for i in vector_ids if i in regex_ids],
        }

        rnd = random.Random(args.seed)
        chosen: dict[str, list[int]] = {}
        for name, ids in buckets.items():
            chosen[name] = ids[: args.sample] if name == "только вектор" \
                else rnd.sample(ids, min(args.sample, len(ids)))

        docs = fetch_docs(conn, [i for ids in chosen.values() for i in ids], args.snippet)

    lines: list[str] = []
    lines.append(f"# Ручная проверка выдачи: объект {args.object} — {pattern.label}\n")
    lines.append(f"Запрос для вектора (`{args.form}`):\n")
    lines.append(f"> {QUERIES[args.object][args.form]}\n")
    lines.append(f"Ключевые слова (реконструкция):\n")
    lines.append(f"```\n{pattern.regex}\n```\n")
    lines.append("## Размеры выдач по всему живому корпусу\n")
    lines.append(f"- ключевые слова подняли: **{len(regex_ids)}**")
    lines.append(f"- вектор, топ-{args.top}: **{len(vector_ids)}**")
    lines.append(f"- пересечение: **{len(buckets['оба метода'])}**")
    lines.append(f"- только вектор: **{len(buckets['только вектор'])}**")
    lines.append(f"- только регекс: **{len(buckets['только регекс'])}**\n")
    lines.append("Читая, отвечайте на один вопрос: **эта новость правда про объект?** "
                 "Пометка «вне пула» означает, что документ не мог быть размечен "
                 "вообще, то есть в наших метриках его не существует.\n")

    for name in ("только вектор", "только регекс", "оба метода"):
        ids = chosen[name]
        lines.append(f"\n## {name} — показано {len(ids)} из {len(buckets[name])}\n")
        for n, doc_id in enumerate(ids, start=1):
            d = docs.get(doc_id)
            if not d:
                continue
            mark = []
            if doc_id not in in_pool:
                mark.append("вне пула")
            row = labels.get(doc_id)
            if row is None:
                mark.append("не размечен")
            else:
                if args.object in (row.get("label_objects") or []):
                    mark.append("метка: событие")
                elif args.object in (row.get("mention_objects") or []):
                    mark.append("метка: упоминание")
                else:
                    mark.append("метка: не относится")
            lines.append(f"**{n}. {d['title']}**  ")
            lines.append(f"`id {doc_id} · {d['time']:%Y-%m-%d} · {d['source']} · "
                         f"{d['len']} знаков · {', '.join(mark)}`\n")
            lines.append(f"{d['text'].strip()}…\n")
            lines.append("---\n")

    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Записано: {args.out}")
    print(f"регекс {len(regex_ids)} · вектор@{args.top} {len(vector_ids)} · "
          f"только вектор {len(buckets['только вектор'])} · "
          f"только регекс {len(buckets['только регекс'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())