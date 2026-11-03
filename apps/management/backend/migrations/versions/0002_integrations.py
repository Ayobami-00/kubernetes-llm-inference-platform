"""Durable integration bindings and imports; legacy workflows keep their simulation binding."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0002"
down_revision = "0001"


def upgrade():
    op.create_table(
        "execution_bindings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("payload", pg.JSONB(), nullable=False),
    )
    op.execute("""INSERT INTO execution_bindings VALUES ('legacy-simulation-v1',
      '{"mode":"simulation","provider":"simulated:v1","host":"simulated:v1",
        "cluster":"simulated:v1","serving":"simulated:v1","runner":"simulated:v1"}')""")
    for table in ("resources", "runs"):
        op.add_column(
            table, sa.Column("binding_id", sa.String(), sa.ForeignKey("execution_bindings.id"))
        )
        op.execute(sa.text(f"UPDATE {table} SET binding_id='legacy-simulation-v1'"))
    op.drop_constraint("jobs_id_fkey", "jobs", type_="foreignkey")
    op.add_column("jobs", sa.Column("operation_id", sa.String(), sa.ForeignKey("operations.id")))
    op.add_column(
        "jobs", sa.Column("binding_id", sa.String(), sa.ForeignKey("execution_bindings.id"))
    )
    op.add_column(
        "jobs", sa.Column("kind", sa.String(), nullable=False, server_default="operation")
    )
    op.execute("UPDATE jobs SET operation_id=id, binding_id='legacy-simulation-v1'")
    op.create_table(
        "import_receipts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("submission_key", sa.String(), nullable=False, unique=True),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("document", pg.JSONB(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("provenance", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("result", pg.JSONB(), nullable=False),
        sa.Column("error", sa.String()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "inventory_snapshots",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("cluster_id", sa.String(), nullable=False, index=True),
        sa.Column("digest", sa.String(), nullable=False),
        sa.Column("document", pg.JSONB(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("fixture", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "prepared_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("digest", sa.String(), nullable=False),
        sa.Column("snapshot", pg.JSONB(), nullable=False),
        sa.Column("fixture", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "artifact_checks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("uri", sa.String(), nullable=False),
        sa.Column("sha256", sa.String()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.String()),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "worker_heartbeats",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    for table in ("execution_bindings", "inventory_snapshots", "prepared_runs"):
        op.execute(f"""CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table}
           FOR EACH ROW EXECUTE FUNCTION reject_revision_mutation()""")
    op.execute("""CREATE FUNCTION protect_import_request() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN
        IF NEW.document IS DISTINCT FROM OLD.document OR NEW.kind<>OLD.kind
          OR NEW.source<>OLD.source OR NEW.provenance<>OLD.provenance
          OR NEW.fingerprint<>OLD.fingerprint OR NEW.submission_key<>OLD.submission_key THEN
          RAISE EXCEPTION 'import input is immutable';
        END IF;
        RETURN NEW;
       END $$;
       CREATE TRIGGER immutable_import_input BEFORE UPDATE ON import_receipts
       FOR EACH ROW EXECUTE FUNCTION protect_import_request();
       CREATE FUNCTION protect_terminal_run() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN
        IF OLD.payload->>'status' IN ('completed','failed','cancelled')
          AND NEW.payload IS DISTINCT FROM OLD.payload THEN
          RAISE EXCEPTION 'terminal run is immutable';
        END IF;
        IF OLD.binding_id IS NOT NULL AND NEW.binding_id IS DISTINCT FROM OLD.binding_id THEN
          RAISE EXCEPTION 'execution binding is immutable';
        END IF;
        RETURN NEW;
       END $$;
       CREATE TRIGGER immutable_terminal_run BEFORE UPDATE ON runs
       FOR EACH ROW EXECUTE FUNCTION protect_terminal_run();
       CREATE FUNCTION protect_resource_binding() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN
        IF OLD.binding_id IS NOT NULL AND NEW.binding_id IS DISTINCT FROM OLD.binding_id THEN
          RAISE EXCEPTION 'execution binding is immutable';
        END IF;
        RETURN NEW;
       END $$;
       CREATE TRIGGER immutable_resource_binding BEFORE UPDATE ON resources
       FOR EACH ROW EXECUTE FUNCTION protect_resource_binding();""")


def downgrade():
    op.execute("DROP TRIGGER immutable_terminal_run ON runs; DROP FUNCTION protect_terminal_run()")
    op.execute(
        "DROP TRIGGER immutable_resource_binding ON resources; "
        "DROP FUNCTION protect_resource_binding()"
    )
    op.execute(
        "DROP TRIGGER immutable_import_input ON import_receipts; "
        "DROP FUNCTION protect_import_request()"
    )
    # Never downgrade while internal work exists; silently deleting it loses accepted imports.
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM jobs WHERE kind<>'operation') THEN
      RAISE EXCEPTION 'remove internal jobs explicitly before downgrade'; END IF; END $$""")
    for table in (
        "worker_heartbeats",
        "artifact_checks",
        "prepared_runs",
        "inventory_snapshots",
        "import_receipts",
    ):
        op.drop_table(table)
    for name in ("kind", "binding_id", "operation_id"):
        op.drop_column("jobs", name)
    op.create_foreign_key("jobs_id_fkey", "jobs", "operations", ["id"], ["id"])
    for table in ("runs", "resources"):
        op.drop_column(table, "binding_id")
    op.drop_table("execution_bindings")
