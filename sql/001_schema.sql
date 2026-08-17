-- Incidents: one row per detected or predicted failure, homed in the region
-- that observed it.
CREATE TABLE IF NOT EXISTS incidents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title               TEXT NOT NULL,
    severity            INT NOT NULL CHECK (severity BETWEEN 1 AND 5),
    status              TEXT NOT NULL DEFAULT 'predicted'
        CHECK (status IN (
            'predicted','detected','preventing','diagnosing',
            'healing','resolved','postmortem'
        )),
    affected_services   TEXT[] NOT NULL,
    was_predicted       BOOLEAN NOT NULL DEFAULT false,
    was_prevented       BOOLEAN NOT NULL DEFAULT false,
    was_auto_resolved   BOOLEAN NOT NULL DEFAULT false,
    playbook_used       UUID,
    root_cause          TEXT,
    symptom_embedding   VECTOR(1024),
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at         TIMESTAMPTZ,
    mttr_seconds        INT
) LOCALITY REGIONAL BY ROW;

CREATE VECTOR INDEX IF NOT EXISTS incidents_symptom_embedding_idx
    ON incidents (symptom_embedding vector_cosine_ops);

-- Precursor snapshots: the trailing telemetry window before an incident,
-- embedded for k-NN matching. Negative rows (led_to_incident = false) are the
-- windows that recovered on their own.
CREATE TABLE IF NOT EXISTS precursor_snapshots (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id           UUID REFERENCES incidents(id),
    service_name          TEXT NOT NULL,
    region                TEXT NOT NULL DEFAULT 'aws-us-east-1',
    trajectory_embedding  VECTOR(1024) NOT NULL,
    window_start          TIMESTAMPTZ NOT NULL,
    window_end            TIMESTAMPTZ NOT NULL,
    outcome_category      TEXT NOT NULL,
    led_to_incident       BOOLEAN NOT NULL DEFAULT true,
    metric_digest         JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE VECTOR INDEX IF NOT EXISTS precursor_category_trajectory_idx
    ON precursor_snapshots (outcome_category, trajectory_embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS precursor_outcome_idx
    ON precursor_snapshots (outcome_category, led_to_incident);

-- Playbooks: the evolving memory. Fitness is never stored as a float; it is
-- derived from the Beta(success_count + 1, failure_count + 1) posterior.
CREATE TABLE IF NOT EXISTS playbooks (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL,
    outcome_category    TEXT NOT NULL,
    precursor_embedding VECTOR(1024) NOT NULL,
    remediation_steps   JSONB NOT NULL,
    inverse_steps       JSONB NOT NULL DEFAULT '[]'::JSONB,
    reversible          BOOLEAN NOT NULL DEFAULT true,
    -- Trial counters; the posterior is computed from these at selection time.
    success_count       INT NOT NULL DEFAULT 0,
    failure_count       INT NOT NULL DEFAULT 0,
    generation          INT NOT NULL DEFAULT 1,
    parent_id           UUID REFERENCES playbooks(id),
    lineage             UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    memory_tier         TEXT NOT NULL DEFAULT 'operational'
        CHECK (memory_tier IN ('experimental','operational','institutional','retired')),
    -- Lifecycle
    status              TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','retired','merged')),
    promoted_at         TIMESTAMPTZ,
    retired_at          TIMESTAMPTZ,
    last_used_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 90-day disuse TTL: bump expires_at = now() + 90d each time a playbook is used
    expires_at          TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '90 days'
) LOCALITY REGIONAL BY ROW;

ALTER TABLE playbooks SET (ttl_expiration_expression = 'expires_at', ttl_job_cron = '0 * * * *');

CREATE VECTOR INDEX IF NOT EXISTS playbooks_precursor_embedding_idx
    ON playbooks (precursor_embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS playbooks_category_status_idx
    ON playbooks (outcome_category, status);

-- Institutional playbooks: proven strategies promoted to a GLOBAL table so
-- every region reads them locally.
CREATE TABLE IF NOT EXISTS institutional_playbooks (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_playbook_id  UUID NOT NULL REFERENCES playbooks(id),
    name                TEXT NOT NULL,
    outcome_category    TEXT NOT NULL,
    precursor_embedding VECTOR(1024) NOT NULL,
    remediation_steps   JSONB NOT NULL,
    inverse_steps       JSONB NOT NULL DEFAULT '[]'::JSONB,
    reversible          BOOLEAN NOT NULL DEFAULT true,
    success_count       INT NOT NULL DEFAULT 0,
    failure_count       INT NOT NULL DEFAULT 0,
    generation          INT NOT NULL DEFAULT 1,
    lineage             UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    promoted_at         TIMESTAMPTZ NOT NULL DEFAULT now()
) LOCALITY GLOBAL;

CREATE VECTOR INDEX IF NOT EXISTS institutional_precursor_embedding_idx
    ON institutional_playbooks (precursor_embedding vector_cosine_ops);

-- Predictions: the changefeed source table. An INSERT here is what drives the
-- webhook into the Step Functions pipeline.
CREATE TABLE IF NOT EXISTS predictions (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_name             TEXT NOT NULL,
    causal_pattern           TEXT NOT NULL,
    predicted_outcome        TEXT NOT NULL,
    predicted_severity       INT NOT NULL CHECK (predicted_severity BETWEEN 1 AND 5),
    -- Beta posterior over the matched precursors' outcomes, stored as its two
    -- parameters so the credible interval can be recomputed downstream.
    alpha                    FLOAT NOT NULL DEFAULT 1.0,
    beta                     FLOAT NOT NULL DEFAULT 1.0,
    matching_precursor_count INT NOT NULL DEFAULT 0,
    current_embedding        VECTOR(1024) NOT NULL,
    predicted_eta            TIMESTAMPTZ,
    prevention_status        TEXT NOT NULL DEFAULT 'pending'
        CHECK (prevention_status IN (
            'pending','preventing','prevented','missed','false_alarm','shadowed'
        )),
    claimed_by               TEXT,
    claimed_at               TIMESTAMPTZ,
    playbook_applied         UUID REFERENCES playbooks(id),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at              TIMESTAMPTZ,
    expires_at               TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '6 hours'
) WITH (ttl_expiration_expression = 'expires_at', ttl_job_cron = '*/10 * * * *');

CREATE INDEX IF NOT EXISTS predictions_status_created_idx
    ON predictions (prevention_status, created_at);

-- Dedup guard for Oracle: at most one open prediction per service and
-- category. Sentinel's claim is a separate SELECT ... FOR UPDATE.
CREATE UNIQUE INDEX IF NOT EXISTS predictions_active_dedup_idx
    ON predictions (service_name, predicted_outcome)
    WHERE prevention_status IN ('pending','preventing');

-- Telemetry embeddings: the sensory tier. Rows expire after 2 hours; only the
-- windows that precede an incident are promoted into precursor_snapshots.
CREATE TABLE IF NOT EXISTS telemetry_embeddings (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_name  TEXT NOT NULL,
    region        TEXT NOT NULL,
    metric_type   TEXT NOT NULL,
    embedding     VECTOR(1024) NOT NULL,
    raw_metrics   JSONB,
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '2 hours'
) WITH (ttl_expiration_expression = 'expires_at', ttl_job_cron = '*/5 * * * *');

CREATE VECTOR INDEX IF NOT EXISTS telemetry_embedding_idx
    ON telemetry_embeddings (embedding vector_cosine_ops);

-- Evolution log: append-only record of every playbook lifecycle transition.
-- No lifecycle mutation is committed without its row here.
CREATE TABLE IF NOT EXISTS evolution_log (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type          TEXT NOT NULL
        CHECK (event_type IN (
            'birth','growth','mutation','competition',
            'merge','promotion','retirement','rollback'
        )),
    playbook_id         UUID REFERENCES playbooks(id),
    parent_id           UUID REFERENCES playbooks(id),
    trigger_incident_id UUID REFERENCES incidents(id),
    fitness_before      FLOAT,   -- posterior mean before the transition
    fitness_after       FLOAT,   -- posterior mean after
    details             JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS evolution_playbook_created_idx
    ON evolution_log (playbook_id, created_at);
