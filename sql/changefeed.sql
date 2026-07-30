-- Replace {RECEIVER_URL} with the receiver Function URL and {SHARED_SECRET}
-- with the value stored in the nexus/changefeed secret before running.

CREATE CHANGEFEED FOR TABLE predictions
  INTO 'webhook-{RECEIVER_URL}?insecure_tls_skip_verify=false'
  WITH updated,
       resolved = '10s',
       min_checkpoint_frequency = '10s',
       webhook_auth_header = 'Bearer {SHARED_SECRET}',
       webhook_sink_config = '{"Flush":{"Bytes":1048576,"Frequency":"1s"},"Retry":{"Max":3}}';
