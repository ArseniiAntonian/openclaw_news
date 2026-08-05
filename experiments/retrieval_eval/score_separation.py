#!/usr/bin/env python3
"""Разделяет ли оценка реранкера релевантное от нерелевантного.

Реранкер мы мерили как пересортировщик: вырос ли recall@K. Это не тот
вопрос. Для фильтра важно другое -- даёт ли его оценка **границу**, то есть
можно ли по ней принять решение «брать / не брать».

Почему есть основания думать, что даёт. Косинус -- непрерывная мера
близости без естественного нуля, поэтому оценки убывают плавно. На наших
данных это подтвердилось: детектор обрыва в `filter_agent.py` срабатывал в
диапазоне 20-43 при защитном минимуме 20, то есть излома в распределении
нет вовсе. Кросс-энкодер обучен на бинарной разметке «релевантно или нет»,
и его выход обычно двумодальный: два скопления и провал между ними. Это и
есть граница.

Скрипт ничего не считает заново -- берёт оценки из кэша реранкера и метки
из эталона. Ни модели, ни сети, ни БД.

Что печатает:

- медианы оценок отдельно для положительных и отрицательных документов и
  насколько распределения перекрываются;
- перебор порога: какая полнота и точность получаются при каждом значении;
- **один ли порог годится для всех объектов** -- это главное. Если лучший
  порог у каждого объекта свой, то калибровать придётся на каждый, и как
  универсальное правило оценка не годится.

Отрицательными считаются только РАЗМЕЧЕННЫЕ документы, не отнесённые к
объекту. Неразмеченные исключаются: их релевантность неизвестна, и
записывать их в мусор значило бы выдумывать.

    python score_separation.py --cache data/rerank_cache.jsonl \\
        --labels data/labels_v2.jsonl
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def load_labels(path: Path, relation: str) -> tuple[dict[int, set[int]], set[int]]:
    truth: dict[int, set[int]] = defaultdict(set)
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
            truth[int(object_id)].add(doc_id)
    return truth, labelled


def load_cache(path: Path) -> dict[str, dict[int, dict[int, float]]]:
    """Ключ кэша -- 'объект:документ:модель:метка'. Группируем по модели."""
    out: dict[str, dict[int, dict[int, float]]] = defaultdict(lambda: defaultdict(dict))
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        parts = row["key"].split(":")
        if len(parts) < 3:
            continue
        object_id, doc_id = int(parts[0]), int(parts[1])
        model = ":".join(parts[2:])
        out[model][object_id][doc_id] = float(row["score"])
    return out


def median(values: list[float]) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Разделяющая способность оценок реранкера")
    ap.add_argument("--cache", type=Path, default=Path("data/rerank_cache.jsonl"))
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--relation", choices=("event", "any"), default="event")
    ap.add_argument("--steps", type=int, default=20, help="сколько порогов перебрать")
    args = ap.parse_args(argv)

    truth, labelled = load_labels(args.labels, args.relation)
    cache = load_cache(args.cache)

    for model, per_object in sorted(cache.items()):
        usable = {o: s for o, s in per_object.items() if o in truth and len(s) >= 20}
        if not usable:
            continue
        print(f"\n{'=' * 72}")
        print(f"Модель: {model}   объектов с оценками: {len(usable)}")
        print("=" * 72)

        print(f"\n{'об':>3} {'полож':>6} {'отриц':>6} | {'медиана+':>9} {'медиана-':>9}"
              f" {'разрыв':>8} | {'перекрытие':>11}")
        for object_id, scores in sorted(usable.items()):
            pos = [s for d, s in scores.items() if d in truth[object_id]]
            neg = [s for d, s in scores.items()
                   if d in labelled and d not in truth[object_id]]
            if not pos or not neg:
                continue
            m_pos, m_neg = median(pos), median(neg)
            # Доля отрицательных, чья оценка выше медианы положительных.
            # Ноль означает чистое разделение, половина -- что оценка
            # бесполезна как признак.
            overlap = sum(1 for s in neg if s >= m_pos) / len(neg)
            print(f"{object_id:>3} {len(pos):>6} {len(neg):>6} | {m_pos:>9.3f} "
                  f"{m_neg:>9.3f} {m_pos - m_neg:>8.3f} | {overlap:>10.1%}")

        # --- один ли порог годится на всех ---
        all_scores = [s for scores in usable.values() for s in scores.values()]
        if not all_scores:
            continue
        lo, hi = min(all_scores), max(all_scores)
        print(f"\nЕдиный порог на все объекты (диапазон оценок {lo:.3f} … {hi:.3f}):")
        print(f"{'порог':>8} {'полнота':>9} {'точность':>9} {'отобрано':>9}")
        best = None
        for i in range(args.steps + 1):
            threshold = lo + (hi - lo) * i / args.steps
            rec_sum = prec_sum = sel_sum = 0.0
            n = 0
            for object_id, scores in usable.items():
                positives = truth[object_id]
                selected = {d for d, s in scores.items() if s >= threshold}
                known = selected & labelled
                found = selected & positives
                # Полнота считается от положительных СРЕДИ ОЦЕНЁННЫХ:
                # документы, которых реранкер не видел, к его решению
                # отношения не имеют.
                reachable = set(scores) & positives
                if not reachable:
                    continue
                rec_sum += len(found) / len(reachable)
                prec_sum += (len(found) / len(known)) if known else 0.0
                sel_sum += len(selected)
                n += 1
            if not n:
                continue
            rec, prec, sel = rec_sum / n, prec_sum / n, sel_sum / n
            print(f"{threshold:>8.3f} {rec:>9.1%} {prec:>9.1%} {sel:>9.0f}")
            if rec >= 0.70 and (best is None or prec > best[2]):
                best = (threshold, rec, prec, sel)

        if best:
            print(f"\nЛучший единый порог при полноте не ниже 70%: {best[0]:.3f} "
                  f"-> полнота {best[1]:.1%}, точность {best[2]:.1%}, "
                  f"в среднем {best[3]:.0f} документов")
        else:
            print("\nЕдиного порога с полнотой 70% не нашлось.")

        # --- у каждого объекта свой порог ---
        print("\nЛучший порог ОТДЕЛЬНО для каждого объекта (полнота >= 70%):")
        print(f"{'об':>3} {'порог':>8} {'полнота':>9} {'точность':>9}")
        per_obj_thresholds = []
        for object_id, scores in sorted(usable.items()):
            positives = truth[object_id]
            reachable = set(scores) & positives
            if not reachable:
                continue
            found_best = None
            values = sorted({s for s in scores.values()})
            for threshold in values:
                selected = {d for d, s in scores.items() if s >= threshold}
                known = selected & labelled
                rec = len(selected & positives) / len(reachable)
                prec = (len(selected & positives) / len(known)) if known else 0.0
                if rec >= 0.70 and (found_best is None or prec > found_best[2]):
                    found_best = (threshold, rec, prec)
            if found_best:
                per_obj_thresholds.append(found_best[0])
                print(f"{object_id:>3} {found_best[0]:>8.3f} {found_best[1]:>9.1%} "
                      f"{found_best[2]:>9.1%}")

        if len(per_obj_thresholds) > 1:
            spread = max(per_obj_thresholds) - min(per_obj_thresholds)
            print(f"\nРазброс порогов между объектами: {spread:.3f} "
                  f"({min(per_obj_thresholds):.3f} … {max(per_obj_thresholds):.3f})")
            print("Чем он больше, тем меньше смысла в едином пороге: калибровать")
            print("придётся каждый объект отдельно, а это уже не «умный фильтр».")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())