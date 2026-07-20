# Инструкция: перенос данных agent_1 -> agent_1_v5

Адресат: OpenClaw-агент на проде. Продолжение `MIGRATION.md` (схема уже
накачена 2026-07-20). Задача 1.3 в
`openspec/changes/rework-agent-1-v5/tasks.md`.

Контекст: скрипт `agents/agent_1/src/agent_1/migrate_v5.py` переносит
`agent_1.raw_items`/`clean_items` в `agent_1_v5.raw_posts`/`clean_posts`.
Полная логика и все решения по неоднозначным местам (откуда берётся
`source`, что делать со старыми дубликатами, как считается `content_hash`
и т.д.) описаны в docstring самого файла — читать его, если нужно понять
"почему так", а не только "что запустить".

Читает только схему `agent_1` (ничего в ней не меняет), пишет только в
`agent_1_v5`.

## 0. Обновить репозиторий

```bash
cd /root/.openclaw/workspace
git pull
cd agents/agent_1
```

## 1. Накатить недостающую DDL-правку

Между накаткой схемы (000-002) и этим скриптом появился ещё один файл —
`source.name_source` должен быть уникальным, чтобы скрипт мог делать
идемпотентный upsert источников:

```bash
AGENT_1_DB_DSN=$(grep -m1 '^AGENT_1_DB_DSN=' .env | cut -d= -f2-)
psql "$AGENT_1_DB_DSN" -v ON_ERROR_STOP=1 -f db/v5/003_source_name_unique.sql
```

Если 003 уже накачен (например, ошибка "constraint already exists") —
это не проблема, значит шаг уже выполнялся, просто едем дальше.

## 2. Поставить зависимости скрипта (если ещё не стоят)

```bash
. .venv/bin/activate
pip install -r requirements.txt
```

## 3. Пробный прогон — `--dry-run`, сначала на небольшом куске

Ничего не коммитит (весь прогон в одной транзакции, откатывается в конце —
это гарантия, не текст в описании; можно перепроверить `SELECT count(*)
FROM agent_1_v5.raw_posts` до и после, числа не должны отличаться).

```bash
PYTHONPATH=src python -m agent_1.migrate_v5 --dry-run --limit 500
```

Прочитать отчёт в конце вывода. На что смотреть:

- `content_hash collisions among 'kept' rows` — если не 0, это значит
  старый exact-дедуп когда-то пропустил дубликат; пришлите список id, не
  доезжайте до реального прогона молча.
- `anomalous preprocess.status values` — если не 0, встретился статус,
  которого скрипт не ожидал; пришлите список, разберёмся, что это, прежде
  чем запускать на всём корпусе.
- `duplicates orphaned` — ожидаемо может быть >0 в единичных случаях (когда
  канонический документ дубликата сам не прошёл по `url`/`published_at`),
  но если число заметное — тоже стоит показать.
- `raw_items skipped, no url` / `no published_at` — сколько документов
  физически не могут лечь в v5-схему (там `url`/`time_post` NOT NULL).
  Ожидается немного или ноль; если много — сообщите, прежде чем продолжать.

## 4. Пробный прогон на всём корпусе (всё ещё `--dry-run`)

```bash
PYTHONPATH=src python -m agent_1.migrate_v5 --dry-run
```

Пришлите полный отчёт целиком.

## 5. Реальный прогон

Только после того, как отчёт из шага 4 согласован (нет неожиданных
anomalies/collisions, или они разобраны и приняты как есть):

```bash
PYTHONPATH=src python -m agent_1.migrate_v5
```

Коммитит батчами (`--batch-size`, по умолчанию 500), безопасно прерывать и
перезапускать — уже перенесённые `raw_items` распознаются и пропускаются
(идемпотентно), пересоздания дублей не будет.

## 6. Проверка после реального прогона

```bash
psql "$AGENT_1_DB_DSN" -c "SELECT count(*) FROM agent_1_v5.raw_posts;"
psql "$AGENT_1_DB_DSN" -c "SELECT count(*) FROM agent_1_v5.clean_posts;"
psql "$AGENT_1_DB_DSN" -c "SELECT drop_reason, is_duplicate, count(*) FROM agent_1_v5.clean_posts GROUP BY 1, 2 ORDER BY 3 DESC;"
psql "$AGENT_1_DB_DSN" -c "SELECT count(*) FROM agent_1_v5.source;"
```

Пришлите вывод и финальный отчёт скрипта (он печатается в консоль в конце
прогона) — сверю с ожиданиями и отмечу задачу 1.3 выполненной.
