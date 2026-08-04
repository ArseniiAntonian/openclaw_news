#!/usr/bin/env python3
"""Проба другого энкодера на сэмпле корпуса.

Зачем. `text-embedding-3-small` обучен на симметричном сходстве: два текста
похожи или нет. Мы же сравниваем фразу из трёх слов с новостью на несколько
тысяч знаков. Модели вроде BGE-M3 обучены именно на парах «короткий запрос
— длинный документ», то есть закрывают этот разрыв на уровне обучения, а не
переписыванием запроса. Плюс наш корпус русский, а OpenAI на русском слабее,
чем на английском.

Почему BGE-M3, а не multilingual-e5: у e5 окно 512 токенов, а p90 документа
в пуле — 4753 знака (~1500 токенов). E5 обрубал бы хвост каждому десятому
документу, и замер сравнивал бы не модели, а обрезку. У BGE-M3 длинный
контекст и те же 1024 измерения, что у нынешних векторов, — форма совпадает.

Почему на сэмпле, а не на всём корпусе. Полный пересчёт 40 тысяч документов
на процессоре — это ночь. Сэмпл (весь пул плюс случайные документы) считается
за час и даёт честный ответ на вопрос «стоит ли платить за полный пересчёт».
Абсолютный recall на уменьшенной вселенной вырастет у ОБЕИХ моделей -- стог
меньше, — поэтому сравнивать их надо на одном и том же сэмпле, что и делает
`run_eval.py --universe-table`.

Пишет в отдельную схему `retrieval_eval`, чтобы ничего из хозяйства Агента 1
не задеть: не понравится результат -- `DROP SCHEMA retrieval_eval CASCADE`.

Два бэкенда, потому что это две РАЗНЫЕ гипотезы:

- `--backend openrouter --model openai/text-embedding-3-large` -- «модель
  побольше». Тот же ключ, что у эмбеддингов Агента 1, ничего ставить не надо,
  считается за минуты. Но обучен так же симметрично, как нынешний 3-small,
  поэтому проверяет только размер модели.
- `--backend local --model BAAI/bge-m3` -- «модель, обученная на асимметрии».
  Требует torch и sentence-transformers (~2 ГБ с весами) и час счёта на
  процессоре, зато проверяет ту самую гипотезу про короткий запрос и длинный
  документ.

Начинать разумно с первого: он почти бесплатен, и если выигрыш даст уже он,
второй может не понадобиться.

    pip install sentence-transformers   # только для --backend local

Запуск:

    python embed_alt_model.py --backend openrouter --model openai/text-embedding-3-large
    python embed_alt_model.py --backend local --model BAAI/bge-m3
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

import psycopg
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from queries import QUERIES  # noqa: E402

SCHEMA = "agent_1_v5"
ALT_SCHEMA = "retrieval_eval"
DEFAULT_ENV = Path("/root/.openclaw/workspace/agents/agent_1/.env")
DEFAULT_MODEL = "openai/text-embedding-3-large"
EMBED_DIMS = 1024


JINA_EMBED_URL = "https://api.jina.ai/v1/embeddings"


def jina_embed(texts: list[str], *, api_key: str, model: str, task: str) -> list[list[float]]:
    """Эмбеддинги Jina с указанием роли текста.

    Ключевое отличие от `text-embedding-3`: у модели РАЗНЫЕ режимы для
    запроса и для документа -- `retrieval.query` и `retrieval.passage`. Она
    обучена на парах «короткая фраза -- длинный текст», то есть закрывает
    ровно тот разрыв, который мы весь эксперимент обходили переписыванием
    запроса. Симметричная модель такого различия не делает вовсе.

    Роль обязана быть правильной: документы, закодированные как запросы,
    окажутся в другой области пространства, и поиск сломается молча.
    """
    body = {"model": model, "input": texts, "task": task,
            "dimensions": EMBED_DIMS}
    last: Exception | None = None
    for attempt in range(1, 6):
        try:
            resp = requests.post(
                JINA_EMBED_URL,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json=body, timeout=180,
            )
            if resp.status_code in (401, 402, 403):
                raise SystemExit(f"Jina отказала ({resp.status_code}): {resp.text[:300]}")
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After") or 20)
                print(f"    429, жду {wait:.0f}с", flush=True)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            return [d["embedding"] for d in data]
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as exc:
            last = exc
            time.sleep(3 * attempt)
    raise RuntimeError(f"Jina не ответила: {last}")


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def vector_literal(vector) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {ALT_SCHEMA}")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {ALT_SCHEMA}.doc_embeddings (
                id_clean_post BIGINT NOT NULL,
                model         TEXT   NOT NULL,
                embedding     VECTOR({EMBED_DIMS}) NOT NULL,
                PRIMARY KEY (id_clean_post, model)
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {ALT_SCHEMA}.query_embeddings (
                object_id INT  NOT NULL,
                form      TEXT NOT NULL,
                model     TEXT NOT NULL,
                embedding VECTOR({EMBED_DIMS}) NOT NULL,
                PRIMARY KEY (object_id, form, model)
            )
            """
        )
    conn.commit()


def pick_sample(conn, pool_path: Path, extra: int, seed: int) -> list[int]:
    """Весь пул плюс случайные документы вне его.

    Пул обязателен: в нём лежат все размеченные документы, без них мерить
    нечего. Случайные добавляются как отвлекающий фон -- без них задача
    была бы неправдоподобно лёгкой, потому что вселенная состояла бы из
    одних тематических новостей.
    """
    pool_ids: list[int] = []
    if pool_path.is_file():
        for line in pool_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                pool_ids.append(json.loads(line)["id_clean_post"])

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id_clean_post FROM {SCHEMA}.clean_posts
            WHERE drop_reason IS NULL AND is_duplicate = FALSE
              AND embedding IS NOT NULL
            """
        )
        all_ids = [row[0] for row in cur.fetchall()]

    pool_set = set(pool_ids)
    rest = [i for i in all_ids if i not in pool_set]
    random.Random(seed).shuffle(rest)
    return sorted(pool_set | set(rest[:extra]))


def fetch_texts(conn, ids: list[int], max_chars: int) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT c.id_clean_post,
                   left(coalesce(c.clean_content, ''), %s)
            FROM {SCHEMA}.clean_posts c
            WHERE c.id_clean_post = ANY(%s)
            ORDER BY c.id_clean_post
            """,
            (max_chars, ids),
        )
        return [(row[0], row[1]) for row in cur.fetchall() if row[1].strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Пересчёт сэмпла другим энкодером")
    ap.add_argument("--backend", choices=("jina", "openrouter", "local"), default="jina",
                    help="jina -- jina-embeddings-v3, обучена на асимметрии "
                         "(разные режимы для запроса и документа), процессор не "
                         "трогает; openrouter -- модели OpenAI; local -- BGE-M3 на "
                         "процессоре, грузит машину надолго")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--pool", type=Path, default=Path("data/pool.jsonl"))
    ap.add_argument("--sample-random", type=int, default=5000,
                    help="сколько случайных документов добавить к пулу как фон")
    ap.add_argument("--max-chars", type=int, default=6000,
                    help="обрезка документа перед эмбеддингом")
    ap.add_argument("--batch", type=int, default=32,
                    help="размер батча: 32 для openrouter, 8 для процессора")
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    ap.add_argument("--dsn-var", default="AGENT_1_DB_DSN")
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--limit", type=int, default=None, help="взять первые N (проба)")
    args = ap.parse_args(argv)

    load_dotenv(args.env_file)
    dsn = os.environ.get(args.dsn_var)
    if not dsn:
        print(f"ERROR: {args.dsn_var} не найден", file=sys.stderr)
        return 1

    if args.backend == "jina":
        jina_key = os.environ.get("JINA_API_KEY", "")
        if not jina_key:
            print("ERROR: нет JINA_API_KEY", file=sys.stderr)
            return 1
        model_name = args.model if args.model != DEFAULT_MODEL else "jina-embeddings-v3"

        def encode(texts: list[str], task: str = "retrieval.passage"):
            return jina_embed(texts, api_key=jina_key, model=model_name, task=task)

        args.model = model_name
        print(f"Бэкенд jina, модель {model_name}, асимметричные режимы")
    elif args.backend == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            print("ERROR: OPENROUTER_API_KEY не найден", file=sys.stderr)
            return 1
        sys.path.insert(0, "/root/.openclaw/workspace/agents/agent_1/src")
        from agent_1 import embed_v5  # noqa: E402

        # Тот же путь, что у документов Агента 1: усечка до 1024 компонент и
        # L2-ренормализация внутри. Сравнение моделей должно отличаться
        # моделью, а не обработкой вектора.
        def encode(texts: list[str]):
            return embed_v5.openrouter_embed(
                texts, api_key=api_key, model=args.model,
                base_url=os.environ.get("OPENROUTER_BASE_URL", embed_v5.DEFAULT_BASE_URL),
            )
        print(f"Бэкенд: OpenRouter, модель {args.model}")
    else:
        from sentence_transformers import SentenceTransformer  # noqa: E402

        print(f"Загружаю {args.model} (процессор, первый раз качает веса)…")
        st_model = SentenceTransformer(args.model, device="cpu")
        dims = st_model.get_sentence_embedding_dimension()
        if dims != EMBED_DIMS:
            print(f"ERROR: у модели {dims} измерений, таблица рассчитана на {EMBED_DIMS}",
                  file=sys.stderr)
            return 1

        def encode(texts: list[str]):
            return st_model.encode(texts, batch_size=len(texts),
                                   normalize_embeddings=True, show_progress_bar=False)

    with psycopg.connect(dsn) as conn:
        ensure_tables(conn)

        ids = pick_sample(conn, args.pool, args.sample_random, args.seed)
        if args.limit:
            ids = ids[: args.limit]
        print(f"Сэмпл: {len(ids)} документов (пул + {args.sample_random} случайных)")

        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id_clean_post FROM {ALT_SCHEMA}.doc_embeddings WHERE model = %s",
                (args.model,),
            )
            done = {row[0] for row in cur.fetchall()}
        if done:
            print(f"Уже посчитано ранее: {len(done)} — пропускаю")
        ids = [i for i in ids if i not in done]

        texts = fetch_texts(conn, ids, args.max_chars)
        print(f"К счёту: {len(texts)} документов")

        started = time.time()
        for offset in range(0, len(texts), args.batch):
            chunk = texts[offset : offset + args.batch]
            vectors = encode([t for _, t in chunk])
            with conn.cursor() as cur:
                for (doc_id, _), vector in zip(chunk, vectors):
                    cur.execute(
                        f"""
                        INSERT INTO {ALT_SCHEMA}.doc_embeddings (id_clean_post, model, embedding)
                        VALUES (%s, %s, %s::vector)
                        ON CONFLICT (id_clean_post, model) DO UPDATE SET embedding = EXCLUDED.embedding
                        """,
                        (doc_id, args.model, vector_literal(vector)),
                    )
            conn.commit()
            done_n = offset + len(chunk)
            if done_n % (args.batch * 25) == 0 or done_n == len(texts):
                elapsed = time.time() - started
                speed = done_n / elapsed if elapsed else 0
                left = (len(texts) - done_n) / speed if speed else 0
                print(f"  {done_n}/{len(texts)}  {speed:.1f} док/с  осталось ~{left/60:.0f} мин",
                      flush=True)

        # Запросы считаются той же моделью: иначе вектор запроса окажется в
        # чужом пространстве и косинус сравнит несравнимое.
        print("Считаю векторы запросов…")
        rows = [(oid, form, text) for oid, forms in QUERIES.items()
                for form, text in forms.items()]
        if args.backend == "jina":
            # Роль запроса, а не документа: в этом весь смысл асимметричной
            # модели, и перепутать здесь -- значит незаметно сломать поиск.
            vectors = encode([t for _, _, t in rows], task="retrieval.query")
        else:
            vectors = encode([t for _, _, t in rows])
        with conn.cursor() as cur:
            for (oid, form, _), vector in zip(rows, vectors):
                cur.execute(
                    f"""
                    INSERT INTO {ALT_SCHEMA}.query_embeddings (object_id, form, model, embedding)
                    VALUES (%s, %s, %s, %s::vector)
                    ON CONFLICT (object_id, form, model) DO UPDATE SET embedding = EXCLUDED.embedding
                    """,
                    (oid, form, args.model, vector_literal(vector)),
                )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE INDEX IF NOT EXISTS doc_embeddings_hnsw
                ON {ALT_SCHEMA}.doc_embeddings USING hnsw (embedding vector_cosine_ops)
                """
            )
        conn.commit()
        print("Индекс HNSW построен")

    print(f"\nГотово. Снести всё: DROP SCHEMA {ALT_SCHEMA} CASCADE;")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())