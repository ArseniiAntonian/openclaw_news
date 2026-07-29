#!/usr/bin/env python3
"""Шаг 1 эксперимента по ретриву: выгрузка пула кандидатов на разметку.

Достаёт из `agent_1_v5.clean_posts` живые документы, попадающие под широкий
невод по теме ИИ (см. `patterns.py`), и кладёт их в JSONL. Этот файл потом
размечается по объектам наблюдения и становится нашим эталоном вместо
неприменимого answer key банка (его окно 2026-06-09..13 с корпусом не
пересекается, проверено 2026-07-29).

Только чтение. Скрипт ничего не пишет в БД и не ходит в сеть.

Ключевое проектное решение -- пул набирается объединением широкого невода и
ключевых слов объектов, а не одними ключевыми словами. Обоснование в шапке
`patterns.py`: иначе recall ключевых слов оказывается 100% по построению.

Запуск (на сервере, где лежит .env с DSN):

    cd /root/.openclaw/workspace/agents/agent_1
    . .venv/bin/activate
    python /root/.openclaw/workspace/experiments/retrieval_eval/build_candidate_pool.py \
        --out /root/.openclaw/workspace/experiments/retrieval_eval/data/pool.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))

from patterns import BROAD_NET, OBJECT_PATTERNS, pool_sql_regex  # noqa: E402

SCHEMA = "agent_1_v5"
DEFAULT_ENV_PATH = Path("/root/.openclaw/workspace/agents/agent_1/.env")
HOST_RE = re.compile(r"^(?:https?://)?(?:www\.)?([^/?#]+)", re.IGNORECASE)


def load_dotenv(path: Path) -> None:
    """Минимальный .env-ридер: не перетирает уже выставленные переменные."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def host_of(url: str | None) -> str:
    if not url:
        return ""
    m = HOST_RE.match(url)
    return m.group(1).lower() if m else ""


def percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(q * len(ordered)))
    return ordered[idx]


def fetch_rows(dsn: str, limit: int | None) -> list[dict[str, Any]]:
    sql = f"""
        SELECT c.id_clean_post,
               c.time_post,
               s.name_source,
               r.url,
               r.title,
               c.clean_content,
               r.metadata->>'summary'   AS summary,
               r.metadata->>'companies' AS companies
        FROM {SCHEMA}.clean_posts c
        JOIN {SCHEMA}.raw_posts r ON r.id_raw_post = c.id_raw_post
        JOIN {SCHEMA}.source   s ON s.id_source   = r.id_source
        WHERE c.drop_reason IS NULL
          AND c.is_duplicate = FALSE
          AND (coalesce(r.title, '') || ' ' || coalesce(c.clean_content, '')) ~* %s
        ORDER BY c.time_post
    """
    if limit is not None:
        sql += f"\n        LIMIT {int(limit)}"

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, (pool_sql_regex(),))
        columns = [d.name for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def tag(text: str) -> tuple[list[str], list[int]]:
    """Какие термины невода и какие объекты сработали на документе."""
    broad = [p.key for p in BROAD_NET if p.compiled().search(text)]
    objects = [p.object_id for p in OBJECT_PATTERNS if p.compiled().search(text)]
    return broad, objects


def build(rows: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for row in rows:
        title = row["title"] or ""
        content = row["clean_content"] or ""
        haystack = f"{title} {content}"
        broad, objects = tag(haystack)
        docs.append(
            {
                "id_clean_post": row["id_clean_post"],
                "time_post": row["time_post"].isoformat() if row["time_post"] else None,
                "source": row["name_source"],
                "host": host_of(row["url"]),
                "url": row["url"],
                "title": title,
                "text": content[:max_chars],
                "text_len": len(content),
                "truncated": len(content) > max_chars,
                "summary": row["summary"],
                "companies_raw": row["companies"],
                "matched_broad": broad,
                "matched_objects": objects,
                # заполняется на шаге 2; None -- ещё не размечено
                "label_objects": None,
                "label_source": None,
            }
        )
    return docs


def report(docs: list[dict[str, Any]]) -> None:
    total = len(docs)
    print(f"\n=== Пул кандидатов: {total} документов ===\n")
    if not total:
        print("Пусто. Проверь, что корпус на месте и регекс не сломан.")
        return

    lengths = [d["text_len"] for d in docs]
    print("Длина текста (символов):")
    print(f"  медиана {percentile(lengths, 0.5)}  p90 {percentile(lengths, 0.9)}"
          f"  p95 {percentile(lengths, 0.95)}  max {max(lengths)}")

    only_broad = sum(1 for d in docs if not d["matched_objects"])
    print(f"\nПоймано только широким неводом (ни один объект не сработал): {only_broad}"
          f" ({only_broad * 100 // total}%)")
    print("Это и есть та часть, на которой ключевые слова объектов можно поймать")
    print("на пропусках -- ради неё пул и набирался шире каталога.")

    print("\nПо объектам (сработал регекс каталога):")
    obj_counter: Counter[int] = Counter()
    for d in docs:
        obj_counter.update(d["matched_objects"])
    by_id = {p.object_id: p for p in OBJECT_PATTERNS}
    for object_id, pattern in sorted(by_id.items()):
        mark = " (approx)" if pattern.approx else ""
        print(f"  {object_id:>2}  {pattern.label[:44]:<44} {obj_counter.get(object_id, 0):>5}{mark}")

    print("\nПо терминам широкого невода:")
    broad_counter: Counter[str] = Counter()
    for d in docs:
        broad_counter.update(d["matched_broad"])
    labels = {p.key: p.label for p in BROAD_NET}
    for key, n in broad_counter.most_common():
        print(f"  {labels.get(key, key):<28} {n:>5}")

    print("\nПо датам:")
    for day, n in sorted(Counter((d["time_post"] or "")[:10] for d in docs).items()):
        print(f"  {day}  {n:>5}")

    print("\nТоп источников:")
    for host, n in Counter(d["host"] for d in docs).most_common(15):
        print(f"  {host:<24} {n:>5}")

    with_companies = sum(1 for d in docs if d["companies_raw"] not in (None, "", "null"))
    print(f"\nС вендорским полем companies: {with_companies} из {total}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Выгрузка пула кандидатов на разметку")
    ap.add_argument("--out", type=Path, default=None,
                    help="куда писать JSONL; без него -- только статистика")
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV_PATH,
                    help=f"откуда читать DSN (по умолчанию {DEFAULT_ENV_PATH})")
    ap.add_argument("--dsn-var", default="AGENT_1_DB_DSN")
    ap.add_argument("--max-chars", type=int, default=6000,
                    help="обрезка текста в выгрузке, чтобы файл не распухал")
    ap.add_argument("--limit", type=int, default=None,
                    help="взять только первые N документов (для прогона на пробу)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(args.env_file)

    dsn = os.environ.get(args.dsn_var)
    if not dsn:
        print(f"ERROR: {args.dsn_var} не найден (искал в {args.env_file})", file=sys.stderr)
        return 1

    started = datetime.now(timezone.utc)
    rows = fetch_rows(dsn, args.limit)
    docs = build(rows, args.max_chars)
    report(docs)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8") as handle:
            for doc in docs:
                handle.write(json.dumps(doc, ensure_ascii=False) + "\n")
        size_mb = args.out.stat().st_size / 1024 / 1024
        print(f"\nЗаписано: {args.out} ({len(docs)} строк, {size_mb:.1f} МБ)")
    else:
        print("\n--out не задан, файл не писался (только статистика).")

    print(f"Время: {(datetime.now(timezone.utc) - started).total_seconds():.1f} c")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())