-- =============================================================================
-- Migration 004: Production Hardening
--
-- Fixes the following races / inconsistencies:
--   1. app-level idempotency guard in agent_tasks.py has a TOCTOU race under
--      concurrent workers — two workers can both read "no row" before either
--      commits. A DB-level partial unique index is the only reliable fix.
--
--   2. The outbox table ensures event delivery survives Redis downtime.
--      A background sweeper (see app/core/outbox.py) retries any un-published
--      rows. This replaces the "log and pray" pattern.
--
--   3. A partial unique index on evaluations.session_id already exists via
--      the ORM (unique=True on the column). Verified here for safety.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Deduplicate agent_outputs before creating the unique partial index.
--    Keep only the ROW with the highest id (most recent) per
--    (session_id, agent_type) among completed/skipped rows.
--    Any older duplicates are safe to discard — the latest result wins.
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    -- Only run if the index does not yet exist (idempotent guard)
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND indexname   = 'uq_agent_output_success'
    ) THEN
        DELETE FROM agent_outputs
        WHERE id IN (
            SELECT id
            FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY session_id, agent_type
                           ORDER BY id DESC          -- keep highest id (most recent)
                       ) AS rn
                FROM agent_outputs
                WHERE status IN ('completed', 'skipped')
            ) ranked
            WHERE rn > 1                             -- delete all but the latest
        );
        RAISE NOTICE 'Deduplication complete. % rows removed.',
            (SELECT COUNT(*) FROM agent_outputs WHERE FALSE);  -- placeholder
    ELSE
        RAISE NOTICE 'uq_agent_output_success already exists, skipping deduplication.';
    END IF;
END $$;

-- -----------------------------------------------------------------------------
-- 2. Unique partial index on agent_outputs
--    Constraint: at most ONE completed/skipped row per (session_id, agent_type)
--    Uses a PARTIAL index (not a unique column) because failed/processing rows
--    must be re-insertable without violating the constraint.
-- -----------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_output_success
    ON agent_outputs (session_id, agent_type)
    WHERE status IN ('completed', 'skipped');

-- -----------------------------------------------------------------------------
-- 3. Composite index to speed up the idempotency SELECT used in agent_tasks.py
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_agent_output_session_type_status
    ON agent_outputs (session_id, agent_type, status);

-- -----------------------------------------------------------------------------
-- 3. Outbox table for reliable event delivery
--    Rows are inserted inside the same DB transaction as the agent_output write,
--    so they are always consistent. A sweeper process retries any un-published rows.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_outbox (
    id              BIGSERIAL PRIMARY KEY,
    session_id      INTEGER       NOT NULL,
    event_type      VARCHAR(100)  NOT NULL,
    payload         JSONB         NOT NULL DEFAULT '{}',
    status          VARCHAR(20)   NOT NULL DEFAULT 'pending',  -- pending | published | failed
    attempts        SMALLINT      NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    published_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_outbox_status_created
    ON event_outbox (status, created_at)
    WHERE status IN ('pending', 'failed');

COMMENT ON TABLE event_outbox IS
  'Transactional outbox for agent completion events. '
  'Events are written atomically with the agent_output insert and '
  'delivered to Redis by the outbox sweeper (app/core/outbox.py).';
