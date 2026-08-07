# Инструкция: накатка таблиц агента 2 на mvp_db (схема agent_1_v5)

Адресат: OpenClaw-агент на проде (у него есть `agents/agent_1/.env` с
`AGENT_1_DB_DSN`, доступ не запрашивать). **В этой рабочей копии
`.env` отсутствует — миграция здесь не выполнялась и не может быть
выполнена**, только подготовлена.

Контекст: `openspec/changes/rework-agent-2-filter/` (задачи 3.1, 10.1,
10.2 в `tasks.md`). Четыре файла в этой папке добавляют три новые
таблицы в СУЩЕСТВУЮЩУЮ схему `agent_1_v5` (её не создают заново —
`CREATE SCHEMA`/`CREATE EXTENSION` уже сделаны миграцией Агента 1,
`agents/agent_1/db/v5/000_bootstrap_schema_v5.sql`). Схему `agent_1`
(старую) эта миграция не трогает.

## 0. Перед запуском

```bash
cd /root/.openclaw/workspace
git pull
cd agents/agent_2
ls db/
# ожидается: 001_observation_objects.sql 002_seed_observation_objects.sql
#             003_agent_2_llm_scores.sql 004_agent_2_relevant_documents.sql
#             MIGRATION.md
```

## 1. Предполётная проверка (ничего не меняет)

```bash
AGENT_1_DB_DSN=$(grep -m1 '^AGENT_1_DB_DSN=' ../agent_1/.env | cut -d= -f2-)
psql "$AGENT_1_DB_DSN" -c "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'agent_1_v5';"
psql "$AGENT_1_DB_DSN" -c "\dt agent_1_v5.*"
```

**Стоп-условия:**

- Схемы `agent_1_v5` нет — сначала нужна миграция Агента 1
  (`agents/agent_1/db/v5/`), эта миграция от неё зависит
  (`REFERENCES observation_objects`, `REFERENCES clean_posts`).
- Таблица `agent_1_v5.observation_objects` (или любая из трёх новых)
  **уже существует** — не перезапускать вслепую, прислать
  `\d agent_1_v5.observation_objects` и т.д. для сверки.

## 2. Накатка (по порядку, останавливаться на первой ошибке)

```bash
psql "$AGENT_1_DB_DSN" -v ON_ERROR_STOP=1 -f db/001_observation_objects.sql
psql "$AGENT_1_DB_DSN" -v ON_ERROR_STOP=1 -f db/002_seed_observation_objects.sql
psql "$AGENT_1_DB_DSN" -v ON_ERROR_STOP=1 -f db/003_agent_2_llm_scores.sql
psql "$AGENT_1_DB_DSN" -v ON_ERROR_STOP=1 -f db/004_agent_2_relevant_documents.sql
```

Каждый файл — одна транзакция; при ошибке откатывается сам. Если
файл упал — не запускать следующие, вернуться с текстом ошибки.

**002 — тестовые данные, не боевой каталог.** Сидит 10 объектов из
`experiments/retrieval_eval/patterns.py`/`queries.py` (объект 1 —
оригинальный regex банка, объекты 2–10 — реконструкция, помечено
в комментарии файла). Когда банк пришлёт настоящий каталог — новая
миграция с реальными данными, не правка этой.

`query_embedding` после 002 у всех строк `NULL` — вычисляется агентом
при первом обращении к объекту (по `search_description`), не миграцией.

## 3. Проверка результата

```bash
psql "$AGENT_1_DB_DSN" -c "\d agent_1_v5.observation_objects"
psql "$AGENT_1_DB_DSN" -c "SELECT id_object, label FROM agent_1_v5.observation_objects ORDER BY id_object;"
psql "$AGENT_1_DB_DSN" -c "\d agent_1_v5.agent_2_llm_scores"
psql "$AGENT_1_DB_DSN" -c "\d agent_1_v5.agent_2_relevant_documents"
```

Ожидается: 10 строк в `observation_objects` (id 1–10), пустые
`agent_2_llm_scores`/`agent_2_relevant_documents`.

## 4. Что прислать обратно

Вывод команд из шага 3 (или шага 1, если остановились на стоп-условии).