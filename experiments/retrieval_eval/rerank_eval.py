#!/usr/bin/env python3
"""Шаг 4: кросс-энкодер поверх векторного отбора.

## Зачем

Замер трижды показал одно: нужные документы **находятся**, но стоят низко.
На сэмпле recall@100 равен 56%, recall@500 -- 84%. Двадцать восемь пунктов
лежат между сотым и пятисотым местом. Смена энкодера дала два пункта,
гибрид и мультизапрос ушли в минус. Порядок в выдаче -- то, во что мы
упираемся.

Первый этап сравнивает два сжатия: документ заранее свёрнут в вектор, не
зная запроса, запрос свёрнут в свой, не зная документа. Всё взаимодействие
между конкретным словом запроса и конкретным куском текста потеряно до
начала сравнения.

Кросс-энкодер получает пару целиком, одним входом, и выдаёт одно число.
Ничего не усредняется, потому что сжатия нет. Поэтому он способен поднять
документ, который первый этап поставил на 180-е место -- но **не способен**
найти то, чего первый этап не принёс: вне топ-N для него ничего не
существует.

## Почему остался только он

Пробовали три варианта. LLM через OpenRouter работала и давала хороший
прирост (+18 пунктов на объекте 6 при глубине 50), но каждый прогон стоит
денег, оценки плавают от запуска к запуску, и кредиты кончились посреди
замера. Jina по API -- настоящий кросс-энкодер на чужом GPU, но это чужой
ключ и чужие лимиты.

Локальный кросс-энкодер медленный, зато бесплатный, детерминированный и ни
от кого не зависит: одинаковый вход всегда даёт одинаковый выход, и
повторный замер не сдвинется сам по себе. Для эксперимента, где мы ловим
разницу в три-пять пунктов, второе не менее важно первого.

Скорость по `bench_reranker.py` на этой машине (6 ядер):

    длина 512 -- 6.11 с/пара -- 163 минуты на 8 объектов по 200 кандидатов
    длина 256 -- 3.50 с/пара --  93 минуты
    длина 128 -- 1.72 с/пара --  46 минут

128 токенов оставляет примерно заголовок и первую строку, то есть отменяет
смысл метода. 256 -- разумный компромисс, 512 -- полное качество.

## Как читать результат

Главная таблица -- **при одинаковой глубине**: столько же документов, но
отсортированных лучше. Сравнивать `после@50` с `до@100` как основной
показатель нельзя: пятьдесят документов дают меньше ста при любой
сортировке, и отрицательное число там означает арифметику, а не провал
метода. Этот вопрос вынесен отдельно и подписан как вторичный.

Оценки кэшируются, повторный запуск досчитывает недостающее.

    python rerank_eval.py --labels data/labels_v2.jsonl --exclude-objects 9
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/root/.openclaw/workspace/agents/agent_1/src")

from patterns import OBJECT_PATTERNS  # noqa: E402
from queries import QUERIES  # noqa: E402
from run_eval import (  # noqa: E402
    SCHEMA, load_dotenv, load_truth, set_ef_search, vector_hits,
)

DEFAULT_ENV = Path("/root/.openclaw/workspace/agents/agent_1/.env")
DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"


def fetch_docs(conn, ids: list[int], chars: int) -> dict[int, tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT c.id_clean_post, coalesce(r.title, ''),
                   left(coalesce(c.clean_content, ''), %s)
            FROM {SCHEMA}.clean_posts c
            JOIN {SCHEMA}.raw_posts r ON r.id_raw_post = c.id_raw_post
            WHERE c.id_clean_post = ANY(%s)
            """,
            (chars, ids),
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def recall(found: set[int], positives: set[int]) -> float:
    return len(found & positives) / len(positives) if positives else 0.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Кросс-энкодер поверх векторного отбора")
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("data/rerank_report.json"))
    ap.add_argument("--cache", type=Path, default=Path("data/rerank_cache.jsonl"))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--form", choices=("name", "aliases", "description"),
                    default="description", help="форма запроса для первого этапа")
    ap.add_argument("--candidates", type=int, default=200,
                    help="сколько документов первый этап отдаёт реранкеру")
    ap.add_argument("--batch", type=int, default=16, help="пар в одном вызове модели")
    ap.add_argument("--doc-chars", type=int, default=1200)
    ap.add_argument("--max-length", type=int, default=512,
                    help="длина входа в токенах. 512 -- полное качество, 163 мин "
                         "на 8 объектов; 256 -- 93 мин; 128 оставляет заголовок и "
                         "отменяет смысл метода")
    ap.add_argument("--threads", type=int, default=None,
                    help="потоков torch; по умолчанию все ядра")
    ap.add_argument("--exclude-objects", type=int, nargs="*", default=[])
    ap.add_argument("--min-positives", type=int, default=5)
    ap.add_argument("--relation", choices=("event", "any"), default="event")
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    ap.add_argument("--dsn-var", default="AGENT_1_DB_DSN")
    args = ap.parse_args(argv)

    load_dotenv(args.env_file)
    dsn = os.environ.get(args.dsn_var)
    if not dsn:
        print(f"ERROR: {args.dsn_var} не найден", file=sys.stderr)
        return 1
    # Ключ нужен только первому этапу: один эмбеддинг запроса на объект.
    # Сам реранкер в сеть не ходит вообще.
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("ERROR: нет OPENROUTER_API_KEY (нужен для вектора запроса)",
              file=sys.stderr)
        return 1

    import torch  # noqa: E402
    from sentence_transformers import CrossEncoder  # noqa: E402

    from agent_1 import embed_v5  # noqa: E402

    threads = args.threads or (os.cpu_count() or 1)
    torch.set_num_threads(threads)
    print(f"Модель {args.model}, потоков {torch.get_num_threads()}, "
          f"длина входа {args.max_length}")
    model = CrossEncoder(args.model, device="cpu", max_length=args.max_length)

    truth, _ = load_truth(args.labels, args.min_positives, args.relation)
    for oid in args.exclude_objects:
        truth.pop(oid, None)

    cache: dict[str, float] = {}
    if args.cache.is_file():
        for line in args.cache.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                cache[row["key"]] = row["score"]
        print(f"Кэш оценок: {len(cache)}")

    by_id = {p.object_id: p for p in OBJECT_PATTERNS}
    report: dict[str, Any] = {"model": args.model, "form": args.form,
                              "candidates": args.candidates,
                              "max_length": args.max_length, "objects": {}}

    args.cache.parent.mkdir(parents=True, exist_ok=True)
    cache_fh = args.cache.open("a", encoding="utf-8")

    with psycopg.connect(dsn) as conn:
        set_ef_search(conn, max(args.candidates * 2, 100))

        for object_id in sorted(truth):
            positives = truth[object_id]
            pattern = by_id[object_id]
            print(f"\n--- Объект {object_id}: {pattern.label}  "
                  f"(положительных {len(positives)})")

            vector = embed_v5.openrouter_embed(
                [QUERIES[object_id][args.form]], api_key=api_key,
                model=os.environ.get("EMBED_MODEL", embed_v5.DEFAULT_MODEL),
                base_url=os.environ.get("OPENROUTER_BASE_URL", embed_v5.DEFAULT_BASE_URL),
            )[0]
            ranked = vector_hits(conn, embed_v5.vector_literal(vector), args.candidates)
            docs = fetch_docs(conn, ranked, args.doc_chars)

            query_text = f"{pattern.label}. {QUERIES[object_id]['description']}"
            todo = [d for d in ranked if f"{object_id}:{d}:{args.model}:{args.max_length}" not in cache]
            print(f"    кандидатов {len(ranked)}, к оценке {len(todo)}")

            started = time.time()
            for offset in range(0, len(todo), args.batch):
                batch = todo[offset : offset + args.batch]
                pairs = []
                for doc_id in batch:
                    title, text = docs.get(doc_id, ("", ""))
                    pairs.append([query_text, f"{title}. {text}"])
                scores = model.predict(pairs, show_progress_bar=False)
                for doc_id, value in zip(batch, scores):
                    key = f"{object_id}:{doc_id}:{args.model}:{args.max_length}"
                    cache[key] = float(value)
                    cache_fh.write(json.dumps({"key": key, "score": float(value)}) + "\n")
                cache_fh.flush()
                done = offset + len(batch)
                elapsed = time.time() - started
                speed = done / elapsed if elapsed else 0
                left = (len(todo) - done) / speed if speed else 0
                print(f"      {done}/{len(todo)}  {speed:.2f} пар/с  "
                      f"осталось ~{left/60:.0f} мин", flush=True)

            # Документы без оценки уходят в конец с сохранением исходного
            # порядка: молча выбрасывать их нельзя -- это была бы потеря
            # recall, замаскированная под работу реранкера.
            scored = [(d, cache.get(f"{object_id}:{d}:{args.model}:{args.max_length}")) for d in ranked]
            missing = [d for d, s in scored if s is None]
            reranked = [d for d, _ in sorted(
                ((d, s) for d, s in scored if s is not None),
                key=lambda ds: (-ds[1], ranked.index(ds[0])))] + missing

            row: dict[str, Any] = {"label": pattern.label, "positives": len(positives),
                                   "unscored": len(missing), "before": {}, "after": {}}
            depths = sorted({k for k in (10, 20, 50, 100, args.candidates)
                             if k <= args.candidates})
            print(f"    {'K':>5} {'до реранка':>12} {'после':>10}")
            for k in depths:
                before = recall(set(ranked[:k]), positives)
                after = recall(set(reranked[:k]), positives)
                row["before"][k] = before
                row["after"][k] = after
                print(f"    {k:>5} {before:>11.1%} {after:>10.1%}")
            if missing:
                print(f"    без оценки: {len(missing)} (ушли в конец списка)")
            report["objects"][str(object_id)] = row

    cache_fh.close()

    print("\n=== Прирост при одинаковой глубине ===")
    print(f"{'об':>3} | {'@50 до':>7} {'@50 после':>10} {'Δ':>7}"
          f" | {'@100 до':>8} {'@100 после':>11} {'Δ':>7}")
    d50, d100 = [], []
    for oid, row in sorted(report["objects"].items(), key=lambda kv: int(kv[0])):
        b50, a50 = row["before"].get(50), row["after"].get(50)
        b100, a100 = row["before"].get(100), row["after"].get(100)
        if b50 is None or b100 is None:
            continue
        d50.append(a50 - b50)
        d100.append(a100 - b100)
        print(f"{oid:>3} | {b50:>7.1%} {a50:>10.1%} {a50 - b50:>+7.1%}"
              f" | {b100:>8.1%} {a100:>11.1%} {a100 - b100:>+7.1%}")
    if d50:
        print(f"{'':>3} | {'среднее':>7} {'':>10} {sum(d50)/len(d50):>+7.1%}"
              f" | {'':>8} {'':>11} {sum(d100)/len(d100):>+7.1%}")

    if args.candidates >= 150:
        print("\n=== Вторичное: хватит ли 50 отсортированных вместо 100 исходных ===")
        print(f"{'об':>3} {'до@100':>8} {'после@50':>10} {'разница':>9}")
        deltas = []
        for oid, row in sorted(report["objects"].items(), key=lambda kv: int(kv[0])):
            before, after = row["before"].get(100), row["after"].get(50)
            if before is None:
                continue
            deltas.append(after - before)
            print(f"{oid:>3} {before:>8.1%} {after:>10.1%} {after - before:>+9.1%}")
        if deltas:
            print(f"{'':>3} {'':>8} {'среднее':>10} {sum(deltas)/len(deltas):>+9.1%}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nОтчёт: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())