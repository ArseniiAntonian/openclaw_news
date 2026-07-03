BEGIN;

CREATE OR REPLACE FUNCTION agent_1.enqueue_preprocess_job()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO agent_1.processing_jobs (
        job_type,
        entity_type,
        entity_id,
        status
    )
    VALUES (
        'preprocess',
        'raw_item',
        NEW.id,
        'pending'
    )
    ON CONFLICT (job_type, entity_type, entity_id)
    WHERE status IN ('pending', 'processing')
    DO NOTHING;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS raw_items_enqueue_preprocess_trg ON agent_1.raw_items;

CREATE TRIGGER raw_items_enqueue_preprocess_trg
AFTER INSERT ON agent_1.raw_items
FOR EACH ROW
EXECUTE FUNCTION agent_1.enqueue_preprocess_job();

COMMIT;
