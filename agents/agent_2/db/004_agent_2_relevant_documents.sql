-- rework-agent-2-filter / tasks.md 10.2
-- Итог отбора агента 2: M:N объект↔документ (design D10). Только
-- документы, прошедшие порог 7.5 и негатив-фильтр — финальный результат,
-- не кандидаты. Никаких полей драйвера/упоминания/KR — вне контракта
-- этого агента (specs/agent_2/spec.md, «KR и драйверы — вне
-- ответственности агента 2»).

BEGIN;

SET search_path TO agent_1_v5, public;

CREATE TABLE agent_2_relevant_documents (
    id_object      BIGINT NOT NULL REFERENCES observation_objects (id_object),
    id_clean_post  BIGINT NOT NULL REFERENCES clean_posts (id_clean_post),
    llm_score      NUMERIC NOT NULL,
    selected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id_object, id_clean_post)
);

CREATE INDEX agent_2_relevant_documents_post_idx
ON agent_2_relevant_documents (id_clean_post);

COMMIT;