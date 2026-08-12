-- Retain 7 days of MVCC history on the two tables the provenance replay reads,
-- so an `AS OF SYSTEM TIME <commit ts>` query still resolves after garbage
-- collection would otherwise have discarded the old versions.

ALTER TABLE precursor_snapshots CONFIGURE ZONE USING gc.ttlseconds = 604800;

ALTER TABLE predictions          CONFIGURE ZONE USING gc.ttlseconds = 604800;
