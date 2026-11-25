"""Add segmented execution state.

Version 1 built the original declarative schema. This version is additive and
idempotent so it can upgrade a database created before segmentation existed as
well as a brand-new one.
"""

from sqlalchemy import inspect, text

from src.models import segments as _segments  # noqa: F401  Register the table.
from src.models.base import metadata

_RUN_COLUMNS = (
    ("plan_digest", "VARCHAR(64) NULL"),
    ("segment_size", "INT NOT NULL DEFAULT 200"),
    ("total_segments", "INT NOT NULL DEFAULT 0"),
    ("done_segments", "INT NOT NULL DEFAULT 0"),
    ("total_trips", "INT NOT NULL DEFAULT 0"),
    ("done_trips", "INT NOT NULL DEFAULT 0"),
    ("last_served_at", "BIGINT NULL"),
)


def upgrade(connection):
    existing = {column["name"] for column in inspect(connection).get_columns("runs")}
    for name, ddl in _RUN_COLUMNS:
        if name not in existing:
            # ADD COLUMN ... is valid on both MySQL and PostgreSQL.
            connection.execute(text(f"ALTER TABLE runs ADD COLUMN {name} {ddl}"))
    metadata.create_all(connection, tables=[_segments.run_segments], checkfirst=True)
