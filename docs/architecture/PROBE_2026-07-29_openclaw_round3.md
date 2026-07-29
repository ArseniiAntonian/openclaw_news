# Промпт для OpenClaw: раунд 3 — проба истории вендора + обработка июльского среза

**Дата:** 2026-07-29
**Предыдущие раунды:** `PROBE_2026-07-29_openclaw_blockers.md` (выполнен),
`PROBE_2026-07-29_openclaw_round2.md` (read-only, можно выполнять в любом порядке)

**Внимание, режим другой.** В отличие от раундов 1 и 2, здесь есть
**один сетевой вызов** и **запись в БД**. Оба разрешены пользователем явно
(2026-07-29). За пределы описанного не выходить.

Всё ниже до конца файла — текст, который отдаётся OpenClaw как есть.

---

## ЗАДАЧА

Две независимые части. Часть 1 — разведка, ничего не меняет. Часть 2 —
догоняет обработку уже лежащего в БД сырья. Выполнять по порядку, но
результат части 1 не влияет на часть 2.

### Что разрешено ровно

- **Часть 1:** POST-запросы к Parsers360 API на чтение. **Без записи в БД.**
- **Часть 2:** запуск `preprocess_v5` и `embed_v5` — они пишут в
  `agent_1_v5.clean_posts`. Это разрешено.
- **Запрещено:** запускать `parsers360_ingest` или `parsers360_ingest_v5`
  (любой ingest, пишущий в `raw_posts`) — в этом раунде бэкфилла нет,
  решение по нему принимается после части 1. Не трогать
  `agent_1.processing_jobs`. Не менять код. Не править `.env`. Не заводить
  cron/systemd.

Рабочий каталог: `/root/.openclaw/workspace/agents/agent_1`.
Окружение: `. .venv/bin/activate`, запуск модулей через `PYTHONPATH=src`.
`.env` подхватывается скриптами самостоятельно.

---

## ЧАСТЬ 1 — отдаёт ли вендор историю за 8–13 июня

Вопрос: параметр `start_at` у Parsers360 реально фильтрует по дате
публикации и отдаёт архив семинедельной давности — или он игнорируется
и возвращается свежак? От ответа зависит, можно ли восстановить окно
9–13 июня и оживить answer key банка на 72 упоминания.

Скрипт ниже **только читает**: он переиспользует `fetch_page` из
`parsers360_ingest` (чистая функция, ходит в API и возвращает JSON, в БД
не пишет) и ничего не вставляет. Сохрани его как
`/tmp/probe_vendor_history.py` и запусти.

```python
import json
import sys
from collections import Counter

sys.path.insert(0, "src")
from agent_1 import parsers360_ingest as v1


def published(item):
    dt = v1.parse_published_at(item)
    return dt.isoformat() if dt else None


def probe(start_at, page=1):
    items = v1.fetch_page(page, start_at)
    if not isinstance(items, list):
        print(f"start_at={start_at} page={page}: НЕ список, тип={type(items)}")
        print(json.dumps(items, ensure_ascii=False)[:600])
        return
    dates = sorted(d for d in (published(i) for i in items) if d)
    print(f"\n=== start_at={start_at} page={page} ===")
    print(f"получено items: {len(items)}")
    if not dates:
        print("ни у одного item не распарсилась дата публикации")
        return
    print(f"min published: {dates[0]}")
    print(f"max published: {dates[-1]}")
    print("распределение по дням:")
    for day, n in sorted(Counter(d[:10] for d in dates).items()):
        print(f"  {day}  {n}")
    print("первые 5 item (порядок как пришёл):")
    for i in items[:5]:
        print(f"  {published(i)} | {i.get('source')} | {str(i.get('url'))[:90]}")
    print("последние 3 item:")
    for i in items[-3:]:
        print(f"  {published(i)} | {i.get('source')} | {str(i.get('url'))[:90]}")


# 1. Целевое окно answer key.
probe("2026-06-08", page=1)
# 2. Контроль: свежая дата. Если распределения по дням совпадают --
#    start_at игнорируется вендором.
probe("2026-07-28", page=1)
# 3. Заведомо старая дата -- есть ли вообще глубокий архив.
probe("2026-05-01", page=1)
# 4. Вторая страница целевого окна -- в какую сторону идёт пагинация.
probe("2026-06-08", page=2)
```

```bash
cd /root/.openclaw/workspace/agents/agent_1
. .venv/bin/activate
PYTHONPATH=src python /tmp/probe_vendor_history.py
```

**Что важно в выводе:**

- различаются ли распределения по дням для `start_at=2026-06-08` и
  `start_at=2026-07-28` (если одинаковые — параметр не работает);
- попадают ли реально документы за 8–13 июня в первую выдачу;
- растут даты от страницы 1 к странице 2 или убывают.

Если API вернул ошибку — приложи текст, не подбирай параметры наугад.
Больше четырёх вызовов не делать: это платный внешний сервис.

---

## ЧАСТЬ 2 — догнать обработку июльского среза

В `raw_posts` 84686 строк до 23 июля, в `clean_posts` — 57000 и только
до 30 июня. ~27686 строк лежат сырыми: без очистки и без эмбеддингов.
Оба воркера сами находят работу анти-джойном, аргументов с датами им не
нужно.

### 2.1. Снимок «до»

```sql
SELECT (SELECT count(*) FROM agent_1_v5.raw_posts) AS raw_rows,
       (SELECT count(*) FROM agent_1_v5.clean_posts) AS clean_rows,
       (SELECT count(*) FROM agent_1_v5.clean_posts
         WHERE drop_reason IS NULL AND is_duplicate = FALSE) AS kept,
       (SELECT count(*) FROM agent_1_v5.clean_posts
         WHERE embedding IS NOT NULL) AS embedded,
       (SELECT max(time_post) FROM agent_1_v5.clean_posts) AS clean_max_time;
```

### 2.2. Препроцессинг: сначала маленькая пачка

Не запускай сразу полный прогон. Сперва одна пачка, чтобы увидеть, что
воркер жив и вердикты выглядят вменяемо:

```bash
cd /root/.openclaw/workspace/agents/agent_1
. .venv/bin/activate
PYTHONPATH=src python -m agent_1.preprocess_v5 --once --batch-size 50
```

Покажи вывод. Если он завершился с ошибкой — стоп, отчёт, дальше не идти.

### 2.3. Препроцессинг: полный прогон

```bash
cd /root/.openclaw/workspace/agents/agent_1
. .venv/bin/activate
nohup env PYTHONPATH=src python -u -m agent_1.preprocess_v5 --drain \
  > logs/preprocess_v5_catchup_2026-07-29.log 2>&1 &
echo "pid=$!"
```

Дождись завершения (`--drain` выходит сам, когда работы не осталось).
Периодически смотри `tail -n 20 logs/preprocess_v5_catchup_2026-07-29.log`.
В отчёт — последние ~30 строк лога и итоговые счётчики.

### 2.4. Проверка между шагами

```sql
SELECT count(*) AS clean_rows,
       count(*) FILTER (WHERE drop_reason IS NULL AND is_duplicate = FALSE) AS kept,
       count(*) FILTER (WHERE embedding IS NULL
                          AND drop_reason IS NULL
                          AND is_duplicate = FALSE) AS awaiting_embedding,
       max(time_post) AS clean_max_time
FROM agent_1_v5.clean_posts;
```

```sql
SELECT drop_reason, count(*) AS n
FROM agent_1_v5.clean_posts
WHERE cleaned_at >= now() - interval '6 hours'
GROUP BY 1
ORDER BY 2 DESC;
```

Второй запрос — санити-чек: если у свежеобработанных строк аномально
высокая доля отсева по одной причине, это повод остановиться и написать,
а не гнать эмбеддинги дальше.

### 2.5. Эмбеддинги: сначала капом

`embed_v5` ходит в OpenRouter (`openai/text-embedding-3-small`, вход
обрезается до 8000 символов, батч 64). Сначала ограниченный прогон:

```bash
cd /root/.openclaw/workspace/agents/agent_1
. .venv/bin/activate
PYTHONPATH=src python -m agent_1.embed_v5 --once --batch-size 64 --max-docs 128
```

Покажи вывод и проверь, что `embedding` появился:

```sql
SELECT count(*) FROM agent_1_v5.clean_posts WHERE embedding IS NOT NULL;
```

### 2.6. Эмбеддинги: полный прогон

```bash
cd /root/.openclaw/workspace/agents/agent_1
. .venv/bin/activate
nohup env PYTHONPATH=src python -u -m agent_1.embed_v5 --drain \
  > logs/embed_v5_catchup_2026-07-29.log 2>&1 &
echo "pid=$!"
```

Это самая долгая часть, порядка тысяч документов через внешний API.
Дождись выхода `--drain`, следи за логом. Если пошли повторяющиеся ошибки
API — останови (`kill <pid>`), приложи лог, не перезапускай в цикле.

### 2.7. Снимок «после»

Тот же запрос, что 2.1, плюс покрытие по месяцам:

```sql
SELECT date_trunc('month', time_post)::date AS month,
       count(*) AS all_rows,
       count(*) FILTER (WHERE drop_reason IS NULL AND is_duplicate = FALSE) AS kept,
       count(*) FILTER (WHERE embedding IS NOT NULL) AS with_embedding
FROM agent_1_v5.clean_posts
GROUP BY 1
ORDER BY 1;
```

**HNSW не трогать.** Индекс уже построен и подхватит новые строки сам.
Ни `REINDEX`, ни `CREATE INDEX`, ни `VACUUM FULL` не запускать.

---

## ЧТО ПРИСЛАТЬ В ОТВЕТ

Markdown-отчёт двумя разделами.

**Часть 1 — вердикт одним предложением:** отдаёт ли вендор документы за
8–13 июня (да / нет / `start_at` игнорируется), плюс вывод скрипта.

**Часть 2:**

- снимки «до» (2.1) и «после» (2.7) рядом;
- сколько строк добавилось в `clean_posts`, сколько kept, сколько
  заэмбеддено;
- до какой даты теперь доходит обработанный корпус;
- разбивка `drop_reason` у свежих строк (2.4) — с пометкой, выглядит ли
  она нормально на фоне старого среза;
- хвосты обоих логов;
- всё, что пошло не так, дословно.

Если на любом шаге результат выглядит неожиданно — остановись и напиши,
не «чини по дороге».