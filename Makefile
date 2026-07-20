WORKERS ?= 4

.DEFAULT_GOAL := help
.PHONY: help setup process watch retry review upload sync-hashes status failed doctor fmt lint typecheck check

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup: ## Install dependencies
	uv sync

process: ## Analyze inbox photos with AI (videos pass straight through). WORKERS=4
	uv run phototag process --workers $(WORKERS)

watch: ## Watch inbox and auto-process new files as they arrive
	uv run phototag watch --workers $(WORKERS)

retry: ## Re-queue failed photos and process them again
	uv run phototag retry
	uv run phototag process --workers $(WORKERS)

review: ## Review pending AI-suggested tags
	uv run phototag review-tags

upload: ## Upload processed photos to Immich. ALBUM="name" optional
	uv run phototag upload $(if $(ALBUM),--album "$(ALBUM)")

sync-hashes: ## Pull Immich asset checksums so duplicates from any client are caught
	uv run phototag immich-sync

status: ## Show processing status
	uv run phototag status

failed: ## Show failed photo details
	uv run phototag status --failed

doctor: ## Fix database/disk drift (stuck or orphaned records)
	uv run phototag doctor

fmt: ## Format code
	uv run ruff format phototag/

lint: ## Lint code
	uv run ruff check .

typecheck: ## Type-check code
	uv run ty check

check: lint typecheck ## Lint + type-check
	uv run ruff format --check phototag/
