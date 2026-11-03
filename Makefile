UV ?= uv
PYTHON = $(UV) run --locked --package platform-contracts python
OPERATOR = $(PYTHON) scripts/infrastructure/manage.py

.PHONY: infra-tools infra-policies infra-check infra-lock infra-profiles infra-bootstrap-plan infra-bootstrap-apply infra-plan infra-preview infra-apply infra-smoke infra-export infra-verify infra-dns-plan infra-dns-preview infra-dns-apply infra-destroy-plan infra-destroy

infra-tools:
	python3 scripts/infrastructure/install_tools.py

infra-policies:
	$(PYTHON) scripts/infrastructure/aws_access.py --account-id 127811062476 --output-dir infra/terraform/state-bootstrap/policies

infra-check:
	$(PYTHON) scripts/infrastructure/checks.py

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
