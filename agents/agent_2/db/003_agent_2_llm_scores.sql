-- rework-agent-2-filter / tasks.md 10.1
-- Кэш решающей LLM-оценки (design D6): ключ ОБЯЗАН включать модель —
-- грабля прототипа: кэш без модели в ключе молча переиспользует чужие
-- оценки. См. openspec/changes/rework-agent-2-filter/design.md, D10.

BEGIN;

SET search_path TO agent_1_v5, public;

CREATE TABLE agent_2_llm_scores (
    id_object      BIGINT NOT NULL REFERENCES observation_objects (id_object),
    id_clean_post  BIGINT NOT NULL REFERENCES clean_posts (id_clean_post),
    model          TEXT NOT NULL,
    score          NUMERIC NOT NULL,
    scored_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id_object, id_clean_post, model)
);

COMMIT;