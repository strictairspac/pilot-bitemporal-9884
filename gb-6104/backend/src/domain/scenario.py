from datetime import timedelta

from src.utils.serial import content_digest

from .contract_terms import ANCHOR_DAY, MAX_TRIPS, charge_cents, parse, validate
from .temporal_sql import compare_revisions

# A month-end recheck takes at most 200 voyages, while an opening batch may take
# up to MAX_TRIPS. Larger batches are recomputed in slices of at most this many
# trips.
SEGMENT_SIZE = 200

CONFIG = {
    "title": "鹭汊引航·费率追溯复核",
    "team": "鹭汊港引航结算组（8 人）",
    "description": "费率补录可以追溯服务日期，按原开账日和追溯日双重口径核算补差及暂缓航次。",
    "direction": "关系建模 / 双时间版本",
    "mechanism": "MySQL 同时查询业务生效与系统登记两个半开区间，按历史认知核对费差。",
    "modules": ["开账交接", "航次与费率", "双时间复算", "港务调账单"],
    "unit": "引航开账批",
    "max_parameter": 180,
    "parameter_label": "复核日（11 月 1 日起第几天）",
    "default_parameter": 23,
    "facts": ["可调账航次", "待核航次", "净补差（分）"],
    "palette": 4,
    "fixture": "LC-PILOT-1109",
}


def trip(voyage, zone, day, ton):
    return {"voyage": voyage, "zone": zone, "service_date": day, "gross_ton": ton}


def version(rev, zone, valid_from, valid_until, entered, superseded, rate, minimum):
    return {
        "revision": rev, "zone": zone, "valid_from": valid_from, "valid_until": valid_until,
        "recorded_from": entered, "recorded_until": superseded, "rate": rate, "minimum": minimum,
    }


TERMS = [
    version("N-ORIG", "NORTH", "2026-11-01", None, "2026-10-15", "2026-11-18", 18000, 45000),
    version("N-NOV1", "NORTH", "2026-11-01", "2026-11-04", "2026-11-18", None, 18000, 45000),
    version("N-RETRO", "NORTH", "2026-11-04", None, "2026-11-18", None, 20000, 48000),
    version("S-ORIG", "SOUTH", "2026-11-01", None, "2026-10-15", None, 19500, 39000),
    version("S-OVERLAP", "SOUTH", "2026-11-03", "2026-11-06", "2026-11-18", None, 22000, 40000),
]

FIXTURES = [
    {
        "code": "LC-PILOT-1109", "title": "LC-PILOT-1109 开账批", "description": "北槽追溯新费率，南槽条款重叠。",
        "payload": {"issued_on": "2026-11-09", "versions": TERMS, "trips": [
            trip("LC-V21", "NORTH", "2026-11-03", 3660),
            trip("LC-V22", "NORTH", "2026-11-04", 2400),
            trip("LC-V23", "NORTH", "2026-11-08", 1200),
            trip("LC-V24", "SOUTH", "2026-11-04", 6010),
            trip("LC-V25", "OUTER", "2026-11-08", 3000),
        ]},
    },
    {
        "code": "LC-PILOT-1110", "title": "南槽单日边界", "description": "南槽重叠条款在 11 月 6 日结束。",
        "payload": {"issued_on": "2026-11-10", "versions": TERMS[3:], "trips": [
            trip("LC-V31", "SOUTH", "2026-11-06", 2700),
        ]},
    },
]


def _charge(record):
    if record["match_count"] != 1:
        return None
    return charge_cents(record["gross_ton"], record["rate_cents"], record["min_cents"])


def _clause(record):
    candidate_count = record["match_count"]
    if candidate_count == 0:
        return "费率缺失"
    if candidate_count > 1:
        return f"{candidate_count} 条费率重叠"
    return record["revision_code"]


def _rows(old_version, new_version):
    rows = []
    for old, new in zip(old_version, new_version, strict=True):
        original, current = _charge(old), _charge(new)
        delta = current - original if original is not None and current is not None else None
        rows.append({
            "航次": old["voyage"], "航段": old["zone"], "服务日": old["service_date"].isoformat(),
            "总吨": old["gross_ton"], "原开账条款": _clause(old), "复核条款": _clause(new),
            "原账（分）": original, "复核账（分）": current, "净差（分）": delta,
        })
    # The worker may receive segments in any order. Sort every slice by the same
    # key so concatenating slices reproduces a single whole-batch ordering.
    rows.sort(key=lambda row: row["航次"])
    return rows


def _iso_date(value):
    return value.isoformat() if value is not None else None


def plan(payload, parameter):
    """Validate a batch and freeze a deterministic, resumable plan for it."""
    if type(parameter) is not int or not 1 <= parameter <= 180:
        raise ValueError("复核日在 11 月 1 日起第 1–180 天")
    trips, terms, issued = parse(payload)
    as_of = ANCHOR_DAY + timedelta(days=parameter - 1)
    if as_of < issued:
        raise ValueError("复核日不得早于原开账日")
    ordered = sorted(trips, key=lambda item: item["voyage"])
    digest = content_digest({
        "parameter": parameter,
        "issued_on": _iso_date(issued),
        "as_of": _iso_date(as_of),
        "trips": [{**item, "service_date": _iso_date(item["service_date"])} for item in ordered],
        "terms": [{
            **item,
            "valid_from": _iso_date(item["valid_from"]),
            "valid_until": _iso_date(item["valid_until"]),
            "recorded_from": _iso_date(item["recorded_from"]),
            "recorded_until": _iso_date(item["recorded_until"]),
        } for item in terms],
    })
    return {
        "parameter": parameter, "issued": issued, "as_of": as_of,
        "trips": ordered, "terms": terms, "digest": digest,
        "trips_count": len(ordered), "segment_size": SEGMENT_SIZE,
        "count": (len(ordered) + SEGMENT_SIZE - 1) // SEGMENT_SIZE,
    }


def evaluate(batch_plan, start, stop, db):
    """Recompute one contiguous slice of a frozen plan."""
    slice_trips = batch_plan["trips"][start:stop]
    if not slice_trips:
        return []
    old_version, new_version = compare_revisions(
        db, slice_trips, batch_plan["terms"], batch_plan["issued"], batch_plan["as_of"],
    )
    return _rows(old_version, new_version)


def summarize(batch_plan, rows):
    adjustable = [row["净差（分）"] for row in rows if row["净差（分）"] is not None]
    held = len(rows) - len(adjustable)
    return {
        "summary": (
            f"按 {_iso_date(batch_plan['issued'])} 开账、{_iso_date(batch_plan['as_of'])} 复核，"
            f"{len(adjustable)} 条可结算，{held} 条暂缓。"
        ),
        "metrics": {"可调账航次": len(adjustable), "待核航次": held, "净补差（分）": sum(adjustable)},
        "rows": rows,
    }


def run(payload, parameter, db):
    """Single-shot convenience path, used for parity checks and small batches."""
    batch_plan = plan(payload, parameter)
    rows = evaluate(batch_plan, 0, batch_plan["trips_count"], db)
    return summarize(batch_plan, rows)
