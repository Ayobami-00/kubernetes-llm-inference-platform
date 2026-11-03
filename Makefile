UV ?= uv
PYTHON = $(UV) run --locked --group infrastructure python
OPERATOR = $(PYTHON) scripts/infrastructure/manage.py

.PHONY: infra-tools infra-policies infra-check infra-lock infra-profiles infra-bootstrap-plan infra-bootstrap-apply infra-plan infra-preview infra-apply infra-smoke infra-export infra-verify infra-dns-plan infra-dns-preview infra-dns-apply infra-destroy-plan infra-destroy

infra-tools:
	python3 scripts/infrastructure/install_tools.py

infra-policies:
	$(PYTHON) scripts/infrastructure/aws_access.py --account-id 127811062476 --output-dir infra/terraform/state-bootstrap/policies

infra-check:
	$(PYTHON) scripts/infrastructure/checks.py

.PHONY: platform-check platform-schema-fetch
platform-check:
	$(PYTHON) scripts/infrastructure/platform_checks.py

platform-schema-fetch:
	$(PYTHON) scripts/infrastructure/platform_checks.py --fetch-schemas

.PHONY: identity-collect identity-plan identity-apply identity-verify-public identity-activate
identity-collect:
	$(PYTHON) scripts/infrastructure/workload_identity.py collect

identity-plan:
	$(OPERATOR) plan --root identity

identity-apply:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the reviewed identity plan digest'; exit 1)
	$(OPERATOR) apply --root identity --approve-plan "$(PLAN_SHA)"

identity-verify-public:
	$(PYTHON) scripts/infrastructure/workload_identity.py verify-public

identity-activate:
	$(PYTHON) scripts/infrastructure/cluster.py activate-identity

.PHONY: infra-platform-preview infra-platform-plan infra-platform-apply cluster-platform-join cluster-verify-platform
infra-platform-preview:
	$(OPERATOR) plan --root platform --preview

infra-platform-plan:
	$(OPERATOR) plan --root platform

infra-platform-apply:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the reviewed platform plan digest'; exit 1)
	$(OPERATOR) apply --root platform --approve-plan "$(PLAN_SHA)"

cluster-platform-join:
	$(PYTHON) scripts/infrastructure/cluster.py platform-join

cluster-verify-platform:
	$(PYTHON) scripts/infrastructure/cluster.py verify-platform

infra-lock:
	$(PYTHON) scripts/infrastructure/checks.py --lock-platforms

infra-profiles:
	$(OPERATOR) profiles

infra-bootstrap-plan:
	$(OPERATOR) plan --root bootstrap

infra-bootstrap-apply:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the reviewed plan digest'; exit 1)
	$(OPERATOR) apply --root bootstrap --approve-plan "$(PLAN_SHA)"

infra-preview:
	$(OPERATOR) plan --root foundation --preview

infra-plan:
	$(OPERATOR) plan --root foundation

infra-apply:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the reviewed plan digest'; exit 1)
	$(OPERATOR) apply --root foundation --approve-plan "$(PLAN_SHA)"

infra-smoke:
	$(OPERATOR) smoke --confirm-prebootstrap

infra-export:
	$(OPERATOR) export $(EXPORT_ARGS)

infra-verify:
	$(OPERATOR) verify

infra-dns-preview:
	@test -n "$(ORIGINS_FILE)" || (echo 'Set ORIGINS_FILE, even for an empty origin map'; exit 1)
	$(OPERATOR) plan --root dns --preview --origins-file "$(ORIGINS_FILE)"

infra-dns-plan:
	@test -n "$(ORIGINS_FILE)" || (echo 'Set ORIGINS_FILE, even for an empty origin map'; exit 1)
	$(OPERATOR) plan --root dns --origins-file "$(ORIGINS_FILE)"

infra-dns-apply:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the reviewed plan digest'; exit 1)
	$(OPERATOR) apply --root dns --approve-plan "$(PLAN_SHA)"

infra-destroy-plan:
	$(OPERATOR) destroy-plan $(DESTROY_ARGS)

infra-destroy:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the reviewed teardown plan digest'; exit 1)
	$(OPERATOR) destroy --approve-plan "$(PLAN_SHA)"

.PHONY: cluster-tools cluster-check cluster-preflight cluster-prepare cluster-mesh cluster-bootstrap cluster-deploy cluster-verify cluster-backup cluster-acceptance
cluster-tools:
	$(UV) sync --locked --all-packages --group infrastructure
	$(UV) run --locked --group infrastructure python scripts/infrastructure/platform_artifacts.py install --all-targets
cluster-check:
	$(UV) run --locked --group infrastructure python scripts/infrastructure/cluster_checks.py
cluster-preflight:
	$(UV) run --locked --group infrastructure python scripts/infrastructure/cluster.py preflight
cluster-prepare:
	$(UV) run --locked --group infrastructure python scripts/infrastructure/cluster.py prepare
cluster-mesh:
	$(UV) run --locked --group infrastructure python scripts/infrastructure/cluster.py mesh $(CLUSTER_ARGS)
cluster-bootstrap:
	$(UV) run --locked --group infrastructure python scripts/infrastructure/cluster.py bootstrap
cluster-deploy:
	$(UV) run --locked --group infrastructure python scripts/infrastructure/cluster.py deploy
cluster-verify:
	$(UV) run --locked --group infrastructure python scripts/infrastructure/cluster.py verify
cluster-backup:
	$(UV) run --locked --group infrastructure python scripts/infrastructure/cluster.py backup
cluster-acceptance:
	$(UV) run --locked --group infrastructure python scripts/infrastructure/cluster.py acceptance
