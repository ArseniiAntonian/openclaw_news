#!/usr/bin/env python3
import json
import os
from datetime import datetime
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parent
ENV_FILE = Path("/root/.openclaw/workspace/agents/agent_1/.env")
STATE_FILE = ROOT / "filter_alert_state.json"
DONE_FILE = ROOT / "filter_alert.done"


def load_dsn() -> str:
    dsn = os.environ.get("AGENT_1_DB_DSN")
    if dsn:
        return dsn
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("AGENT_1_DB_DSN="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("AGENT_1_DB_DSN is not set")


def main() -> int:
    if DONE_FILE.exists():
        return 0

    with psycopg.connect(load_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM demo.clean_items")
            total_docs = cur.fetchone()[0]
            cur.execute("SELECT count(DISTINCT clean_item_id) FROM demo.doc_labels")
            processed_docs = cur.fetchone()[0]
            cur.execute(
                """
                SELECT count(*) FILTER (WHERE relevance) AS relevant
                FROM demo.doc_labels
                """
            )
            relevant_total = cur.fetchone()[0] or 0
            cur.execute(
                """
                SELECT k.id, left(k.text, 30) AS goal,
                       count(*) FILTER (WHERE d.relevance) AS relevant
                FROM demo.kr k
                LEFT JOIN demo.doc_labels d ON d.kr_id = k.id
                GROUP BY k.id, k.text
                ORDER BY k.id
                """
            )
            goals = cur.fetchall()

    previous = None
    if STATE_FILE.exists():
        previous = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("relevant_total")

    STATE_FILE.write_text(
        json.dumps({"relevant_total": relevant_total}, ensure_ascii=False),
        encoding="utf-8",
    )

    now = datetime.now().strftime("%H:%M")
    if previous is not None and relevant_total - previous == 0:
        DONE_FILE.write_text("done\n", encoding="utf-8")
        print(f"[ALERT {now}] фильтр готов")
        return 0

    goals_text = ", ".join(f"«{goal}»={relevant or 0}" for _, goal, relevant in goals)
    print(
        f"[ALERT {now}] фильтр {processed_docs}/{total_docs} | релевантных всего {relevant_total}\n"
        f"по целям: {goals_text}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
