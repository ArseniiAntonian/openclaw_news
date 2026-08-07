"""Общие DB-хелперы агента 2.

Перенесено из `experiments/retrieval_eval/run_eval.py` и
`filter_agent.py` (tasks.md 4.2) без изменения логики: то, что там уже
проверено на граблях (ef_search через `SET`, не `set_config`), не
переизобретается.
"""

from __future__ import annotations

import os
from pathlib import Path

SCHEMA = "agent_1_v5"


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


def to_postgres(pattern: str) -> str:
    """Питоновский регекс -> диалект Postgres.

    Единственное расхождение, которое нас касается: `\\b`. В питоне это
    граница слова, в Postgres -- backspace (граница слова там `\\y`).
    """
    return pattern.replace(r"\b", r"\y")


def set_ef_search(conn, ef_search: int) -> None:
    """Расширить список кандидатов HNSW на время сессии.

    Грабля брифа: `SET hnsw.ef_search = %s` не работает с параметром --
    `SET` принимает только литерал и падает с
    `syntax error at or near "$1"`. Через `set_config`, значение читается
    обратно и сверяется -- тихо не применившийся ef_search испортит
    результат отбора, ничем себя не выдав.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('hnsw.ef_search', %s, false)", (str(ef_search),))
        cur.execute("SHOW hnsw.ef_search")
        actual = cur.fetchone()[0]
    if int(actual) != ef_search:
        raise RuntimeError(
            f"hnsw.ef_search не применился: запрошено {ef_search}, в сессии {actual}"
        )