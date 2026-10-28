import os

from sqlalchemy import create_engine

from alembic import context

config = context.config


def migrate(connection):
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    migrate(connection)
else:
    engine = create_engine(os.environ["VIDEO_DATABASE_URL"])
    with engine.connect() as connection:
        migrate(connection)
    engine.dispose()
