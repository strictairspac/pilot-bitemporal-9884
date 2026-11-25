from time import time

from fastapi import HTTPException
from sqlalchemy import insert, select

from src.constants.job_state import JobState
from src.domain.scenario import CONFIG
from src.models.cases import cases
from src.models.runs import runs
from . import audit, cases as case_service

# Lightweight projection for listings: never ship the full 5,000-row result set
# (or the frozen plan columns) through the list endpoint.
_LIST_COLUMNS = (
    runs.c.id, runs.c.case_id, runs.c.status, runs.c.parameter, runs.c.actor,
    runs.c.created_at, runs.c.finished_at, runs.c.failure, runs.c.digest,
    runs.c.segment_size, runs.c.total_segments, runs.c.done_segments,
    runs.c.total_trips, runs.c.done_trips,
)


def query():
    return select(runs, cases.c.title.label("case_title")).join(cases, cases.c.id == runs.c.case_id)


def listing():
    return select(*_LIST_COLUMNS, cases.c.title.label("case_title")).join(
        cases, cases.c.id == runs.c.case_id
    )


def get(db, run_id):
    run = db.execute(query().where(runs.c.id == run_id)).mappings().first()
    if run is None:
        raise HTTPException(404, "执行批次不存在")
    return dict(run)


def list_entries(db):
    statement = listing().order_by(runs.c.id.desc()).limit(100)
    return [dict(row) for row in db.execute(statement).mappings()]


def enqueue(db, case_id, parameter, actor):
    if parameter > CONFIG["max_parameter"]:
        raise HTTPException(422, f"参数不能超过 {CONFIG['max_parameter']}")
    case_service.get(db, case_id)
    change = db.execute(insert(runs).values(
        case_id=case_id, status=JobState.QUEUED, parameter=parameter, actor=actor, created_at=int(time())
    ))
    run_id = change.inserted_primary_key[0]
    audit.record(db, "enqueue", actor, "run", run_id)
    return get(db, run_id)
