# Инструкция: накатка схемы agent_1_v5 на mvp_db

Адресат: OpenClaw-агент на проде (у него уже есть `agents/agent_1/.env` с
`AGENT_1_DB_DSN`, доступ не запрашивать).

Контекст: `openspec/changes/rework-agent-1-v5/` (задачи 1.1–1.2 в `tasks.md`).
Три файла в этой папке создают **новую, изолированную** схему `agent_1_v5`
внутри `mvp_db`. Существующая схема `agent_1` (`raw_items`, `clean_items` и
всё остальное) этой миграцией не читается и не пишется — трогать её нельзя,
даже случайно (`search_path` в скриптах жёстко выставлен на `agent_1_v5`).

## 0. Перед запуском — обновить репозиторий

```bash
cd /root/.openclaw/workspace
git pull
cd agents/agent_1
```

Убедиться, что файлы на месте:

```bash
ls db/v5/
# ожидается: 000_bootstrap_schema_v5.sql 001_core_tables.sql
#             002_seed_junk_categories.sql MIGRATION.md
```

## 1. Предполётная проверка (ничего не меняет)

Подтянуть DSN из `.env`, не печатая пароль в терминал.

**Не использовать `source .env`** — на проверке (2026-07-20) это ломало
DSN: `.env` не является валидным bash-синтаксисом для `source`
(значение, вероятно, содержит символы, которые shell интерпретирует —
`load_dotenv()` в `preprocess_worker.py` неспроста делает построчный
парсинг руками, а не `source`). Вместо этого — grep+cut, значение не
проходит через shell-парсинг:

```bash
AGENT_1_DB_DSN=$(grep -m1 '^AGENT_1_DB_DSN=' .env | cut -d= -f2-)
```

Проверить версию Postgres и доступность pgvector:

```bash
psql "$AGENT_1_DB_DSN" -c "SELECT version();"
psql "$AGENT_1_DB_DSN" -c "SELECT * FROM pg_available_extensions WHERE name = 'vector';"
psql "$AGENT_1_DB_DSN" -c "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'agent_1_v5';"
```

**Стоп-условия — если любое из этого верно, миграцию не запускать, а вернуться
с результатами проверки:**

- `SELECT version()` показывает Postgres **младше 14** — `001_core_tables.sql`
  использует `ALTER COLUMN ... SET COMPRESSION lz4`, эта фича появилась в
  PG14. На более старой версии файл упадёт (транзакция атомарная, ничего не
  сломает, но накатка не пройдёт) — пришлите версию, я уберу LZ4-строки.
- `pg_available_extensions` не содержит `vector` — значит пакет pgvector не
  установлен на уровне ОС (не БД), `CREATE EXTENSION vector` физически не
  сможет сработать. Нужна установка пакета (`postgresql-<version>-pgvector`
  или сборка из исходников) до накатки — сообщите дистрибутив/версию PG.
- Схема `agent_1_v5` **уже существует** — значит миграция уже частично
  прогонялась раньше. Не перезапускать вслепую, прислать
  `\dt agent_1_v5.*` для сверки, что там есть.

Если все три проверки чистые — можно катить.

## 2. Накатка (по порядку, останавливаться на первой ошибке)

```bash
psql "$AGENT_1_DB_DSN" -v ON_ERROR_STOP=1 -f db/v5/000_bootstrap_schema_v5.sql
psql "$AGENT_1_DB_DSN" -v ON_ERROR_STOP=1 -f db/v5/001_core_tables.sql
psql "$AGENT_1_DB_DSN" -v ON_ERROR_STOP=1 -f db/v5/002_seed_junk_categories.sql
```

Каждый файл — одна транзакция (`BEGIN...COMMIT`): при ошибке внутри файла он
откатывается целиком сам, руками откатывать не нужно. `ON_ERROR_STOP=1`
просто останавливает выполнение сразу на первой ошибке, чтобы не сыпались
вторичные "current transaction is aborted" на каждую следующую строку.

Если один из файлов упал — **не перезапускать следующие**, вернуться с
текстом ошибки.

## 3. Проверка результата

После `000`:

```bash
psql "$AGENT_1_DB_DSN" -c "\dn agent_1_v5"
psql "$AGENT_1_DB_DSN" -c "\dx vector"
```

После `001`:

```bash
psql "$AGENT_1_DB_DSN" -c "\dt agent_1_v5.*"
psql "$AGENT_1_DB_DSN" -c "\d agent_1_v5.clean_posts"
```

Ожидается: таблицы `source`, `raw_posts`, `clean_posts`; в `clean_posts` —
колонка `embedding` типа `vector(1024)`, PK `(id_clean_post, time_post)`,
partial unique index на `content_hash`.

После `002`:

```bash
psql "$AGENT_1_DB_DSN" -c "SELECT category, jsonb_array_length(patterns) AS n, is_business_guard FROM agent_1_v5.junk_categories ORDER BY category;"
```

Ожидается 18 строк (17 junk-категорий + `protected_business_context`).

## 4. Что прислать обратно

Вывод всех команд из шага 3 (или шага 1, если остановились на стоп-условии).
По этому вернусь и отмечу задачи 1.1/1.2 в
`openspec/changes/rework-agent-1-v5/tasks.md` выполненными, дальше —
скрипт миграции данных из `agent_1.raw_items`/`clean_items` (задача 1.3).
