"""Alembic environment — wired to the app's models and settings."""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# make `app` importable when alembic runs from backend/
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: without this, fileConfig() silences
    # every logger created before us (uvicorn's, the app's) and a running
    # server appears to "hang" silently after the first migration run.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    url = (config.get_main_option("sqlalchemy.url") or "").strip()
    if url:
        return url
    # Prefer the namespaced variable; accept the generic one only if it looks
    # like a SQLAlchemy URL (skips e.g. Prisma's `file:./dev.db`).
    for env_var in ("CLIPPER_DATABASE_URL", "DATABASE_URL"):
        env_url = os.environ.get(env_var, "").strip()
        if env_url.startswith(("sqlite://", "postgresql://", "postgres://")):
            if env_url.startswith("postgres://"):
                env_url = "postgresql://" + env_url[len("postgres://"):]
            return env_url
    # dev fallback mirrors app.config.Settings default (backend/data/clips.db)
    return "sqlite:///" + os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data", "clips.db")
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # safe ALTERs on SQLite
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
