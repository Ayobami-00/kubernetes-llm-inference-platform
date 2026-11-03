UV ?= uv
PYTHON = $(UV) run --locked --group infrastructure python
MANAGEMENT_PYTHON = $(UV) run --locked --all-packages --group infrastructure python
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

.PHONY: database-prepare database-initialize database-verify database-backup database-restore-prepare database-restore database-verify-restore
database-prepare:
	$(PYTHON) scripts/infrastructure/database.py prepare

database-initialize:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the exact reviewed database initialization digest'; exit 1)
	$(PYTHON) scripts/infrastructure/database.py initialize --approve-plan "$(PLAN_SHA)"

database-verify:
	$(PYTHON) scripts/infrastructure/database.py verify

database-backup:
	@test -n "$(OPERATION)" || (echo 'Set a stable 12-character lowercase hexadecimal OPERATION'; exit 1)
	$(PYTHON) scripts/infrastructure/database_recovery.py backup --operation "$(OPERATION)"

database-restore-prepare:
	@test -n "$(OPERATION)" || (echo 'Set OPERATION to the completed backup operation'; exit 1)
	$(PYTHON) scripts/infrastructure/database_recovery.py restore-prepare --operation "$(OPERATION)"

database-restore:
	@test -n "$(OPERATION)" -a -n "$(PLAN_SHA)" || (echo 'Set OPERATION and the reviewed restore PLAN_SHA'; exit 1)
	$(PYTHON) scripts/infrastructure/database_recovery.py restore --operation "$(OPERATION)" --approve-plan "$(PLAN_SHA)"

database-verify-restore:
	@test -n "$(OPERATION)" || (echo 'Set OPERATION to the restored backup operation'; exit 1)
	$(PYTHON) scripts/infrastructure/database_recovery.py verify --operation "$(OPERATION)"

.PHONY: database-cleanup-restore database-backup-secret database-verify-secret database-restore-secret
database-cleanup-restore:
	@test -n "$(OPERATION)" || (echo 'Set OPERATION to the verified synthetic restore operation'; exit 1)
	$(PYTHON) scripts/infrastructure/database_cleanup.py --operation "$(OPERATION)"

database-backup-secret:
	$(PYTHON) scripts/infrastructure/database_secrets.py backup

database-verify-secret:
	$(PYTHON) scripts/infrastructure/database_secrets.py verify

database-restore-secret:
	$(PYTHON) scripts/infrastructure/database_secrets.py restore

.PHONY: database-migration-prepare database-migration-restore database-migration-verify database-migration-upgrade
database-migration-prepare:
	$(MANAGEMENT_PYTHON) scripts/infrastructure/database_migration.py prepare

database-migration-restore:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the exact reviewed local data migration'; exit 1)
	$(MANAGEMENT_PYTHON) scripts/infrastructure/database_migration.py restore --approve-plan "$(PLAN_SHA)"

database-migration-verify:
	$(MANAGEMENT_PYTHON) scripts/infrastructure/database_migration.py verify

database-migration-upgrade:
	$(MANAGEMENT_PYTHON) scripts/infrastructure/database_migration.py upgrade

.PHONY: infra-registry-plan infra-registry-apply registry-verify image-tools-build
infra-registry-plan:
	$(OPERATOR) plan --root registry

infra-registry-apply:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the exact reviewed private registry plan'; exit 1)
	$(OPERATOR) apply --root registry --approve-plan "$(PLAN_SHA)"

registry-verify:
	$(PYTHON) scripts/infrastructure/platform_registry.py

.PHONY: management-init-admin management-stage management-plan management-deploy management-verify management-evidence
management-init-admin:
	$(PYTHON) scripts/infrastructure/management_release.py init-admin

management-stage:
	$(PYTHON) scripts/infrastructure/management_release.py stage

management-plan:
	@test -n "$(RECEIPTS)" || (echo 'Set RECEIPTS to the downloaded immutable CI image receipts'; exit 1)
	$(PYTHON) scripts/infrastructure/management_release.py prepare --receipts "$(RECEIPTS)"

management-deploy:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the reviewed management release digest'; exit 1)
	$(PYTHON) scripts/infrastructure/management_release.py deploy --approve-plan "$(PLAN_SHA)"

management-verify:
	$(PYTHON) scripts/infrastructure/management_acceptance.py

management-evidence:
	$(PYTHON) scripts/infrastructure/management_acceptance.py bundle

.PHONY: management-frontend-cost management-frontend-plan management-frontend-apply management-frontend-verify management-frontend-activate management-frontend-deactivate
management-frontend-cost:
	$(PYTHON) scripts/infrastructure/management_frontend.py cost

management-frontend-plan:
	$(OPERATOR) plan --root frontend

management-frontend-apply:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the reviewed frontend Terraform plan digest'; exit 1)
	$(OPERATOR) apply --root frontend --approve-plan "$(PLAN_SHA)"

management-frontend-verify:
	$(PYTHON) scripts/infrastructure/management_frontend.py verify

management-frontend-activate:
	$(PYTHON) scripts/infrastructure/management_frontend.py activate

management-frontend-deactivate:
	$(PYTHON) scripts/infrastructure/management_frontend.py deactivate

.PHONY: observability-cost observability-plan observability-apply observability-verify observability-test-notification
observability-cost:
	$(PYTHON) scripts/infrastructure/observability.py cost

observability-plan:
	$(OPERATOR) plan --root observability

observability-apply:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the reviewed telemetry Terraform plan digest'; exit 1)
	$(OPERATOR) apply --root observability --approve-plan "$(PLAN_SHA)"

observability-verify:
	$(PYTHON) scripts/infrastructure/observability.py verify

observability-test-notification:
	@test -n "$(OPERATION)" || (echo 'Set a unique 12-hex OPERATION for this notification'; exit 1)
	$(PYTHON) scripts/infrastructure/observability.py test-notification --operation "$(OPERATION)"

.PHONY: observability-charts observability-render
observability-charts:
	$(PYTHON) scripts/infrastructure/observability_release.py fetch

observability-render:
	$(PYTHON) scripts/infrastructure/observability_release.py render

.PHONY: monitoring-prepare monitoring-apply monitoring-observe monitoring-firewall monitoring-reconcile
monitoring-firewall:
	$(PYTHON) scripts/infrastructure/telemetry_firewall.py

monitoring-prepare:
	@test -n "$(SECURITY_DIRECTORY)" || (echo 'Set SECURITY_DIRECTORY to the reviewed CI image-scan directory'; exit 1)
	$(PYTHON) scripts/infrastructure/monitoring_runtime.py prepare --security-directory "$(SECURITY_DIRECTORY)"

monitoring-apply:
	@test -n "$(PLAN_SHA)" || (echo 'Set the exact prepared monitoring PLAN_SHA'; exit 1)
	$(PYTHON) scripts/infrastructure/monitoring_runtime.py apply --approve-plan "$(PLAN_SHA)"

monitoring-observe:
	$(PYTHON) scripts/infrastructure/monitoring_runtime.py observe

monitoring-reconcile:
	@test -n "$(PLAN_SHA)" || (echo 'Set the exact interrupted monitoring PLAN_SHA'; exit 1)
	$(PYTHON) scripts/infrastructure/monitoring_runtime.py reconcile --approve-plan "$(PLAN_SHA)"

.PHONY: platform-resize-prepare platform-resize-apply platform-resize-observe platform-resize-verify
platform-resize-prepare:
	@test -n "$(NODE)" -a -n "$(BACKUP_OPERATION)" || (echo 'Set NODE and a verified BACKUP_OPERATION'; exit 1)
	$(PYTHON) scripts/infrastructure/platform_resize.py prepare --node "$(NODE)" --backup-operation "$(BACKUP_OPERATION)"

platform-resize-apply:
	@test -n "$(PLAN_SHA)" || (echo 'Set the exact reviewed PLAN_SHA'; exit 1)
	$(PYTHON) scripts/infrastructure/platform_resize.py apply --approve-plan "$(PLAN_SHA)"

platform-resize-observe:
	$(PYTHON) scripts/infrastructure/platform_resize.py observe

platform-resize-verify:
	$(PYTHON) scripts/infrastructure/platform_resize.py verify

.PHONY: management-verify-renewal
management-verify-renewal:
	@test -n "$(OPERATION)" || (echo 'Set a stable 12-character lowercase hexadecimal OPERATION'; exit 1)
	$(PYTHON) scripts/infrastructure/management_renewal.py --operation "$(OPERATION)"

.PHONY: management-public-ui-verify management-verify-api-recovery
management-public-ui-verify:
	node scripts/management/public_read_smoke.mjs

management-verify-api-recovery:
	@test -n "$(OPERATION)" || (echo 'Set a stable 12-character lowercase hexadecimal OPERATION'; exit 1)
	node scripts/management/public_read_smoke.mjs --restart-api "$(OPERATION)"

.PHONY: management-verify-postgresql-recovery
management-verify-postgresql-recovery:
	@test -n "$(OPERATION)" -a -n "$(BACKUP_OPERATION)" || (echo 'Set OPERATION and the completed BACKUP_OPERATION'; exit 1)
	node scripts/management/public_read_smoke.mjs --fail-primary "$(OPERATION)" --backup-operation "$(BACKUP_OPERATION)"

.PHONY: database-tuning-prepare database-tuning-apply
database-tuning-prepare:
	$(PYTHON) scripts/infrastructure/database_tuning.py prepare

database-tuning-apply:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the exact reviewed database tuning digest'; exit 1)
	$(PYTHON) scripts/infrastructure/database_tuning.py apply --approve-plan "$(PLAN_SHA)"

.PHONY: image-credentials-prepare image-credentials-apply
image-credentials-prepare:
	$(PYTHON) scripts/infrastructure/image_credentials.py prepare

image-credentials-apply:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the reviewed host rollout digest'; exit 1)
	$(PYTHON) scripts/infrastructure/image_credentials.py apply --approve-plan "$(PLAN_SHA)"

image-tools-build:
	@test -n "$(OPERATION)" || (echo 'Set a stable 12-character lowercase hexadecimal OPERATION'; exit 1)
	$(PYTHON) scripts/infrastructure/image_tools.py --operation "$(OPERATION)"

.PHONY: identity-collect identity-plan identity-apply identity-verify-public identity-activate identity-verify-federation
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

identity-verify-federation:
	$(PYTHON) scripts/infrastructure/cluster.py verify-federation

.PHONY: infra-platform-preview infra-platform-plan infra-platform-apply cluster-platform-join cluster-verify-platform
infra-platform-preview:
	$(OPERATOR) plan --root platform --preview

infra-platform-plan:
	$(OPERATOR) plan --root platform

infra-platform-apply:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the reviewed platform plan digest'; exit 1)
	$(OPERATOR) apply --root platform --approve-plan "$(PLAN_SHA)"

cluster-platform-protect:
	$(PYTHON) scripts/infrastructure/cluster.py platform-protect

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

.PHONY: storage-plan storage-apply storage-access-plan storage-access-apply storage-verify cluster-platform-protect
storage-plan:
	$(OPERATOR) plan --root storage

storage-apply:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the reviewed retained-storage plan digest'; exit 1)
	$(OPERATOR) apply --root storage --approve-plan "$(PLAN_SHA)"

storage-access-plan:
	$(OPERATOR) plan --root storage-access

storage-access-apply:
	@test -n "$(PLAN_SHA)" || (echo 'Set PLAN_SHA to the reviewed storage-access plan digest'; exit 1)
	$(OPERATOR) apply --root storage-access --approve-plan "$(PLAN_SHA)"

storage-verify:
	$(PYTHON) scripts/infrastructure/platform_storage.py

.PHONY: platform-services-fetch platform-service-deploy
platform-services-fetch:
	$(PYTHON) scripts/infrastructure/platform_services.py fetch

platform-service-deploy:
	@test -n "$(SERVICE)" || (echo 'Set SERVICE to a reviewed static release'; exit 1)
	$(PYTHON) scripts/infrastructure/platform_services.py deploy --release "$(SERVICE)"

.PHONY: identity-verify-injection cluster-verify-storage
identity-verify-injection:
	$(PYTHON) scripts/infrastructure/cluster.py verify-injection

cluster-verify-storage:
	$(PYTHON) scripts/infrastructure/cluster.py verify-storage

.PHONY: cluster-verify-ebs
cluster-verify-ebs:
	$(PYTHON) scripts/infrastructure/cluster.py verify-ebs
