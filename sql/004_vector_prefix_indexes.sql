-- Prefixed vector indexes for the two hybrid SQL + vector queries.
--
-- The retrieval queries NEXUS actually runs are not pure nearest-neighbour
-- searches; they are filtered ones:
--
--   Oracle:   WHERE outcome_category = $1            ORDER BY embedding <=> $2
--   Sentinel: WHERE outcome_category = $1 AND status = 'active'
--                                             ORDER BY embedding <=> $2
--
-- Against the unprefixed vector indexes in 001_schema.sql the planner cannot
-- combine the two: it picks the secondary index on the filter columns, does an
-- index join and sorts the survivors, and the vector index goes unused. That is
-- correct but it is a scan, and it stops being acceptable as the memory grows.
--
-- CockroachDB vector indexes accept prefix columns before the vector column,
-- which partitions the index by those columns and lets one lookup serve both
-- halves of the query. `EXPLAIN` then shows a `vector search` node with
-- `prefix spans` — that is the shape to look for.
--
-- Note the tradeoff this leaves open. A prefixed index cannot serve a query that
-- supplies no prefix value, and Oracle's neighbourhood query deliberately has no
-- category filter — the category is what it is trying to work out, so filtering
-- by it would assume the conclusion. With `precursor_trajectory_embedding_idx`
-- (the unprefixed index originally declared in 001) removed, that query falls
-- back to a scan and sort. It is invisible at a few hundred snapshots and will
-- not stay invisible as the memory grows; restoring the unprefixed index is the
-- fix, at the cost of a second vector index to maintain on every write.
--
-- Each of these is an index backfill and runs as an asynchronous schema-change
-- job; on a seeded cluster expect a couple of minutes per index.

CREATE VECTOR INDEX IF NOT EXISTS precursor_category_trajectory_idx
    ON precursor_snapshots (outcome_category, trajectory_embedding vector_cosine_ops);

CREATE VECTOR INDEX IF NOT EXISTS playbooks_category_status_precursor_idx
    ON playbooks (outcome_category, status, precursor_embedding vector_cosine_ops);
