# KLIP PostgreSQL chart

C-owned PostgreSQL 17 single-primary persistence for management. Install as its own release before
`klip`, with an existing Secret containing `postgres-password` and `app-password`. Supply either
`persistence.existingClaim` or `persistence.storageClass` for a retained RWO PVC. The application role
is `klip` and database is `klip`; the application secret's `database-url` must match.

This chart does not provide database HA. Password rotation requires changing PostgreSQL roles as
well as Secret values; changing init-time environment does not rotate an initialized database.
Uninstall retains PVCs. Backup and restore are documented in the
[recovery runbook](../../docs/runbooks/management-recovery.md).
