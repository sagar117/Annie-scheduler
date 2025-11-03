# Annie-scheduler — quick run

Short instructions to run the callback checker worker and register the checker cron.

🧩 Step 1 — Start the worker

This runs your `callback_checker.py` so Temporal has a worker ready to execute workflows and activities:

```bash
python callback_checker.py
```

Keep this process running in one terminal. It connects to Temporal and waits for tasks on the `annie-task-queue` (or whichever queue you configured via `TASK_QUEUE`).

🕐 Step 2 — In another terminal, create/start the checker

Once the worker is running, open a new terminal and run:

```bash
python callback_checker.py create_callback_checker --org 22 --interval 5 --start-hour 0 --end-hour 9 --start-now
```

Notes
- The CLI flag names in the repository use hyphens (e.g. `--start-hour`, `--end-hour`) — use those names when invoking the script.
- If you want a unique cron id for each registration (so multiple cron schedules can coexist), pass `--unique-id` when creating the checker.
- See `LOCAL_DEV.md` for a short docker-compose snippet to start a local Temporal dev server.
