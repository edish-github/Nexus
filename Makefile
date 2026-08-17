# NEXUS — one-command operations. Run from the repo root.
# Requires: aws-sam-cli, awscli, uv (or pip), and a populated .env

.PHONY: help deps build deploy destroy migrate migrate-dry seed seed-dry seed-smoke \
        verify verify-full demo-reset live test changefeed dashboard ui ui-build \
        secrets outputs logs-receiver lint fmt

STACK      ?= nexus
# Bound every dashboard read, so one blocked table degrades a panel rather than
# hanging the request.
DB_STATEMENT_TIMEOUT_MS ?= 15000
INFRA      := infra
AWS_REGION ?= us-east-1
# bedrock | local | auto — `auto` uses Titan when AWS credentials resolve and the
# deterministic local embedder otherwise. The two are different vector spaces, so
# changing this means re-running `make seed`.
EMBEDDING_PROVIDER ?= auto
export EMBEDDING_PROVIDER

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

deps: ## Install local Python dependencies
	uv sync

lint: ## Ruff lint
	uv run ruff check .

fmt: ## Ruff format
	uv run ruff format .

test: ## Run the unit tests (no database required)
	uv run python -m pytest

migrate: ## Apply pending SQL migrations
	uv run python scripts/migrate.py

migrate-dry: ## Show pending migrations
	uv run python scripts/migrate.py --dry-run

seed: migrate ## Build the whole demo world from cold: migrate, then seed
	uv run python scripts/seed.py

seed-dry: ## Build the world in memory and report it, writing nothing
	uv run python scripts/seed.py --dry-run

demo-reset: seed ## Restore the exact seeded demo world (alias of `seed`)

verify: ## Phase 2 exit gate: index, retrieval, provenance replay, world integrity
	uv run python scripts/verify_phase2.py --load-rows 0

verify-full: ## As `verify`, plus the 10k-row load check and the TTL reap check
	uv run python scripts/verify_phase2.py --load-rows 10000 --ttl-check

live: ## Run the synthetic fleet with the ramp control API on :8000
	uv run python -m generator.live

dashboard: ## Serve the dashboard read API locally on :8787 (same handler as the Lambda)
	DB_STATEMENT_TIMEOUT_MS=$(DB_STATEMENT_TIMEOUT_MS) uv run python scripts/dashboard_local.py

ui: ## Run the dashboard against $VITE_API_BASE_URL (see frontend/.env)
	cd frontend && npm install && npm run dev

ui-build: ## Build the deployable static bundle into frontend/dist
	cd frontend && npm install && npm run build

seed-smoke: ## Schema smoke test (throwaway rows, hybrid vector query, AOST)
	uv run python scripts/smoke_test.py

changefeed: ## Create predictions changefeed
	uv run python scripts/migrate.py --file sql/changefeed.sql

# AWS stack (SAM)
build:  ## sam build (container build for native deps)
	cd $(INFRA) && sam build

deploy: build  ## Deploy/update the whole stack (one command)
	cd $(INFRA) && sam deploy

destroy:  ## Tear down the whole stack
	cd $(INFRA) && sam delete --stack-name $(STACK)

outputs:  ## Print stack outputs (receiver URL, bus, state machine, bucket)
	aws cloudformation describe-stacks --stack-name $(STACK) \
	  --query 'Stacks[0].Outputs' --output table --region $(AWS_REGION)

secrets:  ## Reminder: how to populate the placeholder secrets
	@echo "aws secretsmanager put-secret-value --secret-id nexus/db \\"
	@echo "  --secret-string '{\"dsn\":\"postgresql://...\"}' --region $(AWS_REGION)"
	@echo "aws secretsmanager put-secret-value --secret-id nexus/changefeed \\"
	@echo "  --secret-string '{\"shared_secret\":\"<random>\"}' --region $(AWS_REGION)"
	@echo "aws secretsmanager put-secret-value --secret-id nexus/ccloud \\"
	@echo "  --secret-string '{\"api_key\":\"<ccloud-ro-key>\"}' --region $(AWS_REGION)"

logs-receiver:  ## Tail the changefeed receiver logs
	cd $(INFRA) && sam logs -n ReceiverFunction --stack-name $(STACK) --tail
