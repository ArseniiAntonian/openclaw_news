# LOG

## 2026-07-14

- Агент переведён в режим «вопрос-ответ **только по схеме `demo`**» базы `mvp_db`; другой функционал (боевые воркеры, ingest, очередь `agent_1.processing_jobs`, запись в БД) в этом режиме не трогается.
- Создан авторитетный операционный контракт `DEMO_QA.md`: источник данных (`demo.raw_items` ~57k, `demo.clean_items` ~50k, `demo.doc_labels` ~355k, `demo.kr` 5 строк), список из 5 KR, канонический SQL и обязательный формат отчёта.
- Структура `demo` подтверждена прямым запросом к БД: `doc_labels.impact ∈ {neutral, positive, negative}` (негатива 17, позитива 56), ссылка берётся из `clean_items.url`, обоснование — из `doc_labels.raw_json.why_for_goal` + цитаты в `raw_json.mentions`, KR-текст — из `demo.kr.text`.
- Зафиксирован рабочий рецепт «пример негативной тональности»: `demo.kr.text` → `demo.doc_labels` c `impact='negative'` → матч по `kr_id` и `clean_item_id` → `demo.clean_items.url`; ответ в полях **КР / Влияние / Обоснование / Ссылка**, по умолчанию 3 примера, при повторе — новая подборка.
- Обновлены `AGENTS.md`, `SOUL.md`, `MEMORY.md`, `USER.md`, `_tracking/STATE.md`: переориентированы на demo-only Q&A; из STATE.md убран прежний primary-`agent_1`/fallback-`demo` порядок источников.

## 2026-06-30

- Добавлен изолированный демо-контур A1 в `agents/agent_1/demo/` с отдельной схемой PostgreSQL `demo`.
- Вынесен отдельный ingest для июня 2026 без изменения боевого `parsers360_ingest.py`; диапазон дат задаётся явно.
- Для frozen corpus реализован exact-дедуп по `content_hash` и near-dup по тем же параметрам MinHash-LSH, что и в боевом `preprocess_worker.py`.
- Добавлен синхронный публичный прогон `demo_run.py` без `processing_jobs`: enrichment через `agent_2`, фильтр корпуса, трёхшаговая разметка через `agent_1`, чек-трасса и лог `demo/run_trace.log`.
- Явно зафиксировано, что demo-contour изолирован от схемы `agent_1` и от её очереди из-за боевого backlog.

## 2026-07-01

- В `preprocess_worker.py` добавлен light boilerplate cleanup для шумных англоязычных новостных блоков: вырезаются маркеры `Your browser does not support the video tag`, `Don't Miss`, `Most Read`, `Read Next`, `Related Articles`, `Recommended`.
- Перед cleanup добавлено разлепление склеенных токенов вида `TuesdayDon't` и `MissNews`, чтобы маркеры шумовых блоков стабильно находились и отрезались.
- В `tests/test_preprocess_worker.py` добавлен регрессионный тест на очистку английского boilerplate из реального вида Parsers360-контента.
- `preprocess_worker.py` переведён в режим ru-only для новостного корпуса: если после очистки текст уверенно классифицируется как не-русский, raw row получает `source_metadata.preprocess.status='filtered_out'` с `reason='non_russian_text'`, job закрывается как `done`, а `clean_items` не создаётся.
- В language detection добавлен явный порог по объёму буквенного текста и доле кириллицы (`min_alpha_chars=20`, `min_russian_letter_ratio=0.55`) вместо старого правила `cyrillic >= latin`.
- Добавлен скрипт `scripts/export_fresh_cleaned_news.py` для свежей re-clean выгрузки последних raw news без записи назад в БД; он пишет полную CSV со статусами и companion `cleaned_only`, плюс `.gz`.
- Собрана свежая выгрузка на последних 20k news: `agent_1_latest_20000_fresh_ru_recleaned_processed_news.csv(.gz)` и `agent_1_latest_20000_fresh_ru_recleaned_cleaned_only.csv(.gz)`. По сводке скрипта: `cleaned=19699`, `filtered_out=301`, всего `20000`.
- Экспортный скрипт расширен опцией `--target-cleaned`: он может читать больше raw rows и останавливаться ровно на целевом числе cleaned rows.
- Собрана свежая выгрузка на `50k cleaned`: `agent_1_latest_50000_fresh_ru_recleaned_processed_news.csv(.gz)` и `agent_1_latest_50000_fresh_ru_recleaned_cleaned_only.csv(.gz)`. По сводке скрипта: `cleaned=50000`, `filtered_out=995`, всего обработано `50995` raw news; допарсинг не понадобился.

## 2026-07-02

- Собрана новая свежая выгрузка на `50k cleaned` с датированным basename: `agent_1_latest_50000_fresh_ru_recleaned_2026-07-02_processed_news.csv(.gz)` и `agent_1_latest_50000_fresh_ru_recleaned_2026-07-02_cleaned_only.csv(.gz)`.
- По сводке скрипта: `cleaned=50000`, `filtered_out=995`, всего обработано `50995` raw news; языки в processed-файле: `ru=50000`, `en=995`.
- `preprocess_worker.py` заменён на новую версию с exact-dedup по `title + full clean_text`, summary-independent exact key и DB fallback-проверкой на точные дубли между несколькими воркерами.
- Старые результаты preprocessing сброшены: удалены `clean_items` и `label_kr` jobs, очищен `source_metadata.preprocess`, очередь `preprocess` пересобрана заново на все `57000` raw rows со статусом `pending`.

## 2026-07-03

- В `label_kr_worker.py` добавлен новый relevance-gate перед impact-разметкой: один общий LLM-вызов на документ выбирает только связанные KR, а `positive/negative/neutral` теперь ставятся только по ним.
- Нерелевантные к KR новости больше не должны попадать в `neutral` просто по факту прохода через impact prompt; для них `document_kr_labels` не создаётся.
- Для скорости не добавлялся отдельный per-KR бинарный вызов: relevance делается одним multi-KR prompt по всем source-allowed KR документа.
- В `raw_items.source_metadata.label_kr.source_filter` теперь пишутся `candidate_kr_ids`, `relevant_kr_ids`, `irrelevant_kr_ids` и краткие `relevance_matches`.
- Тесты `tests/test_label_kr_worker.py` обновлены под новый порядок вызовов и дополнены кейсом, что нерелевантный KR не доходит до impact.
- Добавлен скрипт `scripts/export_dedup_review.py` для ручной оценки качества дедупа: он выгружает пару `duplicate -> canonical` в одну строку с `duplicate_kind`, `similarity`, `minhash_similarity`, raw/meta-полями и очищенными текстами обеих сторон.
- Собраны dedup-review выгрузки: `out/agent_1_dedup_review_all_2026-07-03.csv(.gz)` на все `3103` дубликата (`exact=2190`, `near=913`) и `out/agent_1_dedup_review_near_2026-07-03.csv(.gz)` на `913` near-дубликатов.
- В `preprocess_worker.py` добавлен новый regex-layer перед dedup: он отфильтровывает только консервативный набор явного human-interest/noise (`weather`, `animals`, `gardening`, `celebrities`, `sleep`, `food`, `newborns`, `missing persons` и т.п.) и пишет `source_metadata.preprocess.reason='junk_topic_regex'`.
- Для защиты бизнес-сигналов не активированы самые широкие рискованные категории из присланного списка: `war_and_geopolitics`, `accidents_and_emergencies`, `epidemics_and_disease`, `medical_cases_and_hospitals`, `housing_and_utilities`, `tourism_and_travel`, `entertainment`, `consumer_tech_and_crypto`, `social_issues`, `education_and_social`.
- Добавлен business-guard: если в тексте есть явный банковый / сберовский / продуктовый контекст (`Сбер`, `банк`, `платежи`, `юрлица`, `зарплатный`, `GenAI`, `GigaChat`, `приложение`, `клиенты` и др.), regex-layer не фильтрует новость даже при совпадении шумовых паттернов.
- Попутно выровнены `tests/test_preprocess_worker.py` с текущей MinHash-конфигурацией `128 / 32x4`; узкий прогон `test_preprocess_worker.py + test_label_kr_worker.py` прошёл.
- По отдельному запросу включены ещё три regex-категории: `crime_and_fraud`, `sports`, `transport_and_airport`.
- Business-guard для этого поджат: убраны слишком широкие защитные токены вроде общего `счет` и `терминал`, вместо них оставлены более точные банковые формы (`расчетный счет`, `банковская карта`, `платежный терминал`, `POS-терминал`, `мобильное приложение` и т.п.), чтобы спорт и аэропорты не пролезали как ложный business-context.
- После расширения regex-layer узкий прогон тестов снова прошёл: `46 tests OK`.
- По явному запросу капитана старый preprocess-run сброшен: удалены `clean_items`, связанные downstream-сущности, `preprocess/label_kr/extract_semantics` metadata в `raw_items.source_metadata`, очередь `processing_jobs` очищена и заново собрана только как `preprocess pending` на все `57000` raw rows.
- Старый живой `preprocess_worker` был остановлен перед reset, чтобы не гонять очередь в гонке с удалением данных.
- Новый rerun запущен воркером `/root/.openclaw/workspace/agents/agent_1/.venv/bin/python -u -m agent_1.preprocess_worker --log-file logs/preprocess_worker_rerun_2026-07-03.log`.
- На раннем срезе после старта: `clean_items=135`, `preprocess done=158`, `preprocess pending=56840`, `label_kr pending=134`; в логе уже видны новые исходы `filtered_junk_religion_and_obituaries`, `filtered_junk_crime_and_fraud`, `filtered_junk_weather`, `duplicate_exact`, `cleaned`.

## 2026-07-06

- Зафиксирована пользовательская долговременная инструкция для запросов "привести примеры" по новостям: давать ровно 3 новости, минимальное обоснование к каждой, обязательную ссылку-пример и при повторе делать новую подборку.
- Создан pending Skill Workshop proposal `news-example-selection-rule-20260706-93e7a3731c` с тем же правилом; без отдельного утверждения он не применён как live skill.
- Капитан уточнил желаемый формат для запросов вроде "приведи пример негативной тональности": нужны ответы как подборка реальных/корпусных новостей с заголовком, кратким `Почему negative` и ссылкой, а не абстрактный пример текста.

## 2026-07-07

- Капитан уточнил формат примеров тональности: в ответе дополнительно указывать название КР (`КР: <название КР>`), относительно которого оценивается тональность новости.
- Капитан исправил формулировку: нужно указывать не придуманное название КР, а фактическую цель банка из разметки — поле `GOAL` / `goal_text`, в коде связанное с `agent_1.key_results.title` (`kr_title`).
- Капитан уточнил отображение: фактическую цель банка в ответах всё равно называть `КР`, а не `Цель банка`; добавлять жирное и текстовое выделение ключевых частей ответа.

## 2026-07-08

- Капитан уточнил обязательный DB-backed формат для запросов вроде "приведи пример негативной тональности": при ответе зайти в `mvp_db`, найти публикацию, текст KR, ссылку, `impact` и grounded-обоснование, затем отдать отчёт в полях `КР`, `влияние`, `обоснование`, `ссылка на публикацию`.
- Проверено read-only подключение через `AGENT_1_DB_DSN`: база `mvp_db`, схема `agent_1` содержит `clean_items`, `raw_items`, `key_results`, `document_kr_labels`; на момент проверки `agent_1.document_kr_labels` пустая, в `demo.doc_labels` есть негативные примеры как fallback.
- Капитан уточнил стиль отчёта по негативной тональности: не писать служебное предупреждение про fallback/пустую боевую разметку, давать 3 новости, не использовать фразу "В сохранённой разметке указано", после `обоснование` сразу писать фактическую причину негативного влияния; стиль должен быть формальным отчётом.

## 2026-07-09

- По уточнению Капитана возобновление прогона переведено с боевой очереди `agent_1.processing_jobs` на CSV-контур: источник данных — `/root/news_goal_*.csv`, а не все новости из `raw_items`.
- Для CSV-прогона использован существующий скрипт `scripts/benchmark_label_csv.py`, который размечает строки теми же prompt'ами, что и `label_kr_worker`, но без чтения общей очереди preprocess/label_kr.
- Найден частичный прошлый smoke-run в `/root/agent1_prompt_benchmark_smoke/`: `doc_cache.jsonl` содержал `1` fingerprint и был дополнен до `2` после пробного прогона одной новой строки из `news_goal_1_Топ-2_NPS_ММБ.csv`.
- Полный CSV-run поднят в фоне командой `python3 scripts/benchmark_label_csv.py` по 5 входным файлам `/root/news_goal_1_*.csv ... /root/news_goal_5_*.csv` с `--output-dir /root/agent1_prompt_benchmark_smoke` и `--session-key-prefix csv-resume-2026-07-09`.
- На старте run зафиксирован статус: `files=5`, `rows=7513`, `cache_loaded=2`; процесс живёт как PID `952240`, лог — `/root/agent1_prompt_benchmark_smoke/run_2026-07-09.log`.
