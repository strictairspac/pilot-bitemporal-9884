from pydantic import BaseModel, Field
from src.constants.job_state import JobState


class RunInput(BaseModel):
    parameter: int = Field(strict=True, ge=1)


class RunEntry(BaseModel):
    id: int
    case_id: int
    case_title: str
    status: JobState
    parameter: int
    actor: str
    created_at: int
    finished_at: int | None
    result: dict | None = None
    failure: str | None
    digest: str | None
    segment_size: int = 0
    total_segments: int = 0
    done_segments: int = 0
    total_trips: int = 0
    done_trips: int = 0
