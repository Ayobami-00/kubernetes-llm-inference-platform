from alembic import context
from platform_management.db.models import Base
from platform_management.db.session import engine
from sqlalchemy import text


def migrate(connection):
    connection.execute(text("SELECT pg_advisory_lock(73490124)"))
    connection.commit()
    try:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.execute(text("SELECT pg_advisory_unlock(73490124)"))
        connection.commit()


provided = context.config.attributes.get("connection")
if provided is not None:
    migrate(provided)
else:
    with engine().connect() as connection:
        migrate(connection)
