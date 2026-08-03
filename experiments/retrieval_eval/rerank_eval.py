#!/usr/bin/env python3
"""Шаг 4: реранкер поверх векторного отбора.

## Зачем это главный резерв

Замер трижды показал одно и то же: нужные документы **находятся**, но стоят
низко. На сэмпле recall@100 равен 56%, а recall@500 -- 84%. Двадцать восемь
пунктов лежат между сотым и пятисотым местом. Для сравнения, смена энкодера
с 3-small на 3-large дала два пункта. То есть порядок в выдаче стоит вчетверо
дороже, чем качество самого поиска.

Причина в том, как устроен первый этап. Документ заранее сжат в один вектор,
запрос -- в другой, и сравниваются только эти два сжатия. Всё, что не попало
в усреднение, потеряно до того, как поиск начался.

Реранкер работает иначе: он смотрит на пару «запрос -- документ» целиком и
судит по тексту, а не по его сжатию. Поэтому он способен поднять документ,
который первый этап поставил на 180-е место. Но он **не может** найти то, что
первый этап не принёс: всё, чего нет в топ-N, для него не существует.

## Два бэкенда

`--backend crossenc` -- `bge-reranker-v2-m3`, классический кросс-энкодер.
После установки бесплатен при любом числе прогонов и детерминирован:
одинаковый вход даёт одинаковые оценки. Для замера, где мы ловим разницу в
три-пять пунктов, второе важно не меньше первого. Цена -- torch и веса,
пара гигабайт на диске.

`--backend llm` -- модель через OpenRouter. Ничего не ставить, но каждый
прогон стоит денег, а оценки плавают от запуска к запуску.

Изначально я взял LLM, отвергнув кросс-энкодер по двум причинам, и обе
оказались неверны. Скорость я завысил: на процессоре с батчингом это
порядка пяти-десяти пар в секунду, то есть минуты на весь прогон, а не на
объект. А ограничение в 512 токенов не мешает, потому что документы и так
режутся до `--doc-chars` (по умолчанию 1200 знаков, около 400 токенов).

## Как читать результат

Сравнивается recall@50 после реранкинга с recall@100 до него. Смысл в том,
что реранкер должен отдавать **вдвое меньше документов, но не меньше нужного**:
дальше по пайплайну идёт экстрактор, и размер его входа -- это деньги.

Оценки кэшируются в файл, поэтому повторный запуск не переспрашивает модель.

    python rerank_eval.py --labels data/labels_v2.jsonl --exclude-objects 9 \\
        --backend crossenc --model BAAI/bge-reranker-v2-m3
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import psycopg
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/root/.openclaw/workspace/agents/agent_1/src")

from patterns import OBJECT_PATTERNS  # noqa: E402
from queries import QUERIES  # noqa: E402
from run_eval import (  # noqa: E402
    ALT_SCHEMA, SCHEMA, load_dotenv, load_truth, set_ef_search, vector_hits,
)

DEFAULT_ENV = Path("/root/.openclaw/workspace/agents/agent_1/.env")
DEFAULT_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_CE_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
REQUEST_TIMEOUT = 180
RETRIES = 4

PROMPT = """Ты оцениваешь релевантность новостей объекту мониторинга.

Объект: {label}
Что к нему относится: {description}

Ниже {n} новостей. Для каждой поставь оценку от 0 до 10:

10 — новость целиком об этом объекте, он в центре сюжета
7-9 — объект прямо затронут событием новости
4-6 — объект упомянут, но новость в основном про другое
1-3 — связь косвенная или притянутая
0 — к объекту отношения не имеет

Оценивай каждую новость саму по себе, не сравнивая с соседними по списку.

Верни СТРОГО JSON, без пояснений и markdown:
{{"scores": [{{"id": <id новости>, "score": <0-10>}}]}}

Оцени все {n} новостей, ничего не пропуская.

Новости:
"""


def chat(prompt: str, *, api_key: str, model: str, base_url: str,
         max_tokens: int) -> str:
    """Один запрос к модели.

    `max_tokens` задаётся явно и намеренно скупо. OpenRouter резервирует
    кредиты под заявленный потолок ответа, а не под фактический: без этого
    параметра подставляется значение по умолчанию (65536), и запрос падает
    с HTTP 402 «недостаточно кредитов», хотя ответ занимает полторы сотни
    токенов -- десяток строк JSON с оценками.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json=body, timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 402:
                # Кредитов не хватает -- повторять бессмысленно, они не
                # появятся сами. Падаем сразу, не тратя попытки.
                raise SystemExit(
                    "OpenRouter: недостаточно кредитов. "
                    f"Модель {model}, max_tokens={max_tokens}. "
                    f"Возьми модель дешевле через --model. {resp.text[:300]}"
                )
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}")
            return resp.json()["choices"][0]["message"]["content"]
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as exc:
            last = exc
            if attempt < RETRIES:
                time.sleep(3 * attempt)
    raise RuntimeError(f"реранкер не ответил после {RETRIES} попыток: {last}")


def parse_scores(raw: str) -> dict[int, float]:
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.splitlines() if not l.startswith("```"))
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("в ответе нет JSON")
    payload = json.loads(text[start : end + 1])
    out: dict[int, float] = {}
    for row in payload.get("scores") or []:
        if isinstance(row, dict) and isinstance(row.get("id"), int):
            try:
                out[row["id"]] = float(row.get("score", 0))
            except (TypeError, ValueError):
                continue
    return out


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
    ap = argparse.ArgumentParser(description="Реранкер поверх векторного отбора")
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("data/rerank_report.json"))
    ap.add_argument("--cache", type=Path, default=Path("data/rerank_cache.jsonl"))
    ap.add_argument("--backend", choices=("crossenc", "llm"), default="crossenc",
                    help="crossenc -- локальный кросс-энкодер, бесплатен и детерминирован; "
                         "llm -- модель через OpenRouter, ничего не ставить, но платно")
    ap.add_argument("--model", default=None,
                    help="имя модели. По умолчанию BAAI/bge-reranker-v2-m3 для "
                         "crossenc и anthropic/claude-sonnet-5 для llm")
    ap.add_argument("--form", choices=("name", "aliases", "description"),
                    default="description", help="форма запроса для первого этапа")
    ap.add_argument("--candidates", type=int, default=200,
                    help="сколько документов первый этап отдаёт реранкеру")
    ap.add_argument("--batch", type=int, default=10, help="документов в одном запросе")
    ap.add_argument("--workers", type=int, default=4, help="параллельных запросов")
    ap.add_argument("--doc-chars", type=int, default=1200)
    ap.add_argument("--ce-max-length", type=int, default=512,
                    help="длина входа кросс-энкодера в токенах. Меньше -- быстрее "
                         "нелинейно; 256 обрежет половину документов, но заголовок "
                         "и лид останутся, а релевантность обычно видна там")
    ap.add_argument("--ce-threads", type=int, default=None,
                    help="потоков torch; по умолчанию все ядра")
    ap.add_argument("--exclude-objects", type=int, nargs="*", default=[])
    ap.add_argument("--min-positives", type=int, default=5)
    ap.add_argument("--relation", choices=("event", "any"), default="event")
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    ap.add_argument("--dsn-var", default="AGENT_1_DB_DSN")
    args = ap.parse_args(argv)

    if args.model is None:
        args.model = DEFAULT_CE_MODEL if args.backend == "crossenc" else DEFAULT_MODEL

    load_dotenv(args.env_file)
    dsn = os.environ.get(args.dsn_var)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not dsn:
        print("ERROR: нет AGENT_1_DB_DSN", file=sys.stderr)
        return 1
    if not api_key:
        # Ключ нужен всегда: первый этап (векторный отбор) идёт через
        # OpenRouter независимо от того, чем реранкуем.
        print("ERROR: нет OPENROUTER_API_KEY (нужен для векторного отбора)", file=sys.stderr)
        return 1

    cross_encoder = None
    if args.backend == "crossenc":
        import torch  # noqa: E402
        from sentence_transformers import CrossEncoder  # noqa: E402

        # По умолчанию torch на сервере часто берёт одно ядро, и модель
        # считается во столько раз медленнее, сколько ядер простаивает.
        # Первый прогон дал 5.7 секунды на пару -- скорее всего именно из-за
        # этого.
        threads = args.ce_threads or (os.cpu_count() or 1)
        torch.set_num_threads(threads)
        print(f"Загружаю кросс-энкодер {args.model} "
              f"(процессор, потоков {torch.get_num_threads()}, "
              f"длина входа {args.ce_max_length})…")
        cross_encoder = CrossEncoder(args.model, device="cpu",
                                     max_length=args.ce_max_length)
    base_url = os.environ.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL)

    from agent_1 import embed_v5  # noqa: E402

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
                              "candidates": args.candidates, "objects": {}}

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
                base_url=base_url,
            )[0]
            ranked = vector_hits(conn, embed_v5.vector_literal(vector), args.candidates)
            docs = fetch_docs(conn, ranked, args.doc_chars)

            todo = [d for d in ranked if f"{object_id}:{d}:{args.model}" not in cache]
            print(f"    кандидатов {len(ranked)}, к оценке {len(todo)}")

            batches = [todo[i : i + args.batch] for i in range(0, len(todo), args.batch)]

            query_text = f"{pattern.label}. {QUERIES[object_id]['description']}"

            def score_batch(batch: list[int]) -> dict[int, float]:
                if cross_encoder is not None:
                    # Кросс-энкодер получает пару целиком: запрос и документ в
                    # одном входе. Именно это и отличает его от первого этапа,
                    # где оба сжимались в векторы по отдельности.
                    pairs = []
                    for doc_id in batch:
                        title, text = docs.get(doc_id, ("", ""))
                        pairs.append([query_text, f"{title}. {text}"])
                    scores = cross_encoder.predict(pairs, show_progress_bar=False)
                    return {doc_id: float(v) for doc_id, v in zip(batch, scores)}

                blocks = []
                for doc_id in batch:
                    title, text = docs.get(doc_id, ("", ""))
                    blocks.append(f"--- id: {doc_id}\nЗаголовок: {title}\nТекст: {text}")
                prompt = (PROMPT.format(label=pattern.label,
                                        description=QUERIES[object_id]["description"],
                                        n=len(batch))
                          + "\n\n".join(blocks))
                # ~40 токенов на запись плюс запас на обёртку JSON.
                budget = 200 + 40 * len(batch)
                return parse_scores(chat(prompt, api_key=api_key, model=args.model,
                                         base_url=base_url, max_tokens=budget))

            if batches:
                started = time.time()
                # Кросс-энкодер и так батчится внутри и держит один процесс;
                # потоки ему только мешают. Потоки нужны LLM, где время уходит
                # на ожидание сети.
                workers = 1 if cross_encoder is not None else args.workers
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for n, scores in enumerate(pool.map(score_batch, batches), start=1):
                        for doc_id, value in scores.items():
                            key = f"{object_id}:{doc_id}:{args.model}"
                            cache[key] = value
                            cache_fh.write(json.dumps({"key": key, "score": value}) + "\n")
                        cache_fh.flush()
                        if n % 5 == 0 or n == len(batches):
                            print(f"      батч {n}/{len(batches)}  "
                                  f"{time.time() - started:.0f}с", flush=True)

            # Документы без оценки (модель пропустила) уходят в конец, сохраняя
            # исходный порядок: молча выбрасывать их нельзя -- это была бы
            # потеря recall, замаскированная под работу реранкера.
            scored = [(d, cache.get(f"{object_id}:{d}:{args.model}")) for d in ranked]
            missing = [d for d, s in scored if s is None]
            reranked = [d for d, s in sorted(
                ((d, s) for d, s in scored if s is not None),
                key=lambda ds: (-ds[1], ranked.index(ds[0])))] + missing

            row: dict[str, Any] = {"label": pattern.label, "positives": len(positives),
                                   "unscored": len(missing), "before": {}, "after": {}}
            print(f"    {'K':>5} {'до реранка':>12} {'после':>10}")
            # Дедупликация глубин: при --candidates 50 список (10,20,50,100,50)
            # печатал K=50 дважды. И K больше числа кандидатов бессмысленны --
            # recall там упирается в размер выдачи, а не в метод.
            depths = sorted({k for k in (10, 20, 50, 100, args.candidates)
                             if k <= args.candidates})
            for k in depths:
                before = recall(set(ranked[:k]), positives)
                after = recall(set(reranked[:k]), positives)
                row["before"][k] = before
                row["after"][k] = after
                mark = "  <-- вдвое меньше документов" if k == 50 else ""
                print(f"    {k:>5} {before:>11.1%} {after:>10.1%}{mark}")
            if missing:
                print(f"    без оценки: {len(missing)} (ушли в конец списка)")
            report["objects"][str(object_id)] = row

    cache_fh.close()

    print("\n=== Итог: реранкер отдаёт вдвое меньше документов ===")
    print(f"{'об':>3} {'до@100':>8} {'после@50':>10} {'разница':>9}")
    deltas = []
    for oid, row in sorted(report["objects"].items(), key=lambda kv: int(kv[0])):
        before = row["before"][100]
        after = row["after"][50]
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