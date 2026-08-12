-- Multi-region topology: aws-us-east-1 (primary), aws-eu-west-1, aws-ap-south-1.
-- Edit these three region names to match the cluster before running.
--
-- On CockroachDB Cloud both of the settings below are enabled by default and
-- cannot be set by a non-admin SQL user. Uncomment them only on a self-hosted
-- cluster, where the app user has the privilege:
--   SET CLUSTER SETTING feature.vector_index.enabled = true;
--   SET CLUSTER SETTING kv.rangefeed.enabled = true;

-- Configure the database primary region.
ALTER DATABASE nexus SET PRIMARY REGION "aws-us-east-1";

-- Register additional database regions.
ALTER DATABASE nexus ADD REGION IF NOT EXISTS "aws-eu-west-1";
ALTER DATABASE nexus ADD REGION IF NOT EXISTS "aws-ap-south-1";

-- Configure the database to tolerate the loss of a single region.
ALTER DATABASE nexus SURVIVE REGION FAILURE;
