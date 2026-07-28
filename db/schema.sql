-- TABLE 1: Incidents (the event record)
CREATE TABLE incidents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    severity INT NOT NULL CHECK (severity BETWEEN 1 AND 5),
    status TEXT NOT NULL DEFAULT 'predicted'
        CHECK (status IN (
            'predicted','detected','preventing','diagnosing',
            'healing','resolved','postmortem'
        )),
    affected_services TEXT[] NOT NULL,
    region TEXT NOT NULL DEFAULT 'us-east-1',
    was_predicted BOOLEAN DEFAULT false,
    was_prevented BOOLEAN DEFAULT false,
    was_auto_resolved BOOLEAN DEFAULT false,
    playbook_used UUID,
    root_cause TEXT,
    symptom_embedding VECTOR(1536),
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    mttr_seconds INT,
    crdb_region crdb_internal_region NOT NULL DEFAULT default_to_database_primary_region(gateway_region())
) LOCALITY REGIONAL BY ROW;

CREATE VECTOR INDEX ON incidents (symptom_embedding)
    WITH (lists = 100, quantizer = rabitq);

-- TABLE 2: Playbooks (the evolving memory)
CREATE TABLE playbooks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    outcome_category TEXT NOT NULL,
    precursor_embedding VECTOR(1536) NOT NULL,
    remediation_steps JSONB NOT NULL,
    -- Darwinian fields
    fitness FLOAT NOT NULL DEFAULT 0.5,
    success_count INT DEFAULT 0,
    failure_count INT DEFAULT 0,
    generation INT DEFAULT 1,
    parent_id UUID REFERENCES playbooks(id),
    lineage UUID[] DEFAULT ARRAY[]::UUID[],
    memory_tier TEXT DEFAULT 'operational'
        CHECK (memory_tier IN ('experimental','operational','institutional','retired')),
    -- Lifecycle
    status TEXT DEFAULT 'active' CHECK (status IN ('active','retired','merged')),
    promoted_at TIMESTAMPTZ,
    retired_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- TTL: unused playbooks expire after 90 days
    expires_at TIMESTAMPTZ DEFAULT now() + INTERVAL '90 days'
) WITH (ttl_expiration_expression = 'expires_at', ttl_job_cron = '0 * * * *');

CREATE VECTOR INDEX ON playbooks (precursor_embedding)
    WITH (lists = 50, quantizer = rabitq);

-- TABLE 3: Telemetry Embeddings (sensory memory)
CREATE TABLE telemetry_embeddings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_name TEXT NOT NULL,
    region TEXT NOT NULL,
    metric_type TEXT NOT NULL,
    embedding VECTOR(1536) NOT NULL,
    raw_metrics JSONB,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '2 hours'
) WITH (ttl_expiration_expression = 'expires_at', ttl_job_cron = '*/5 * * * *');

CREATE VECTOR INDEX ON telemetry_embeddings (embedding)
    WITH (lists = 100, quantizer = rabitq);

-- TABLE 4: Predictions (Oracle output)
CREATE TABLE predictions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    causal_pattern TEXT NOT NULL,
    predicted_outcome TEXT NOT NULL,
    predicted_severity INT NOT NULL,
    confidence FLOAT NOT NULL,
    current_embedding VECTOR(1536) NOT NULL,
    matching_precursor_count INT,
    predicted_by TIMESTAMPTZ NOT NULL,
    prevention_status TEXT DEFAULT 'pending'
        CHECK (prevention_status IN (
            'pending','preventing','prevented','missed','false_alarm'
        )),
    playbook_applied UUID REFERENCES playbooks(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '6 hours'
) WITH (ttl_expiration_expression = 'expires_at', ttl_job_cron = '*/10 * * * *');

-- TABLE 5: Evolution Log (audit trail of memory changes)
CREATE TABLE evolution_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type TEXT NOT NULL
        CHECK (event_type IN (
            'birth','growth','mutation','competition',
            'merge','promotion','retirement','death'
        )),
    playbook_id UUID REFERENCES playbooks(id),
    parent_id UUID REFERENCES playbooks(id),
    details JSONB NOT NULL,
    fitness_before FLOAT,
    fitness_after FLOAT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
