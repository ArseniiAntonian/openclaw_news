#!/usr/bin/env python3
"""Разбор эталона: где расходятся метки модели и регексы каталога.

Читает `data/label_queue.jsonl` (там лежит `matched_objects` -- что поймали
регексы) и `data/labels.jsonl` (метки модели). Ни модель, ни БД не трогает,
считается мгновенно.

Зачем. Сводная цифра «регекс не поймал N% положительных» смешивает два
разных явления, и решения из неё делать нельзя:

- **регекс узкий** -- документ по делу, ключевые слова его не ловят. Для
  объектов 2-10 это может быть дефект *нашей реконструкции*, а не методики
  банка: оригинальные паттерны в документации обрезаны, и я восстанавливал
  их по алиасам. Такие пропуски -- артефакт, а не результат.
- **модель перелейбелила** -- документ отмечен объектом, к которому
  относится натянуто. Тогда «пропуск» вымышленный.

Разложение по объектам показывает, какое из двух доминирует: у объекта с
дословно известным регексом (только объект 1) расхождение читается как
ошибка модели, у остальных -- как сумма двух эффектов.

    python analyze_labels.py --queue data/label_queue.jsonl --labels data/labels.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from patterns import OBJECT_PATTERNS  # noqa: E402


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Разбор расхождений меток и регексов")
    ap.add_argument("--queue", type=Path, required=True)
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--sample", type=int, default=8, help="сколько заголовков показать в примерах")
    args = ap.parse_args(argv)

    for path in (args.queue, args.labels):
        if not path.is_file():
            print(f"ERROR: не найден {path}", file=sys.stderr)
            return 1

    queue = {d["id_clean_post"]: d for d in read_jsonl(args.queue)}
    labels = {d["id_clean_post"]: d for d in read_jsonl(args.labels)}
    print(f"Очередь: {len(queue)}   Размечено: {len(labels)}\n")

    print(f"{'об':>3}  {'объект':<40} {'оба':>5} {'только':>7} {'только':>7}")
    print(f"{'':>3}  {'':<40} {'':>5} {'модель':>7} {'регекс':>7}")

    for pattern in OBJECT_PATTERNS:
        oid = pattern.object_id
        both = model_only = regex_only = 0
        model_only_ids: list[int] = []
        regex_only_ids: list[int] = []

        for doc_id, doc in queue.items():
            label_row = labels.get(doc_id)
            if label_row is None:
                continue  # ещё не размечено
            by_model = oid in (label_row.get("label_objects") or [])
            by_regex = oid in (doc.get("matched_objects") or [])
            if by_model and by_regex:
                both += 1
            elif by_model:
                model_only += 1
                model_only_ids.append(doc_id)
            elif by_regex:
                regex_only += 1
                regex_only_ids.append(doc_id)

        mark = "" if not pattern.approx else " *"
        print(f"{oid:>3}  {pattern.label[:40]:<40} {both:>5} {model_only:>7} {regex_only:>7}{mark}")

        if oid == 1 and (model_only or regex_only):
            print("       ^ регекс объекта 1 известен дословно: расхождение здесь")
            print("         читается как ошибка модели, а не реконструкции")

    print("\n* -- регекс реконструирован по алиасам, оригинал в документации обрезан.")
    print("«только модель» у таких объектов = узость нашей реконструкции + перелейблинг модели,")
    print("развести их без настоящих паттернов банка нельзя.")
    print("«только регекс» = документ поймали ключевые слова, а модель не подтвердила:")
    print("это ложные срабатывания ключевых слов (или пропуск модели).")

    # Примеры для глаз: без них таблица не интерпретируется.
    worst = max(OBJECT_PATTERNS, key=lambda p: sum(
        1 for doc_id, doc in queue.items()
        if doc_id in labels
        and p.object_id in (labels[doc_id].get("label_objects") or [])
        and p.object_id not in (doc.get("matched_objects") or [])
    ))
    ids = [
        doc_id for doc_id, doc in queue.items()
        if doc_id in labels
        and worst.object_id in (labels[doc_id].get("label_objects") or [])
        and worst.object_id not in (doc.get("matched_objects") or [])
    ]
    if ids:
        print(f"\nПримеры «только модель» по объекту {worst.object_id} ({worst.label}) — "
              f"{min(args.sample, len(ids))} из {len(ids)}:")
        for doc_id in ids[: args.sample]:
            print(f"  {(queue[doc_id].get('title') or '')[:96]}")
        print("Если заголовки по делу — регекс правда узкий. Если натянуты — перелейблинг.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())