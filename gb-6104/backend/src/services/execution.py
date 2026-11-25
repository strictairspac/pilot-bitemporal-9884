import logging
import os
import socket
from time import time
from uuid import uuid4

from sqlalchemy import and_, insert, or_, select, update

from src.config.resources import engine
from src.constants.job_state import JobState, SegmentState
from src.domain import scenario
from src.models.cases import cases
from src.models.runs import runs
from src.models.segments import run_segments
from src.services.audit import record
from src.utils.serial import content_digest

logger = logging.getLogger(__name__)

# A worker keeps a segment leased while it recomputes. If the process dies, the
# lease lapses and another worker (or a restarted one) resumes that segment.
LEASE_SECONDS = max(1, int(os.environ.get("SEGMENT_LEASE_SECONDS", "30")))
CANDIDATES = 20
MAX_ATTEMPTS = max(1, int(os.environ.get("SEGMENT_MAX_ATTEMPTS", "5")))
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"

# Permanent input problems fail the batch; anything else is treated as transient
# so the lease can lapse and another attempt can be made.
_TERMINAL = (ValueError, TypeError, KeyError, OverflowError)


class JobFailure(Exception):
    def __init__(self, run_id, cause):
        super().__init__(str(cause))
        self.run_id = run_id
        self.cause = cause


class RetriesExceeded(Exception):
    """A segment kept failing transiently; fail the batch instead of looping."""


def _reason(cause):
    if isinstance(cause, RetriesExceeded):
        return "分段重试次数超过上限"
    if isinstance(cause, _TERMINAL):
        return str(cause) or "输入资料或批次参数不符合要求"
    return "处理失败，请检查 worker 日志"


def service_one():
    """Plan one queued batch, or recompute one claimable segment.

    Returns True when work was done, False when the queue is empty.
    """
    if plan_one():
        return True
    claim = claim_one()
    if claim is None:
        return False
    try:
        rows, batch = compute_one(claim)
    except _TERMINAL as exc:
        fail_segment(claim, exc)
        return True
    except Exception:
        # Transient failure: hand the segment back so it resumes later.
        logger.exception("Segment %s of run %s failed; retrying", claim["seq"], claim["run_id"])
        if claim["attempts"] >= MAX_ATTEMPTS:
            fail_segment(claim, RetriesExceeded())
        else:
            release_lease(claim)
        return True
    complete_one(claim, rows, batch)
    return True


def _claimable(now):
    return or_(
        run_segments.c.status == SegmentState.PENDING,
        and_(run_segments.c.status == SegmentState.RUNNING, run_segments.c.lease_expires_at < now),
    )


def plan_one():
    """Freeze a deterministic plan and create its segment rows exactly once."""
    with engine.begin() as db:
        task = db.execute(
            select(runs.c.id, runs.c.case_id, runs.c.parameter)
            .where(runs.c.status == JobState.QUEUED, runs.c.total_segments == 0)
            .order_by(runs.c.id).limit(1).with_for_update(skip_locked=True)
        ).mappings().first()
        if task is None:
            return False
        payload = db.scalar(select(cases.c.payload).where(cases.c.id == task["case_id"]))
        try:
            batch = scenario.plan(payload, task["parameter"])
        except Exception as exc:
            # Let the plan transaction roll back before the worker records the error.
            raise JobFailure(task["id"], exc) from exc
        db.execute(insert(run_segments), [
            {
                "run_id": task["id"], "seq": seq,
                "slice_start": seq * batch["segment_size"],
                "slice_stop": min((seq + 1) * batch["segment_size"], batch["trips_count"]),
                "status": SegmentState.PENDING, "lease_token": 0, "attempts": 0,
            }
            for seq in range(batch["count"])
        ])
        db.execute(update(runs).where(runs.c.id == task["id"]).values(
            status=JobState.RUNNING, plan_digest=batch["digest"],
            segment_size=batch["segment_size"], total_segments=batch["count"],
            total_trips=batch["trips_count"], done_segments=0, done_trips=0,
            last_served_at=int(time()),
        ))
        record(db, str(JobState.RUNNING), "worker", "run", task["id"])
        return True


def claim_one():
    """Lease the least-recently-served claimable segment.

    Ordering by ``last_served_at`` rotates service across batches, so a long
    opening batch cannot starve a short recheck batch. A segment whose lease has
    lapsed becomes claimable again, which is what makes an interrupted run
    resume on its own. The conditional ``UPDATE`` is the claim itself, so two
    workers racing for the same segment cannot both win and no lock is held
    while a segment is being recomputed.
    """
    now = int(time())
    with engine.begin() as db:
        candidates = db.execute(
            select(run_segments.c.id)
            .join(runs, runs.c.id == run_segments.c.run_id)
            .where(runs.c.status.in_((JobState.QUEUED, JobState.RUNNING)), _claimable(now))
            .order_by(runs.c.last_served_at, run_segments.c.attempts, runs.c.id, run_segments.c.seq)
            .limit(CANDIDATES)
        ).scalars().all()
        for segment_id in candidates:
            won = db.execute(update(run_segments).where(
                run_segments.c.id == segment_id, _claimable(now),
            ).values(
                status=SegmentState.RUNNING, lease_owner=WORKER_ID,
                lease_token=run_segments.c.lease_token + 1,
                lease_expires_at=now + LEASE_SECONDS,
                attempts=run_segments.c.attempts + 1,
            ))
            if won.rowcount != 1:
                continue
            row = db.execute(
                select(run_segments).where(run_segments.c.id == segment_id)
            ).mappings().first()
            db.execute(update(runs).where(runs.c.id == row["run_id"]).values(
                status=JobState.RUNNING, last_served_at=now,
            ))
            return dict(row)
    return None


def compute_one(claim):
    """Recompute one slice outside the write transaction."""
    with engine.begin() as db:
        row = db.execute(
            select(cases.c.payload, runs.c.parameter, runs.c.plan_digest)
            .join(runs, runs.c.case_id == cases.c.id)
            .where(runs.c.id == claim["run_id"])
        ).mappings().first()
        batch = scenario.plan(row["payload"], row["parameter"])
        if batch["digest"] != row["plan_digest"]:
            raise ValueError("资料内容与执行计划不一致")
        rows = scenario.evaluate(batch, claim["slice_start"], claim["slice_stop"], db)
    return rows, batch


def complete_one(claim, rows, batch):
    """Persist a segment result only while the claim's lease is still ours.

    The write is fenced by ``lease_token``: a worker that lost its lease and
    comes back later cannot overwrite progress a takeover worker already saved.
    """
    now = int(time())
    with engine.begin() as db:
        stored = db.execute(update(run_segments).where(
            run_segments.c.id == claim["id"], run_segments.c.run_id == claim["run_id"],
            run_segments.c.status == SegmentState.RUNNING,
            run_segments.c.lease_owner == WORKER_ID,
            run_segments.c.lease_token == claim["lease_token"],
        ).values(
            status=SegmentState.DONE, result=rows, finished_at=now,
            lease_owner=None, lease_expires_at=None,
        ))
        if stored.rowcount != 1:
            # A takeover worker owns or finished this slice; leave their row intact.
            return False
        # Serialize counter increments and finalisation per run.
        run = db.execute(
            select(runs.c.status, runs.c.done_segments, runs.c.total_segments)
            .where(runs.c.id == claim["run_id"]).with_for_update()
        ).mappings().first()
        if run is None or run["status"] != JobState.RUNNING:
            # A failed or already-finalised batch must not be advanced.
            return True
        db.execute(update(runs).where(runs.c.id == claim["run_id"]).values(
            done_segments=runs.c.done_segments + 1,
            done_trips=runs.c.done_trips + (claim["slice_stop"] - claim["slice_start"]),
            last_served_at=now,
        ))
        if run["done_segments"] + 1 >= run["total_segments"]:
            _finish_run(db, claim["run_id"], batch, now)
        return True


def _finish_run(db, run_id, batch, now):
    """Assemble stored slices once every segment is done.

    Concatenating the already-sorted slices by ``seq`` reproduces the exact row
    order, metrics and digest of a single whole-batch run.
    """
    parts = db.execute(
        select(run_segments.c.result).where(
            run_segments.c.run_id == run_id, run_segments.c.status == SegmentState.DONE,
        ).order_by(run_segments.c.seq)
    ).scalars().all()
    rows = [row for part in parts for row in (part or [])]
    expected = db.scalar(select(runs.c.total_trips).where(runs.c.id == run_id))
    if len(rows) != expected:
        # A durable invariant broke; never publish a partial batch.
        db.execute(update(runs).where(runs.c.id == run_id).values(
            status=JobState.FAILED, failure=f"航次汇总不完整：{len(rows)}/{expected}",
            finished_at=now,
        ))
        record(db, str(JobState.FAILED), "worker", "run", run_id)
        return
    result = scenario.summarize(batch, rows)
    db.execute(update(runs).where(
        runs.c.id == run_id, runs.c.status == JobState.RUNNING,
    ).values(
        status=JobState.DONE, result=result, digest=content_digest(result), finished_at=now,
    ))
    record(db, "done", "worker", "run", run_id)


def release_lease(claim):
    """Hand a segment back for retry after a transient failure."""
    with engine.begin() as db:
        db.execute(update(run_segments).where(
            run_segments.c.id == claim["id"], run_segments.c.run_id == claim["run_id"],
            run_segments.c.status == SegmentState.RUNNING,
            run_segments.c.lease_owner == WORKER_ID,
            run_segments.c.lease_token == claim["lease_token"],
        ).values(status=SegmentState.PENDING, lease_owner=None, lease_expires_at=None))


def fail_segment(claim, cause):
    """Fail a batch when its claimed segment is permanently invalid."""
    now = int(time())
    with engine.begin() as db:
        stored = db.execute(update(run_segments).where(
            run_segments.c.id == claim["id"], run_segments.c.run_id == claim["run_id"],
            run_segments.c.status == SegmentState.RUNNING,
            run_segments.c.lease_owner == WORKER_ID,
            run_segments.c.lease_token == claim["lease_token"],
        ).values(
            status=SegmentState.FAILED, failure=_reason(cause)[:320], finished_at=now,
            lease_owner=None, lease_expires_at=None,
        ))
        if stored.rowcount != 1:
            return False
        db.execute(select(runs.c.id).where(runs.c.id == claim["run_id"]).with_for_update())
        finish_run_failure(db, claim["run_id"], cause, now)
        return True


def fail(job_failure):
    with engine.begin() as db:
        run_id = job_failure.run_id
        status = db.scalar(
            select(runs.c.status).where(runs.c.id == run_id).with_for_update()
        )
        if status != JobState.QUEUED:
            return
        finish_run_failure(db, run_id, job_failure.cause, int(time()))


def finish_run_failure(db, run_id, cause, now):
    db.execute(update(runs).where(runs.c.id == run_id).values(
        status=JobState.FAILED, failure=_reason(cause)[:320], finished_at=now,
    ))
    record(db, str(JobState.FAILED), "worker", "run", run_id)
