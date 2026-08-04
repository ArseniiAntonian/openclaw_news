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

## Два бэкенда

`--backend jina` (по умолчанию) -- кросс-энкодер `jina-reranker-v3` по API,
на их GPU. Бесплатный лимит в 10 млн токенов, наш процессор не трогает
вообще. Контекст 131 тысяча токенов, поэтому документ можно отдавать
целиком, не обрезая до 1200 знаков, как приходится локальной модели.
Нужен ключ в JINA_API_KEY.

`--backend crossenc` -- локальный `bge-reranker-v2-m3`. **Грузит процессор
надолго**, поэтому по умолчанию берёт половину ядер, а не все: полный
прогон занимает часы, и в это время машина занята не только нами. Замер
`bench_reranker.py` на шести ядрах: 6.11 с/пара при 512 токенах (163 минуты
на 8 объектов), 3.50 при 256 (93 минуты), 1.72 при 128 (46 минут, но 128
токенов оставляет заголовок и отменяет смысл метода).

Что уже известно по числам. LLM через OpenRouter давала +11.6 пункта в
среднем при глубине 100, до +18 на отдельных объектах. Локальный
кросс-энкодер на 256 токенах -- всего +1.4, а на объекте 3 ухудшал порядок
на 9 пунктов. Jina не мерена ни разу: другая модель, другое поведение
ожидаемо, но это ожидание, а не результат.

## Как читать результат

Главная таблица -- **при одинаковой глубине**: столько же документов, но
отсортированных лучше. Сравнивать `после@50` с `до@100` как основной
показатель нельзя: пятьдесят документов дают меньше ста при любой
сортировке, и отрицательное число там означает арифметику, а не провал
метода. Этот вопрос вынесен отдельно и подписан как вторичный.

Оценки кэшируются, повторный запуск досчитывает недостающее.

    python rerank_eval.py --labels data/labels_v2.jsonl --exclude-objects 9
    python rerank_eval.py --labels ... --backend crossenc --threads 2
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
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/root/.openclaw/workspace/agents/agent_1/src")

from patterns import OBJECT_PATTERNS  # noqa: E402
from queries import QUERIES  # noqa: E402
from run_eval import (  # noqa: E402
    SCHEMA, load_dotenv, load_truth, set_ef_search, vector_hits,
)

DEFAULT_ENV = Path("/root/.openclaw/workspace/agents/agent_1/.env")
DEFAULT_CE_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_JINA_MODEL = "jina-reranker-v3"
JINA_URL = "https://api.jina.ai/v1/rerank"
REQUEST_TIMEOUT = 180
RETRIES = 6


class TokenPacer:
    """Держит расход под лимитом токенов в минуту.

    У Jina 100 000 токенов в минуту. Превышение возвращает 429, и это
    временный отказ: ждать помогает, повторять сразу -- нет. Поэтому лимит
    соблюдается ДО отправки, а не ловится по факту: считаем, сколько токенов
    ушло за последние 60 секунд, и если очередной запрос не влезает --
    спим ровно столько, чтобы старые записи вышли из окна.

    Оценка токенов грубая, по числу знаков. Для русского примерно три знака
    на токен; берём с запасом, потому что недооценка приводит к 429, а
    переоценка -- всего лишь к лишней паузе.
    """

    def __init__(self, tokens_per_minute: int) -> None:
        self.limit = tokens_per_minute
        self.window: list[tuple[float, int]] = []

    @staticmethod
    def estimate(texts: list[str]) -> int:
        return sum(len(t) for t in texts) // 3 + 50

    def wait_for(self, tokens: int) -> None:
        while True:
            now = time.time()
            self.window = [(t, n) for t, n in self.window if now - t < 60]
            used = sum(n for _, n in self.window)
            if used + tokens <= self.limit * 0.9 or not self.window:
                self.window.append((now, tokens))
                return
            oldest = self.window[0][0]
            sleep_for = max(1.0, 60 - (now - oldest) + 0.5)
            print(f"      пауза {sleep_for:.0f}с: в окне {used} токенов из {self.limit}",
                  flush=True)
            time.sleep(sleep_for)


def jina_rerank(query: str, documents: list[str], *, api_key: str,
                model: str, pacer: "TokenPacer") -> list[float]:
    """Оценки от Jina в порядке переданных документов.

    Ответ приходит отсортированным по релевантности, с полем `index` --
    позицией во входном списке. Раскладываем обратно по исходному порядку:
    сортировкой занимается вызывающий код, и делать её в двух местах -- верный
    способ перепутать.
    """
    body = {"model": model, "query": query, "documents": documents,
            "top_n": len(documents)}
    pacer.wait_for(TokenPacer.estimate(documents + [query]))
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.post(
                JINA_URL,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json=body, timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code in (401, 402, 403):
                # Ключ или исчерпанная квота -- повторять бессмысленно.
                raise SystemExit(f"Jina отказала ({resp.status_code}): {resp.text[:300]}")
            if resp.status_code == 429:
                # Лимит скорости -- временный: ждём и повторяем. Это НЕ то же
                # самое, что 402: квота не кончилась, кончилась минута.
                wait = float(resp.headers.get("Retry-After") or 20)
                print(f"      429 от Jina, жду {wait:.0f}с", flush=True)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            scores = [0.0] * len(documents)
            for item in resp.json()["results"]:
                scores[int(item["index"])] = float(item["relevance_score"])
            return scores
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as exc:
            last = exc
            if attempt < RETRIES:
                time.sleep(3 * attempt)
    raise RuntimeError(f"Jina не ответила после {RETRIES} попыток: {last}")


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
    ap.add_argument("--backend", choices=("jina", "crossenc"), default="jina",
                    help="jina -- по API на их GPU, бесплатный лимит, процессор "
                         "не трогает; crossenc -- локально, грузит CPU часами")
    ap.add_argument("--model", default=None,
                    help="по умолчанию jina-reranker-v3 либо BAAI/bge-reranker-v2-m3")
    ap.add_argument("--form", choices=("name", "aliases", "description"),
                    default="description", help="форма запроса для первого этапа")
    ap.add_argument("--candidates", type=int, default=200,
                    help="сколько документов первый этап отдаёт реранкеру")
    ap.add_argument("--batch", type=int, default=None,
                    help="документов в одном вызове: 50 для jina, 16 для crossenc")
    ap.add_argument("--doc-chars", type=int, default=None,
                    help="знаков текста на документ: 6000 для jina (контекст "
                         "131k позволяет), 1200 для crossenc (окно 512 токенов)")
    ap.add_argument("--max-length", type=int, default=512,
                    help="длина входа в токенах. 512 -- полное качество, 163 мин "
                         "на 8 объектов; 256 -- 93 мин; 128 оставляет заголовок и "
                         "отменяет смысл метода")
    ap.add_argument("--jina-tpm", type=int, default=100_000,
                    help="лимит токенов в минуту у Jina; скрипт сам держит расход "
                         "ниже него, чтобы не ловить 429")
    ap.add_argument("--threads", type=int, default=None,
                    help="потоков torch для crossenc. По умолчанию ПОЛОВИНА ядер: "
                         "прогон идёт часами, и занимать машину целиком нельзя")
    ap.add_argument("--exclude-objects", type=int, nargs="*", default=[])
    ap.add_argument("--min-positives", type=int, default=5)
    ap.add_argument("--relation", choices=("event", "any"), default="event")
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    ap.add_argument("--dsn-var", default="AGENT_1_DB_DSN")
    args = ap.parse_args(argv)

    if args.model is None:
        args.model = DEFAULT_JINA_MODEL if args.backend == "jina" else DEFAULT_CE_MODEL
    if args.batch is None:
        # 20 документов по 3000 знаков -- около 20 тысяч токенов на запрос,
        # пятая часть минутного лимита. Прежние 50 по 6000 давали сто тысяч,
        # то есть весь лимит одним запросом, и 429 приходил сразу.
        args.batch = 20 if args.backend == "jina" else 16
    if args.doc_chars is None:
        args.doc_chars = 3000 if args.backend == "jina" else 1200

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

    from agent_1 import embed_v5  # noqa: E402

    jina_key = os.environ.get("JINA_API_KEY", "")
    model = None
    if args.backend == "jina":
        if not jina_key:
            print("ERROR: нет JINA_API_KEY", file=sys.stderr)
            return 1
        print(f"Бэкенд jina, модель {args.model}, "
              f"документ до {args.doc_chars} знаков, "
              f"лимит {args.jina_tpm} токенов/мин")
    else:
        import torch  # noqa: E402
        from sentence_transformers import CrossEncoder  # noqa: E402

        # Половина ядер, а не все: прогон идёт часами, и оставить машину без
        # процессора на это время нельзя.
        cores = os.cpu_count() or 1
        threads = args.threads or max(1, cores // 2)
        torch.set_num_threads(threads)
        print(f"Бэкенд crossenc, модель {args.model}, "
              f"потоков {torch.get_num_threads()} из {cores}, "
              f"длина входа {args.max_length}")
        print("ВНИМАНИЕ: локальный прогон надолго займёт процессор.")
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

    # Для crossenc на оценку влияет окно в токенах, для jina -- сколько
    # знаков документа мы отдали. В ключе должно стоять то, что влияет.
    cache_tag = args.max_length if args.backend == "crossenc" else f"c{args.doc_chars}"

    by_id = {p.object_id: p for p in OBJECT_PATTERNS}
    report: dict[str, Any] = {"model": args.model, "form": args.form,
                              "candidates": args.candidates,
                              "max_length": args.max_length, "objects": {}}

    pacer = TokenPacer(args.jina_tpm)

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
            todo = [d for d in ranked if f"{object_id}:{d}:{args.model}:{cache_tag}" not in cache]
            print(f"    кандидатов {len(ranked)}, к оценке {len(todo)}")

            started = time.time()
            for offset in range(0, len(todo), args.batch):
                batch = todo[offset : offset + args.batch]
                pairs = []
                for doc_id in batch:
                    title, text = docs.get(doc_id, ("", ""))
                    pairs.append([query_text, f"{title}. {text}"])
                if args.backend == "jina":
                    scores = jina_rerank(query_text, [d for _, d in pairs],
                                         api_key=jina_key, model=args.model,
                                         pacer=pacer)
                else:
                    scores = model.predict(pairs, show_progress_bar=False)
                for doc_id, value in zip(batch, scores):
                    key = f"{object_id}:{doc_id}:{args.model}:{cache_tag}"
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
            scored = [(d, cache.get(f"{object_id}:{d}:{args.model}:{cache_tag}")) for d in ranked]
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