"""Use the existing runtime connection, never a source-directory database."""

from alembic import context

from tvpet_console.store import Base

context.configure(connection=context.config.attributes["connection"], target_metadata=Base.metadata)
with context.begin_transaction():
    context.run_migrations()
