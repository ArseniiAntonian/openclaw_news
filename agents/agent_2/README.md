# agent_2 Runtime

## Purpose

`agent_2` (агент-фильтратор) отбирает документы из корпуса новостей
(`agent_1_v5.clean_posts`) под каждый объект наблюдения из каталога
(`agent_1_v5.observation_objects`) — ретривом: лексический канал +
векторный канал → объединение → LLM-оценка по рубрике 0–10 → порог 7.5
→ негатив-фильтр как вето → запись в
`agent_1_v5.agent_2_relevant_documents`.

Конструкция обоснована замером на 1318 размеченных документах — см.
`docs/architecture/retrieval_research_report.md` и
`openspec/changes/rework-agent-2-filter/`.

## Install

```bash
cd /root/.openclaw/workspace/agents/agent_2
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

`python3` явно на шаге создания venv: на проде голый `python` не
гарантирован. Дальше внутри активированного venv `python` работает
штатно. Подробнее — `db/MIGRATION.md`, разделы 5–7 (первичная накатка,
окружение, регрессионный тест).

## Constraints

- KR, драйверы, «Упоминания» — вне контракта, читает выход другой агент.
- Каталог объектов — вход, не порождается этим агентом.
- Решающая LLM — только через OpenClaw, модель — параметром конфигурации.
- Регрессионный тест на `experiments/retrieval_eval/data/labels_v2.jsonl`
  — полнота ≥70% как условие приёмки.

## Previous version

Прежний агент 2 (промпт «бизнес-цель → JSON») — архивирован в
`agents/_archive/agent_2_2026-08-06/`.