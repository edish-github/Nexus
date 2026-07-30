ALTER TABLE precursor_snapshots CONFIGURE ZONE USING gc.ttlseconds = 604800;

ALTER TABLE predictions          CONFIGURE ZONE USING gc.ttlseconds = 604800;
