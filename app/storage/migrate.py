from pathlib import Path

from alembic import command
from alembic.config import Config
from langgraph.checkpoint.postgres import PostgresSaver

from app.storage.db import create_database
from config.settings import get_settings


def migrate(database_url: str) -> None:
    engine, _ = create_database(database_url)
    try:
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        if engine.dialect.name == "postgresql":
            with PostgresSaver.from_conn_string(database_url.replace("postgresql+psycopg://", "postgresql://", 1)) as saver:
                saver.setup()
    finally:
        engine.dispose()


def main() -> None:
    migrate(get_settings().database_url)


if __name__ == "__main__":
    main()
