-- rework-agent-1-v5 / tasks.md 2.2
-- label_kr_worker.py and extract_semantics_worker.py were removed from
-- Agent 1's orchestration (preprocess_worker.py no longer enqueues
-- label_kr jobs). These tables stop receiving new writes as of this
-- change but are NOT dropped or truncated -- they remain the historical
-- silver dataset for the relevance/impact/entities/events labeling that
-- moves to future Agents 2/4. Purely documentation (COMMENT ON TABLE);
-- no schema or data change.

BEGIN;

SET search_path TO agent_1, public;

COMMENT ON TABLE document_kr_labels IS
  'READ-ONLY LEGACY (rework-agent-1-v5, 2026-07-20): label_kr_worker.py '
  'removed from Agent 1 orchestration, stops receiving new rows. Historical '
  'silver dataset for relevance/impact/tonality labeling -- future home is '
  'Agent 4. Not dropped; do not delete.';

COMMENT ON TABLE document_enrichments IS
  'READ-ONLY LEGACY (rework-agent-1-v5, 2026-07-20): extract_semantics_worker.py '
  'removed from Agent 1 orchestration, stops receiving new rows. Historical '
  'silver dataset for entities/events extraction -- future home is Agent 4 '
  '(GLiNER + driver layer). Not dropped; do not delete.';

COMMENT ON TABLE label_kr_step_checkpoints IS
  'READ-ONLY LEGACY (rework-agent-1-v5, 2026-07-20): checkpoints for the '
  'removed label_kr_worker.py steps. Stops receiving new rows. Not dropped.';

COMMENT ON TABLE extract_semantics_step_checkpoints IS
  'READ-ONLY LEGACY (rework-agent-1-v5, 2026-07-20): checkpoints for the '
  'removed extract_semantics_worker.py steps. Stops receiving new rows. '
  'Not dropped.';

COMMENT ON TABLE llm_call_logs IS
  'READ-ONLY LEGACY (rework-agent-1-v5, 2026-07-20): call log for the '
  'removed label_kr_worker.py / extract_semantics_worker.py. Stops '
  'receiving new rows. Not dropped.';

COMMIT;
