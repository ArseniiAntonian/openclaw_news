## 1. Архивация старого агента 2

- [x] 1.1 Переместить `agents/agent_2/*` (`AGENTS.md`, `HEARTBEAT.md`,
      `IDENTITY.md`, `README.md`, `SOUL.md`, `TOOLS.md`, `USER.md`,
      `DREAMS.md`) в архивное расположение, не удаляя файлы (`trash`
      запрещён здесь — это перенос, не удаление).
      → `agents/_archive/agent_2_2026-08-06/` (git mv).
- [x] 1.2 Зафиксировать в архивной копии дату и причину архивации (снята
      генерация объектов из бизнес-цели — каталог теперь приходит от
      банка). → `agents/_archive/agent_2_2026-08-06/ARCHIVE_NOTE.md`.

## 2. Идентичность нового агента 2 в OpenClaw

Старый `agents/agent_2/*` был статичным prompt-only агентом без
инструментов (`TOOLS.md`: «no tools enabled»). Новый агент 2 —
код-ориентированный (БД, OpenClaw-вызовы для LLM-оценки), ближе по духу
к `agents/agent_1/*`, чем к прежнему себе. Файлы пишутся заново, не
правкой архивной копии.

- [x] 2.1 Написать `agents/agent_2/AGENTS.md`: миссия — отбор документов
      под объект наблюдения ретривом (лексика + вектор + LLM-порог),
      жёсткие границы из брифа (`docs/architecture/
      ONBOARDING_filter_agent.md`, разделы 7–8): не трогать
      `agent_1.processing_jobs`, не запускать `parsers360_ingest` без
      явного указания, секреты не коммитить, `trash` вместо `rm`, не
      грузить CPU (тяжёлое — через API), подтверждать необратимые внешние
      действия у пользователя.
- [x] 2.2 Написать `IDENTITY.md`, `SOUL.md`, `USER.md`, `HEARTBEAT.md`,
      `DREAMS.md`, `README.md` по структуре `agents/agent_1/*` (агент
      работает с БД и инструментами, а не только генерирует текст).
      `USER.md` — обращение «Капитан», как у остальных агентов
      воркспейса. `HEARTBEAT.md`/`DREAMS.md` можно оставить пустыми
      (как у agent_1) — самонаполняются рантаймом OpenClaw.
- [x] 2.3 Написать `TOOLS.md` с уже известными граблями из брифа, чтобы
      не наступать заново: `hnsw.ef_search` только через `set_config()`
      (не `SET ... = %s`), 429 от LLM API — временная ошибка (пейсер +
      Retry-After), кэш решающей оценки обязан включать модель в ключ,
      OpenRouter резервирует кредит под `max_tokens` (HTTP 402 при
      дефолтных значениях).

## 3. Контракт объекта наблюдения

- [x] 3.1 Создать таблицу `agent_1_v5.observation_objects` (DDL):
      `id_object, label, aliases text[], keywords text, negative_filter
      text, source_weights jsonb, search_description text,
      query_embedding vector(1024), created_at, updated_at`.
      → `agents/agent_2/db/001_observation_objects.sql` +
      `002_seed_observation_objects.sql` (тестовые данные). **Файлы
      написаны, НЕ применены** — в этом окружении нет `.env`/доступа к
      БД; накатка — по `agents/agent_2/db/MIGRATION.md`, на стороне с
      боевым DSN.
- [x] 3.2 Добавить `search_description` как обязательное поле в контракт
      входной таблицы «Объекты» (`docs/architecture/
      observation_objects_context.md`, раздел 2). → таблица дополнена
      строкой `search_description` (было сделано в
      `proposal_search_description.md`, раздел 6; здесь зафиксировано
      и в DDL — `NOT NULL` + `CHECK (btrim(...) <> '')`).
- [x] 3.3 Реализовать проверку контракта: объект без непустого
      `search_description` MUST трактоваться как ошибка входа, не
      подставлять название как fallback. → constraint на уровне БД
      (шаг 3.1) плюс проверка в коде агента, см. группу 4.

## 4. Каркас нового пакета агента 2

- [x] 4.1 Завести пакет нового агента 2 (по аналогии со структурой
      `agents/agent_1/src/agent_1/`). → `agents/agent_2/src/agent_2/`.
- [x] 4.2 Перенести и адаптировать `experiments/retrieval_eval/
      filter_agent.py`, `patterns.py`, `queries.py` как основу — не
      переписывать с нуля. → каналы/логика в `channels.py`/
      `filter_agent.py`; данные `patterns.py`/`queries.py` перенесены как
      сид каталога (`agents/agent_2/db/002_seed_observation_objects.sql`),
      т.к. каталог теперь в БД, а не в Python-константах.
- [x] 4.3 Удалить/не переносить `adaptive_cutoff()` и связанные с ней
      CLI-параметры (`--min-keep`, `--max-keep`, `--min-ratio`,
      `--sweep`) — отсечка по разрыву заменяется LLM-рубрикой (design D3).
      → не перенесено; порог реализован в `llm_scoring.THRESHOLD`.

## 5. Лексический канал

- [x] 5.1 Перенести `keyword_hits()` / `to_postgres()` без изменения
      логики регексов каталога. → `channels.py`/`db.py`.
- [x] 5.2 Перенести `negative_hits()` как основу для шага негатив-вето
      (раздел 8). → `channels.py`.

## 6. Векторный канал

- [x] 6.1 Перенести `vector_scored()`, переключить текст запроса на
      `search_description` объекта вместо названия/алиасов. →
      `channels.vector_scored` + `catalog.ensure_query_embedding`
      (эмбеддинг `search_description`, кэшируется в
      `observation_objects.query_embedding`).
- [x] 6.2 Обеспечить глубину выдачи не менее 500 документов на объект.
      → `channels.DEFAULT_CANDIDATES_DEPTH = 500`.
- [x] 6.3 Перенести `set_ef_search()` (`set_config()`, не `SET ... = %s`)
      и вызывать его с ef_search не меньше глубины выдачи. → `db.py`,
      вызывается в `filter_agent.main()` до поиска.

## 7. Объединение каналов

- [x] 7.1 Реализовать объединение (union) результатов лексического и
      векторного каналов как единственный режим сборки кандидатов;
      не оставлять режим пересечения даже как опцию. →
      `channels.union_candidates` — единственный режим, режима
      пересечения в коде нет вовсе.

## 8. Решающая LLM-оценка по рубрике

- [x] 8.1 Сформулировать словесную рубрику 0–10 (десять — новость целиком
      об объекте, четыре-шесть — объект упомянут, ноль — не относится) как
      промпт для решающей оценки. → `llm_scoring.RUBRIC_PROMPT_TEMPLATE`.
- [x] 8.2 Реализовать батчевый вызов решающей оценки через OpenClaw
      (паттерн `--openclaw-cmd`/`--agent-id`/`--model`/`--thinking`,
      рейт-лимит вызовов в минуту — см. `agent_1/label_kr_worker.py`),
      без прямого обращения к OpenRouter для этого шага. →
      `llm_scoring.call_openclaw`/`score_candidates`.
- [x] 8.3 Сделать модель решающей оценки параметром конфигурации вызова,
      без хардкода Opus/Sonnet в коде. → `ScoringConfig.model`,
      `--model`/`AGENT_2_SCORING_MODEL`.
- [x] 8.4 Реализовать кэш решающих оценок в
      `agent_1_v5.agent_2_llm_scores` с ключом (id_object, id_clean_post,
      model) — см. группу 10 для DDL. →
      `llm_scoring.fetch_cached_scores`/`store_score`.
- [x] 8.5 Реализовать обработку HTTP 429: бэкофф с учётом `Retry-After`,
      без падения и без молчаливого пропуска документа. →
      `llm_scoring.call_openclaw_with_backoff`,
      `AgentCapacityError`/`_parse_retry_after_seconds`.

## 9. Порог и негатив-фильтр

- [x] 9.1 Реализовать отбор по порогу 7.5 на объединённом наборе
      кандидатов вместо `adaptive_cutoff()`. → `filter_agent.filter_object`,
      шаг 5 (`THRESHOLD`).
- [x] 9.2 Применить негатив-фильтр каталога как вето после порога (не до
      него), на оставшихся после порога документах. →
      `filter_agent.filter_object`, шаг 6, вызывает
      `channels.negative_hits` только на `above_threshold`.

## 10. Таблицы результата и запись

- [x] 10.1 Создать таблицу `agent_1_v5.agent_2_llm_scores` (DDL):
      `id_object, id_clean_post, model, score, scored_at`,
      `PRIMARY KEY (id_object, id_clean_post, model)`. →
      `agents/agent_2/db/003_agent_2_llm_scores.sql` (не применена —
      см. группу 3, `MIGRATION.md`).
- [x] 10.2 Создать таблицу `agent_1_v5.agent_2_relevant_documents` (DDL):
      `id_object, id_clean_post, llm_score, selected_at`,
      `PRIMARY KEY (id_object, id_clean_post)`. Без полей драйвера/KR —
      см. `specs/agent_2/spec.md`, требование «KR и драйверы вне
      ответственности агента 2». →
      `agents/agent_2/db/004_agent_2_relevant_documents.sql` (не
      применена).
- [x] 10.3 Реализовать запись итога отбора (документы, прошедшие порог
      7.5 и негатив-фильтр, раздел 9) в `agent_2_relevant_documents` —
      по паре `(id_object, id_clean_post)` на строку, без глобальной
      дедупликации между объектами (M:N). →
      `filter_agent.write_relevant_documents`.

## 11. Регрессионный тест на эталоне

- [x] 11.1 Подключить `experiments/retrieval_eval/data/labels_v2.jsonl`
      как источник эталона для регрессионного теста нового пакета. →
      `agents/agent_2/scripts/run_regression.py:load_truth`. **Файл
      эталона в этой рабочей копии отсутствует** (`data/.gitignore`
      исключает всё содержимое, `labels_v2.jsonl` не выгружен локально)
      — скрипт написан и синтаксически проверен, не прогнан.
- [x] 11.2 Реализовать прогон полного пайплайна (каналы → объединение →
      LLM-оценка → порог → негатив-вето) по объектам эталона и подсчёт
      полноты (recall) на выходе. →
      `run_regression.py` вызывает `filter_agent.filter_object(...,
      dry_run=True)` по каждому размеченному объекту и считает recall
      против `truth`.
- [x] 11.3 Зафиксировать порог приёмки: средняя полнота по объектам
      эталона не ниже 70%; прогон, не проходящий порог, MUST блокировать
      приёмку change. → `run_regression.py:ACCEPTANCE_RECALL_THRESHOLD`,
      exit code 1 при недоборе.

## 12. Приёмка

- [ ] 12.1 Прогнать регрессионный тест (раздел 11) и приложить результат
      (полнота/точность по объектам) к change. **БЛОКЕР: невозможно в
      этой рабочей копии** — нет `.env` (`AGENT_1_DB_DSN`,
      `OPENROUTER_API_KEY`), нет доступа к `openclaw` CLI, эталон
      `labels_v2.jsonl` не выгружен локально (`data/.gitignore`
      исключает содержимое). Скрипт готов
      (`agents/agent_2/scripts/run_regression.py`) — прогнать на стороне
      с боевым `.env`, по аналогии с `agents/agent_1/db/v5/MIGRATION.md`.
      **Обновление (прод, 2026-08-06):** DDL (3.1, 10.1, 10.2) накатан
      успешно — 10 объектов в `observation_objects`, обе рабочие таблицы
      пустые, PK/индексы совпадают с DDL. Первая попытка прогнать
      регрессионный тест упала: `python: command not found` — на этом
      хосте нет системного `python`, только `python3`, и venv для
      агента 2 никогда не создавался (пробел брифа — не было шага
      установки окружения). Исправлено: `agents/agent_2/db/MIGRATION.md`
      разделы 5–7 и `README.md` — явный `python3 -m venv .venv` +
      `pip install -r requirements.txt`. Сам прогон эталона всё ещё не
      выполнен.
- [x] 12.2 Проверить вручную на нескольких документах, что кэш решающей
      оценки не путает модели (грабля: кэш без модели в ключе). →
      живой БД нет, проверено юнит-тестами на подменной БД-обвязке:
      `tests/test_filter_agent.py::CacheModelIsolationTests` (3 теста,
      все зелёные) — SQL кэша фильтрует и пишет по модели, смена модели
      не возвращает чужую оценку.
- [x] 12.3 Обновить `docs/architecture/system_architecture_2026-07-31.md`
      (раздел 10 «Расхождения») — снять как закрытые пункты «Порядок
      фильтров» и «Форма запроса», зафиксировать финальное решение.
      → сделано, оба пункта помечены «ЗАКРЫТО (2026-08-06)» со ссылкой
      на реализацию.