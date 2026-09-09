"""
Database connection management.

Uses SQLite for local development. Switch DATABASE_URL env var to
a PostgreSQL connection string (e.g. Supabase) for production.
"""

import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///chess_analytics.db"
)

# SQLite needs check_same_thread=False for FastAPI's async context
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(DATABASE_URL, connect_args=connect_args, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency — yields a DB session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Columns added to an existing table after it was first created. create_all()
# only creates missing *tables*, so a database made before one of these columns
# existed keeps working but silently lacks it.
_ADDED_COLUMNS = {
    "games": {"variant": "VARCHAR(30)"},
}


def _add_missing_columns():
    """Bring an existing database up to the current model (idempotent)."""
    inspector = inspect(engine)
    for table, columns in _ADDED_COLUMNS.items():
        if table not in inspector.get_table_names():
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        with engine.begin() as conn:
            for name, ddl_type in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))


def init_db():
    """Create all tables and add any newly-introduced columns (idempotent)."""
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
