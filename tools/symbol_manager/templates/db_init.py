# DbManager/db_init.py
from __future__ import annotations
import os
import sys
import pathlib
import psycopg
from psycopg import sql
# Set up logging
import logging
log = logging.getLogger("db_init")
log.setLevel(logging.INFO)

# ---- BOOTSTRAP (works from batch or `python -m`) --------------------------------

_THIS_DIR = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, os.pardir))
_COMMON_DIR = os.path.join(_PROJECT_ROOT, "common")
for path in [_PROJECT_ROOT, _COMMON_DIR]:
    if path not in sys.path:
        sys.path.insert(0, path)
# ----------------------------------------------------------------------------------


DB_URL = os.getenv("DATABASE_URL")
SCHEMA_PATH = pathlib.Path(__file__).resolve().parent / "sql" / "schema.sql"


def _split_db_url(url: str) -> tuple[str, str]:
    """
    Return (admin_url, db_name), where admin_url points to the same server but the 'postgres' DB.
    """
    if not url:
        raise ValueError("DATABASE_URL is not set in environment variables.")
    # Strip trailing slash to be safe
    base = url.rstrip("/")
    host_part, _, db_name = base.rpartition("/")
    if not host_part or not db_name:
        raise ValueError(f"Unexpected DATABASE_URL format: {url!r}")
    admin_url = f"{host_part}/postgres"
    return admin_url, db_name


def delete_and_create_new_main_database() -> None:
    """Drop (if exists) and create the main database."""
    print("Creating new main database...")

    admin_url, db_name = _split_db_url(DB_URL)

    print(f"Connecting to admin DB: {admin_url}")
    with psycopg.connect(admin_url) as conn:
        # autocommit is required for CREATE/DROP DATABASE statements
        conn.autocommit = True
        with conn.cursor() as cur:
            # DROP DATABASE IF EXISTS <dbname>;
            drop_stmt = sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name))
            print(f"Executing: {drop_stmt.as_string(conn)}")
            cur.execute(drop_stmt)

            # CREATE DATABASE <dbname>;
            create_stmt = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name))
            print(f"Executing: {create_stmt.as_string(conn)}")
            cur.execute(create_stmt)

    print(f"Database {db_name} created successfully.")


def verify_and_init_database() -> None:
    """Verify the database connection and initialize schema from SCHEMA_PATH."""
    if not DB_URL:
        raise ValueError("DATABASE_URL is not set in environment variables.")

    print(f"Connecting to target DB: {DB_URL}")
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            # Verify connection
            cur.execute("SELECT 1;")
            got = cur.fetchone()
            if got is None or got[0] != 1:
                raise ConnectionError("Failed to verify database connection.")

            # Initialize schema
            if not SCHEMA_PATH.exists():
                raise FileNotFoundError(f"Schema file not found at {SCHEMA_PATH}")

            print(f"Initializing database schema from {SCHEMA_PATH}...")
            schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

            # Preferred: execute all statements at once (lets Postgres parse correctly)
            try:
                cur.execute(sql.SQL(schema_sql))
            except Exception as e:
                print(f"Error initializing schema: {e}")
                raise
            conn.commit()

            print("Database schema initialized.")


def drop_and_create_database() -> None:
    """Full cycle: drop/create DB, then verify and initialize schema."""
    if not DB_URL:
        print("ERROR: set DATABASE_URL in .env", file=sys.stderr)
        sys.exit(1)

    print(f"Dropping & Creating DB: {DB_URL}")
    delete_and_create_new_main_database()

    print(f"Verify and init DB: {DB_URL}")
    verify_and_init_database()

    print("Database initialized. Success!")
