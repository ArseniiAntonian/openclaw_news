> **Статус (2026-07-17):** ретро-фиксация (секция 1 tasks) выполнена и
> синкается в main-спеки как снимок «как есть». Спека
> `agent_1-deterministic-enrichment` **отменена** — не реализована и в
> архитектуре v5 не существует (см. `docs/architecture/pipeline_v5_context.md`);
> в main-спеки не переносится. Дальнейшая эволюция Агента 1 — в change
> `rework-agent-1-v5`.

## Why

Пайплайн `agent_1` (Parsers360 ingestion → preprocessing/dedup → KR labeling →
semantic enrichment → deterministic enrichment) уже построен и работает в
проде, но нигде не зафиксирован как OpenSpec-спека. Единственное описание
поведения — `agents/agent_1/PIPELINE_V1.md` и `agents/agent_1/README.md`,
которые местами разошлись с реальным кодом (например, параметры MinHash:
доки говорят `num_perm=64, b=16, r=4`, а код — `128, 32x4`). Нужна
ретроактивная фиксация того, что `agent_1` реально делает сегодня, по коду,
как единый источник правды для будущих изменений.

## What Changes

- Задокументировано текущее поведение пайплайна `agent_1` как набор из одной
  umbrella-спеки и пяти детальных спек по этапам. Новый код не пишется — это
  фиксация уже существующей системы.
- Расхождение документации и кода по MinHash устранено в спеке в пользу кода
  (`MINHASH_SIZE=128`, `MINHASH_BANDS=32`, `MINHASH_ROWS_PER_BAND=4`).
- Демо-контур (`agents/agent_1/demo/`) явно вынесен как non-goal — временный
  и одноразовый, спека под него не создаётся.
- Физическая схема БД (`CREATE TABLE` и точный список колонок) сознательно
  не централизуется отдельной спекой сейчас: БД скоро будет переделана по
  ERD, который пришлёт пользователь, и получит отдельную спеку
  `agent_1-data-model` позже. Сегодняшние реальные имена таблиц/колонок
  упоминаются в спеках там, где это делает текст понятнее, но не как
  формальное "владение схемой".
- Поведение agent_2 (обогащение `key_results.enrichment`) не описывается —
  для `agent_1-kr-labeling` это только внешнее предусловие.
- Обнаружено, что этап 8 (`agent_1-deterministic-enrichment` — расширение
  аббревиатур и триграммная статистика отзывов) описан в `PIPELINE_V1.md`,
  но не реализован: в `agents/agent_1/src/agent_1/` и
  `agents/agent_1/scripts/` нет ни одного воркера, читающего/пишущего
  `corporate_abbreviations` или `review_trigrams`; в БД есть только пустые
  таблицы-заготовки из `db/001_init.sql`. В отличие от остальных пяти
  капабилити, эта спека фиксируется не ретроактивно, а как обычный
  forward-looking план — реализация ещё предстоит, и `tasks.md` содержит
  реальные задачи на её выполнение.

## Capabilities

### New Capabilities
- `agent_1`: umbrella-спека — назначение агента, разделение ответственности
  между `agent_1` (LLM, только семантические задачи) и Python-воркерами (всё
  детерминированное), карта пайплайна, границы/non-goals (демо-контур вне
  скоупа, физическая схема БД вне скоупа до появления ERD).
- `agent_1-ingestion`: Parsers360 ingestion → `raw_items`
  (`parsers360_ingest.py`).
- `agent_1-preprocessing`: cleanup, определение языка (ru-only),
  junk-topic regex фильтрация с business-guard, exact dedup, MinHash
  near-dup (`preprocess_worker.py`).
- `agent_1-kr-labeling`: source-ranking фильтр, relevance gate,
  impact-разметка, Sber PR-like флаг, entity tonality, checkpointing,
  LLM call logging (`label_kr_worker.py`).
- `agent_1-semantic-enrichment`: извлечение сущностей и событий,
  groundedness-валидация (`extract_semantics_worker.py`).
- `agent_1-deterministic-enrichment`: расширение аббревиатур и триграммная
  статистика отзывов — чистый Python, не агент.

### Modified Capabilities
- (нет — все спеки новые, ранее ни одна из них не существовала)

## Impact

- Затронутый код: `agents/agent_1/src/agent_1/*.py`,
  `agents/agent_1/PIPELINE_V1.md`, `agents/agent_1/README.md` (только как
  источники для чтения — сами файлы этим change не переписываются).
- Изменений в рантайме, БД или API нет — это документирующий change.
- После архивации следующий change (когда придёт ERD) добавит
  `agent_1-data-model` и, при необходимости, MODIFIED-дельты к
  поведенческим спекам, если новые названия таблиц/колонок изменят
  формулировки.
