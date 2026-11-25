from sqlalchemy import JSON, BigInteger, Column, ForeignKey, Index, Integer, String, Table, UniqueConstraint
from .base import metadata


run_segments = Table(
    "run_segments", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", ForeignKey("runs.id"), nullable=False),
    Column("seq", Integer, nullable=False),
    # Stable slice boundaries into the case's sorted trip list.
    Column("slice_start", Integer, nullable=False),
    Column("slice_stop", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    # Fencing token: each successful claim increments it. A worker that lost its
    # claim cannot write with a stale token.
    Column("lease_owner", String(64), nullable=True),
    Column("lease_token", BigInteger, nullable=False, default=0),
    Column("lease_expires_at", BigInteger, nullable=True),
    Column("attempts", Integer, nullable=False, default=0),
    Column("result", JSON(none_as_null=True), nullable=True),
    Column("failure", String(320), nullable=True),
    Column("finished_at", BigInteger, nullable=True),
    UniqueConstraint("run_id", "seq", name="uq_run_segment_seq"),
    Index("ix_run_segment_run_status", "run_id", "status"),
)
