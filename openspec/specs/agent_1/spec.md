# agent_1 Specification

## Purpose

Umbrella-спека агента `agent_1`: назначение, разделение ответственности между
LLM-агентом и Python-воркерами, карта пайплайна и non-goals.

> Снимок «как есть» на 2026-07-17 (change `document-agent-1-pipeline`).
> Архитектура находится в процессе перехода на пайплайн v5
> (`docs/architecture/pipeline_v5_context.md`); переделка — change
> `rework-agent-1-v5`.

## Requirements

### Requirement: Назначение и границы agent_1
`agent_1` MUST отвечать только за задачи, требующие семантического
суждения над уже предобработанным документом: разметку impact против
активных `key_results` (`0/1`-подобные бинарные метки `positive`/
`negative`/`neutral`), извлечение сущностей и извлечение событий. Вся
детерминированная механика пайплайна (создание и claim job'ов, извлечение
текста из `raw_payload`, html/unicode/whitespace-нормализация, определение
языка, exact dedup, MinHash near-dup, повторные прогоны и backfill) MUST
выполняться Python-воркерами, а не агентом.

#### Scenario: Задача детерминирована и не требует смыслового суждения
- **WHEN** задача пайплайна может быть надёжно реализована в Python (парсинг,
  нормализация текста, дедуп, статистика)
- **THEN** она реализуется в Python-воркере и не делегируется `agent_1`

#### Scenario: Задача требует смыслового суждения над текстом
- **WHEN** задача требует понимания смысла документа относительно KR,
  сущностей или событий
- **THEN** она может быть делегирована `agent_1`, и только в этом случае

### Requirement: Карта пайплайна
Пайплайн `agent_1` MUST состоять из следующих этапов, каждый из которых
покрыт отдельной детальной спекой:

1. `agent_1-ingestion` — Parsers360 ingestion в `raw_items`.
2. `agent_1-preprocessing` — cleanup, определение языка, junk-topic
   фильтрация, exact dedup, MinHash near-dup, запись в `clean_items`.
3. `agent_1-kr-labeling` — source-ranking фильтр, relevance gate,
   impact-разметка, Sber PR-like флаг, entity tonality, запись в
   `document_kr_labels`.
4. `agent_1-semantic-enrichment` — извлечение сущностей и событий, запись в
   `document_enrichments`.

(Этап `agent_1-deterministic-enrichment` — расширение аббревиатур и
триграммная статистика отзывов — был запланирован, но не реализован и
отменён переходом на v5; в этой спеке не описывается.)

Каждый этап MUST передавать документ следующему этапу через
`agent_1.processing_jobs` (создание job-строки со статусом `pending` для
следующего `job_type`), а не напрямую.

#### Scenario: Документ проходит весь путь без ошибок
- **WHEN** новый документ попадает в `raw_items` и последовательно проходит
  preprocessing, kr-labeling и semantic-enrichment без ошибок и без
  фильтрации
- **THEN** для него существуют строки в `clean_items`, `document_kr_labels`
  (хотя бы одна) и `document_enrichments`

#### Scenario: Документ отфильтрован на промежуточном этапе
- **WHEN** документ отфильтрован на этапе `agent_1-preprocessing` (например,
  как дубликат или не-русский текст) или его метки полностью отфильтрованы
  на этапе `agent_1-kr-labeling`
- **THEN** он не порождает `processing_jobs`-строку для следующего этапа, а
  исходная причина фиксируется в `raw_items.source_metadata`

### Requirement: Внешние предусловия agent_1-kr-labeling
`agent_1-kr-labeling` MUST рассматривать `key_results.enrichment`
(JSON-поля `тема`, `ключевые_слова`, `типы_источников`) как внешний вход,
которым владеет `agent_2`, а не как часть капабилити `agent_1`. Эта спека
и любые её детальные спеки MUST NOT описывать, как `agent_2` формирует
`enrichment` — только то, как `agent_1-kr-labeling` реагирует на его
наличие или отсутствие.

#### Scenario: KR ещё не обогащён agent_2
- **WHEN** активный `key_result` не имеет `enrichment` (`enrichment IS NULL`)
- **THEN** `agent_1-kr-labeling` обрабатывает этот KR в режиме fail-open, не
  дожидаясь `agent_2`

### Requirement: Non-goals
Эта капабилити и её детальные спеки MUST NOT описывать:

- демо-контур `agents/agent_1/demo/` (отдельная схема `demo`,
  `demo_ingest.py`, `demo_run.py`) — он временный, не используется в
  проде (`agent_1.processing_jobs`, боевые воркеры) и планируется к сносу;
- физическую схему БД как формальный источник правды (`CREATE TABLE`,
  исчерпывающий список колонок и ограничений) — БД переделывается по
  ERD v5 (`docs/architecture/schema_v5.dot`); схема данных Агента 1 v5
  фиксируется в change `rework-agent-1-v5`; до тех пор детальные спеки
  могут упоминать сегодняшние реальные имена таблиц/колонок инлайн, но не
  как исчерпывающий контракт;
- внутреннюю логику `agent_2` (обогащение key results, `kr_enrichment_sync.py`
  вызывает `agent_2`, не `agent_1`).

#### Scenario: Кто-то ищет описание демо-контура в этой спеке
- **WHEN** читатель ищет поведение `agents/agent_1/demo/` в спеках `agent_1`
- **THEN** он не находит его — демо-контур явно вне скоупа всех спек
  `agent_1`
