BEGIN;

CREATE TABLE IF NOT EXISTS agent_1.label_kr_step_checkpoints (
    clean_item_id BIGINT NOT NULL REFERENCES agent_1.clean_items(id) ON DELETE CASCADE,
    kr_id BIGINT NOT NULL REFERENCES agent_1.key_results(id) ON DELETE CASCADE,
    step_name TEXT NOT NULL CHECK (
        step_name IN ('impact', 'sber_paid_news', 'entity_tonality')
    ),
    status TEXT NOT NULL DEFAULT 'done' CHECK (status = 'done'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    raw_output TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (clean_item_id, kr_id, step_name)
);

CREATE INDEX IF NOT EXISTS label_kr_step_checkpoints_lookup_idx
ON agent_1.label_kr_step_checkpoints (clean_item_id, kr_id, step_name, status);


CREATE TABLE IF NOT EXISTS agent_1.llm_call_logs (
    id BIGSERIAL PRIMARY KEY,
    worker TEXT NOT NULL,
    clean_item_id BIGINT REFERENCES agent_1.clean_items(id) ON DELETE SET NULL,
    kr_id BIGINT REFERENCES agent_1.key_results(id) ON DELETE SET NULL,
    step_name TEXT NOT NULL,
    job_id BIGINT,
    model TEXT,
    session_key TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    success BOOLEAN NOT NULL,
    prompt_chars INTEGER NOT NULL CHECK (prompt_chars >= 0),
    output_chars INTEGER CHECK (output_chars IS NULL OR output_chars >= 0),
    usage JSONB CHECK (usage IS NULL OR jsonb_typeof(usage) = 'object'),
    error TEXT
);

CREATE INDEX IF NOT EXISTS llm_call_logs_lookup_idx
ON agent_1.llm_call_logs (worker, clean_item_id, kr_id, step_name, started_at);

COMMIT;
