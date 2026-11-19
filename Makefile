UV ?= uv

.PHONY: sync test lint format cluster-up cluster-down

sync:
	$(UV) sync

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

# Unimplemented targets fail. A target that succeeds without doing anything is the
# same class of lie as a stub that returns True (decision 0004).
cluster-up:
	@echo "make cluster-up: not implemented until Milestone 2 (testbed + topology + workload generator)." >&2
	@exit 1

cluster-down:
	@echo "make cluster-down: not implemented until Milestone 2 (testbed + topology + workload generator)." >&2
	@exit 1
