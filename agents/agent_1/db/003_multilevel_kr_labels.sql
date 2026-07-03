BEGIN;

ALTER TABLE agent_1.document_kr_labels
    ADD COLUMN IF NOT EXISTS impact TEXT,
    ADD COLUMN IF NOT EXISTS signal_strength TEXT,
    ADD COLUMN IF NOT EXISTS theme TEXT,
    ADD COLUMN IF NOT EXISTS dashboard_description TEXT,
    ADD COLUMN IF NOT EXISTS why_for_goal TEXT,
    ADD COLUMN IF NOT EXISTS reasoning_steps JSONB,
    ADD COLUMN IF NOT EXISTS uncertainty TEXT,
    ADD COLUMN IF NOT EXISTS confidence NUMERIC(2,1),
    ADD COLUMN IF NOT EXISTS is_sber_paid_news SMALLINT,
    ADD COLUMN IF NOT EXISTS prompt1_payload JSONB,
    ADD COLUMN IF NOT EXISTS prompt2_payload JSONB,
    ADD COLUMN IF NOT EXISTS prompt3_payload JSONB;

UPDATE agent_1.document_kr_labels
SET
    impact = COALESCE(impact, 'positive'),
    signal_strength = COALESCE(signal_strength, 'direct'),
    theme = COALESCE(theme, 'legacy'),
    dashboard_description = COALESCE(dashboard_description, 'Legacy positive KR label.'),
    why_for_goal = COALESCE(why_for_goal, 'Legacy label created before multi-level prompts.'),
    reasoning_steps = COALESCE(reasoning_steps, '[]'::jsonb),
    uncertainty = COALESCE(uncertainty, 'Legacy label does not store uncertainty.'),
    confidence = COALESCE(confidence, 0.5),
    prompt1_payload = COALESCE(
        prompt1_payload,
        jsonb_build_object(
            'legacy', true,
            'impact', 'positive',
            'evidence', to_jsonb(evidence)
        )
    );

ALTER TABLE agent_1.document_kr_labels
    ALTER COLUMN impact SET NOT NULL,
    ALTER COLUMN signal_strength SET NOT NULL,
    ALTER COLUMN theme SET NOT NULL,
    ALTER COLUMN dashboard_description SET NOT NULL,
    ALTER COLUMN why_for_goal SET NOT NULL,
    ALTER COLUMN reasoning_steps SET NOT NULL,
    ALTER COLUMN uncertainty SET NOT NULL,
    ALTER COLUMN confidence SET NOT NULL,
    ALTER COLUMN prompt1_payload SET NOT NULL;

ALTER TABLE agent_1.document_kr_labels
    DROP CONSTRAINT IF EXISTS document_kr_labels_impact_check,
    DROP CONSTRAINT IF EXISTS document_kr_labels_signal_strength_check,
    DROP CONSTRAINT IF EXISTS document_kr_labels_theme_check,
    DROP CONSTRAINT IF EXISTS document_kr_labels_dashboard_description_check,
    DROP CONSTRAINT IF EXISTS document_kr_labels_why_for_goal_check,
    DROP CONSTRAINT IF EXISTS document_kr_labels_reasoning_steps_check,
    DROP CONSTRAINT IF EXISTS document_kr_labels_uncertainty_check,
    DROP CONSTRAINT IF EXISTS document_kr_labels_confidence_check,
    DROP CONSTRAINT IF EXISTS document_kr_labels_is_sber_paid_news_check,
    DROP CONSTRAINT IF EXISTS document_kr_labels_prompt1_payload_check,
    DROP CONSTRAINT IF EXISTS document_kr_labels_prompt2_payload_check,
    DROP CONSTRAINT IF EXISTS document_kr_labels_prompt3_payload_check;

ALTER TABLE agent_1.document_kr_labels
    ADD CONSTRAINT document_kr_labels_impact_check
        CHECK (impact IN ('positive', 'negative', 'neutral')),
    ADD CONSTRAINT document_kr_labels_signal_strength_check
        CHECK (signal_strength IN ('direct', 'indirect')),
    ADD CONSTRAINT document_kr_labels_theme_check
        CHECK (btrim(theme) <> ''),
    ADD CONSTRAINT document_kr_labels_dashboard_description_check
        CHECK (btrim(dashboard_description) <> ''),
    ADD CONSTRAINT document_kr_labels_why_for_goal_check
        CHECK (btrim(why_for_goal) <> ''),
    ADD CONSTRAINT document_kr_labels_reasoning_steps_check
        CHECK (jsonb_typeof(reasoning_steps) = 'array'),
    ADD CONSTRAINT document_kr_labels_uncertainty_check
        CHECK (btrim(uncertainty) <> ''),
    ADD CONSTRAINT document_kr_labels_confidence_check
        CHECK (confidence IN (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)),
    ADD CONSTRAINT document_kr_labels_is_sber_paid_news_check
        CHECK (is_sber_paid_news IN (0, 1)),
    ADD CONSTRAINT document_kr_labels_prompt1_payload_check
        CHECK (jsonb_typeof(prompt1_payload) = 'object'),
    ADD CONSTRAINT document_kr_labels_prompt2_payload_check
        CHECK (prompt2_payload IS NULL OR jsonb_typeof(prompt2_payload) = 'object'),
    ADD CONSTRAINT document_kr_labels_prompt3_payload_check
        CHECK (prompt3_payload IS NULL OR jsonb_typeof(prompt3_payload) = 'object');

COMMIT;
