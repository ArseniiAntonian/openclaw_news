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

from patterns import CONTROL_PATTERNS, OBJECT_PATTERNS  # noqa: E402


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Разбор расхождений меток и регексов")
    ap.add_argument("--queue", type=Path, required=True)
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--sample", type=int, default=8, help="сколько заголовков показать в примерах")
    ap.add_argument("--object", type=int, default=None,
                    help="показать заголовки всех трёх категорий по одному объекту")
    ap.add_argument("--strict", action="store_true",
                    help="считать за метку только «событие». По умолчанию учитываются "
                         "и упоминания -- именно их ловят ключевые слова каталога")
    args = ap.parse_args(argv)

    for path in (args.queue, args.labels):
        if not path.is_file():
            print(f"ERROR: не найден {path}", file=sys.stderr)
            return 1

    queue = {d["id_clean_post"]: d for d in read_jsonl(args.queue)}
    labels = {d["id_clean_post"]: d for d in read_jsonl(args.labels)}
    print(f"Очередь: {len(queue)}   Размечено: {len(labels)}\n")

    # Контроль качества меток. Считается здесь, а не только внутри
    # разметчика, потому что состав CONTROL_PATTERNS может пополниться уже
    # после того, как очередь собрана и разметка проведена: пересобрать
    # очередь и пересчитать контроль стоит секунды, а повторная разметка --
    # часы. Соединение идёт по id_clean_post, метки при этом не трогаются.
    print("Контроль по однозначным брендовым подстрокам:")
    total = agreed = 0
    for ctl in CONTROL_PATTERNS:
        oid = ctl.object_id
        docs = [i for i, d in queue.items()
                if oid in (d.get("control_objects") or []) and i in labels]
        if not docs:
            print(f"  {ctl.label:<18} документов нет "
                  f"(очередь собрана без этого паттерна — пересобери triage_pool)")
            continue
        hit = sum(
            1 for i in docs
            if oid in set(labels[i].get("label_objects") or [])
            | set(labels[i].get("mention_objects") or [])
        )
        strict = sum(1 for i in docs if oid in (labels[i].get("label_objects") or []))
        total += len(docs)
        agreed += hit
        print(f"  {ctl.label:<18} найдено {len(docs):>4}   отмечено {hit:>4}"
              f"  ({hit * 100 // len(docs):>3}%)   из них событием {strict:>4}")
    if total:
        print(f"  ИТОГО              найдено {total:>4}   отмечено {agreed:>4}"
              f"  ({agreed * 100 // total:>3}%)")
        print("  ниже ~80% => меткам верить нельзя\n")
    else:
        print()

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
            # Регекс каталога ловит упоминания, поэтому сравнивать его надо
            # с суммой «событие + упоминание», иначе расхождение считается
            # там, где методы просто отвечают на разные вопросы.
            by_model = oid in (label_row.get("label_objects") or []) or (
                not args.strict and oid in (label_row.get("mention_objects") or [])
            )
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
            print("       ^ регекс объекта 1 известен дословно, но НЕ точен: альтернатива")
            print("         «кандинск» ловит художника наравне с моделью Сбера, поэтому")
            print("         «только регекс» здесь -- в том числе ложные срабатывания")

    print("\n* -- регекс реконструирован по алиасам, оригинал в документации обрезан.")
    print("«только модель» у таких объектов = узость нашей реконструкции + перелейблинг модели,")
    print("развести их без настоящих паттернов банка нельзя.")
    print("«только регекс» = документ поймали ключевые слова, а модель не подтвердила:")
    print("это ложные срабатывания ключевых слов (или пропуск модели).")

    # Разбор одного объекта целиком: три категории с заголовками. Нужен,
    # когда контроль показал расхождение и надо понять, кто именно не прав --
    # регекс, модель или постановка вопроса в промпте разметки.
    if args.object is not None:
        target = next((p for p in OBJECT_PATTERNS if p.object_id == args.object), None)
        if target is None:
            print(f"ERROR: объекта {args.object} нет в каталоге", file=sys.stderr)
            return 1
        groups: dict[str, list[int]] = {"оба": [], "только модель": [], "только регекс": []}
        for doc_id, doc in queue.items():
            row = labels.get(doc_id)
            if row is None:
                continue
            by_model = args.object in (row.get("label_objects") or []) or (
                not args.strict and args.object in (row.get("mention_objects") or [])
            )
            by_regex = args.object in (doc.get("matched_objects") or [])
            if by_model and by_regex:
                groups["оба"].append(doc_id)
            elif by_model:
                groups["только модель"].append(doc_id)
            elif by_regex:
                groups["только регекс"].append(doc_id)

        print(f"\n=== Объект {args.object}: {target.label} ===")
        for name, ids in groups.items():
            print(f"\n--- {name}: {len(ids)}")
            if name == "только регекс" and ids:
                print("    (ключевые слова сработали, модель не подтвердила —")
                print("     смотри, правда ли эти новости про объект)")
            for doc_id in ids[: args.sample]:
                conf = (labels[doc_id].get("confidence") or "?")[:4]
                print(f"    [{conf:>4}] {(queue[doc_id].get('title') or '')[:88]}")
        return 0

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