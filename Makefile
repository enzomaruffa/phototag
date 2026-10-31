WORKERS ?= 4

.DEFAULT_GOAL := help
.PHONY: help setup card import stacks stacks-export stacks-merge drive-export process watch retry review upload sync-hashes status failed doctor fmt lint typecheck test check

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup: ## Install dependencies
	uv sync

card: ## SD card → inbox → dedup → AI → tag review → Immich upload, in one go. ALBUM="name" optional
	-uv run phototag immich-sync
	@mkdir -p .phototag; uv run phototag receive --no-qr > .phototag/receive.log 2>&1 & pid=$$!; \
	trap 'kill $$pid 2>/dev/null' EXIT; \
	echo "📡 Receiving manifests from Open Alpha Link while the card copies (log: .phototag/receive.log)"; \
	uv run phototag import-card && uv run phototag stacks match
	uv run phototag process --workers $(WORKERS)
	uv run phototag review-tags
	uv run phototag upload $(if $(ALBUM),--album "$(ALBUM)")

.PHONY: receive
receive: ## Accept manifests pushed by Open Alpha Link on the local network (prints the pairing QR)
	uv run phototag receive

import: ## Copy new photos/videos from an SD card into the inbox (asks which card)
	uv run phototag import-card

stacks: ## Import Open Alpha Link sequence manifests and match them to inbox photos
	uv run phototag stacks match
	uv run phototag stacks list

stacks-export: ## Copy each stack into its own folder for focus-stacking software. OUT=dir KIND=focus_stack optional
	$(if $(OUT),,$(error OUT=dir is required))
	uv run phototag stacks export --out "$(OUT)" $(if $(KIND),--kind $(KIND))

stacks-merge: ## Merge each focus stack into one sharp image. OUT=dir ID=sequence INBOX=1 (tag and upload it too) FORCE=1 optional
	uv run phototag stacks merge $(if $(OUT),--out "$(OUT)") $(if $(ID),--id $(ID)) $(if $(INBOX),--to-inbox) $(if $(FORCE),--force)

drive-export: ## Finished photos as JPEGs in one folder per rating. OUT=dir SINCE/UNTIL="YYYY-MM-DD[ HH:MM]"
	uv run python scripts/drive_export.py $(if $(OUT),--out "$(OUT)") $(if $(SINCE),--since "$(SINCE)") $(if $(UNTIL),--until "$(UNTIL)")

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
	uv run ruff format phototag/ tests/

lint: ## Lint code
	uv run ruff check .

typecheck: ## Type-check code
	uv run ty check

test: ## Run the test suite
	uv run pytest -q

check: lint typecheck test ## Lint + type-check + tests
	uv run ruff format --check phototag/ tests/
