"""Packaged Alembic migrations executed before accepting requests."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine


def upgrade(engine: Engine) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
