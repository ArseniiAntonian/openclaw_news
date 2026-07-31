#!/usr/bin/env python3
"""Шаг 3: замер ретрива против размеченного эталона.

Отвечает на вопрос, ради которого затевался эксперимент: как искать
документы под объект наблюдения — ключевыми словами, вектором по короткому
названию объекта или вектором по развёрнутому описанию.

Вход  -- `data/labels.jsonl` (эталон, выход `label_queue.py`) и БД.
Выход -- таблица recall по объектам и методам, плюс JSON-отчёт.

## Что меряется и чего НЕ меряется

Меряется **recall**: какую долю размеченных положительных документов метод
поднял. Именно он важен по построению задачи -- недостанутый документ
дальше по пайплайну не вернётся ничем, тогда как лишний отсеется на
следующих шагах (см. хэндофф, раздел 3).

**Precision честно померить нельзя**, и скрипт её не печатает. Метод
возвращает документы за пределами размеченной 1318-й очереди, а их
релевантность нам неизвестна: они не мусор и не попадания, они просто
не размечены. Любая «точность» здесь была бы выдумкой.

## Смещение, которое надо держать в голове при чтении цифр

Эталон размечен только внутри пула, а пул набирался как
`широкий невод ∪ ключевые слова каталога`. Значит:

- документ, который ловят ключевые слова, гарантированно был в пуле и мог
  получить метку;
- документ, релевантный по смыслу, но без ИИ-лексики и без ключевых слов,
  в пул не попадал вообще -- и вектор, найдя его, **не получит за это
  баллов**, потому что метки у него нет.

Смещение работает **в пользу регекса**. Поэтому: проигрыш регекса
значим, а его выигрыш -- нет. Если вектор выигрывает вопреки смещению,
вывод крепкий.

## Симметрия эмбеддингов

Документы эмбеддились из `clean_content` (без заголовка), с усечкой до
8000 знаков, моделью `openai/text-embedding-3-small` через OpenRouter, с
клиентской усечкой до 1024 компонент и L2-ренормализацией. Запрос обязан
пройти ровно тот же путь, иначе косинус сравнивает разные пространства --
поэтому здесь переиспользуется `embed_v5.openrouter_embed`, а не свой
вызов API.

Только чтение БД. Пишет один файл отчёта.

    python run_eval.py --labels data/labels.jsonl --out data/eval_report.json
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

SCHEMA = "agent_1_v5"
DEFAULT_ENV = Path("/root/.openclaw/workspace/agents/agent_1/.env")
DEFAULT_KS = (10, 50, 100, 500)

# Вторая ось эксперимента: чем представлен запрос. Три формы одного и того
# же объекта -- это и есть измеряемая переменная, поэтому они выписаны
# здесь явно, а не собираются из чужих структур.
QUERIES: dict[int, dict[str, str]] = {
    1: {
        "name": "GigaChat",
        "aliases": "GigaChat ГигаЧат Гигачат Гигачад гига нейросеть Сбера Кандинский Kandinsky",
        "description": "Новости о GigaChat — генеративной нейросети Сбера: запуск функций, обновления моделей, партнёрства, применение в продуктах, а также о Кандинском для генерации изображений.",
    },
    2: {
        "name": "YandexGPT и Алиса",
        "aliases": "YandexGPT ЯндексГПТ YaGPT Алиса AI Яндекс Нейро Yandex Cloud ML",
        "description": "Новости об ИИ-продуктах Яндекса: языковой модели YandexGPT, голосовом помощнике Алиса, нейропоиске и облачных ML-сервисах Яндекса.",
    },
    3: {
        "name": "Open-source модели для self-hosting",
        "aliases": "DeepSeek Qwen Llama Mistral открытые модели open-source LLM",
        "description": "Новости об открытых языковых моделях, которые можно развернуть на своей инфраструктуре: DeepSeek, Qwen, Llama, Mistral — релизы, лицензии, сравнения с закрытыми моделями.",
    },
    4: {
        "name": "OpenAI и ChatGPT",
        "aliases": "OpenAI ChatGPT GPT-4 GPT-5 Sora Anthropic Claude чат-гпт",
        "description": "Новости о глобальных лидерах генеративного ИИ: OpenAI и ChatGPT, Anthropic и Claude — релизы моделей, расследования и иски, ограничения доступа, конкуренция.",
    },
    5: {
        "name": "Доверие к ИИ",
        "aliases": "доверие к ИИ страх ИИ восприятие нейросетей хайп вокруг ИИ",
        "description": "Новости об отношении общества к искусственному интеллекту: доверие и недоверие, страхи и скепсис, недовольство ИИ-продуктами, опросы о восприятии нейросетей.",
    },
    6: {
        "name": "Регуляторика ИИ",
        "aliases": "закон об ИИ регулирование ИИ маркировка ИИ-контента ГОСТ ИИ AI Act персональные данные",
        "description": "Новости о государственном регулировании искусственного интеллекта: законопроекты, обязательная маркировка ИИ-контента, требования к персональным данным, стандарты и запреты.",
    },
    7: {
        "name": "GPU и вычислительные мощности",
        "aliases": "GPU видеокарты дефицит чипов дата-центры ИИ Nvidia вычислительные мощности",
        "description": "Новости о железе для искусственного интеллекта: графические ускорители и ИИ-чипы, память, дефицит и поставки, строительство и стоимость дата-центров под ИИ-нагрузку.",
    },
    8: {
        "name": "Корпоративное внедрение GenAI",
        "aliases": "внедрение ИИ в бизнесе enterprise AI ИИ на предприятии цифровизация",
        "description": "Новости о применении генеративного ИИ в компаниях и на производстве: внедрения в отраслях, корпоративные платформы, автоматизация процессов, эффекты для бизнеса.",
    },
    9: {
        "name": "Лидеры мнений в теме ИИ",
        "aliases": "эксперты по ИИ ИИ-инфлюенсеры евангелисты ИИ AI-блогеры",
        "description": "Публичные высказывания экспертов и лидеров мнений об искусственном интеллекте: прогнозы, оценки влияния нейросетей на профессии и рынок, интервью специалистов.",
    },
    10: {
        "name": "Инциденты и безопасность GenAI",
        "aliases": "утечки ИИ галлюцинации нейросети дипфейк запрет ИИ сбой нейросети",
        "description": "Новости о происшествиях вокруг генеративного ИИ: утечки данных, галлюцинации моделей, дипфейки и мошенничество с использованием нейросетей, сбои и блокировки.",
    },
}


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


def load_truth(path: Path, min_positives: int, relation: str
               ) -> tuple[dict[int, set[int]], set[int]]:
    """object_id -> положительные id, плюс множество ВСЕХ размеченных id.

    `relation` выбирает определение положительного:
      "event"  -- только события (новость про объект по существу);
      "any"    -- события и упоминания вместе, то есть то, на что нацелены
                  ключевые слова каталога банка.

    Разметка хранит оба уровня, поэтому смена определения не требует
    повторного прогона модели.

    Множество всех размеченных нужно, чтобы отличать «документ размечен и к
    объекту не относится» от «документ вообще не размечен». Первое -- ошибка
    метода, второе -- неизвестность, и смешивать их нельзя.
    """
    truth: dict[int, set[int]] = {}
    labelled: set[int] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        doc_id = int(row["id_clean_post"])
        labelled.add(doc_id)
        ids = list(row.get("label_objects") or [])
        if relation == "any":
            ids += list(row.get("mention_objects") or [])
        for object_id in ids:
            truth.setdefault(int(object_id), set()).add(doc_id)
    return {k: v for k, v in truth.items() if len(v) >= min_positives}, labelled


def fetch_titles(conn, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT c.id_clean_post, coalesce(r.title, '')
            FROM {SCHEMA}.clean_posts c
            JOIN {SCHEMA}.raw_posts r ON r.id_raw_post = c.id_raw_post
            WHERE c.id_clean_post = ANY(%s)
            """,
            (list(ids),),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def regex_hits(conn, pattern: str, negative: str | None) -> set[int]:
    sql = f"""
        SELECT c.id_clean_post
        FROM {SCHEMA}.clean_posts c
        JOIN {SCHEMA}.raw_posts r ON r.id_raw_post = c.id_raw_post
        WHERE c.drop_reason IS NULL AND c.is_duplicate = FALSE
          AND (coalesce(r.title,'') || ' ' || coalesce(c.clean_content,'')) ~* %s
    """
    params: list[Any] = [to_postgres(pattern)]
    if negative:
        sql += "  AND (coalesce(r.title,'') || ' ' || coalesce(c.clean_content,'')) !~* %s\n"
        params.append(to_postgres(negative))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return {row[0] for row in cur.fetchall()}


def vector_hits(conn, vector_literal: str, limit: int) -> list[int]:
    """Топ-K по косинусу. Порядок сохраняется -- он нужен для recall@K."""
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
            (vector_literal, limit),
        )
        return [row[0] for row in cur.fetchall()]


def recall(found: set[int], positives: set[int]) -> float:
    return len(found & positives) / len(positives) if positives else 0.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Замер ретрива против эталона")
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    ap.add_argument("--dsn-var", default="AGENT_1_DB_DSN")
    ap.add_argument("--min-positives", type=int, default=5,
                    help="объекты с меньшим числом положительных не мерим -- шум")
    ap.add_argument("--ks", type=int, nargs="+", default=list(DEFAULT_KS))
    ap.add_argument("--examples", type=int, default=5,
                    help="сколько заголовков показывать в каждой категории примеров")
    ap.add_argument("--exclude-objects", type=int, nargs="*", default=[],
                    help="не мерить эти объекты. Нужно для тех, где разметка "
                         "заведомо испорчена: объект 9 после уточнения "
                         "формулировки собрал 293 метки «только модель» против "
                         "2 совпадений с каталогом, то есть стал означать "
                         "«любая новость, где кто-то высказался об ИИ»")
    ap.add_argument("--relation", choices=("event", "any"), default="event",
                    help="что считать положительным: только события (по умолчанию) "
                         "или события вместе с упоминаниями -- второе ближе к тому, "
                         "на что нацелены ключевые слова каталога")
    args = ap.parse_args(argv)

    load_dotenv(args.env_file)
    dsn = os.environ.get(args.dsn_var)
    if not dsn:
        print(f"ERROR: {args.dsn_var} не найден", file=sys.stderr)
        return 1
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY не найден", file=sys.stderr)
        return 1

    from agent_1 import embed_v5  # noqa: E402  -- нужен .env, загруженный выше

    if not args.labels.is_file():
        print(f"ERROR: не найден {args.labels}", file=sys.stderr)
        return 1
    truth, labelled_ids = load_truth(args.labels, args.min_positives, args.relation)
    for object_id in args.exclude_objects:
        if truth.pop(object_id, None) is not None:
            print(f"Объект {object_id} исключён из замера (--exclude-objects)")
    if not truth:
        print("Эталон пуст или во всех объектах слишком мало положительных.", file=sys.stderr)
        return 1
    labelled_total = len(labelled_ids)

    by_id = {p.object_id: p for p in OBJECT_PATTERNS}
    max_k = max(args.ks)
    report: dict[str, Any] = {"labelled_total": labelled_total, "objects": {}}

    relation_note = ("только события" if args.relation == "event"
                     else "события и упоминания вместе")
    print(f"\nЭталон: {labelled_total} размеченных документов, "
          f"объектов к замеру: {len(truth)} (порог {args.min_positives} положительных)")
    print(f"Положительным считается: {relation_note} (--relation {args.relation})\n")
    print("Precision не считается намеренно: релевантность документов вне "
          "размеченной очереди неизвестна.")
    print("Смещение эталона -- в пользу регекса (см. шапку файла): его "
          "проигрыш значим, выигрыш -- нет.\n")

    with psycopg.connect(dsn) as conn:
        for object_id in sorted(truth):
            positives = truth[object_id]
            pattern = by_id[object_id]
            queries = QUERIES[object_id]

            print(f"--- Объект {object_id}: {pattern.label}")
            print(f"    положительных в эталоне: {len(positives)}"
                  + ("   [регекс реконструирован]" if pattern.approx else ""))

            row: dict[str, Any] = {"label": pattern.label, "approx_regex": pattern.approx,
                                   "positives": len(positives), "methods": {}}

            # 1. Ключевые слова каталога, с негатив-фильтром и без него.
            for name, neg in (("regex", None), ("regex+негатив", pattern.negative)):
                hits = regex_hits(conn, pattern.regex, neg)
                value = recall(hits, positives)
                row["methods"][name] = {"recall": value, "returned": len(hits)}
                print(f"    {name:<16} recall {value:5.1%}   поднято документов: {len(hits)}")

            # 2. Три формы запроса через вектор.
            for form in ("name", "aliases", "description"):
                text = queries[form]
                vector = embed_v5.openrouter_embed(
                    [text], api_key=api_key,
                    model=os.environ.get("EMBED_MODEL", embed_v5.DEFAULT_MODEL),
                    base_url=os.environ.get("OPENROUTER_BASE_URL", embed_v5.DEFAULT_BASE_URL),
                )[0]
                ranked = vector_hits(conn, embed_v5.vector_literal(vector), max_k)
                per_k = {}
                for k in sorted(args.ks):
                    value = recall(set(ranked[:k]), positives)
                    per_k[k] = value
                row["methods"][f"vector:{form}"] = {"recall_at_k": per_k}
                cells = "  ".join(f"@{k} {per_k[k]:5.1%}" for k in sorted(args.ks))
                print(f"    vector:{form:<9} {cells}")

            # 3. Цепочка из архитектурной схемы банка (2026-07-31): сначала
            # семантический поиск, ЗАТЕМ фильтр по ключевым словам и
            # негатив-фильтр по отобранному. Это пересечение, а не
            # объединение, поэтому recall цепочки ограничен сверху recall'ом
            # ключевых слов -- всё, что вектор нашёл, а каталог не ловит,
            # отсекается вторым шагом. Меряем явно, чтобы цена этого решения
            # была числом, а не рассуждением.
            regex_filtered = regex_hits(conn, pattern.regex, pattern.negative)
            chain_form = max(
                ("name", "aliases", "description"),
                key=lambda f: row["methods"][f"vector:{f}"]["recall_at_k"][max_k],
            )
            chain_vector = embed_v5.openrouter_embed(
                [queries[chain_form]], api_key=api_key,
                model=os.environ.get("EMBED_MODEL", embed_v5.DEFAULT_MODEL),
                base_url=os.environ.get("OPENROUTER_BASE_URL", embed_v5.DEFAULT_BASE_URL),
            )[0]
            chain_ranked = vector_hits(conn, embed_v5.vector_literal(chain_vector), max_k)
            chain_per_k = {}
            for k in sorted(args.ks):
                chain_per_k[k] = recall(set(chain_ranked[:k]) & regex_filtered, positives)
            row["methods"][f"схема: vector:{chain_form} → ключевые слова"] = {
                "recall_at_k": chain_per_k}
            cells = "  ".join(f"@{k} {chain_per_k[k]:5.1%}" for k in sorted(args.ks))
            print(f"    схема (И)        {cells}")

            # 4. Вариант B из хэндоффа: объединение регекса и вектора.
            regex_set = regex_hits(conn, pattern.regex, None)
            best_form = max(
                ("name", "aliases", "description"),
                key=lambda f: row["methods"][f"vector:{f}"]["recall_at_k"][max_k],
            )
            vector_best = embed_v5.openrouter_embed(
                [queries[best_form]], api_key=api_key,
                model=os.environ.get("EMBED_MODEL", embed_v5.DEFAULT_MODEL),
                base_url=os.environ.get("OPENROUTER_BASE_URL", embed_v5.DEFAULT_BASE_URL),
            )[0]
            union_k = min(max_k, 100)
            ranked_best = vector_hits(conn, embed_v5.vector_literal(vector_best), union_k)
            union = regex_set | set(ranked_best)
            value = recall(union, positives)
            row["methods"][f"regex ∪ vector:{best_form}@{union_k}"] = {
                "recall": value, "returned": len(union)}
            print(f"    объединение      recall {value:5.1%}   "
                  f"(регекс ∪ vector:{best_form}@{union_k}, поднято {len(union)})")

            # --- примеры: числа без заголовков не интерпретируются ---
            vector_only = (positives & set(ranked_best)) - regex_set
            missed_all = positives - regex_set - set(ranked_best)
            # Ложные срабатывания регекса считаем ТОЛЬКО по размеченным
            # документам: поднятый, но неразмеченный документ -- это
            # неизвестность, а не ошибка.
            regex_false = (regex_set & labelled_ids) - positives

            titles = fetch_titles(conn, vector_only | missed_all | regex_false)
            buckets = (
                ("вектор нашёл, ключевые слова пропустили", vector_only,
                 "ради этого затевался эксперимент"),
                ("не нашёл никто", missed_all,
                 "потолок обоих методов на этом корпусе"),
                ("ключевые слова подняли ошибочно", regex_false,
                 "только среди размеченных; неразмеченные не в счёт"),
            )
            row["examples"] = {}
            for name, ids, note in buckets:
                row["examples"][name] = {
                    "count": len(ids),
                    "titles": [titles.get(i, "") for i in sorted(ids)[: args.examples]],
                }
                if not ids:
                    continue
                print(f"    · {name}: {len(ids)}  ({note})")
                for doc_id in sorted(ids)[: args.examples]:
                    print(f"        {titles.get(doc_id, '')[:92]}")
            print()

            report["objects"][str(object_id)] = row

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Отчёт: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())