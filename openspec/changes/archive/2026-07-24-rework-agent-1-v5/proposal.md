## Why

Система переходит на архитектуру пайплайна v5 с шестью агентами
(`docs/architecture/pipeline_v5_context.md`): монолитная разметка Агента 1
разбирается по специализированным агентам, оркестрируемым Агентом 6.
Зона ответственности Агента 1 сужается до **сбор → очистка → дедуп →
эмбеддинги**; вся LLM-разметка из него уходит (сущности — в Агент 4 /
GLiNER + драйверный слой, оценка релевантности — на этап после
кластеризации, обогащение КР — в Агент 2).

Вторая причина — производительность. Текущая скорость препроцессинга
~10 400 raw/час (≈345 мс/док) при измеренной стоимости реальных вычислений
~22 мс/док (бенчмарк MinHash 128 перм + шинглы word-5/char-17 на 4000 симв,
1 ядро): ~94% времени — per-document round-trips в БД, не вычисления.
Разрыв 16x до параллелизма, ~60x с векторизацией и несколькими ядрами.

Полный рабочий документ: `docs/architecture/agent1_rework_proposal.md`.

## What Changes

- Новая схема хранения Агента 1 по ERD v5 (`docs/architecture/schema_v5.dot`):
  `source` → `raw_posts` → `clean_posts` (Postgres + pgvector, LZ4-компрессия
  контента, композитный PK с `time_post` как задел под партиционирование).
- Эмбеддинги переезжают в Агент 1: `text-embedding-3-small`,
  `dimensions=1024`, колонка `clean_posts.embedding vector(1024)`,
  HNSW-индекс только на ней.
- **BREAKING**: удаление всех LLM-шагов из Агента 1 — воркеры
  `label_kr_worker.py`, `extract_semantics_worker.py` и
  `kr_enrichment_sync.py` выводятся из состава Агента 1; таблицы
  `document_enrichments` / `document_kr_labels` Агентом 1 больше не
  наполняются.
- Производительность препроцессинга: батчевый claim
  (`FOR UPDATE SKIP LOCKED LIMIT 100`, одна транзакция на пачку),
  in-memory LSH, векторизованный MinHash (numpy), multiprocessing.
  Цель: ≤25 мс/док на ядро, ≥400k raw/час на 4 ядрах.
- Junk-regex категории выносятся из кода в таблицу БД (пополнение без
  деплоя).
- Отбраковка фиксируется строкой в `clean_posts` со статусом/`drop_reason`
  (инвариант: clean_posts = вердикт очистки по каждому raw).
- Наблюдаемость: агрегируемая статистика качества по источникам (задел под
  agent_memory).

## Non-Goals

- Кластеризация (Агент 3), GLiNER-разметка и драйверный слой (Агент 4),
  обогащение КР (Агент 2), отчёты (Агент 5), оркестрация (Агент 6) —
  отдельные changes.
- Дистилляция/локальный инференс LLM.
- Партиционирование по `time_post` — PK проектируется под него, включение
  отдельным change.
- agent_memory — только сбор статистики, без таблицы памяти.
- Перекалибровка порогов LSH/levenshtein — переносятся как есть, иначе
  критерий эквивалентности с текущим пайплайном теряет смысл.

## Capabilities

### New Capabilities
- `agent_1-embeddings`: генерация эмбеддингов для чистых недубликатов
  (`text-embedding-3-small`, dims=1024, батчи, идемпотентность, HNSW после
  bulk-загрузки).
- `agent_1-source-stats`: агрегируемая статистика качества по источникам
  (`total_raw, pct_junk, pct_non_russian, pct_duplicates, avg_content_len,
  last_seen_at`) — задел под agent_memory.

### Modified Capabilities
- `agent_1`: umbrella — новая зона ответственности (сбор → очистка → дедуп
  → эмбеддинги), место в 6-агентном пайплайне v5, LLM-этапы исключаются из
  карты пайплайна.
- `agent_1-ingestion`: новая схема `source` / `raw_posts` (атомарные поля,
  `url` = пермалинк новости, LZ4, инвариант неприкосновенности raw,
  композитный PK с `time_post`).
- `agent_1-preprocessing`: запись в `clean_posts` вместо `clean_items`,
  junk-категории из таблицы БД, отбраковка с `drop_reason` в `clean_posts`,
  in-memory LSH + векторизованный MinHash, батчевый claim, целевые числа
  производительности.

### Removed Capabilities
- `agent_1-kr-labeling`: уходит из Агента 1 — релевантность/impact/tonality
  выполняются после кластеризации (Агент 4; там же чинится баг с per-KR
  вызовами document-level шагов — кэширование по документу).
- `agent_1-semantic-enrichment`: уходит из Агента 1 — извлечение сущностей
  и событий выполняет Агент 4 (GLiNER-слоты + отдельный шаг связывания).

## Impact

- Код: `agents/agent_1/src/agent_1/` — `preprocess_worker.py`
  (переписывается на батчи/numpy), `parsers360_ingest.py` (новая целевая
  схема), новый embedding-воркер; `label_kr_worker.py`,
  `extract_semantics_worker.py`, `label_prompts.py`,
  `kr_enrichment_sync.py` — выводятся из Агента 1 (код сохраняется до
  создания Агентов 2/4, но из очереди Агента 1 исключается).
- БД: новые таблицы `source`, `raw_posts`, `clean_posts` (+ таблица
  junk-категорий, `CREATE EXTENSION vector`); миграция данных из текущих
  `agent_1.raw_items` / `clean_items`.
- Тесты: `tests/test_preprocess_worker.py` адаптируется; тесты LLM-воркеров
  замораживаются вместе с кодом.
- Внешние зависимости: API эмбеддингов (text-embedding-3-small), numpy,
  pgvector.
- Downstream: Агент 3 читает `clean_posts.embedding` и пишет
  `clean_posts.ID_cluster` (сознательное отступление от стадийности,
  фиксируется в спеке preprocessing как внешняя запись).
