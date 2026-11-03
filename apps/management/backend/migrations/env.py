from alembic import context
from platform_management.db.models import Base
from platform_management.db.session import engine

with engine().connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
