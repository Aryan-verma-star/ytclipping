"""Database URL normalization (the Neon production path).

SQLAlchemy 2.1 made psycopg (v3) the default dialect for bare postgresql://
URLs while this app ships psycopg2-binary — a live Neon verification
(2026-10-03) caught the resulting ModuleNotFoundError before it could reach
production. normalize_database_url pins the driver explicitly.
"""

from __future__ import annotations

import pytest

from app.config import normalize_database_url


def test_bare_postgresql_gets_psycopg2_driver():
    assert (
        normalize_database_url("postgresql://u:p@host/db?sslmode=require")
        == "postgresql+psycopg2://u:p@host/db?sslmode=require"
    )


def test_query_string_preserved_verbatim():
    url = (
        "postgresql://neondb_owner:secret@ep-x-pooler.aws.neon.tech/neondb"
        "?sslmode=require&channel_binding=require"
    )
    out = normalize_database_url(url)
    assert out.startswith("postgresql+psycopg2://")
    assert out.endswith("?sslmode=require&channel_binding=require")


def test_legacy_heroku_scheme_normalized():
    assert (
        normalize_database_url("postgres://u:p@host/db")
        == "postgresql+psycopg2://u:p@host/db"
    )


def test_explicit_driver_left_untouched():
    # a user who explicitly requests a different driver knows what they want
    assert (
        normalize_database_url("postgresql+psycopg://u:p@host/db")
        == "postgresql+psycopg://u:p@host/db"
    )


def test_sqlite_untouched():
    assert normalize_database_url("sqlite:///tmp/x.db") == "sqlite:///tmp/x.db"


def test_empty_and_garbage_returned_as_is():
    assert normalize_database_url("") == ""
    assert normalize_database_url("mysql://x") == "mysql://x"


def test_settings_resolved_url_pins_driver(tmp_path):
    from tests.conftest import make_settings

    s = make_settings(
        tmp_path, database_url="postgresql://u:p@host/db?sslmode=require"
    )
    assert s.resolved_database_url == "postgresql+psycopg2://u:p@host/db?sslmode=require"


def test_settings_rejects_non_database_url(tmp_path):
    from tests.conftest import make_settings

    s = make_settings(tmp_path, database_url="file:./prisma.db")
    with pytest.raises(ValueError, match="CLIPPER_DATABASE_URL"):
        _ = s.resolved_database_url
