#!/usr/bin/env python3
"""Шаг 1.5: дешёвый отсев мусора из пула кандидатов, без модели.

Пул набирался широким неводом, и это дало 74% документов, которые не поймал
ни один регекс каталога. Но эта доля обманчива: туда попали и настоящие
пропуски ключевых слов, и просто мусор -- новость про Симпсонов в Fortnite,
где «ИИ» упомянут одной проходной фразой. Разметка обоих сортов стоит
одинаково, а ценность разная.

Идея отсева: считаем, сколько раз тема реально встречается в документе и где.
Одно упоминание в середине текста -- почти наверняка проходная фраза. Пять
упоминаний или упоминание в заголовке -- почти наверняка текст по делу.

Скрипт ничего не решает молча: сперва печатает распределение, чтобы порог
выбирался по числам, и обязательно показывает случайную выборку того, что
правило выбрасывает. Отсев, который режет настоящие попадания, хуже
отсутствия отсева -- потерянное на этом шаге в эталон уже не вернётся.

Только чтение файла. Ни БД, ни сети.

    python triage_pool.py --pool data/pool.jsonl                  # статистика
    python triage_pool.py --pool data/pool.jsonl --out data/label_queue.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from patterns import BROAD_NET, OBJECT_PATTERNS  # noqa: E402

# Сколько первых знаков текста считаем лидом новости.
LEAD_CHARS = 300

# Объединённые регексы: одна альтернация ищет непересекающиеся вхождения
# слева направо, поэтому «нейросеть» не посчитается дважды за счёт двух
# альтернатив внутри одного набора.
BROAD_RE = re.compile("|".join(f"(?:{p.regex})" for p in BROAD_NET), re.I | re.U)
OBJECT_RE = re.compile("|".join(f"(?:{p.regex})" for p in OBJECT_PATTERNS), re.I | re.U)


def measure(doc: dict[str, Any]) -> dict[str, Any]:
    """Признаки «насколько документ реально про тему»."""
    title = doc.get("title") or ""
    text = doc.get("text") or ""
    body = f"{title}\n{text}"

    broad_hits = len(BROAD_RE.findall(body))
    object_hits = len(OBJECT_RE.findall(body))
    # Приблизительно: наборы могут пересекаться («нейросеть Сбера» попадает
    # в оба). Для отсева этой точности достаточно.
    topic_hits = broad_hits + object_hits

    in_title = bool(BROAD_RE.search(title) or OBJECT_RE.search(title))

    first = BROAD_RE.search(text) or OBJECT_RE.search(text)
    first_pos = first.start() if first else -1
    in_lead = bool(first and first.start() < LEAD_CHARS)

    return {
        "topic_hits": topic_hits,
        "in_title": in_title,
        "first_pos": first_pos,
        "in_lead": in_lead,
        "has_object": bool(doc.get("matched_objects")),
    }


def keep(m: dict[str, Any], min_hits: int) -> bool:
    """Правило отсева. Намеренно щедрое: сомнение трактуем в пользу оставить.

    Оставляем, если выполнено хоть одно:
      - сработал регекс каталога (это измеряемая часть, её не трогаем);
      - тема есть в заголовке (заголовок про тему => текст про тему);
      - тема упомянута min_hits раз и более;
      - тема в лиде И упомянута хотя бы дважды.

    Последнее условие намеренно парное. Само по себе положение в лиде никого
    не спасает: на проверке выяснилось, что одиночное проходное упоминание
    часто стоит в первых строках («ИИ от Epic Games будет использоваться...»
    в новости про Симпсонов в Fortnite), и одного этого признака хватало,
    чтобы мусор проходил отсев насквозь.
    """
    return (
        m["has_object"]
        or m["in_title"]
        or m["topic_hits"] >= min_hits
        or (m["in_lead"] and m["topic_hits"] >= 2)
    )


def bucket(hits: int) -> str:
    if hits <= 1:
        return "1"
    if hits == 2:
        return "2"
    if hits <= 4:
        return "3-4"
    if hits <= 9:
        return "5-9"
    return "10+"


def report(
    docs: list[dict[str, Any]], measures: list[dict[str, Any]], min_hits: int
) -> tuple[list[int], list[int]]:
    total = len(docs)
    print(f"\n=== Отсев пула: {total} документов на входе ===\n")

    print("Распределение по числу упоминаний темы:")
    by_bucket: Counter[str] = Counter(bucket(m["topic_hits"]) for m in measures)
    for key in ("1", "2", "3-4", "5-9", "10+"):
        n = by_bucket.get(key, 0)
        print(f"  {key:>4} упоминаний  {n:>5}  ({n * 100 // total if total else 0}%)")

    print("\nПризнаки:")
    print(f"  тема в заголовке          {sum(1 for m in measures if m['in_title']):>5}")
    print(f"  первое упоминание в лиде  {sum(1 for m in measures if m['in_lead']):>5}")
    print(f"  сработал регекс каталога  {sum(1 for m in measures if m['has_object']):>5}")

    kept = [i for i, m in enumerate(measures) if keep(m, min_hits)]
    dropped = [i for i, m in enumerate(measures) if not keep(m, min_hits)]
    print(f"\nПравило (min_hits={min_hits}):")
    print(f"  остаётся   {len(kept):>5}")
    print(f"  выброшено  {len(dropped):>5}  ({len(dropped) * 100 // total if total else 0}%)")

    # Сколько среди оставшихся -- «мимо каталога». Это и есть материал, ради
    # которого пул набирался шире: кандидаты в пропуски ключевых слов.
    kept_no_object = sum(1 for i in kept if not measures[i]["has_object"])
    print(f"  из них мимо каталога {kept_no_object:>5}  <- кандидаты в пропуски")

    return kept, dropped


def show_sample(docs, measures, idx, title, n, seed):
    print(f"\n--- {title}: случайные {min(n, len(idx))} из {len(idx)} ---")
    rnd = random.Random(seed)
    for i in rnd.sample(idx, min(n, len(idx))):
        m = measures[i]
        d = docs[i]
        flags = []
        if m["in_title"]:
            flags.append("загол")
        if m["has_object"]:
            flags.append(f"объекты={d.get('matched_objects')}")
        mark = (" [" + ", ".join(flags) + "]") if flags else ""
        print(f"  hits={m['topic_hits']:>2} {(d.get('title') or '')[:88]}{mark}")


def compact(doc: dict[str, Any], m: dict[str, Any], head_chars: int) -> dict[str, Any]:
    """Урезанная запись для разметки: без полного текста."""
    return {
        "id_clean_post": doc["id_clean_post"],
        "time_post": doc["time_post"],
        "source": doc["source"],
        "host": doc["host"],
        "title": doc["title"],
        "summary": doc.get("summary"),
        "text_head": (doc.get("text") or "")[:head_chars],
        "text_len": doc.get("text_len"),
        "matched_objects": doc.get("matched_objects"),
        "topic_hits": m["topic_hits"],
        "in_title": m["in_title"],
        "label_objects": None,
        "label_source": None,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Отсев мусора из пула кандидатов")
    ap.add_argument("--pool", type=Path, required=True, help="вход: pool.jsonl")
    ap.add_argument("--out", type=Path, default=None,
                    help="куда писать очередь на разметку; без него -- только статистика")
    ap.add_argument("--min-hits", type=int, default=3,
                    help="порог по числу упоминаний темы (по умолчанию 3)")
    ap.add_argument("--head-chars", type=int, default=800,
                    help="сколько знаков текста класть в очередь на разметку")
    ap.add_argument("--sample", type=int, default=25, help="размер показываемых выборок")
    ap.add_argument("--seed", type=int, default=17)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.pool.is_file():
        print(f"ERROR: не найден {args.pool}", file=sys.stderr)
        return 1

    docs = [json.loads(line) for line in args.pool.read_text(encoding="utf-8").splitlines() if line.strip()]
    measures = [measure(d) for d in docs]

    kept, dropped = report(docs, measures, args.min_hits)

    # Главная проверка: не режет ли правило живое. Смотреть обязательно.
    show_sample(docs, measures, dropped, "ВЫБРОШЕНО (проверить на ложные потери)",
                args.sample, args.seed)
    kept_no_obj = [i for i in kept if not measures[i]["has_object"]]
    show_sample(docs, measures, kept_no_obj, "ОСТАЛОСЬ мимо каталога (кандидаты в пропуски)",
                args.sample, args.seed + 1)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8") as handle:
            for i in kept:
                handle.write(json.dumps(compact(docs[i], measures[i], args.head_chars),
                                        ensure_ascii=False) + "\n")
        size_mb = args.out.stat().st_size / 1024 / 1024
        print(f"\nЗаписано: {args.out} ({len(kept)} строк, {size_mb:.1f} МБ)")
    else:
        print("\n--out не задан, очередь не писалась (только статистика).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())