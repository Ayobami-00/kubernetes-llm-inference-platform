"""Initial management schema and database-enforced historical immutability."""

from alembic import op

revision = "0001"
down_revision = None

# Frozen migration: subsequent ORM changes must use a new migration.
SCHEMA = (
    """
    CREATE TABLE login_attempts (
        id SERIAL NOT NULL,
        address VARCHAR NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """CREATE INDEX ix_login_attempts_address ON login_attempts (address)""",
    """
    CREATE TABLE observations (
        id VARCHAR NOT NULL,
        payload JSONB NOT NULL,
        next_refresh TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """
    CREATE TABLE operations (
        id VARCHAR NOT NULL,
        target_id VARCHAR NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """CREATE INDEX ix_operations_target_id ON operations (target_id)""",
    """
    CREATE TABLE preferences (
        id VARCHAR NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """
    CREATE TABLE provider_intents (
        id VARCHAR NOT NULL,
        group_id VARCHAR NOT NULL,
        state VARCHAR NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """CREATE INDEX ix_provider_intents_group_id ON provider_intents (group_id)""",
    """
    CREATE TABLE resources (
        id VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        payload JSONB NOT NULL,
        active_operation_id VARCHAR,
        PRIMARY KEY (id)
    )

    """,
    """CREATE INDEX ix_resources_kind ON resources (kind)""",
    """
    CREATE TABLE runs (
        id VARCHAR NOT NULL,
        snapshot JSONB NOT NULL,
        payload JSONB NOT NULL,
        context JSONB NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """
    CREATE TABLE sessions (
        token_hash VARCHAR NOT NULL,
        csrf_token VARCHAR NOT NULL,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        PRIMARY KEY (token_hash)
    )

    """,
    """
    CREATE TABLE simulated_instances (
        id VARCHAR NOT NULL,
        intent_id VARCHAR NOT NULL,
        group_id VARCHAR NOT NULL,
        state VARCHAR NOT NULL,
        allocated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """CREATE INDEX ix_simulated_instances_group_id ON simulated_instances (group_id)""",
    """CREATE INDEX ix_simulated_instances_intent_id ON simulated_instances (intent_id)""",
    """
    CREATE TABLE specifications (
        key VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        resource_id VARCHAR NOT NULL,
        current_revision INTEGER NOT NULL,
        draft JSONB,
        version INTEGER NOT NULL,
        PRIMARY KEY (key)
    )

    """,
    """CREATE INDEX ix_specifications_kind ON specifications (kind)""",
    """
    CREATE TABLE events (
        id SERIAL NOT NULL,
        operation_id VARCHAR NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        message VARCHAR NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(operation_id) REFERENCES operations (id)
    )

    """,
    """CREATE INDEX ix_events_operation_id ON events (operation_id)""",
    """
    CREATE TABLE jobs (
        id VARCHAR NOT NULL,
        state VARCHAR NOT NULL,
        due_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        lease_until TIMESTAMP WITH TIME ZONE,
        owner VARCHAR,
        attempt INTEGER NOT NULL,
        checkpoint JSONB NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(id) REFERENCES operations (id)
    )

    """,
    """CREATE INDEX jobs_due ON jobs (state, due_at)""",
    """
    CREATE TABLE revisions (
        key VARCHAR NOT NULL,
        spec_key VARCHAR NOT NULL,
        revision INTEGER NOT NULL,
        document JSONB NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (key),
        UNIQUE (spec_key, revision),
        FOREIGN KEY(spec_key) REFERENCES specifications (key)
    )

    """,
    """CREATE INDEX ix_revisions_spec_key ON revisions (spec_key)""",
    """
    CREATE TABLE submissions (
        key VARCHAR NOT NULL,
        fingerprint VARCHAR NOT NULL,
        operation_id VARCHAR NOT NULL,
        PRIMARY KEY (key),
        FOREIGN KEY(operation_id) REFERENCES operations (id)
    )

    """,
)


def upgrade():
    for statement in SCHEMA:
        op.execute(statement)
    op.execute("""
    CREATE FUNCTION reject_revision_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'Published revisions are immutable'; END $$;
    CREATE TRIGGER immutable_revisions BEFORE UPDATE OR DELETE ON revisions
        FOR EACH ROW EXECUTE FUNCTION reject_revision_mutation();
    CREATE FUNCTION protect_run_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.snapshot IS DISTINCT FROM OLD.snapshot THEN
        RAISE EXCEPTION 'Run snapshots are immutable';
      END IF;
      IF OLD.payload->>'status' IN ('completed','failed','cancelled')
         AND NEW.payload IS DISTINCT FROM OLD.payload THEN
        RAISE EXCEPTION 'Terminal run records are immutable';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER immutable_run_snapshot BEFORE UPDATE ON runs
        FOR EACH ROW EXECUTE FUNCTION protect_run_snapshot();
    """)


def downgrade():
    op.execute('DROP TABLE "submissions"')
    op.execute('DROP TABLE "revisions"')
    op.execute('DROP TABLE "jobs"')
    op.execute('DROP TABLE "events"')
    op.execute('DROP TABLE "specifications"')
    op.execute('DROP TABLE "simulated_instances"')
    op.execute('DROP TABLE "sessions"')
    op.execute('DROP TABLE "runs"')
    op.execute('DROP TABLE "resources"')
    op.execute('DROP TABLE "provider_intents"')
    op.execute('DROP TABLE "preferences"')
    op.execute('DROP TABLE "operations"')
    op.execute('DROP TABLE "observations"')
    op.execute('DROP TABLE "login_attempts"')
    op.execute("DROP FUNCTION protect_run_snapshot(); DROP FUNCTION reject_revision_mutation();")
