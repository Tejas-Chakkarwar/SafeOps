UV ?= uv

.PHONY: sync test lint format cluster-up cluster-down cluster-test bootstrap-scope steady-state desired-state

sync:
	$(UV) sync

# Offline tests. They never touch a cluster.
test:
	$(UV) run pytest

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

# Bootstrap (decision 0006). The only entry points that may use the bootstrap phase.
cluster-up:
	$(UV) run python -m safeops.bootstrap up

cluster-down:
	$(UV) run python -m safeops.bootstrap down

# Re-render the pinned charts and rewrite config/bootstrap_scope.yaml. Review the diff,
# and amend decision 0006, before committing a change to it.
bootstrap-scope:
	$(UV) run python -m safeops.bootstrap render-scope

# Live-cluster tests (RBAC, harness path). They need `make cluster-up` first.
cluster-test:
	SAFEOPS_CLUSTER_TESTS=1 $(UV) run pytest tests/cluster -v

steady-state:
	$(UV) run safeops steady-state

# Regenerate config/desired_state.yaml from manifests/testbed + topology (decision 0013).
desired-state:
	$(UV) run python -m safeops.environment.desired write
