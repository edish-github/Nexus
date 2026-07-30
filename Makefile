# NEXUS — one-command operations. Run from the repo root.
# Requires: aws-sam-cli, awscli, uv (or pip), and a populated .env

.PHONY: help deps build deploy destroy migrate migrate-dry seed-smoke changefeed \
        secrets outputs logs-receiver lint fmt

STACK      ?= nexus
INFRA      := infra
AWS_REGION ?= us-east-1

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

deps: ## Install local Python dependencies
	uv sync

lint: ## Ruff lint
	uv run ruff check .

fmt: ## Ruff format
	uv run ruff format .

migrate: ## Apply pending SQL migrations
	uv run --no-project python scripts/migrate.py

migrate-dry: ## Show pending migrations
	uv run --no-project python scripts/migrate.py --dry-run

seed-smoke: ## Seed demo data and run smoke test
	uv run --no-project python scripts/smoke_test.py

changefeed: ## Create predictions changefeed
	uv run --no-project python scripts/migrate.py --file sql/changefeed.sql

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
