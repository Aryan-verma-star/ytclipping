"""Debug: run the app startup migration path step by step."""

import logging
import sys
import time

logging.basicConfig(level=logging.DEBUG)

sys.path.insert(0, "/home/z/my-project/backend")

from app.config import BACKEND_DIR, get_settings

settings = get_settings()
print("db url:", settings.resolved_database_url, flush=True)

from app.db.base import Database

print("creating Database...", flush=True)
db = Database(settings.resolved_database_url)
print("engine ok:", db.engine, flush=True)

print("testing simple connect...", flush=True)
t0 = time.time()
with db.engine.connect() as conn:
    from sqlalchemy import text

    conn.execute(text("SELECT 1"))
print(f"connect ok in {time.time() - t0:.2f}s", flush=True)

from alembic import command
from alembic.config import Config as AlembicConfig

print("running alembic upgrade head...", flush=True)
t0 = time.time()
cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
cfg.set_main_option("sqlalchemy.url", settings.resolved_database_url)
command.upgrade(cfg, "head")
print(f"alembic ok in {time.time() - t0:.2f}s", flush=True)

print("DONE", flush=True)
