from alembic import context

from app.storage.models import Base


def run() -> None:
    connection = context.config.attributes["connection"]
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


run()
