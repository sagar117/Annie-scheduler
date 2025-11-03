# callback_checker.py
"""
CallbackWindowChecker — checks ALL completed Annie calls for each patient today,
and initiates follow-up calls (agent 'annie_RPM_followup') for patients with no readings.

Fix: _ts_key now returns a float epoch timestamp for consistent sorting.
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from temporalio import workflow, activity
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.common import RetryPolicy

# Local implementations (moved here so this file is self-contained)
# - connect_client: helper to connect to Temporal (same retry behavior as annie_scheduler)
# - list_calls_for_org_on_date, fetch_call_readings: activities that call backend endpoints.
TASK_QUEUE = os.getenv("ANNIE_TASK_QUEUE", "annie-task-queue")
TEMPORAL_ENDPOINT = os.getenv("TEMPORAL_ENDPOINT", "localhost:7233")


async def connect_client(retries: int = 6, delay: int = 5) -> Client:
    """Connect to Temporal endpoint with a small retry loop.

    Mirrors the helper in `annie_scheduler.py` but keeps defaults local to this module.
    """
    for i in range(retries):
        try:
            client = await Client.connect(TEMPORAL_ENDPOINT)
            logger.info("Connected to Temporal at %s", TEMPORAL_ENDPOINT)
            return client
        except Exception as e:
            logger.warning("Temporal connect failed (%d/%d): %s", i + 1, retries, e)
            await asyncio.sleep(delay)
    logger.error("Could not connect to Temporal at %s after %d attempts", TEMPORAL_ENDPOINT, retries)
    raise RuntimeError("Temporal connect failed")


@activity.defn
async def list_calls_for_org_on_date(org_id: int, yyyy_mm_dd: str) -> list:
    """Activity: GET /api/calls?org_id={org_id}&date={yyyy_mm_dd}

    Uses `API_BASE` and `AUTH_HEADER` defined later in this file. Returns a list or [] on non-JSON response.
    """
    import aiohttp

    headers = {"Accept": "application/json"}
    if AUTH_HEADER:
        headers["Authorization"] = AUTH_HEADER
    logger.info("Activity[list_calls_for_org_on_date] org=%s date=%s", org_id, yyyy_mm_dd)
    url = f"{API_BASE}/api/calls/"
    params = {"org_id": org_id, "date": yyyy_mm_dd}
    async with aiohttp.ClientSession() as s:
        resp = await s.get(url, params=params, timeout=30, allow_redirects=True, headers=headers)
        text = await resp.text()
        if resp.status >= 400:
            logger.error("Activity[list_calls_for_org_on_date] GET %s returned %s body=%s", resp.url, resp.status, text)
            resp.raise_for_status()
        try:
            js = await resp.json()
        except Exception:
            logger.error("Activity[list_calls_for_org_on_date] response not JSON: %s", text)
            return []
        logger.info("Activity[list_calls_for_org_on_date] returned %d records", len(js) if isinstance(js, list) else 0)
        return js


@activity.defn
async def fetch_call_readings(call_id: str):
    """Activity: GET /api/calls/{call_id}/readings -> raw JSON or text

    Uses `API_BASE` and `AUTH_HEADER` defined later in this file.
    """
    import aiohttp

    headers = {"Accept": "application/json"}
    if AUTH_HEADER:
        headers["Authorization"] = AUTH_HEADER
    logger.info("Activity[fetch_call_readings] %s", call_id)
    async with aiohttp.ClientSession() as s:
        resp = await s.get(f"{API_BASE}/api/calls/{call_id}/readings", timeout=30, headers=headers)
        if resp.status == 404:
            return None
        resp.raise_for_status()
        txt = await resp.text()
        try:
            return json.loads(txt)
        except Exception:
            return txt

# Logging
LOG_LEVEL = os.getenv("CALLBACK_CHECKER_LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("callback_checker")
DEBUG_FULL_RESPONSES = os.getenv("DEBUG_FULL_RESPONSES", "0") in ("1", "true", "True", "TRUE")

def _safe_json_dump(obj: Any, max_len: int = 2000) -> str:
    try:
        s = json.dumps(obj, default=str, ensure_ascii=False)
    except Exception:
        try:
            s = str(obj)
        except Exception:
            s = "<unserializable>"
    return s if len(s) <= max_len else s[:max_len] + "...(truncated)"

# -------------------------
# Normalizer for readings
# -------------------------
def parse_readings_from_response(raw: Any) -> List[Dict]:
    def _parse_candidate(candidate) -> List[Dict]:
        out: List[Dict] = []
        if candidate is None:
            return out
        if isinstance(candidate, str):
            s = candidate.strip()
            if not s:
                return out
            try:
                return _parse_candidate(json.loads(s))
            except Exception:
                return out
        if isinstance(candidate, dict):
            if "value" in candidate and isinstance(candidate["value"], list):
                for it in candidate["value"]:
                    out.extend(_parse_candidate(it))
                return out
            measurement_keys = {"systolic", "diastolic", "bp", "hr", "heartrate", "spo2", "type", "value"}
            if measurement_keys.intersection(k.lower() for k in candidate.keys()):
                out.append(candidate)
                return out
            for key in ("readings", "data", "rows", "entries"):
                if key in candidate and isinstance(candidate[key], list):
                    for it in candidate[key]:
                        out.extend(_parse_candidate(it))
                    return out
            return out
        if isinstance(candidate, list):
            for item in candidate:
                out.extend(_parse_candidate(item))
            return out
        if isinstance(candidate, (int, float)):
            return [{"value": candidate}]
        return out

    normalized: List[Dict] = []
    if isinstance(raw, list):
        # If items already look like measurements, return them
        if raw and isinstance(raw[0], dict) and any(k in raw[0] for k in ("systolic", "diastolic", "BP", "bp", "value", "type")):
            for it in raw:
                if isinstance(it, dict):
                    normalized.append(it)
            return normalized
        for row in raw:
            if isinstance(row, dict):
                v = row.get("value")
                rt = row.get("raw_text")
                if v is not None:
                    normalized.extend(_parse_candidate(v))
                elif rt is not None:
                    normalized.extend(_parse_candidate(rt))
                else:
                    normalized.extend(_parse_candidate(row))
            else:
                normalized.extend(_parse_candidate(row))
        return normalized
    if isinstance(raw, dict):
        normalized.extend(_parse_candidate(raw))
        return normalized
    normalized.extend(_parse_candidate(raw))
    return normalized

# -------------------------
# Deterministic activity to get EST "now"
# -------------------------
@activity.defn
async def get_est_now_iso() -> str:
    from datetime import datetime
    try:
        import zoneinfo
        est = zoneinfo.ZoneInfo("US/Eastern")
        return datetime.now(est).isoformat()
    except Exception:
        return datetime.now().isoformat()

# -------------------------
# New activities: patient lookup & start follow-up call
# -------------------------
API_BASE = os.getenv("ANNIE_API_BASE", "https://134210b168e4.ngrok-free.app")
AUTH_HEADER = os.getenv("ANNIE_API_AUTH_HEADER")  # optional

def _auth_headers() -> Dict[str, str]:
    h = {"Accept": "application/json"}
    if AUTH_HEADER:
        h["Authorization"] = AUTH_HEADER
    return h

@activity.defn
async def get_patient(patient_id: str) -> Dict[str, Any]:
    import aiohttp
    url = f"{API_BASE}/api/patients/{patient_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=_auth_headers(), timeout=20) as resp:
            txt = await resp.text()
            try:
                data = json.loads(txt)
            except Exception:
                data = txt
            logger.info("Activity[get_patient] GET %s -> %s %s", url, resp.status, _safe_json_dump(data, 1000))
            resp.raise_for_status()
            return data

@activity.defn
async def start_followup_call(org_id: str, patient_id: str, to_number: str, agent: str = "annie_RPM_followup", meta: Dict[str, Any] | None = None) -> Any:
    import aiohttp
    url = f"{API_BASE}/api/calls/outbound"
    payload = {"org_id": int(org_id), "patient_id": int(patient_id), "to_number": to_number, "agent": agent}
    if meta:
        payload["meta"] = meta
    headers = _auth_headers() | {"Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
            txt = await resp.text()
            try:
                data = json.loads(txt)
            except Exception:
                data = txt
            logger.info("Activity[start_followup_call] POST %s payload=%s -> %s %s",
                        url, _safe_json_dump(payload, 800), resp.status, _safe_json_dump(data, 1000))
            resp.raise_for_status()
            return data

# -------------------------
# Configuration
# -------------------------
FOLLOWUP_AGENT = "annie_RPM_followup"
MAX_OUTBOUND_PER_RUN = int(os.getenv("CALLBACK_MAX_OUTBOUND_PER_RUN", "50"))
RATE_LIMIT_SLEEP_SEC = float(os.getenv("CALLBACK_RATE_LIMIT_SLEEP_SEC", "0.6"))
RECENT_CALL_COOLDOWN_MIN = int(os.getenv("CALLBACK_RECENT_CALL_COOLDOWN_MIN", "30"))

# -------------------------
# Workflow
# -------------------------
@workflow.defn
class CallbackWindowChecker:
    @workflow.run
    async def run(
        self,
        org_id: int,
        interval_minutes: int = 5,
        start_hour_est: int = 9,
        end_hour_est: int = 17,
        test_mode: bool = False,
    ) -> Dict:
        wf_id = workflow.info().workflow_id
        logger.info("[%s] CallbackWindowChecker start org=%s interval=%s window=%s-%s test=%s",
                    wf_id, org_id, interval_minutes, start_hour_est, end_hour_est, test_mode)

        # 1) EST now via activity
        est_iso = await workflow.execute_activity(get_est_now_iso, start_to_close_timeout=timedelta(seconds=10))
        try:
            est_now = datetime.fromisoformat(est_iso)
        except Exception:
            try:
                from dateutil import parser as _p
                est_now = _p.parse(est_iso)
            except Exception:
                est_now = workflow.now()
        est_date = est_now.date().isoformat()
        current_hour = est_now.hour if hasattr(est_now, "hour") else workflow.now().hour
        logger.info("[%s] EST now=%s hour=%d date=%s", wf_id, est_iso, current_hour, est_date)

        # 2) window check
        if not (start_hour_est <= current_hour < end_hour_est):
            logger.info("[%s] Outside window %s-%s EST (hour=%d) - skipping", wf_id, start_hour_est, end_hour_est, current_hour)
            return {"skipped": True, "current_hour": current_hour}

        # 3) list calls for org/date
        calls = await workflow.execute_activity(
            list_calls_for_org_on_date,
            args=[org_id, est_date],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=3),
        )
        logger.info("[%s] Raw calls response (type=%s) -> %s",
                    wf_id, type(calls).__name__,
                    _safe_json_dump(calls, max_len=4000 if DEBUG_FULL_RESPONSES else 1000))
        if not isinstance(calls, list):
            calls = []

        # 4) filter completed calls by Annie (original)
        completed_by_annie = [
            c for c in calls
            if str(c.get("status", "")).lower() == "completed"
            and str(c.get("agent", "")).lower() == "annie_rpm"
        ]
        logger.info("[%s] Completed_by_annie count=%d", wf_id, len(completed_by_annie))

        # 5) helper: return comparable float timestamp for a call
        def _ts_key_to_epoch(call: Dict) -> float:
            """
            Return epoch seconds as float for sorting. Try parsing start_time; if not possible,
            fallback to numeric id. Always returns float.
            """
            st = call.get("start_time") or call.get("startTime") or call.get("created_at")
            if st:
                try:
                    dt = datetime.fromisoformat(str(st))
                except Exception:
                    try:
                        from dateutil import parser as _p
                        dt = _p.parse(str(st))
                    except Exception:
                        dt = None
                if dt is not None:
                    # normalize to UTC and return epoch
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    else:
                        dt = dt.astimezone(timezone.utc)
                    return float(dt.timestamp())
            # fallback to id
            try:
                cid = call.get("id") or call.get("call_id")
                return float(int(cid))
            except Exception:
                return 0.0

        # 6) build per-patient set of completed Annie calls (we'll check all calls)
        patients_set = set()
        for c in completed_by_annie:
            pid = c.get("patient_id")
            if pid is not None:
                try:
                    patients_set.add(int(pid))
                except Exception:
                    patients_set.add(pid)

        total_completed_patients = len(patients_set)
        logger.info("[%s] Patients with at least one completed Annie call today: %d", wf_id, total_completed_patients)

        # 7) For each patient: check ALL completed Annie calls for the day (newest->oldest).
        missing_patient_ids: List[Any] = []
        samples: List[Dict[str, Any]] = []

        def _calls_for_pid(pid) -> List[Dict]:
            out = [c for c in completed_by_annie if str(c.get("patient_id")) == str(pid)]
            out.sort(key=_ts_key_to_epoch, reverse=True)
            return out

        def _is_measurement_like(obj: Dict) -> bool:
            if not isinstance(obj, dict):
                return False
            ks = set(k.lower() for k in obj.keys())
            return bool(ks.intersection({"systolic", "diastolic", "bp", "hr", "heartrate", "spo2", "value", "type"}))

        for pid in sorted(patients_set):
            logger.info("[%s] Checking all calls for patient=%s", wf_id, pid)
            calls_for_pid = _calls_for_pid(pid)
            if not calls_for_pid:
                logger.info("[%s] No completed Annie calls for patient=%s (skip)", wf_id, pid)
                missing_patient_ids.append(pid)
                samples.append({"patient_id": pid, "reason": "no_completed_calls"})
                continue

            found_valid = False
            for idx, call in enumerate(calls_for_pid):
                call_id = call.get("id") or call.get("call_id") or call.get("CallSid")
                logger.info("[%s] patient=%s checking call[%d]=%s start_time=%s", wf_id, pid, idx, call_id, call.get("start_time"))
                try:
                    raw_readings = await workflow.execute_activity(
                        fetch_call_readings,
                        args=[str(call_id)],
                        start_to_close_timeout=timedelta(seconds=25),
                        retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=2),
                    )
                except Exception as e:
                    logger.exception("[%s] fetch_call_readings failed for call=%s patient=%s: %s", wf_id, call_id, pid, e)
                    samples.append({"patient_id": pid, "call_id": call_id, "error": str(e)})
                    continue

                logger.info("[%s] patient=%s call=%s raw_readings_preview=%s", wf_id, pid, call_id, _safe_json_dump(raw_readings, max_len=500))
                normalized = parse_readings_from_response(raw_readings)
                valid = [r for r in normalized if _is_measurement_like(r)]
                logger.info("[%s] patient=%s call=%s normalized=%d valid=%d", wf_id, pid, call_id, len(normalized), len(valid))
                if valid:
                    logger.info("[%s] patient=%s satisfied by call=%s (found %d valid readings)", wf_id, pid, call_id, len(valid))
                    found_valid = True
                    samples.append({"patient_id": pid, "call_id": call_id, "valid_count": len(valid), "preview": normalized[:3]})
                    break
                else:
                    samples.append({"patient_id": pid, "call_id": call_id, "valid_count": 0, "preview": normalized[:2]})

            if not found_valid:
                logger.info("[%s] patient=%s has NO valid readings in any completed Annie calls today", wf_id, pid)
                missing_patient_ids.append(pid)

        logger.info("[%s] Missing readings patients=%s", wf_id, missing_patient_ids)

        # 8) FOLLOW-UP CALLS
        cooldown_delta = timedelta(minutes=RECENT_CALL_COOLDOWN_MIN)
        def _latest_any_call_for_pid(pid) -> Dict | None:
            per_pid = [c for c in calls if str(c.get("patient_id")) == str(pid)]
            if not per_pid:
                return None
            per_pid.sort(key=_ts_key_to_epoch, reverse=True)
            return per_pid[0]

        outbound_count = 0
        for pid in missing_patient_ids:
            if outbound_count >= MAX_OUTBOUND_PER_RUN:
                logger.info("[%s] Reached MAX_OUTBOUND_PER_RUN=%d, stopping follow-ups this run", wf_id, MAX_OUTBOUND_PER_RUN)
                break

            last_call = _latest_any_call_for_pid(pid)
            last_dt_utc = None
            if last_call:
                st = last_call.get("start_time") or last_call.get("startTime") or last_call.get("created_at")
                last_dt = None
                if st:
                    try:
                        last_dt = datetime.fromisoformat(str(st))
                    except Exception:
                        try:
                            from dateutil import parser as _p
                            last_dt = _p.parse(str(st))
                        except Exception:
                            last_dt = None
                if last_dt:
                    if last_dt.tzinfo is None:
                        last_dt_utc = last_dt.replace(tzinfo=timezone.utc)
                    else:
                        last_dt_utc = last_dt.astimezone(timezone.utc)

            now_utc = workflow.now().astimezone(timezone.utc)
            if last_dt_utc and (now_utc - last_dt_utc) < cooldown_delta:
                logger.info("[%s] Skip patient=%s: recent call at %s within cooldown %d min", wf_id, pid, last_dt_utc.isoformat(), RECENT_CALL_COOLDOWN_MIN)
                continue

            # resolve patient phone
            try:
                patient = await workflow.execute_activity(
                    get_patient,
                    args=[str(pid)],
                    start_to_close_timeout=timedelta(seconds=15),
                    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3),
                )
            except Exception as e:
                logger.warning("[%s] Cannot fetch patient=%s (skip): %s", wf_id, pid, e)
                continue

            phone = (patient or {}).get("phone")
            if not phone:
                logger.warning("[%s] Patient=%s missing phone, skip follow-up", wf_id, pid)
                continue

            meta = {"reason": "no_readings", "detected_by": wf_id, "checker_date": est_date}
            try:
                res = await workflow.execute_activity(
                    start_followup_call,
                    args=[str(org_id), str(pid), str(phone), FOLLOWUP_AGENT, meta],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=1),
                )
                logger.info("[%s] Follow-up started for patient=%s phone=%s agent=%s -> %s", wf_id, pid, phone, FOLLOWUP_AGENT, _safe_json_dump(res, 800))
                outbound_count += 1
            except Exception as e:
                logger.exception("[%s] start_followup_call failed for patient=%s: %s", wf_id, pid, e)

            await workflow.sleep(timedelta(seconds=RATE_LIMIT_SLEEP_SEC))

        result = {
            "org_id": org_id,
            "date_est": est_date,
            "total_completed_patients": total_completed_patients,
            "missing_readings_count": len(missing_patient_ids),
            "missing_patient_ids": missing_patient_ids,
            "samples": samples,
            "missing_patients_called": outbound_count,
            "agent": FOLLOWUP_AGENT,
        }
        logger.info("[%s] Result preview: %s", wf_id, _safe_json_dump(result, max_len=2000))
        return result

# -------------------------
# Cron registration helper
# -------------------------
async def create_callback_checker_cron(
    org_id: int,
    interval_minutes: int = 5,
    start_hour_est: int = 9,
    end_hour_est: int = 17,
    start_now: bool = True,
    replace_existing: bool = False,
    unique_id: bool = False,
):
    client = await connect_client()
    if interval_minutes <= 0 or interval_minutes > 60:
        raise ValueError("interval_minutes must be 1..60")
    cron_expr = f"*/{int(interval_minutes)} * * * *" if interval_minutes > 1 else "*/1 * * * *"

    base_id = f"callback-checker-{org_id}-{interval_minutes}m"
    wf_id = base_id if not unique_id else f"{base_id}-{int(time.time())}"

    if replace_existing and not unique_id:
        try:
            await client.get_workflow_handle(wf_id).terminate("replaced_by_new_callback_registration")
            logger.info("create_callback_checker_cron: terminated existing id=%s", wf_id)
        except Exception:
            logger.info("create_callback_checker_cron: no existing id=%s to terminate (ok)", wf_id)

    handle = await client.start_workflow(
        CallbackWindowChecker.run,
        args=[org_id, interval_minutes, start_hour_est, end_hour_est, False],
        id=wf_id,
        task_queue=TASK_QUEUE,
        cron_schedule=cron_expr,
    )
    logger.info("create_callback_checker_cron: registered cron id=%s org=%s cron=%s", handle.id, org_id, cron_expr)

    if start_now:
        once_id = f"{base_id}-once-{int(time.time())}"
        try:
            now_h = await client.start_workflow(
                CallbackWindowChecker.run,
                args=[org_id, interval_minutes, start_hour_est, end_hour_est, False],
                id=once_id,
                task_queue=TASK_QUEUE,
            )
            logger.info("create_callback_checker_cron: started immediate one-off run id=%s", now_h.id)
        except Exception as e:
            logger.exception("create_callback_checker_cron: failed immediate run; cron still registered: %s", e)

    return handle

# -------------------------
# Worker / CLI
# -------------------------
async def run_worker():
    client = await Client.connect(TEMPORAL_ENDPOINT)
    logger.info("Connected to Temporal at %s", TEMPORAL_ENDPOINT)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[CallbackWindowChecker],
        activities=[
            get_est_now_iso,
            list_calls_for_org_on_date,
            fetch_call_readings,
            get_patient,
            start_followup_call,
        ],
    )
    logger.info("CallbackChecker worker created - running.")
    await worker.run()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "create_callback_checker":
        org = 6
        interval = 5
        start_hour = 9
        end_hour = 17
        start_now = "--start-now" in sys.argv
        replace = "--replace" in sys.argv
        unique = "--unique-id" in sys.argv

        if "--org" in sys.argv:
            org = int(sys.argv[sys.argv.index("--org") + 1])
        if "--interval" in sys.argv:
            interval = int(sys.argv[sys.argv.index("--interval") + 1])
        if "--start-hour" in sys.argv:
            start_hour = int(sys.argv[sys.argv.index("--start-hour") + 1])
        if "--end-hour" in sys.argv:
            end_hour = int(sys.argv[sys.argv.index("--end-hour") + 1])

        async def _run():
            await create_callback_checker_cron(
                org_id=org,
                interval_minutes=interval,
                start_hour_est=start_hour,
                end_hour_est=end_hour,
                start_now=start_now,
                replace_existing=replace,
                unique_id=unique,
            )
        try:
            asyncio.run(_run())
            print(f"Callback checker cron registered for org={org} every {interval}min between {start_hour}:00-{end_hour}:00 EST (start_now={start_now}).")
        except Exception:
            logger.exception("Failed to create callback checker cron")
            sys.exit(1)
        sys.exit(0)

    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("Worker interrupted by user, exiting.")
    except Exception:
        logger.exception("Worker failed")
        raise
