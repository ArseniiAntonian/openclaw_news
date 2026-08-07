-- rework-agent-2-filter / tasks.md 3.1
-- Каталог объектов наблюдения. Живёт в СУЩЕСТВУЮЩЕЙ схеме agent_1_v5
-- (решение пользователя 2026-08-06: "всё в одном месте, так нагляднее") —
-- эта миграция НЕ создаёт схему/расширение, они уже есть
-- (agents/agent_1/db/v5/000_bootstrap_schema_v5.sql).
--
-- См. openspec/changes/rework-agent-2-filter/design.md, D10.

BEGIN;

SET search_path TO agent_1_v5, public;

CREATE TABLE observation_objects (
    id_object          BIGSERIAL PRIMARY KEY,
    label              TEXT NOT NULL,
    aliases            TEXT[] NOT NULL DEFAULT '{}',
    keywords           TEXT,           -- regex, лексический канал включения
    negative_filter    TEXT,           -- regex, вето после порога
    source_weights     JSONB,
    search_description TEXT NOT NULL,  -- обязательное поле, источник текста
                                        -- для query_embedding (design D2)
    query_embedding    VECTOR(1024),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT observation_objects_search_description_not_blank
        CHECK (btrim(search_description) <> '')
);

COMMIT;