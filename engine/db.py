"""Connection management for the sidecar engine database.

Engine output lives in its own SQLite file rather than in chess_analytics.db.
Nothing is added to games or moves: absence of a position_evals row *is* the
"not analyzed" state, so no existing query changes behaviour and there is no
nullable column to backfill.

Analysis queries open the canonical database and ATTACH this one, which keeps
the join native while leaving the canonical file read-only during a batch.
"""

import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

ENGINE_DATABASE_URL = os.getenv(
    "ENGINE_DATABASE_URL",
    "sqlite:///chess_engine.db",
)

# The canonical database, reused from the app so the two cannot drift apart.
CANONICAL_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///chess_analytics.db",
)

# The alias the engine database is attached under. Analysis SQL says
# engine.position_evals; canonical tables stay unqualified.
ATTACH_ALIAS = "engine"

Base = declarative_base()

engine = create_engine(
    ENGINE_DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _sqlite_path(url: str) -> str:
    """Strip the SQLAlchemy scheme off a SQLite URL to get a file path.

    ATTACH takes a path, not a URL.
    """
    if not url.startswith("sqlite"):
        raise ValueError(
            f"ATTACH-based analysis is SQLite-only, got {url!r}. "
            "The sidecar layout trades PostgreSQL support for a disposable "
            "engine database; see the design doc."
        )
    return url.split("///", 1)[1] if "///" in url else ":memory:"


def attach_engine_db(conn):
    """ATTACH the engine database onto an open canonical connection."""
    conn.exec_driver_sql(
        f"ATTACH DATABASE '{_sqlite_path(ENGINE_DATABASE_URL)}' AS {ATTACH_ALIAS}"
    )


def analysis_engine():
    """A canonical-database engine with the sidecar attached to every connection.

    Queries against it can join games and moves to engine.move_evals directly.
    A LEFT JOIN yields NULL for games nobody has analyzed, which is the whole
    point of keeping engine data out of the canonical tables.
    """
    canonical = create_engine(
        CANONICAL_DATABASE_URL,
        connect_args={"check_same_thread": False},
        echo=False,
    )

    @event.listens_for(canonical, "connect")
    def _attach(dbapi_conn, _record):
        dbapi_conn.execute(
            f"ATTACH DATABASE '{_sqlite_path(ENGINE_DATABASE_URL)}' AS {ATTACH_ALIAS}"
        )

    return canonical
