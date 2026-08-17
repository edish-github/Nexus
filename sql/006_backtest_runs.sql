-- Backtest runs: the honesty layer, stored rather than recomputed.
--
-- The numbers the dashboard shows have to come from somewhere defensible. A
-- leave-one-out sweep over the rows already in `precursor_snapshots` is cheap
-- and in-sample: every window is scored against a memory that was built from
-- windows exactly like it, and the result flatters itself.
--
-- The real backtest scores the held-out set in `demo/backtest_set.jsonl` —
-- windows the seeder deliberately never wrote to the database — by embedding
-- each one and running Oracle's own retrieval and emit gate against it. That is
-- out-of-sample, and it measures the whole decision rather than the posterior
-- alone: a window Oracle declines to predict on counts as a negative, because
-- in production that is exactly what silence means.
--
-- It cannot run inside the read API. It needs the embedder and the held-out
-- file, neither of which a dashboard Lambda has, so `scripts/backtest.py` runs
-- it and writes the result here. The dashboard reads the newest row.
--
-- `LOCALITY GLOBAL`: one row every few days, read by every region on every
-- dashboard load. That is the exact access pattern GLOBAL exists for.

CREATE TABLE IF NOT EXISTS backtest_runs (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- How the numbers were produced, so a reader never has to guess whether
    -- they are in-sample.
    method             TEXT NOT NULL,
    k                  INT NOT NULL,
    min_similarity     FLOAT NOT NULL,
    min_matches        INT NOT NULL,
    emit_threshold     FLOAT NOT NULL,
    embedding_provider TEXT NOT NULL,
    -- The confusion matrix, and the corpus it was measured against.
    sample_size        INT NOT NULL,
    memory_size        INT NOT NULL,
    true_positive      INT NOT NULL,
    false_positive     INT NOT NULL,
    false_negative     INT NOT NULL,
    true_negative      INT NOT NULL,
    precision          FLOAT,
    recall             FLOAT,
    median_lead_minutes FLOAT,
    -- Oracle's stated ETA on a held-out window, whose true remaining time is 0.
    median_eta_minutes FLOAT,
    -- Confidence buckets: stated posterior versus realized rate. This is the
    -- calibration check, kept as data so the chart is never a mock.
    calibration        JSONB NOT NULL DEFAULT '[]'::JSONB,
    -- Every scored window, so a disputed number can be traced to the row that
    -- produced it rather than argued about.
    detail             JSONB NOT NULL DEFAULT '[]'::JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
) LOCALITY GLOBAL;

CREATE INDEX IF NOT EXISTS backtest_runs_created_idx
    ON backtest_runs (created_at DESC);
