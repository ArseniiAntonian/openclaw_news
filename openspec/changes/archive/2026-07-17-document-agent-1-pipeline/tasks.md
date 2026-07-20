## 1. Ретроактивная фиксация (уже реализовано, сверено с кодом)

- [x] 1.1 Спека `agent_1` (umbrella) сверена с `agents/agent_1/IDENTITY.md`,
      `SOUL.md` и `PIPELINE_V1.md` — назначение, разделение ответственности
      и non-goals зафиксированы верно.
- [x] 1.2 Спека `agent_1-ingestion` сверена с
      `agents/agent_1/src/agent_1/parsers360_ingest.py` и `README.md`.
- [x] 1.3 Спека `agent_1-preprocessing` сверена с
      `agents/agent_1/src/agent_1/preprocess_worker.py`; параметры MinHash
      взяты из кода (`128`/`32×4`), а не из устаревших `PIPELINE_V1.md`/
      `README.md` (`64`/`16×4`); список из 17 junk-topic категорий и
      business-guard сверен построчно.
- [x] 1.4 Спека `agent_1-kr-labeling` сверена с
      `agents/agent_1/src/agent_1/label_kr_worker.py`: source-ranking
      фильтр, relevance gate, четыре LLM-шага
      (`relevance`/`impact`/`sber_paid_news`/`entity_tonality`),
      чекпоинты, capacity-ошибки.
- [x] 1.5 Спека `agent_1-semantic-enrichment` сверена с
      `agents/agent_1/src/agent_1/extract_semantics_worker.py`: шаги
      `entities`/`events`, groundedness-валидация, чекпоинты.
- [x] 1.6 Подтверждено, что `key_results.enrichment` — зона ответственности
      `agent_2` (`kr_enrichment_sync.py` вызывает `agent_id=agent_2`, не
      `agent_1`); в спеках это отражено только как внешнее предусловие.
- [x] 1.7 Подтверждено, что демо-контур (`agents/agent_1/demo/`) изолирован
      от боевого пайплайна и намеренно не покрыт ни одной из спек.

## 2. agent_1-deterministic-enrichment — ОТМЕНЕНО (superseded by v5)

> 2026-07-17: пайплайн переведён на архитектуру v5
> (`docs/architecture/pipeline_v5_context.md`). Этап deterministic-enrichment
> в v5 не существует: расшифровка аббревиатур КР уходит в Агент 2, триграммная
> статистика отзывов в v5-схеме отсутствует. Задачи 2.1–2.6 не выполняются,
> спека `agent_1-deterministic-enrichment` при sync/archive не переносится в
> main-спеки.

- [ ] ~~2.1 Реализовать детекцию кандидатов-аббревиатур~~ (отменено)
- [ ] ~~2.2 Реализовать расширение аббревиатур по корпоративному словарю~~
      (отменено)
- [ ] ~~2.3 Реализовать backfill-прогон~~ (отменено)
- [ ] ~~2.4 Реализовать скрипт триграммной статистики~~ (отменено)
- [ ] ~~2.5 Покрыть оба воркера тестами~~ (отменено)
- [ ] ~~2.6 Сверить спеку с фактическим кодом~~ (отменено)

## 3. Не входит в этот change (для справки)

- [x] 3.1 ERD пришёл в виде схемы v5 (`docs/architecture/schema_v5.dot`,
      `pipeline_v5_context.md`). Вместо точечного `agent_1-data-model`
      создаётся change `rework-agent-1-v5` с полной переделкой Агента 1.
- [ ] ~~3.2 Поправить `PIPELINE_V1.md` и `README.md` по MinHash~~ (отменено —
      оба документа целиком устаревают с переходом на v5).
