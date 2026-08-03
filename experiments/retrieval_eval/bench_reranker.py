#!/usr/bin/env python3
"""Замер скорости кросс-энкодера на этом конкретном железе.

Я дважды оценил её на глаз и дважды ошибся: сперва «около секунды на пару»
(вышло слишком пессимистично), потом «пять-десять пар в секунду» (вышло
оптимистично в тридцать раз -- реально 5.7 секунды на пару). Поэтому здесь
не оценка, а измерение.

Что проверяется и почему именно это:

- **число потоков torch.** По умолчанию torch на сервере часто берёт одно
  ядро. Если ядер восемь, а занято одно, разница восьмикратная -- и это
  самая вероятная причина увиденной медлительности.
- **длина входа.** Внимание в трансформере растёт быстрее линейного по
  длине, поэтому 256 токенов вместо 512 дают больше, чем двукратное
  ускорение. Медиана документа в пуле -- 1234 знака, это около 400 токенов,
  так что 256 обрежет половину документов, но заголовок и лид останутся,
  а релевантность обычно видна именно там.
- **размер модели.** bge-reranker-v2-m3 -- 568M параметров. Вдвое меньшая
  многоязычная модель считается примерно вдвое быстрее.

Скрипт ничего не пишет и никуда не ходит, кроме скачивания весов.

    python bench_reranker.py
    python bench_reranker.py --models BAAI/bge-reranker-v2-m3 --lengths 512 256 128
"""

from __future__ import annotations

import argparse
import os
import time

QUERY = ("Инциденты и безопасность GenAI. Новости о происшествиях вокруг "
         "генеративного ИИ: утечки данных, галлюцинации моделей, дипфейки и "
         "мошенничество с использованием нейросетей, сбои и блокировки.")

DOC = ("Утечка данных через ИИ-сервис затронула тысячи пользователей. "
       "Злоумышленники использовали дипфейк для обмана сотрудников и получили "
       "доступ к внутренним системам компании. Специалисты по информационной "
       "безопасности сообщают о росте числа атак с применением нейросетей. "
       "Работа отдельных моделей была временно ограничена до выяснения "
       "обстоятельств. Представители компании заявили, что расследование "
       "продолжается и о результатах будет сообщено дополнительно. ") * 4


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Скорость кросс-энкодера на этой машине")
    ap.add_argument("--models", nargs="+", default=["BAAI/bge-reranker-v2-m3"])
    ap.add_argument("--lengths", type=int, nargs="+", default=[512, 256, 128])
    ap.add_argument("--pairs", type=int, default=16, help="пар в одном замере")
    ap.add_argument("--threads", type=int, default=None,
                    help="потоков torch; по умолчанию все ядра")
    args = ap.parse_args(argv)

    import torch
    from sentence_transformers import CrossEncoder

    cores = os.cpu_count() or 1
    threads = args.threads or cores
    torch.set_num_threads(threads)
    print(f"Ядер: {cores}   потоков torch: {torch.get_num_threads()}")
    print(f"Пар в замере: {args.pairs}\n")

    print(f"{'модель':<34} {'длина':>6} {'сек/пара':>10} {'пар/сек':>9} "
          f"{'200 пар':>9} {'8 объектов':>11}")
    print("-" * 84)

    for model_name in args.models:
        for length in args.lengths:
            try:
                model = CrossEncoder(model_name, device="cpu", max_length=length)
                pairs = [[QUERY, DOC] for _ in range(args.pairs)]
                model.predict(pairs[:2], show_progress_bar=False)  # прогрев
                started = time.time()
                model.predict(pairs, show_progress_bar=False)
                elapsed = time.time() - started
            except Exception as exc:  # noqa: BLE001
                print(f"{model_name[:34]:<34} {length:>6}   ОШИБКА: {exc}")
                continue

            per_pair = elapsed / args.pairs
            per_200 = per_pair * 200
            per_run = per_200 * 8
            print(f"{model_name[:34]:<34} {length:>6} {per_pair:>10.2f} "
                  f"{1/per_pair:>9.1f} {per_200/60:>8.1f}м {per_run/60:>10.1f}м")

    print("\nПорог применимости: полный прогон -- это 8 объектов по 200 пар.")
    print("Дольше 15 минут на прогон делает эксперимент неудобным, дольше часа --")
    print("бессмысленным, потому что в продакшне это повторяется регулярно.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())