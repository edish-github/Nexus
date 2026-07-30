-- TABLE 1: incidents
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
    region              TEXT NOT NULL DEFAULT 'us-east-1',
    was_predicted       BOOLEAN NOT NULL DEFAULT false,
    was_prevented       BOOLEAN NOT NULL DEFAULT false,
    was_auto_resolved   BOOLEAN NOT NULL DEFAULT false,
    playbook_used       UUID,
    root_cause          TEXT,
    symptom_embedding   VECTOR(1024),
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at         TIMESTAMPTZ,
    mttr_seconds        INT,
    crdb_region         crdb_internal_region NOT NULL
        DEFAULT default_to_database_primary_region(gateway_region())
) LOCALITY REGIONAL BY ROW AS crdb_region;

CREATE VECTOR INDEX IF NOT EXISTS incidents_symptom_embedding_idx
    ON incidents (symptom_embedding vector_cosine_ops);

-- TABLE 2: precursor_snapshots
CREATE TABLE IF NOT EXISTS precursor_snapshots (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id           UUID REFERENCES incidents(id),
    service_name          TEXT NOT NULL,
    region                TEXT NOT NULL DEFAULT 'us-east-1',
    trajectory_embedding  VECTOR(1024) NOT NULL,
    window_start          TIMESTAMPTZ NOT NULL,
    window_end            TIMESTAMPTZ NOT NULL,
    outcome_category      TEXT NOT NULL,
    led_to_incident       BOOLEAN NOT NULL DEFAULT true,
    metric_digest         JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE VECTOR INDEX IF NOT EXISTS precursor_trajectory_embedding_idx
    ON precursor_snapshots (trajectory_embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS precursor_outcome_idx
    ON precursor_snapshots (outcome_category, led_to_incident);

-- TABLE 3: playbooks
CREATE TABLE IF NOT EXISTS playbooks (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL,
    outcome_category    TEXT NOT NULL,
    precursor_embedding VECTOR(1024) NOT NULL,
    remediation_steps   JSONB NOT NULL,
    inverse_steps       JSONB NOT NULL DEFAULT '[]'::JSONB,
    reversible          BOOLEAN NOT NULL DEFAULT true,
    -- selection counters posterior derived from success/failure
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
    expires_at          TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '90 days',
    crdb_region         crdb_internal_region NOT NULL
        DEFAULT default_to_database_primary_region(gateway_region())
) LOCALITY REGIONAL BY ROW AS crdb_region;

ALTER TABLE playbooks SET (ttl_expiration_expression = 'expires_at', ttl_job_cron = '0 * * * *');

CREATE VECTOR INDEX IF NOT EXISTS playbooks_precursor_embedding_idx
    ON playbooks (precursor_embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS playbooks_category_status_idx
    ON playbooks (outcome_category, status);

-- TABLE 4: institutional_playbooks
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

-- TABLE 5: predictions for changefeed
CREATE TABLE IF NOT EXISTS predictions (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_name             TEXT NOT NULL,
    causal_pattern           TEXT NOT NULL,
    predicted_outcome        TEXT NOT NULL,
    predicted_severity       INT NOT NULL CHECK (predicted_severity BETWEEN 1 AND 5),
    -- Beta posterior over matched precursors' outcomes
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

-- Idempotent claim (Sentinel): first writer wins via SELECT ... FOR UPDATE
CREATE UNIQUE INDEX IF NOT EXISTS predictions_active_dedup_idx
    ON predictions (service_name, predicted_outcome)
    WHERE prevention_status IN ('pending','preventing');

-- TABLE 6: telemetry_embeddings  (2h row-level TTL)
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

-- TABLE 7: evolution_log  (append-only audit trail)
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
