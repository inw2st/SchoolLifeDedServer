from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
import re
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query

try:
    from pycomcigan import TimeTable, get_school_code
except ImportError:  # pragma: no cover - dependency is installed in deployment
    TimeTable = None
    get_school_code = None


APP_TIMEZONE = ZoneInfo("Asia/Seoul")
KOREAN_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
PERIOD_PREFIX_RE = re.compile(r"^\s*(\d+)교시:\s*(.*)$")
TEACHER_RE = re.compile(r"\(([^()]*)\)")


@dataclass
class SchoolCandidate:
    school_name: str
    region_name: str | None = None
    school_code: str | None = None
    region_code: str | None = None
    raw: Any = None


app = FastAPI(
    title="SchoolLife Comcigan Relay",
    version="0.1.0",
    description=(
        "pycomcigan relay server for searching schools and validating "
        "date-specific timetable lookups."
    ),
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/meta")
def meta() -> dict[str, Any]:
    today = datetime.now(APP_TIMEZONE).date()
    current_monday = week_monday(today)
    next_monday = current_monday + timedelta(days=7)
    return {
        "timezone": "Asia/Seoul",
        "today": today.isoformat(),
        "current_week": {
            "week_num": 0,
            "monday": current_monday.isoformat(),
            "sunday": (current_monday + timedelta(days=6)).isoformat(),
        },
        "next_week": {
            "week_num": 1,
            "monday": next_monday.isoformat(),
            "sunday": (next_monday + timedelta(days=6)).isoformat(),
        },
        "limitations": [
            "pycomcigan documents week_num=0 for current week and week_num=1 for next week",
            "arbitrary past/far-future dates are not guaranteed by the library",
        ],
    }


@app.get("/schools/search")
def search_schools(
    q: str = Query(..., min_length=1, description="School name or region keyword"),
) -> dict[str, Any]:
    ensure_library_ready()
    candidates = find_school_candidates(q)
    return {
        "query": q,
        "count": len(candidates),
        "schools": [asdict(candidate) for candidate in candidates],
    }


@app.get("/schools/resolve")
def resolve_school(
    school_name: str = Query(..., min_length=1),
    region_name: str | None = Query(default=None),
    school_code: str | None = Query(default=None),
) -> dict[str, Any]:
    ensure_library_ready()
    candidate = select_school_candidate(
        school_name=school_name,
        region_name=region_name,
        school_code=school_code,
    )
    return {"school": asdict(candidate)}


@app.get("/timetable/verify")
def verify_timetable(
    school_name: str = Query(..., min_length=1),
    grade: int = Query(..., ge=1, le=12),
    class_num: int = Query(..., ge=1, le=50),
    target_date: str = Query(..., description="YYYY-MM-DD"),
    region_name: str | None = Query(default=None),
    school_code: str | None = Query(default=None),
    include_weekly_grid: bool = Query(default=True),
) -> dict[str, Any]:
    ensure_library_ready()
    try:
        parsed_date = parse_iso_date(target_date)
        week_num = infer_week_num(parsed_date)
        candidate = select_school_candidate(
            school_name=school_name,
            region_name=region_name,
            school_code=school_code,
        )
        timetable_obj = load_timetable(candidate.school_name, week_num)

        daily_subjects, weekly_grid = extract_grade_class_schedule(
            timetable_data=getattr(timetable_obj, "timetable", None),
            grade=grade,
            class_num=class_num,
            target_date=parsed_date,
        )

        homeroom_name = None
        if hasattr(timetable_obj, "homeroom"):
            try:
                homeroom_name = make_json_safe(timetable_obj.homeroom(grade, class_num))
            except Exception:
                homeroom_name = None

        return {
            "school": asdict(candidate),
            "request": {
                "target_date": parsed_date.isoformat(),
                "weekday": weekday_payload(parsed_date),
                "grade": grade,
                "class_num": class_num,
                "week_num": week_num,
            },
            "daily_subjects": daily_subjects,
            "weekly_grid": weekly_grid if include_weekly_grid else None,
            "homeroom": homeroom_name,
            "raw_summary": {
                "daily_count": len(daily_subjects),
                "weekly_day_count": len(weekly_grid),
                "timetable_type": type(getattr(timetable_obj, "timetable", None)).__name__,
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "unexpected timetable verification error",
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        ) from exc


@app.get("/timetable/debug")
def debug_timetable(
    school_name: str = Query(..., min_length=1),
    grade: int = Query(..., ge=1, le=12),
    class_num: int = Query(..., ge=1, le=50),
    target_date: str = Query(..., description="YYYY-MM-DD"),
    region_name: str | None = Query(default=None),
    school_code: str | None = Query(default=None),
) -> dict[str, Any]:
    ensure_library_ready()

    parsed_date = parse_iso_date(target_date)
    week_num = infer_week_num(parsed_date)
    candidate = select_school_candidate(
        school_name=school_name,
        region_name=region_name,
        school_code=school_code,
    )
    timetable_obj = load_timetable(candidate.school_name, week_num)
    timetable_data = getattr(timetable_obj, "timetable", None)

    grade_bucket = safe_index(timetable_data, grade)
    class_bucket = safe_index(grade_bucket, class_num)

    return {
        "school": asdict(candidate),
        "request": {
            "target_date": parsed_date.isoformat(),
            "grade": grade,
            "class_num": class_num,
            "week_num": week_num,
        },
        "time_table_object_type": type(timetable_obj).__name__,
        "time_table_object_dir": sorted(
            name for name in dir(timetable_obj) if not name.startswith("__")
        ),
        "time_table_object_dict": make_json_safe(getattr(timetable_obj, "__dict__", {})),
        "timetable_type": type(timetable_data).__name__,
        "grade_bucket_type": type(grade_bucket).__name__ if grade_bucket is not None else None,
        "class_bucket_type": type(class_bucket).__name__ if class_bucket is not None else None,
        "raw_class_bucket": make_json_safe(class_bucket),
        "normalized_weekly_grid": normalize_week_schedule(class_bucket) if class_bucket is not None else None,
        "string_repr": str(timetable_obj),
    }


def ensure_library_ready() -> None:
    if TimeTable is None or get_school_code is None:
        raise HTTPException(
            status_code=500,
            detail=(
                "pycomcigan is not installed. Run `pip install -r requirements.txt` "
                "before starting the server."
            ),
        )


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="target_date must use YYYY-MM-DD format",
        ) from exc


def week_monday(value: date) -> date:
    return value - timedelta(days=value.weekday())


def infer_week_num(target: date) -> int:
    today = datetime.now(APP_TIMEZONE).date()
    current_monday = week_monday(today)
    target_monday = week_monday(target)
    diff_days = (target_monday - current_monday).days

    if diff_days not in (0, 7):
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    "pycomcigan currently exposes current week and next week only "
                    "(week_num 0 or 1)."
                ),
                "today": today.isoformat(),
                "supported_dates": {
                    "current_week": {
                        "from": current_monday.isoformat(),
                        "to": (current_monday + timedelta(days=6)).isoformat(),
                    },
                    "next_week": {
                        "from": (current_monday + timedelta(days=7)).isoformat(),
                        "to": (current_monday + timedelta(days=13)).isoformat(),
                    },
                },
            },
        )

    return diff_days // 7


def weekday_payload(value: date) -> dict[str, Any]:
    weekday = value.weekday()
    return {
        "index": weekday,
        "name_ko": KOREAN_WEEKDAYS[weekday],
    }


def find_school_candidates(query: str) -> list[SchoolCandidate]:
    try:
        result = get_school_code(query)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"school lookup failed: {exc}") from exc

    if not isinstance(result, list):
        raise HTTPException(
            status_code=502,
            detail=f"unexpected school lookup response type: {type(result).__name__}",
        )

    return [normalize_school_candidate(item) for item in result]


def normalize_school_candidate(item: Any) -> SchoolCandidate:
    if isinstance(item, dict):
        return SchoolCandidate(
            school_name=str(item.get("school_name") or item.get("name") or ""),
            region_name=string_or_none(item.get("region_name") or item.get("region")),
            school_code=string_or_none(item.get("school_code") or item.get("code")),
            region_code=string_or_none(item.get("region_code")),
            raw=item,
        )

    if isinstance(item, (list, tuple)):
        parts = list(item)
        return SchoolCandidate(
            school_name=str(parts[2]) if len(parts) > 2 else "",
            region_name=str(parts[1]) if len(parts) > 1 else None,
            school_code=str(parts[0]) if len(parts) > 0 else None,
            region_code=str(parts[3]) if len(parts) > 3 else None,
            raw=make_json_safe(parts),
        )

    return SchoolCandidate(school_name=str(item), raw=make_json_safe(item))


def string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def select_school_candidate(
    school_name: str,
    region_name: str | None,
    school_code: str | None,
) -> SchoolCandidate:
    candidates = find_school_candidates(school_name)

    matches = [
        candidate
        for candidate in candidates
        if candidate.school_name == school_name
    ]

    if region_name:
        matches = [
            candidate
            for candidate in matches
            if candidate.region_name == region_name
        ]

    if school_code:
        matches = [
            candidate
            for candidate in matches
            if candidate.school_code == school_code
        ]

    if len(matches) == 1:
        return matches[0]

    if not matches:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "no exact school match found",
                "search_name": school_name,
                "region_name": region_name,
                "school_code": school_code,
                "candidates": [asdict(candidate) for candidate in candidates[:20]],
            },
        )

    raise HTTPException(
        status_code=409,
        detail={
            "message": "multiple schools matched; add region_name or school_code",
            "matches": [asdict(candidate) for candidate in matches[:20]],
        },
    )


def load_timetable(school_name: str, week_num: int) -> Any:
    try:
        return TimeTable(school_name, week_num=week_num)
    except TypeError:
        # Older versions may not accept a keyword argument.
        try:
            return TimeTable(school_name, week_num)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"timetable lookup failed: {exc}",
            ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"timetable lookup failed: {exc}",
        ) from exc


def extract_grade_class_schedule(
    timetable_data: Any,
    grade: int,
    class_num: int,
    target_date: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if timetable_data is None:
        raise HTTPException(status_code=502, detail="timetable data is missing")

    grade_bucket = safe_index(timetable_data, grade)
    class_bucket = safe_index(grade_bucket, class_num)

    if class_bucket is None:
        raise HTTPException(
            status_code=404,
            detail=f"grade {grade} class {class_num} timetable was not found",
        )

    weekly_grid = normalize_week_schedule(class_bucket)
    weekday_index = target_date.weekday()

    if weekday_index >= len(weekly_grid):
        raise HTTPException(
            status_code=404,
            detail="target weekday was not present in the weekly timetable payload",
        )

    daily_subjects = weekly_grid[weekday_index]["periods"]
    return daily_subjects, weekly_grid


def safe_index(container: Any, index: int) -> Any:
    if isinstance(container, dict):
        if index in container:
            return container[index]
        key = str(index)
        return container.get(key)

    if isinstance(container, (list, tuple)):
        if 0 <= index < len(container):
            return container[index]
        alt = index - 1
        if 0 <= alt < len(container):
            return container[alt]

    return None


def normalize_week_schedule(class_bucket: Any) -> list[dict[str, Any]]:
    if not isinstance(class_bucket, (list, tuple)):
        raise HTTPException(
            status_code=502,
            detail=f"unexpected class timetable shape: {type(class_bucket).__name__}",
        )

    normalized_days = normalize_day_buckets(class_bucket)
    weekly_grid: list[dict[str, Any]] = []

    for weekday_position, day_bucket in enumerate(normalized_days):
        if weekday_position >= len(KOREAN_WEEKDAYS):
            break

        periods: list[dict[str, Any]] = []
        if isinstance(day_bucket, (list, tuple)):
            for period_position, raw_subject in enumerate(day_bucket, start=1):
                periods.append(parse_period_entry(raw_subject, period_position))
        else:
            periods.append(parse_period_entry(day_bucket, 1))

        weekly_grid.append(
            {
                "weekday_index": weekday_position,
                "weekday_name_ko": KOREAN_WEEKDAYS[weekday_position],
                "periods": periods,
            }
        )

    return weekly_grid


def normalize_day_buckets(class_bucket: Any) -> list[Any]:
    days = list(class_bucket)

    # Some pycomcigan schools return a leading dummy slot, then Monday-Friday.
    # Example: [dummy, mon, tue, wed, thu, fri]
    if len(days) == 6 and is_effectively_empty_day(days[0]):
        return days[1:]

    return days


def is_effectively_empty_day(day_bucket: Any) -> bool:
    if isinstance(day_bucket, (list, tuple)):
        if not day_bucket:
            return True
        return all(parse_period_entry(item, idx + 1)["is_placeholder"] for idx, item in enumerate(day_bucket))
    return parse_period_entry(day_bucket, 1)["is_placeholder"]


def normalize_subject(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_period_entry(raw_value: Any, fallback_period: int) -> dict[str, Any]:
    raw_text = normalize_subject(raw_value)
    parsed_period = fallback_period
    body = raw_text

    match = PERIOD_PREFIX_RE.match(raw_text)
    if match:
        parsed_period = int(match.group(1))
        body = match.group(2).strip()

    is_substitution = body.endswith("(대체)") or body.endswith(" (대체)")
    if is_substitution:
        body = body[: body.rfind("(대체)")].strip()

    teacher_name = None
    teacher_matches = TEACHER_RE.findall(body)
    if teacher_matches:
        teacher_name = teacher_matches[-1].strip() or None
        body = TEACHER_RE.sub("", body).strip()

    subject_name = normalize_placeholder_subject(body)
    is_placeholder = subject_name == ""

    return {
        "period": parsed_period,
        "display_text": raw_text,
        "subject": subject_name,
        "teacher": teacher_name,
        "is_substitution": is_substitution,
        "is_placeholder": is_placeholder,
        "raw": make_json_safe(raw_value),
    }


def normalize_placeholder_subject(value: str) -> str:
    cleaned = value.strip()
    if cleaned in {"", "()"}:
        return ""

    # pycomcigan sometimes returns placeholders like "(홍길*)" or empty parens.
    if cleaned.startswith("(") and cleaned.endswith(")"):
        inner = cleaned[1:-1].strip()
        if inner == "" or "*" in inner:
            return ""

    return cleaned


def make_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except Exception:
            return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(key): make_json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [make_json_safe(item) for item in value]
    return str(value)
