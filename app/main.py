"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience, but environment variables take precedence)
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Real environment variables must take precedence over .env
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes(database):
    """Idempotently create required indexes on startup."""
    # employees indexes
    database.employees.create_index([("emp_code", ASCENDING)], unique=True)
    database.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    database.employees.create_index([("joined_on", ASCENDING)])

    # attendance_logs indexes
    database.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    database.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    database.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)])
    database.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)]
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create required indexes idempotently at startup
    init_indexes(db)
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """Round half-up per R8."""
    if val is None:
        return None
    d = Decimal(str(val))
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def parse_hhmm(s: str) -> tuple[int, int]:
    h, m = map(int, s.split(":"))
    return h, m


def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: Instants are truncated to whole seconds before any calculation."""
    return dt.replace(microsecond=0)


def epoch_ms_to_datetime_utc(ms: int) -> datetime:
    """Convert epoch milliseconds to timezone-aware UTC datetime truncated to seconds."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    return int(dt.timestamp()) * 1000


def get_attendance_date(
    punch_in_utc: datetime, shift_start: str, shift_end: str
) -> tuple[str, datetime, datetime]:
    """
    R1: Time zone and attendance date (IST).
    For overnight shifts (shift_end <= shift_start), a punch-in earlier than shift_end
    belongs to the shift that started the previous day.
    Returns: (attendance_date_str, shift_start_ist, shift_end_ist)
    """
    punch_in_ist = truncate_to_seconds(punch_in_utc.astimezone(IST))
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    is_overnight = (eh, em) <= (sh, sm)

    if is_overnight and (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second) < (eh, em, 0):
        attendance_date = punch_in_ist.date() - timedelta(days=1)
    else:
        attendance_date = punch_in_ist.date()

    date_str = attendance_date.isoformat()
    start_dt = datetime.combine(attendance_date, time(sh, sm), tzinfo=IST)
    if is_overnight:
        end_dt = datetime.combine(attendance_date + timedelta(days=1), time(eh, em), tzinfo=IST)
    else:
        end_dt = datetime.combine(attendance_date, time(eh, em), tzinfo=IST)

    return date_str, start_dt, end_dt


def compute_late_minutes(punch_in_utc: datetime, start_dt_ist: datetime) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly > 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start.
    """
    punch_in_ist = truncate_to_seconds(punch_in_utc.astimezone(IST))
    diff_sec = (punch_in_ist - start_dt_ist).total_seconds()
    if diff_sec > 600:  # strictly > 10:00
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    p_in = truncate_to_seconds(punch_in_utc)
    p_out = truncate_to_seconds(punch_out_utc)
    sec = (p_out - p_in).total_seconds()
    hours = sec / 3600.0
    return round_half_up(hours, 2)


def compute_overtime(punch_out_utc: datetime, end_dt_ist: datetime) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30; otherwise 0.
    """
    punch_out_ist = truncate_to_seconds(punch_out_utc.astimezone(IST))
    diff_sec = (punch_out_ist - end_dt_ist).total_seconds()
    minutes = int(diff_sec // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
from enum import Enum
from pydantic import BaseModel, Field, model_validator
from fastapi import FastAPI, HTTPException, Query, Path


# --------------------------------------------------------------------------- #
# Models & Schemas
# --------------------------------------------------------------------------- #
class StatusEnum(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[StatusEnum] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serialization Helpers
# --------------------------------------------------------------------------- #
def to_epoch_ms(val) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return datetime_to_epoch_ms(val)
    if isinstance(val, (int, float)):
        return int(val)
    return None


def serialize_employee_doc(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


def serialize_history_entry(entry: dict) -> dict:
    changes = {}
    for field, change in entry.get("changes", {}).items():
        from_val = change.get("from")
        to_val = change.get("to")
        if field in ("punch_in", "punch_out"):
            from_val = to_epoch_ms(from_val)
            to_val = to_epoch_ms(to_val)
        changes[field] = {"from": from_val, "to": to_val}

    return {
        "at": to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance_doc(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0) if doc.get("late_minutes") is not None else 0,
        "overtime_minutes": doc.get("overtime_minutes", 0) if doc.get("overtime_minutes") is not None else 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Endpoints provided (Part A)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    now_utc = truncate_to_seconds(datetime.now(timezone.utc))
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match."),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        punch_in_utc = truncate_to_seconds(datetime.now(timezone.utc))

    date_str, start_dt_ist, _ = get_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_utc, start_dt_ist)

    status_str = body.status.value if hasattr(body.status, "value") else str(body.status)
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": status_str,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_doc(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusEnum] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    status_str = status.value if status is not None else None
    q, sort = build_attendance_query(emp_code, date_from, date_to, status_str)

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort(sort)
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        punch_out_utc = truncate_to_seconds(datetime.now(timezone.utc))

    # Find the candidate record (prefer open record, else most recent record with punch_in)
    open_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_out": None, "punch_in": {"$ne": None}},
        sort=[("punch_in", DESCENDING)],
    )

    if open_record:
        record = open_record
    else:
        record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
            sort=[("punch_in", DESCENDING)],
        )

    if not record:
        raise HTTPException(status_code=404, detail="No punch-in record found for punch-out")

    punch_in_utc = record["punch_in"]

    # 422 if punched_at <= punch_in
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")

    # If punched_at > punch_in + 24h
    if punch_out_utc > punch_in_utc + timedelta(hours=24):
        if record.get("punch_out") is not None:
            raise HTTPException(status_code=404, detail="No open punch-in record found")
        raise HTTPException(
            status_code=422, detail="punched_at cannot be more than 24 hours after punch_in"
        )

    # 409 if already punched out
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    # Reconstruct shift end datetime in IST
    sh, sm = parse_hhmm(emp["shift_start"])
    eh, em = parse_hhmm(emp["shift_end"])
    is_overnight = (eh, em) <= (sh, sm)
    rec_date = datetime.fromisoformat(record["date"]).date()
    if is_overnight:
        end_date = rec_date + timedelta(days=1)
    else:
        end_date = rec_date
    end_dt_ist = datetime.combine(end_date, time(eh, em), tzinfo=IST)

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime_minutes = compute_overtime(punch_out_utc, end_dt_ist)
    half_day = compute_half_day(work_hours)

    # Concurrency safe: atomic find_one_and_update matching punch_out is None
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(
            status_code=409, detail="Record was punched out by a concurrent request"
        )

    return serialize_attendance_doc(updated_doc)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not emp or not record:
        raise HTTPException(status_code=404, detail="Employee or attendance record not found")

    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0) if record.get("late_minutes") is not None else 0
    old_overtime_minutes = (
        record.get("overtime_minutes", 0) if record.get("overtime_minutes") is not None else 0
    )
    old_half_day = bool(record.get("half_day", False))

    new_status = body.status.value if body.status is not None else old_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch_in or punch_out when status is ABSENT or LEAVE",
            )
        target_punch_in = None
        target_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            target_punch_in = epoch_ms_to_datetime_utc(body.punch_in)
        else:
            target_punch_in = old_punch_in

        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        # R1: punch_in must stay on the record's attendance date
        calc_date, start_dt_ist, end_dt_ist = get_attendance_date(
            target_punch_in, emp["shift_start"], emp["shift_end"]
        )
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {calc_date}, not {date}",
            )

        if body.punch_out is not None:
            target_punch_out = epoch_ms_to_datetime_utc(body.punch_out)
        else:
            target_punch_out = old_punch_out

        if target_punch_out is not None:
            if target_punch_out <= target_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if target_punch_out > target_punch_in + timedelta(hours=24):
                raise HTTPException(
                    status_code=422,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )

        new_late_minutes = compute_late_minutes(target_punch_in, start_dt_ist)
        if target_punch_out is not None:
            new_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            new_overtime_minutes = compute_overtime(target_punch_out, end_dt_ist)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Detect actual changes
    changes = {}
    if old_status != new_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if old_punch_in != target_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": target_punch_in}
    if old_punch_out != target_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": target_punch_out}
    if old_work_hours != new_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if old_late_minutes != new_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if old_overtime_minutes != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if old_half_day != new_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request made no changes to the record")

    history_entry = {
        "at": truncate_to_seconds(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    existing_history = record.get("history")
    if existing_history is None:
        history_filter = {"$or": [{"history": {"$exists": False}}, {"history": None}, {"history": []}]}
    else:
        history_filter = {"history": existing_history}

    filter_query = {
        "_id": record["_id"],
        **history_filter,
    }

    update_query = {
        "$set": {
            "status": new_status,
            "punch_in": target_punch_in,
            "punch_out": target_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        },
        "$push": {"history": history_entry},
    }

    updated_doc = db.attendance_logs.find_one_and_update(
        filter_query,
        update_query,
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Concurrent modification conflict")

    return serialize_attendance_doc(updated_doc)


# --------------------------------------------------------------------------- #
# Query Builders & Analytics Aggregation Pipelines (Part B2 & Part C)
# --------------------------------------------------------------------------- #
def get_month_date_range(month_str: str) -> tuple[str, str, int]:
    year, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    return f"{month_str}-01", f"{month_str}-{num_days:02d}", num_days


def compute_employee_working_days(joined_on_str: str, month_str: str) -> int:
    year, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = max(date(year, m, 1), date.fromisoformat(joined_on_str))
    end_date = date(year, m, num_days)
    if start_date > end_date:
        return 0
    working_days = 0
    cur = start_date
    while cur <= end_date:
        if cur.weekday() < 5:  # Monday=0 ... Friday=4
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def build_attendance_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
    return q, sort


def build_employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str):
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": {
                            "if": {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {"dateString": "$date"}
                                                }
                                            },
                                            5,
                                        ]
                                    },
                                ]
                            },
                            "then": {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            "else": 0.0,
                        }
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def build_department_summary_pipeline(
    month_start: str, month_end: str, department: Optional[str] = None
):
    match_stage = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": {
                                    "if": {
                                        "$and": [
                                            {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$lte": [
                                                    {
                                                        "$isoDayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$log.date"
                                                            }
                                                        }
                                                    },
                                                    5,
                                                ]
                                            },
                                        ]
                                    },
                                    "then": {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1.0]},
                                    "else": 0.0,
                                }
                            },
                        }
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "item",
                            "in": "$$item.work_hours",
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$cond": [{"$eq": ["$$log.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$cond": [{"$eq": ["$$log.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(
    month_start: str, month_end: str, limit: int = 10, department: Optional[str] = None
):
    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {
                            "$rank": {}
                        }
                    },
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def build_department_trend_pipeline(
    emp_codes: list[str],
    join_dates: list[str],
    date_from: str,
    date_to: str,
    dt_from: datetime,
    dt_to_exclusive: datetime,
):
    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": date_from, "$lte": date_to},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": {
                            "if": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            "then": {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            "else": 0.0,
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "date_str": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [dt_from, dt_to_exclusive],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date_dt"}, 5]},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"date": 1}},
        {
            "$addFields": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": join_dates,
                            "as": "jd",
                            "cond": {"$lte": ["$$jd", "$date"]},
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": {
                        "if": {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        "then": {"$divide": ["$present_count", "$headcount"]},
                        "else": None,
                    }
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
    ]


# --------------------------------------------------------------------------- #
# Analytics Endpoints (Part B2)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    m_start, m_end, _ = get_month_date_range(month)
    working_days = compute_employee_working_days(emp["joined_on"], month)

    pipeline = build_employee_monthly_pipeline(emp_code, m_start, m_end)
    res = list(db.attendance_logs.aggregate(pipeline))
    stats = res[0] if res else {
        "present_days": 0.0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }

    present_days = stats["present_days"]
    attendance_pct = (
        round_half_up((present_days / working_days) * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats["leave_days"],
        "late_count": stats["late_count"],
        "total_late_minutes": stats["total_late_minutes"],
        "total_overtime_minutes": stats["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    m_start, m_end, _ = get_month_date_range(month)
    pipeline = build_department_summary_pipeline(m_start, m_end, department)
    docs = list(db.employees.aggregate(pipeline))
    items = []
    for d in docs:
        avg_wh = round_half_up(d["avg_work_hours"], 2) if d["avg_work_hours"] is not None else None
        items.append(
            {
                "department": d["department"],
                "headcount": d["headcount"],
                "present_days": d["present_days"],
                "avg_work_hours": avg_wh,
                "late_count": d["late_count"],
                "total_late_minutes": d["total_late_minutes"],
                "leave_count": d["leave_count"],
                "on_duty_count": d["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    m_start, m_end, _ = get_month_date_range(month)
    pipeline = build_late_leaderboard_pipeline(m_start, m_end, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    emp_docs = list(db.employees.find({"department": department}))
    if not emp_docs:
        raise HTTPException(status_code=404, detail="Department not found")

    emp_codes = [e["emp_code"] for e in emp_docs]
    join_dates = [e["joined_on"] for e in emp_docs]

    dt_from = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    dt_to = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc)
    dt_to_exclusive = dt_to + timedelta(days=1)

    pipeline = build_department_trend_pipeline(
        emp_codes, join_dates, from_date, to_date, dt_from, dt_to_exclusive
    )
    docs = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for d in docs:
        rate = round_half_up(d["attendance_rate"], 4) if d["attendance_rate"] is not None else None
        mavg = round_half_up(d["moving_avg_7d"], 4) if d["moving_avg_7d"] is not None else None
        items.append(
            {
                "date": d["date"],
                "is_working_day": bool(d["is_working_day"]),
                "headcount": d["headcount"],
                "present_count": d["present_count"],
                "late_count": d["late_count"],
                "attendance_rate": rate,
                "moving_avg_7d": mavg,
            }
        )

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint (Part C)
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str = Path(
        ...,
        pattern="^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$",
    ),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusEnum] = Query(None),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        status_val = status.value if status is not None else None
        q, sort_list = build_attendance_query(emp_code, date_from, date_to, status_val)
        skip = (page - 1) * page_size
        explain_doc = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": dict(sort_list),
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required for employee_monthly",
            )
        m_start, m_end, _ = get_month_date_range(month)
        pipeline = build_employee_monthly_pipeline(emp_code, m_start, m_end)
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422, detail="month is required for department_summary"
            )
        m_start, m_end, _ = get_month_date_range(month)
        pipeline = build_department_summary_pipeline(m_start, m_end, department)
        explain_doc = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_doc}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422, detail="month is required for late_leaderboard"
            )
        m_start, m_end, _ = get_month_date_range(month)
        pipeline = build_late_leaderboard_pipeline(m_start, m_end, limit or 10, department)
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    elif endpoint == "department_trend":
        dept = department
        if not dept or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required for department_trend",
            )
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from or (d_to - d_from).days > 92:
            raise HTTPException(status_code=422, detail="Invalid date range")

        emp_docs = list(db.employees.find({"department": dept}))
        emp_codes = [e["emp_code"] for e in emp_docs]
        join_dates = [e["joined_on"] for e in emp_docs]
        dt_from = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
        dt_to = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc)
        dt_to_exclusive = dt_to + timedelta(days=1)

        pipeline = build_department_trend_pipeline(
            emp_codes, join_dates, from_date, to_date, dt_from, dt_to_exclusive
        )
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

