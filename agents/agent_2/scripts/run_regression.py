#!/usr/bin/env python3
"""Регрессионный тест: полнота фильтратора на эталоне.

tasks.md 11.1-11.3. Условие приёмки (specs/agent_2-filtering/spec.md,
«Регрессионный тест на эталоне — условие приёмки»): средняя полнота по
объектам эталона не ниже 70%.

Прогоняет ПОЛНЫЙ конвейер агента 2 (`filter_agent.filter_object`, каналы
-> объединение -> LLM-оценка -> порог -> негатив-вето) по каждому объекту
эталона в режиме `--dry-run` (не пишет в agent_2_relevant_documents) и
сверяет результат с размеченными положительными.

`load_truth`/`recall` перенесены из `experiments/retrieval_eval/
run_eval.py` без изменения логики.

Требует боевого окружения (`AGENT_1_DB_DSN`, `OPENROUTER_API_KEY`,
доступный `openclaw` CLI) -- в этой рабочей копии (нет `.env`,
эталон `data/labels_v2.jsonl` не выгружен) прогнать нельзя, только
подготовлено. См. openspec/changes/rework-agent-2-filter/tasks.md,
группа 11 -- пометка о блокере.

    python scripts/run_regression.py --labels experiments/retrieval_eval/data/labels_v2.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_2 import catalog  # noqa: E402
from agent_2.db import load_dotenv, set_ef_search  # noqa: E402
from agent_2.filter_agent import DEFAULT_AGENT_ID, filter_object  # noqa: E402
from agent_2.llm_scoring import ScoringConfig, resolve_default_openclaw_cmd  # noqa: E402

ACCEPTANCE_RECALL_THRESHOLD = 0.70  # specs/agent_2-filtering/spec.md
DEFAULT_ENV = Path(__file__).resolve().parents[1] / ".env"


def load_truth(
    path: Path, relation: str = "event"
) -> dict[int, set[int]]:
    """object_id -> положительные id_clean_post.

    Перенесено из experiments/retrieval_eval/run_eval.py:load_truth без
    изменения логики (relation="event"|"any").
    """
    truth: dict[int, set[int]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        doc_id = int(row["id_clean_post"])
        ids = list(row.get("label_objects") or [])
        if relation == "any":
            ids += list(row.get("mention_objects") or [])
        for object_id in ids:
            truth.setdefault(int(object_id), set()).add(doc_id)
    return truth


def recall(found: set[int], positives: set[int]) -> float:
    return len(found & positives) / len(positives) if positives else 0.0


def main(argv: list[str] | None = None) -> int:
    # Грабля прода 2026-08-07: nohup ... > file делает stdout/stderr
    # блочно-буферизованными (не терминал) -- при внешнем убийстве
    # процесса (OOM-killer, рестарт среды) буфер не успевает сброситься
    # на диск, и лог остаётся пустым несмотря на реально сделанную
    # работу (объекты уже частично оценены и лежат в кэше). Строчная
    # буферизация не зависит от того, вспомнит ли вызывающий `-u`.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    ap = argparse.ArgumentParser(description="Регрессионный тест агента 2 на эталоне")
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--relation", choices=("event", "any"), default="event")
    ap.add_argument("--candidates", type=int, default=500)
    ap.add_argument("--openclaw-cmd", default=os.getenv("AGENT_2_OPENCLAW_CMD", resolve_default_openclaw_cmd()))
    ap.add_argument("--agent-id", default=os.getenv("AGENT_2_SCORING_AGENT_ID", DEFAULT_AGENT_ID))
    ap.add_argument("--model", default=os.getenv("AGENT_2_SCORING_MODEL"))
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    ap.add_argument("--dsn-var", default="AGENT_1_DB_DSN")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.labels.is_file():
        print(f"ERROR: эталон не найден: {args.labels}", file=sys.stderr)
        return 1

    load_dotenv(args.env_file)
    dsn = os.environ.get(args.dsn_var)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not dsn:
        print(f"ERROR: {args.dsn_var} не найден (нет .env в этом окружении?)", file=sys.stderr)
        return 1

    truth = load_truth(args.labels, args.relation)
    if not truth:
        print("ERROR: эталон пуст", file=sys.stderr)
        return 1

    scoring_config = ScoringConfig(
        openclaw_cmd=args.openclaw_cmd,
        agent_id=args.agent_id,
        model=args.model,
    )

    results: dict[str, Any] = {}
    recalls: list[float] = []

    with psycopg.connect(dsn) as conn:
        set_ef_search(conn, max(args.candidates * 2, 100))

        for object_id, positives in sorted(truth.items()):
            # Сбой на одном объекте MUST NOT останавливать обработку
            # остальных -- грабля прода 2026-08-06: без этого защитного
            # блока падение на объекте 1 обрывало весь прогон, объекты
            # 2-10 не запускались вовсе, и это никак не отражалось в
            # выводе. rollback() на случай, если исключение оставило
            # соединение в aborted-транзакции (store_score коммитит
            # после каждой оценки, так что потеря данных минимальна).
            try:
                obj = catalog.fetch_object(conn, object_id)

                # dry_run=True: регрессионный тест не должен писать в
                # agent_2_relevant_documents -- только измеряет.
                report = filter_object(
                    conn, obj,
                    candidates_depth=args.candidates,
                    scoring_config=scoring_config,
                    api_key=api_key,
                    dry_run=True,
                )
            except Exception as exc:
                conn.rollback()
                print(f"ERROR: объект {object_id} упал: {exc} -- "
                      f"пропущен явно, остальные объекты продолжаются", file=sys.stderr)
                results[str(object_id)] = {"error": str(exc)}
                continue

            selected_ids: set[int] = report["selected_ids"]

            value = recall(selected_ids, positives)
            recalls.append(value)
            results[str(object_id)] = {
                "label": obj.label,
                "positives": len(positives),
                "selected": report["selected"],
                "recall": value,
            }
            print(f"объект {object_id} ({obj.label}): recall {value:.1%} "
                  f"(положительных {len(positives)}, отобрано {report['selected']})")

    mean_recall = sum(recalls) / len(recalls) if recalls else 0.0
    print(f"\nСредняя полнота: {mean_recall:.1%} (порог приёмки "
          f"{ACCEPTANCE_RECALL_THRESHOLD:.0%})")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps({"mean_recall": mean_recall, "objects": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if mean_recall < ACCEPTANCE_RECALL_THRESHOLD:
        print("FAIL: полнота ниже порога приёмки", file=sys.stderr)
        return 1
    print("OK: полнота не ниже порога приёмки")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())