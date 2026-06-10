-- Galaxy Platform — Trace Ledger Schema
-- Run once against the PostgreSQL Flexible Server instance
-- Append-only — no UPDATE, no DELETE permissions granted to app user

CREATE TABLE IF NOT EXISTS trace_ledger (
    id              BIGSERIAL       PRIMARY KEY,
    run_id          TEXT            NOT NULL,
    module_id       TEXT            NOT NULL,
    agent_type      TEXT            NOT NULL,
    nhi_id          TEXT            NOT NULL,       -- Entra NHI client ID of the agent
    action          TEXT            NOT NULL,       -- "llm_call" | "file_read" | "decision" | "agent_start" | "agent_complete" | "escalation"
    input_summary   TEXT,                          -- first 200 chars of prompt (PII scrubbed, never full prompt)
    output_summary  TEXT,                          -- first 200 chars of response
    tokens_used     INTEGER         NOT NULL DEFAULT 0,
    attempt         INTEGER         NOT NULL,
    outcome         TEXT            NOT NULL,       -- "success" | "blocked" | "escalated" | "failed"
    entry_hash      TEXT            NOT NULL,       -- SHA-256 of this entry's content
    prev_hash       TEXT            NOT NULL,       -- hash of previous entry (chain link)
    recorded_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_trace_run_id   ON trace_ledger (run_id);
CREATE INDEX IF NOT EXISTS idx_trace_module   ON trace_ledger (run_id, module_id);
CREATE INDEX IF NOT EXISTS idx_trace_agent    ON trace_ledger (agent_type, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_trace_nhi      ON trace_ledger (nhi_id, recorded_at DESC);

-- App user — append only, no UPDATE or DELETE
-- Run as superuser:
-- CREATE USER galaxy_app WITH PASSWORD '...';   -- use Key Vault managed secret
-- GRANT SELECT, INSERT ON trace_ledger TO galaxy_app;
-- GRANT USAGE, SELECT ON SEQUENCE trace_ledger_id_seq TO galaxy_app;

-- Compliance queries
-- Full run trace:
--   SELECT * FROM trace_ledger WHERE run_id = 'run-xxx' ORDER BY id ASC;
--
-- Token cost by agent type:
--   SELECT agent_type, SUM(tokens_used) FROM trace_ledger WHERE run_id = 'run-xxx' GROUP BY agent_type;
--
-- Self-heal loop failures:
--   SELECT * FROM trace_ledger WHERE outcome = 'failed' AND recorded_at > NOW() - INTERVAL '24 hours';
--
-- All actions by a specific NHI:
--   SELECT * FROM trace_ledger WHERE nhi_id = 'xxxxxxxx-scanner-client-id' ORDER BY recorded_at DESC;
