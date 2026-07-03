# STATE

## Демо-контур (schema demo)

Снимок от: 2026-06-30

- Изолированный демо-стенд живёт в схеме `demo` базы `mvp_db` и в директории `agents/agent_1/demo/`.
- В `demo.raw_items` лежит замороженное июньское сырьё Parsers360 за диапазон `2026-06-01..2026-06-30`.
- В `demo.clean_items` лежит тот же корпус после exact-дедупа по `content_hash` и near-dup по MinHash-LSH (`num_perm=64`, `b=16`, `r=4`, threshold `0.7`), включая пометки `is_duplicate` и `dup_of`.
- В `demo.kr` сохраняются пользовательские запросы и enrichment от `agent_2`.
- В `demo.doc_labels` сохраняются результаты синхронной трёхшаговой разметки `agent_1` для демо-прогонов.
- `demo/demo_ingest.py` поднимает схему `demo`, загружает июньский диапазон, строит frozen corpus и печатает сводку по объёму, источникам и дням.
- `demo/demo_run.py` выполняет линейный демо-прогон `запрос -> enrichment -> filter -> labeling`, пишет человекочитаемый trace в stdout и дублирует его в `demo/run_trace.log`.
- Это отдельный демонстрационный контур. Он не использует `agent_1.processing_jobs`, не запускает боевые воркеры и не пишет в схему `agent_1`.
