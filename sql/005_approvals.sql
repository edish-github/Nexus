-- Approval requests: the human-in-the-loop tier.
--
-- When Oracle is confident but the winning playbook is irreversible — rotating a
-- certificate, pruning a disk — Sentinel does not execute. It writes a row here
-- with the evidence bundle a human needs to decide, and the prediction stays in
-- `preventing` until someone answers. The dashboard's approval card reads and
-- writes exactly this table.
--
-- `evidence` carries the whole bundle so the card can be rendered, and the
-- decision audited, without re-running any of Sentinel's queries: the matched
-- precursors, the competition draws, the posterior, and the provenance
-- timestamp the evidence can be replayed at.
--
-- A request that nobody answers must not pin a prediction in `preventing`
-- forever, so rows carry a deadline and a 24-hour Row-Level TTL reaps them once
-- they are long dead. The sweeper that acts on `expires_at` is Phase 7.

CREATE TABLE IF NOT EXISTS approvals (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prediction_id    UUID NOT NULL REFERENCES predictions(id) ON DELETE CASCADE,
    playbook_id      UUID NOT NULL REFERENCES playbooks(id),
    service_name     TEXT NOT NULL,
    outcome_category TEXT NOT NULL,
    -- Why a human is being asked at all: which step has no inverse.
    reason           TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected','expired')),
    evidence         JSONB NOT NULL DEFAULT '{}'::JSONB,
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at       TIMESTAMPTZ,
    decided_by       TEXT,
    -- The decision deadline, not the row's lifetime.
    deadline         TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '30 minutes',
    expires_at       TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '24 hours'
) WITH (ttl_expiration_expression = 'expires_at', ttl_job_cron = '0 * * * *');

CREATE INDEX IF NOT EXISTS approvals_status_requested_idx
    ON approvals (status, requested_at);

-- At most one open approval per prediction: a duplicate changefeed delivery must
-- not raise the same question twice.
CREATE UNIQUE INDEX IF NOT EXISTS approvals_open_per_prediction_idx
    ON approvals (prediction_id)
    WHERE status = 'pending';
