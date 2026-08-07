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

## 4. Что прислать обратно (по накатке)

Вывод команд из шага 3 (или шага 1, если остановились на стоп-условии).

## 5. Окружение для регрессионного теста (один раз)

**На этом хосте бинарник `python` не гарантирован** (встречалось
`python: command not found` — есть только `python3`). Venv создаём явно
через `python3`; внутри активированного venv `python` уже работает
штатно (это симлинк venv, не системный бинарник) — тот же паттерн, что
в `agents/agent_1/README.md`, раздел «Install».

```bash
cd /root/.openclaw/workspace/agents/agent_2
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 6. Прогон регрессионного теста

### ВАЖНО: запускать только durable-раннером, не из execution tool

Прогон длинный: ~500 кандидатов на объект × 8 меряемых объектов,
пачками по 10 при рейт-лимите 20 вызовов/мин — десятки минут
непрерывной работы. **Сессия execution tool столько не живёт.**

Установлено эмпирически 2026-08-07: три попытки подряд оборвались на
разном объёме работы (25 → 792 → +110 оценок), без ошибки в логе, без
OOM (`dmesg`/`journalctl` чисты, cgroup-лимитов нет, свободно 9 из
11 Gi). Процесс не падал — его убивали вместе с сессией. `nohup`
защищает от SIGHUP, но не от сноса всей процесс-группы; `wait $PID`
вдобавок держит сессию открытой до конца прогона, то есть гарантирует
попадание под таймаут.

Правильный способ — transient systemd-unit: собственный cgroup,
владелец systemd, живёт независимо от вызывающей сессии.

```bash
cd /root/.openclaw/workspace

systemd-run --unit=agent2-regression --collect \
  --working-directory=/root/.openclaw/workspace \
  agents/agent_2/.venv/bin/python \
    agents/agent_2/scripts/run_regression.py \
    --labels experiments/retrieval_eval/data/labels_v2.jsonl \
    --env-file agents/agent_1/.env \
    --out agents/agent_2/data/regression_report.json
```

Команда возвращает управление сразу — **не ждать её**. Проверять
отдельными вызовами, в любой момент:

```bash
systemctl status agent2-regression --no-pager
journalctl -u agent2-regression --no-pager | tail -40
```

По завершении: `systemctl status` покажет итоговый exit code,
`journalctl` — весь вывод (recall по объектам + средний), отчёт
окажется в `agents/agent_2/data/regression_report.json`.

Альтернативы, если `systemd-run` недоступен: `tmux new -d`/`screen -dm`
(живая сессия вне execution tool) или запуск с внешнего терминала.
`nohup … & wait` — не годится, проверено.

### Прогресс не теряется между попытками

Кэш `agent_1_v5.agent_2_llm_scores` хранит все уже полученные оценки
(ключ: объект + документ + модель). Повторный запуск не пересчитывает
их и продолжает с места обрыва — деньги на LLM тратятся один раз.
При необходимости дробить прогон: `--only-objects 2,3,4`.

### Условие приёмки

Средняя полнота ≥70% (`openspec/changes/rework-agent-2-filter/specs/
agent_2-filtering/spec.md`). Скрипт возвращает exit code 1, если порог
не достигнут, — это тоже полезный результат, не ошибка запуска.

По умолчанию меряются не все объекты: `--min-positives 5` отбрасывает
объекты, где положительных слишком мало для осмысленного числа, а
`--exclude-objects 9` — объект с заведомо испорченной разметкой. Оба
значения из оригинального замера, менять только осознанно.

Требует `AGENT_1_DB_DSN` и `OPENROUTER_API_KEY` в `agents/agent_2/.env`
(или `agents/agent_1/.env` — скрипт по умолчанию ищет
`agents/agent_2/.env`, при отсутствии передать
`--env-file agents/agent_1/.env`) и рабочий `openclaw` CLI в PATH.

## 7. Что прислать обратно (по регрессионному тесту)

Полный вывод `journalctl -u agent2-regression` (recall по каждому
объекту + средний) и итоговый статус из `systemctl status`.
Если упадёт на импорте/подключении — текст ошибки целиком, не
пересказ, не чинить вслепую.