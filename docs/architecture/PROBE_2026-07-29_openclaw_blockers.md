# Промпт для OpenClaw: проверка блокеров эвала ретрива (шаг 0)

**Дата:** 2026-07-29
**Контекст:** `HANDOFF_2026-07-28_retrieval_experiment.md`, раздел 4 (блокеры 4.1 и 4.2)
**Режим:** только чтение. Ни одного `INSERT`/`UPDATE`/`DELETE`/`CREATE`/`ALTER`.

Всё ниже до конца файла — текст, который отдаётся OpenClaw на сервере как есть.

---

## ЗАДАЧА

Ты работаешь на сервере с БД `agent_1_v5`. Нужно ответить на два вопроса,
от которых зависит план эксперимента по ретриву. Задача **диагностическая,
только SELECT**. Ничего не создавать, не менять, не удалять.

**Вопрос 1 (даты).** Есть ли в корпусе окно **2026-06-09 … 2026-06-13**?
У нас есть готовый answer key от банка (72 упоминания) с этими датами.
Если июньских документов нет — answer key неприменим, эвал придётся
размечать самим по нашему корпусу.

**Вопрос 2 (матчибельность).** Можно ли сматчить упоминания из answer key
с нашими документами? Ссылки в answer key в основном агрегаторные
(`dzen.ru/news/story/...` — story-страница Дзена, а не статья). Матчибельными
выглядят только `3dnews.ru`, `1prime.ru`, `ria.ru`, `news.ru`, плюс
упоминаются `spbit`, `habr`, `cnews`, `gazeta`. Нужно понять, по чему вообще
матчить: по URL, по домену+дате+заголовку, или матчить нечем.

### Ограничения

- Только `SELECT`. Никаких DDL/DML.
- Не трогать `agent_1.processing_jobs` и связанные таблицы.
- Не запускать `parsers360_ingest` (это поход в сеть).
- DSN — `AGENT_1_DB_DSN` из `/root/.openclaw/workspace/agents/agent_1/.env`
  (БД `mvp_db`).
- Перед запуском выставь `SET statement_timeout = '120s';`. Если запрос
  упёрся в таймаут — не подбирай обходы молча, зафиксируй факт в отчёте
  и иди дальше.
- Схема: `agent_1_v5`. Таблицы: `source`, `raw_posts`, `clean_posts`.
  `title` и `url` живут в `raw_posts`, очищенный текст — в
  `clean_posts.clean_content`. Связь: `clean_posts.id_raw_post = raw_posts.id_raw_post`,
  источник — `raw_posts.id_source = source.id_source`.
- «Живой» документ = `drop_reason IS NULL AND is_duplicate = FALSE`.
  Везде, где это различие имеет значение, считай оба числа.

---

## БЛОК A — покрытие по датам

### A1. Общие границы корпуса

```sql
SELECT 'raw_posts' AS t, count(*) AS n, min(time_post) AS min_t, max(time_post) AS max_t
FROM agent_1_v5.raw_posts
UNION ALL
SELECT 'clean_posts_all', count(*), min(time_post), max(time_post)
FROM agent_1_v5.clean_posts
UNION ALL
SELECT 'clean_posts_kept', count(*), min(time_post), max(time_post)
FROM agent_1_v5.clean_posts
WHERE drop_reason IS NULL AND is_duplicate = FALSE
UNION ALL
SELECT 'clean_posts_kept_with_embedding', count(*), min(time_post), max(time_post)
FROM agent_1_v5.clean_posts
WHERE drop_reason IS NULL AND is_duplicate = FALSE AND embedding IS NOT NULL;
```

### A2. Помесячная гистограмма (весь корпус)

```sql
SELECT date_trunc('month', time_post)::date AS month,
       count(*) AS all_rows,
       count(*) FILTER (WHERE drop_reason IS NULL AND is_duplicate = FALSE) AS kept,
       count(*) FILTER (WHERE embedding IS NOT NULL) AS with_embedding
FROM agent_1_v5.clean_posts
GROUP BY 1
ORDER BY 1;
```

### A3. Июньское окно по дням (шире, чем нужно: 01–20 июня)

```sql
SELECT date_trunc('day', time_post)::date AS d,
       count(*) AS all_rows,
       count(*) FILTER (WHERE drop_reason IS NULL AND is_duplicate = FALSE) AS kept,
       count(*) FILTER (WHERE embedding IS NOT NULL) AS with_embedding
FROM agent_1_v5.clean_posts
WHERE time_post >= '2026-06-01' AND time_post < '2026-06-21'
GROUP BY 1
ORDER BY 1;
```

### A4. Откуда что взялось: разрез по парсерам

```sql
SELECT parser, count(*) AS n, min(time_post) AS min_t, max(time_post) AS max_t,
       min(collected_at) AS min_collected, max(collected_at) AS max_collected
FROM agent_1_v5.raw_posts
GROUP BY 1
ORDER BY 2 DESC;
```

---

## БЛОК B — матчибельность источников и URL

### B1. Инвентарь доменов (топ-60 по объёму)

```sql
SELECT lower(substring(url from '^(?:https?://)?(?:www\.)?([^/?#]+)')) AS host,
       count(*) AS n,
       min(time_post) AS min_t,
       max(time_post) AS max_t
FROM agent_1_v5.raw_posts
GROUP BY 1
ORDER BY 2 DESC
LIMIT 60;
```

### B2. Целевые домены answer key — есть ли они и попадают ли в июнь

```sql
WITH h AS (
  SELECT lower(substring(url from '^(?:https?://)?(?:www\.)?([^/?#]+)')) AS host,
         time_post
  FROM agent_1_v5.raw_posts
)
SELECT host,
       count(*) AS total,
       count(*) FILTER (WHERE time_post >= '2026-06-01' AND time_post < '2026-07-01') AS june,
       count(*) FILTER (WHERE time_post >= '2026-06-09' AND time_post < '2026-06-14') AS window_0609_0613
FROM h
WHERE host ~ '(^|\.)(dzen\.ru|3dnews\.ru|1prime\.ru|ria\.ru|news\.ru|habr\.com|habr\.ru|cnews\.ru|gazeta\.ru|spbit\.ru)$'
GROUP BY 1
ORDER BY 2 DESC;
```

### B3. Форма ссылок Дзена — story-страницы или статьи

```sql
SELECT CASE
         WHEN url ILIKE '%dzen.ru/news/story%' THEN 'dzen_news_story'
         WHEN url ILIKE '%dzen.ru/news%'       THEN 'dzen_news_other'
         WHEN url ILIKE '%dzen.ru%'            THEN 'dzen_other'
         ELSE 'not_dzen'
       END AS kind,
       count(*) AS n
FROM agent_1_v5.raw_posts
GROUP BY 1
ORDER BY 2 DESC;
```

Плюс 10 примеров URL Дзена, если они есть:

```sql
SELECT url, title, time_post
FROM agent_1_v5.raw_posts
WHERE url ILIKE '%dzen.ru%'
ORDER BY time_post DESC
LIMIT 10;
```

### B4. Прямой матч по конкретным URL из answer key

Это точки, где answer key даёт статейные ссылки. Ищем префиксы.

```sql
SELECT url, title, time_post
FROM agent_1_v5.raw_posts
WHERE url ILIKE '%3dnews.ru/1143489%'
   OR url ILIKE '%1prime.ru/20260613%'
   OR url ILIKE '%ria.ru/20260613%'
   OR url ILIKE '%news.ru/usa/genprokurory%'
ORDER BY time_post
LIMIT 50;
```

### B5. Что лежит в `metadata` — вдруг там есть исходная ссылка/источник

```sql
SELECT k, count(*) AS n
FROM agent_1_v5.raw_posts, LATERAL jsonb_object_keys(metadata) AS k
WHERE metadata IS NOT NULL
GROUP BY 1
ORDER BY 2 DESC
LIMIT 30;
```

И три полных примера `metadata` (обрезанных), чтобы увидеть форму:

```sql
SELECT id_raw_post, parser, left(metadata::text, 800) AS metadata_head
FROM agent_1_v5.raw_posts
WHERE metadata IS NOT NULL
ORDER BY id_raw_post DESC
LIMIT 3;
```

### B6. Заполненность заголовков (от неё зависит нечёткий матчинг по title)

```sql
SELECT count(*) AS n,
       count(title) AS with_title,
       count(*) FILTER (WHERE title IS NOT NULL AND length(btrim(title)) > 0) AS with_nonempty_title
FROM agent_1_v5.raw_posts;
```

---

## БЛОК C — есть ли в корпусе сами события answer key

Это главный блок. Даже если URL не матчатся и дат нет, важно понять,
покрывает ли корпус **те же сюжеты**. Ищем по тексту, без URL.

```sql
WITH doc AS (
  SELECT c.id_clean_post,
         c.time_post,
         coalesce(r.title, '') || ' ' || coalesce(c.clean_content, '') AS txt
  FROM agent_1_v5.clean_posts c
  JOIN agent_1_v5.raw_posts r ON r.id_raw_post = c.id_raw_post
  WHERE c.drop_reason IS NULL AND c.is_duplicate = FALSE
),
pat(label, rx) AS (VALUES
  ('d1  Anthropic / Claude Fable 5',      '(anthropic|claude)'),
  ('d2  генпрокуроры США / OpenAI',       '(генпрокурор|генеральн\w+ прокурор).{0,200}openai|openai.{0,200}(генпрокурор|генеральн\w+ прокурор)'),
  ('d5  Xiaomi MiMo Code',                '(mimo\s?code|xiaomi.{0,120}(ии-?агент|нейросет))'),
  ('d6  Хегсет / Пентагон / Anthropic',   '(хегсет|пентагон).{0,200}anthropic|anthropic.{0,200}(хегсет|пентагон)'),
  ('d17 Orion soft',                      'orion\s?soft|орион\s?софт'),
  ('d18 Госдума / маркировка ИИ-контента','(госдум|законопроект).{0,200}маркировк.{0,120}(ии|искусственн\w+ интеллект|контент)'),
  ('d20 Mistral AI раунд',                'mistral.{0,200}(млрд|раунд|инвестиц|оценк)'),
  ('d21 Google / Samsung / TPU',          '(google|гугл).{0,200}(samsung|самсунг).{0,200}(tpu|чип)|tpu.{0,200}samsung')
)
SELECT pat.label,
       count(*) FILTER (WHERE doc.txt ~* pat.rx) AS total_hits,
       count(*) FILTER (WHERE doc.txt ~* pat.rx
                          AND doc.time_post >= '2026-06-01'
                          AND doc.time_post <  '2026-07-01') AS june_hits,
       count(*) FILTER (WHERE doc.txt ~* pat.rx
                          AND doc.time_post >= '2026-06-09'
                          AND doc.time_post <  '2026-06-14') AS window_hits
FROM pat CROSS JOIN doc
GROUP BY pat.label
ORDER BY 2 DESC;
```

Если по какому-то сюжету есть попадания — покажи по 3 примера (id, дата,
домен, заголовок), чтобы было видно, тот ли это сюжет. Достаточно
одного-двух самых объёмных лейблов, не всех.

---

## БЛОК D — плотность корпуса по ключевым словам объектов наблюдения

Это уже задел на «свой answer key»: если банковское окно не подошло,
нужно знать, хватит ли корпуса, чтобы разметить эвал самим.

Regex ниже — из каталога объектов. Объект 1 приведён полностью, объекты
2/3/4/7 — упрощённые версии (полные паттерны в наших документах обрезаны).

```sql
WITH doc AS (
  SELECT c.id_clean_post,
         c.time_post,
         coalesce(r.title, '') || ' ' || coalesce(c.clean_content, '') AS txt
  FROM agent_1_v5.clean_posts c
  JOIN agent_1_v5.raw_posts r ON r.id_raw_post = c.id_raw_post
  WHERE c.drop_reason IS NULL AND c.is_duplicate = FALSE
),
obj(id, name, rx) AS (VALUES
  (1, 'GigaChat',              'gigachat|гигачат|гигачад|кандинск|kandinsky|нейросет\w* сбер|giga\s?chat'),
  (2, 'YandexGPT / Алиса',     'yandexgpt|yagpt|яндекс\s?gpt|яндексгпт|yandex\s?cloud|алиса\s+(ai|нейро)'),
  (3, 'Open-source модели',    'deepseek|\mqwen\M|\mllama\M|mistral\s?ai|\mmistral\M|open[- ]?source\s+(llm|модел)'),
  (4, 'OpenAI / ChatGPT',      'openai|chatgpt|\mgpt-?[45]\M|чат-?гпт|чат-?джипити|\msora\M|anthropic'),
  (7, 'GPU и мощности',        'nvidia|видеокарт\w*|\mgpu\M|дата-?центр\w*\s+(ии|для ии)')
)
SELECT obj.id, obj.name,
       count(*) FILTER (WHERE doc.txt ~* obj.rx) AS total_hits,
       count(*) FILTER (WHERE doc.txt ~* obj.rx
                          AND doc.time_post >= '2026-06-01'
                          AND doc.time_post <  '2026-07-01') AS june_hits
FROM obj CROSS JOIN doc
GROUP BY obj.id, obj.name
ORDER BY obj.id;
```

---

## ЧТО ПРИСЛАТЬ В ОТВЕТ

Markdown-отчёт. Для каждого запроса — идентификатор (A1, A2, …) и его
вывод как есть, без пересказа. Длинные таблицы обрезай сверху по
значимости, но пиши, что обрезал.

В конце — четыре явных вердикта, каждый одним предложением:

1. **Окно 2026-06-09…13:** есть / нет / частично (сколько живых документов
   с эмбеддингом попадает в окно).
2. **Матч по URL:** возможен / невозможен (сколько из 4 префиксов B4
   нашлось; какая доля Дзена — story-страницы).
3. **Матч нечёткий (домен+дата+заголовок):** реалистичен / нет
   (есть ли целевые домены в корпусе, заполнены ли заголовки).
4. **Своя разметка:** хватит ли корпуса (числа из блоков C и D).

Если какой-то запрос упал — приложи текст ошибки, не переписывай запрос
на глаз более чем один раз.

Ничего не чинить, не досоздавать и не «пока я тут» — только отчёт.