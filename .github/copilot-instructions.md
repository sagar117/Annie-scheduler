## Purpose
This repository contains Temporal workflows and activities that drive Annie outbound calls and follow-ups. These instructions orient AI coding agents on the project's structure, conventions, and concrete examples so changes are safe and effective.

## Big picture (short)
- Temporal-based: workflows (deterministic logic) live in `annie_scheduler.py` / `annie_scheduler_orglevel.py` and `callback_checker.py` implements a separate workflow for follow-ups.
- Activities handle all external I/O (HTTP to the backend). Workflows call activities via `workflow.execute_activity` or start child workflows.
- Cron registration is done by starting workflows with `cron_schedule` (per-patient and org-level cron patterns are present).

## Key files
- `annie_scheduler.py` / `annie_scheduler_orglevel.py` — main workflows and activities (PatientDailyCall, OrgDailyScheduler, activities: `start_call`, `get_call_details`, `fetch_call_readings`, `mark_call_completed`, `list_patients_for_org`, `list_calls_for_org_on_date`).
- `callback_checker.py` — CallbackWindowChecker workflow: inspects completed calls and starts follow-up calls via `start_followup_call` activity.

## Runtime & common env vars
- Temporal: set `TEMPORAL_ENDPOINT` (default `localhost:7233`) and `TASK_QUEUE` (default `annie-task-queue`).
- Backend API: `ANNIE_BASE_URL` / `ANNIE_API_BASE` (backend host) and optional `ANNIE_API_AUTH_HEADER` / `ANNIE_API_AUTH_HEADER` for auth.
- Tuning/debug: `CALL_POLL_INTERVAL`, `CALL_POLL_ATTEMPTS`, `MAX_DAILY_RETRIES`, `EXTRA_DEBUG` (set to `1` to enable debug logs), `FORCE_RETRY` (dev-only behavior).

## How to run (examples)
- Run worker (starts Temporal Worker and processes workflows/activities):
  - python annie_scheduler.py    # default: runs worker for `annie_scheduler.py`
  - python callback_checker.py   # runs CallbackWindowChecker worker
- Start a test workflow and wait for result:
  - python annie_scheduler.py test --patient 2 --org 1 --wait
- Register per-patient cron schedules:
  - python annie_scheduler.py per_patient_cron --org 6 --hour 13 --minute 22 --start-now --replace
- Register callback checker cron:
  - python callback_checker.py create_callback_checker --org 6 --interval 5 --start-now --replace

## Important code patterns and conventions (do not break)
- Activities are the only place to perform HTTP/network I/O. Use `workflow.execute_activity` inside workflows. Avoid calling network code directly from workflow code (except client-only helpers like `_fetch_patients_http`).
- Deterministic constraints: workflow code must not use non-deterministic system calls directly; use `workflow.now()` and `workflow.sleep()` for time operations.
- Signals: `PatientDailyCall.call_status_signal(call_id, status)` is used to notify workflows of call status; workflows also poll via `get_call_details` activity as fallback.
- Readings normalization: code uses `normalize_readings_payload` / `parse_readings_from_response` to treat many backend shapes as measurement-like objects — pay attention when changing parsing logic.
- Cron IDs and naming:
  - Per-patient cron id: `patient-scheduler-{org_id}-{patient_id}`
  - Org scheduler id: `org-scheduler-{org_id}`
  - Callback checker base id: `callback-checker-{org_id}-{interval}m`

## Integration/endpoints (observed)
- Common API paths used by activities:
  - GET /api/patients[?org_id]
  - GET /api/patients/{id}
  - POST /api/calls/outbound
  - GET /api/calls/{call_id}
  - GET /api/calls/{call_id}/readings
  - POST /api/calls/{call_id}/complete

## Debugging tips
- To see verbose logs: set `EXTRA_DEBUG=1` and restart the worker.
- If Temporal connection fails, ensure `TEMPORAL_ENDPOINT` points to a reachable Temporal server (local dev or staging). The code retries a few times before failing.
- To simulate call flows in dev: use `test_mode` flags or `python annie_scheduler.py test --wait` which uses `test_mode=false` by default; set env `ANNIE_API_AUTH_HEADER` if backend requires auth.

## Guidance for AI edits (safety & locality)
- When changing anything in workflows, also update tests or add a small local runner (`start_test_workflow`) to validate behavior.
- Preserve activity contracts (inputs/outputs) — other services expect `call_id` strings and reading payload shapes.
- Avoid introducing non-determinism into workflow code. If a change requires external state, migrate it into an activity.

## Where to look for examples
- `annie_scheduler.py`: start/worker/cron CLI examples and activity usage. Search for `start_workflow`, `start_child_workflow`, `workflow.execute_activity`, and `@activity.defn` / `@workflow.defn`.
- `callback_checker.py`: examples of listing backend calls, parsing readings and starting follow-up calls.

If any of the runtime commands, env names, or backend endpoints are outdated or you want more examples (unit tests, CI steps), tell me which area to expand and I will iterate.
