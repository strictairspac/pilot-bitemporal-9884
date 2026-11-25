from sqlalchemy import JSON, BigInteger, Column, ForeignKey, Integer, String, Table
from .base import metadata


runs = Table(
    "runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("case_id", ForeignKey("cases.id"), nullable=False, index=True),
    Column("status", String(16), nullable=False, index=True),
    Column("parameter", Integer, nullable=False),
    Column("actor", String(48), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("finished_at", BigInteger, nullable=True),
    Column("result", JSON(none_as_null=True), nullable=True),
    Column("failure", String(320), nullable=True),
    Column("digest", String(64), nullable=True),
    # Segmented execution progress. Older rows keep the defaults (0 / NULL).
    Column("plan_digest", String(64), nullable=True),
    Column("segment_size", Integer, nullable=False, default=200),
    Column("total_segments", Integer, nullable=False, default=0),
    Column("done_segments", Integer, nullable=False, default=0),
    Column("total_trips", Integer, nullable=False, default=0),
    Column("done_trips", Integer, nullable=False, default=0),
    # Rounds-robin cursor so a stuck long batch cannot starve short batches.
    Column("last_served_at", BigInteger, nullable=True),
)
