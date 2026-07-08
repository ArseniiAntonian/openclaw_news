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

## 2. agent_1-deterministic-enrichment (ещё предстоит реализовать)

- [ ] 2.1 Реализовать детекцию кандидатов-аббревиатур в `clean_text`
      детерминированными правилами/регулярными выражениями.
- [ ] 2.2 Реализовать расширение аббревиатур по корпоративному словарю
      (`corporate_abbreviations`: `short`, `expanded`), с результатом
      `status='resolved'`/`'unresolved'` согласно спеке.
- [ ] 2.3 Реализовать backfill-прогон: после пополнения словаря
      пересчитывать `unresolved` записи в `resolved` без повторного
      прохождения агентских стадий.
- [ ] 2.4 Реализовать отдельный Python-скрипт триграммной статистики для
      `document_type='review'`, пишущий в `review_trigrams`
      (`trigram`, `count`, `relative_frequency`).
- [ ] 2.5 Покрыть оба воркера тестами по образцу
      `tests/test_preprocess_worker.py`.
- [ ] 2.6 После реализации — сверить спеку `agent_1-deterministic-enrichment`
      с фактическим кодом и убрать пометку "форвард-лукинг" в её заголовке.

## 3. Не входит в этот change (для справки)

- [ ] 3.1 Когда придёт ERD по новой БД — создать отдельный change со спекой
      `agent_1-data-model` и, при необходимости, точечные MODIFIED-дельты
      к пяти поведенческим спекам.
- [ ] 3.2 По желанию — отдельным change поправить `PIPELINE_V1.md` и
      `README.md`, чтобы их описание MinHash (`64`/`16×4`) совпадало с
      кодом (`128`/`32×4`); в этом change сами доки не редактируются.
