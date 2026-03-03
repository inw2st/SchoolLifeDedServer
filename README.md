# SchoolLife pycomcigan relay

This service wraps `pycomcigan` so you can verify whether a school, grade, class, and date resolve to the expected timetable before wiring the iOS app to it.

## Endpoints

### `GET /health`

Basic health check.

### `GET /meta`

Returns the server timezone and the current/next-week windows that `pycomcigan` can safely support.

### `GET /schools/search?q=경기북과학고`

Searches school candidates using `pycomcigan.get_school_code()`.

### `GET /schools/resolve?school_name=경기북과학고&region_name=경기`

Returns a single exact-match school after filtering by school name and optional region or school code.

### `GET /timetable/verify?...`

Example:

```text
/timetable/verify?school_name=경기북과학고&region_name=경기&grade=3&class_num=1&target_date=2026-03-05
```

Response includes:

- matched school metadata
- derived `week_num`
- the specific day's period list
- the whole week grid for the same grade/class so you can compare against the real school timetable

## Local run

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

## Railway

Deploy this directory as the service root. `railway.json` already sets the start command and health check.
