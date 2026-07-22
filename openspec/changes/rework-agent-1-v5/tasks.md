# Tasks: rework-agent-1-v5

Стадии независимо проверяемы; после каждой — чекпоинт (тесты зелёные,
коммит). Rollback до стадии 6 = переключение очереди обратно на старые
таблицы/воркеры (ничего не удаляется до приёмки).

## 1. Схема данных v5 (зона Агента 1)

- [x] 1.1 DDL: `CREATE EXTENSION vector`; таблицы `source`, `raw_posts`,
      `clean_posts` по `docs/architecture/schema_v5.dot` — композитные PK с
      `time_post`, LZ4-компрессия `content`/`clean_content`, nullable
      `ID_cluster` (под Агент 3), поле `drop_reason`, уникальный индекс
      exact-dedup ключа. Накачено 2026-07-20 на `mvp_db`, схема
      `agent_1_v5` (PG16, pgvector 0.6.0, пакет `postgresql-16-pgvector`),
      без затрагивания схемы `agent_1`; структура `clean_posts` сверена
      через `\d` — 4 таблицы, PK/FK/partial unique index как в спеке.
- [x] 1.2 DDL: таблица junk-категорий; сид текущими 17 категориями и
      business-guard regex из `preprocess_worker.py`. Сид сверен построчно
      с кодом до накатки (число regex-фрагментов совпало по всем 17
      категориям); после накатки подтверждено 18 строк в
      `agent_1_v5.junk_categories` с ожидаемым числом паттернов на каждую.
- [x] 1.3 Скрипт миграции: `agent_1.raw_items` → `raw_posts` (разбор
      склеенных полей в атомарные, резолв `source`),
      `clean_items` → `clean_posts` (включая перенос вердиктов
      отбраковки из `raw_items.source_metadata.preprocess` в
      `drop_reason`). `agents/agent_1/src/agent_1/migrate_v5.py` (батчи,
      идемпотентен на повтор, `--dry-run` в одной откатываемой транзакции,
      savepoint на content_hash-коллизии); DDL-довесок
      `003_source_name_unique.sql`. Инструкция —
      `agents/agent_1/db/v5/DATA_MIGRATION.md`.
- [x] 1.4 Прогон выполнен 2026-07-20 на `mvp_db` (изоляция через отдельную
      схему `agent_1_v5`, а не физическую копию БД — тот же эффект
      безопасности: `agent_1` не тронута, миграция идемпотентна и
      воспроизводима при необходимости). Числа `--dry-run` и реального
      прогона совпали один в один: 57000 `raw_posts`; 1233 `clean_posts`
      (942 kept + 286 dropped + 5 duplicate) + 55767 unprocessed
      (легитимно без строки — старый пайплайн их никогда не обрабатывал);
      1946 `source`. 0 content_hash-коллизий, 0 anomalous status, 0
      orphaned duplicates, 0 скипов по `url`/`published_at`. Отдельная
      находка вне скоупа миграции: 98% корпуса (55767/57000) никогда не
      проходили через старый `preprocess_worker` — это будущая нагрузка
      для v5-воркера (стадия 3), а не проблема самой миграции.

## 2. Вывод LLM-этапов из Агента 1 (BREAKING)

- [x] 2.1 Исключить постановку `label_kr` job'ов из препроцессинга;
      `label_kr_worker.py`, `extract_semantics_worker.py`,
      `kr_enrichment_sync.py`, `label_prompts.py` — убрать из
      запуска/оркестрации Агента 1 (код остаётся в репо как референс для
      Агентов 2/4). Удалён вызов `enqueue_label_job()` и сама функция из
      `preprocess_worker.py` (единственная точка постановки `label_kr` в
      коде — оркестрации/cron/systemd в репозитории нет вообще, это
      server-side; сами воркер-файлы не тронуты). Тесты
      `test_preprocess_worker.py` зелёные (19/19).
- [x] 2.2 Пометить `document_kr_labels` / `document_enrichments` /
      чекпоинт-таблицы как read-only наследие (комментарий в БД/доках);
      подтвердить, что silver-датасет разметки сохранён.
      `agents/agent_1/db/007_mark_kr_labeling_readonly.sql` накачен на
      `mvp_db` 2026-07-20 (`COMMENT ON TABLE`, без изменений схемы/данных)
      — все 5 комментариев подтверждены через
      `obj_description(..., 'pg_class')` на `document_kr_labels`,
      `document_enrichments`, `label_kr_step_checkpoints`,
      `extract_semantics_step_checkpoints`, `llm_call_logs`. Живых
      процессов `label_kr_worker`/`extract_semantics_worker` на сервере не
      найдено (не крон, не оркестрация — были только ручные запуски).
      942 pending `label_kr`-джобы в `agent_1.processing_jobs` — сходится
      1:1 с числом "kept" из миграции данных (1.4), подтверждает
      целостность silver-датасета (ничего не потеряно, очередь просто
      не разгребалась и дальше не разгребётся, что и требуется).
- [x] 2.3 Обновить `agents/agent_1/README.md`, `IDENTITY.md` и снести/
      пометить устаревшим `PIPELINE_V1.md` (заменён спеками и
      `docs/architecture/`). `PIPELINE_V1.md` — баннер "Superseded" сверху,
      содержимое сохранено как референс под старый код воркеров;
      `README.md` — секция preprocess worker обновлена (больше не ставит
      `label_kr`), секция KR labeling worker помечена legacy/disconnected;
      `IDENTITY.md` — роль переписана под v5 (сбор/очистка/дедуп/
      эмбеддинги, ноль LLM-вызовов).

## 3. Перф-переделка препроцессинга

- [x] 3.1 cProfile «до» на реальном корпусе (2026-07-21,
      `scripts/bench_preprocess_compute.py` + end-to-end профиль текущего
      воркера). **Опровергло посылку «94% I/O»:** compute = 176 мс/док,
      MinHash-сигнатура = **150 мс/док**, build_shingles = 8.6 мс/док;
      end-to-end 87% wall-time в `build_minhash_signature_from_shingles`,
      I/O в профиле не виден. shingles/doc: median 1426, mean 1865, max
      4538 → кэш шинглов ~1.7 ГБ на 50k (D8 пересмотрен: по умолчанию не
      кэшируем). Приоритет стадии переставлен: смена MinHash-схемы (3.3) —
      основной рычаг, делаем первой; I/O (3.2) — не throughput-рычаг.
- [~] 3.2 I/O-слой воркера под v5-схему написан: новый модуль
      `src/agent_1/preprocess_v5.py`. Claim — anti-join `raw_posts` без
      строки `clean_posts`, `FOR UPDATE OF raw_posts SKIP LOCKED LIMIT 100`,
      claim+запись в одной транзакции (D9); two-phase bulk-insert в
      `clean_posts` (kept с `RETURNING id`, затем дубли по карте
      `id_raw_post→id_clean_post` — решает self-FK `id_canonical_post`);
      `content` из `raw_posts.content`; язык не хранится (`non_russian` в
      `drop_reason`). Чистая логика (verdict, two-phase резолв) покрыта
      юнит-тестами `tests/test_preprocess_v5.py`. **DB-интеграция (реальный
      claim/bulk-write) — за прогоном OpenClaw (не отмечено done).**
      Старый `preprocess_worker.py` остаётся легаси-референсом.
- [x] 3.3 MinHash: схема хэш-миксинга заменена на нативно
      `uint64`-векторизуемую `(x XOR mask_i) * odd_multiplier_i` (128
      масок/множителей, детерминированы от фикс-сидов, множители нечётные),
      numpy broadcast `(128, N)` + `min(axis=1)` в
      `build_minhash_signature_from_shingles` (`preprocess_worker.py`).
      Пороги/шинглование/бэнды (D3) не тронуты; accept/reject остаётся
      точным Jaccard. Локальная валидация
      (`scratchpad/validate_minhash.py`): детерминизм ✓; несмещённость —
      оценка сходится с истинным Jaccard идентично старой схеме; recall
      кандидатов для принимаемых пар (истинный J≥0.7) = 1.000 как у старой,
      FP реже; скорость **2.0 мс/док против 93.9 (47x)**. 3 юнит-теста
      добавлены в `test_preprocess_worker.py` (22/22 зелёные). numpy → в
      `requirements.txt`.
      **blake2b на горячем пути заменён на crc32** (`hash_shingle`) после
      re-measure 3.7 — тот показал, что после векторизации сигнатуры узким
      местом стал `build_shingles`/`hash64` (blake2b, 13.9s из 23.8s
      стартовой загрузки кэша). crc32 (stdlib, без новой зависимости):
      build_shingles **2.2x быстрее** (1.22 vs 2.65 мс/док), а решения
      дедупа **не меняются вообще** — Jaccard над шинглами байт-в-байт
      идентичен blake2b (max разница 0.0000 на 200 near-dup парах, 0
      коллизий на ~2000 шинглов/док). 64-битный blake2b оставлен только для
      деривации масок/множителей MinHash (256 вызовов на импорте).
- [x] 3.4 In-memory LSH в `preprocess_v5.py`: `DedupState` грузится из
      существующих kept `clean_posts` на старте (`load_dedup_state`, JOIN к
      `raw_posts` за title), растёт по ходу; exact-dedup через `content_hash`
      + partial UNIQUE. Кэш шинглов **не вводим** (D8 пересмотрен после 3.1):
      шинглы кандидата пересчитываются при band-коллизии, экономим ~1.7 ГБ.
- [x] 3.5 Junk из `agent_1_v5.junk_categories` (`load_junk_state` +
      `build_junk_state_from_rows`, кэш в памяти процесса, `is_business_guard`
      → guard-паттерн через тот же `compile_pattern`); отбраковка пишет
      `drop_reason='junk:<категория>'`. `classify_junk_topic` в
      `preprocess_worker.py` рефакторнут под прокидываемые паттерны
      (идентичная логика старому и v5). Покрыто тестами.
- [x] 3.6 Multiprocessing — **НЕ нужен** (решено по re-measure 3.7):
      реальная обработка = ~21 мс/док на 1 ядре (process_batch 4.2s/200) →
      ~685k/час на 4 ядрах, цель 400k перекрыта. Начальный bulk-бэкфилл 55k
      всё равно одним воркером (D10). Multiprocessing не строим.
- [x] 3.7 Re-measure выполнен на реальном корпусе (2026-07-21, 200 док):
      векторизация MinHash (3.3) подтверждена — сигнатуры ушли из топа
      профиля; реальная обработка ~21 мс/док (в цели). Вскрыл новое узкое
      место — `build_shingles`/blake2b (→ crc32, см. 3.3) и стартовую
      загрузку кэша O(корпуса) (см. ниже, структурный follow-up).
- [ ] 3.8 Адаптировать `tests/test_preprocess_worker.py` под v5-схему
      (`clean_posts`, anti-join claim, батчевую механику); тесты
      корректности новой MinHash-схемы (сигнатура детерминирована, recall
      кандидатов сохраняется) и junk-из-БД. MinHash/junk/verdict уже
      покрыты (`test_preprocess_v5.py`); остаётся интеграционный тест
      claim/anti-join (нужна БД — за OpenClaw или отдельный DB-тест).
- [ ] 3.9 (follow-up из 3.7) Стартовая загрузка dedup-кэша — O(корпуса):
      `load_dedup_state` пересчитывает шинглы всех kept `clean_posts` на
      каждом старте (~23.8s на 1097 → минуты на 45k). Для бэкфилла не
      блокер (старт один), но для стационарных рестартов надо ограничить.
      Варианты: (a) тайм-окно near-dup кэша (грузить только последние N
      дней — near-dup ловит синдикацию в пределах дней, не через всю
      историю; но меняет эквивалентность 6.1, проверить), (b) персистить
      MinHash-сигнатуру в колонке `clean_posts` (старт без пересчёта
      шинглов). Решить после бэкфилла.

## 4. Эмбеддинги

- [~] 4.1 Embedding-воркер написан: `src/agent_1/embed_v5.py` через
      **OpenRouter** (`openai/text-embedding-3-small`, OpenAI-совместимый
      `/embeddings`, `requests`, `OPENROUTER_API_KEY`). Claim только
      `drop_reason IS NULL AND is_duplicate=false AND embedding IS NULL`
      (FOR UPDATE SKIP LOCKED — эмбеддинг API-bound, параллелится);
      батчи; идемпотентность через `embedding IS NULL`. **Dimensions:**
      OpenRouter не документирует `dimensions`, поэтому не зависим от него —
      запрашиваем `dimensions=1024`, но defensively обрезаем первые 1024 +
      L2-ренорм на клиенте (Matryoshka-эквивалент серверной обрезки).
      Чистая логика (truncate/normalize, parse, vector-literal, cap) —
      9 юнит-тестов. **API-прогон — за OpenClaw (нужен ключ в .env).**
- [~] 4.2 HNSW-индекс написан (`db/v5/005_embedding_hnsw.sql`,
      `vector_cosine_ops`, строить ПОСЛЕ заливки). Bulk-прогон + накатка
      индекса — за OpenClaw.
- [ ] 4.3 Тесты идемпотентности («дубликат/отбракованный не эмбеддится» —
      гарантировано claim-условием; «0 вызовов на повторе» — проверяется
      на реальном прогоне OpenClaw, т.к. это про SQL-claim, не про чистую
      функцию).

## 5. Статистика источников

- [x] 5.1 SQL-view `agent_1_v5.source_stats` написан
      (`db/v5/004_source_stats_view.sql`): `total_raw, processed, pct_junk,
      pct_non_russian, pct_duplicates, avg_content_len, last_seen_at` по
      `id_source` из `raw_posts`+`clean_posts`. Проценты — над processed (не
      total_raw), чтобы 55767 unprocessed не занижали метрики до конца
      бэкфилла. Накачен на `mvp_db` 2026-07-22, sanity-проверка (топ-10 по
      `processed`) выглядит здраво (championat 74% junk — спортивный сайт,
      источник-фид с числовым именем — 42% дубликатов, похоже на wire).

## 6. Приёмка

- [x] 6.1 Эквивалентность v5/v1 проверена (2026-07-22,
      `scripts/check_v1_equivalence.py`, read-only). Методология: v1 реально
      обработал только 1233 из 57000 raw (см. 1.4) — сравнение "v5 против
      v1 на тех же документах" технически возможно только на этом
      пересечении (остальные 55767 не имеют v1-вердикта в принципе).
      Скрипт восстанавливает эти 1233 через `cleaned_at`-разрыв между
      прогоном миграции (2026-07-20) и бэкфилла (2026-07-22), пересчитывает
      их вердикт чистой `compute_verdict` с dedup-кэшем, из которого эти
      1233 исключены (не сравниваются сами с собой), сравнивает с
      мигрированным v1-вердиктом — без единой записи в БД.
      **Результат: 1232/1233 = 99.92% совпадение, 1 расхождение (0.08%,
      допуск ≤0.5%)**: `id_raw_post=57720` v1=kept → v5=duplicate.
      Объяснимо и не баг: тот же эффект, что рост доли дублей с 0.4% до 7%
      между узкой миграционной выборкой и полным бэкфиллом — v1 на момент
      обработки этого документа ещё не видел его пару в своём крошечном
      кэше, а v5 при полном прогоне корпуса — видит.
- [x] 6.2 Скорость подтверждена на **реальном бэкфилле** (не только на
      синтетике 3.7): по временным меткам лога `--drain`,
      `12:47:42→12:48:54` (72с) на `total 46500→55357` (8857 док) =
      **123 док/с ≈ 442 800 док/час на одном процессе**, без
      multiprocessing. Цель ≥400k/час перекрыта на 1 ядре. (cProfile-цифра
      21 мс/док из 3.7 была занижена оверхедом самого профайлера — реальная
      непрофилированная скорость выше, что и подтверждает этот замер.)
      С эмбеддингами — по-прежнему за прогоном OpenClaw (упирается в
      OpenRouter, сейчас заблокировано на 401, см. 4.1).
- [ ] 6.3 `embedding` заполнен у 100% чистых недубликатов, NULL у 100%
      дублей/отбракованных. **Заблокировано на OpenRouter 401** (см. 4.1) —
      ключ не проходит даже прямой curl-запрос в обход кода
      (`{"error":{"message":"User not found.","code":401}}`), нужен новый
      ключ на openrouter.ai/keys.
- [ ] 6.4 Повторный запуск на обработанном корпусе: 0 вызовов эмбеддера,
      0 изменённых строк. Заблокировано тем же (нечем прогнать первый
      реальный вызов, чтобы проверить второй).
- [ ] 6.5 Ни одна строка `raw_posts` не удалена/изменена очисткой
      (контрольный запрос до/после). Структурно гарантировано кодом —
      `preprocess_v5.py` не делает ни одного `UPDATE`/`DELETE` на
      `raw_posts`, только `SELECT ... FOR UPDATE OF` (блокировка без
      мутации); `raw_posts` оставался на 57000 на всех проверках после
      бэкфилла. Финальный контрольный запрос — при закрытии стадии.
- [ ] 6.6 cProfile-отчёты приложены к PR; переключение прода на новые
      таблицы, старые — read-only до отдельного change на снос.
