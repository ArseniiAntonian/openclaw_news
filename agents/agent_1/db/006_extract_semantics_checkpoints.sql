BEGIN;

CREATE TABLE IF NOT EXISTS agent_1.extract_semantics_step_checkpoints (
    clean_item_id BIGINT NOT NULL REFERENCES agent_1.clean_items(id) ON DELETE CASCADE,
    step_name TEXT NOT NULL CHECK (step_name IN ('entities', 'events')),
    status TEXT NOT NULL DEFAULT 'done' CHECK (status = 'done'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    raw_output TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (clean_item_id, step_name)
);

CREATE INDEX IF NOT EXISTS extract_semantics_step_checkpoints_lookup_idx
ON agent_1.extract_semantics_step_checkpoints (clean_item_id, step_name, status);

COMMIT;
