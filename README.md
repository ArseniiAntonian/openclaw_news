# OpenClaw Workspace

Этот репозиторий лучше вести как `private`, если цель — руками отслеживать всю систему в одном месте.

Что обычно коммитить:
- `agents/` — код, инструкции и документацию агентов
- `skills/` — локальные skills
- `AGENTS.md`, `SOUL.md`, `TOOLS.md`, `USER.md`, `IDENTITY.md`, `HEARTBEAT.md` — корневые правила и операционные заметки
- `requirements.txt` и прочие безопасные конфиги

Что намеренно не коммитится через `.gitignore`:
- `.openclaw/`
- `memory/` и `MEMORY.md`
- `openclaw-workspace-state.json`
- `.env`
- `logs/`, `out/`
- локальные выгрузки `csv/gz/tsv`
- runtime-артефакты вроде `pid/log/out`

Если позже понадобится более чистое разделение, `agents/agent_1` можно вынести в отдельный репозиторий без ломки этой структуры.
