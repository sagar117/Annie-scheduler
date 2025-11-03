# Local development: run Temporal and the worker

This project uses Temporal workflows. For local development you can run a Temporal dev server via Docker Compose and then run the worker (the Python file) locally.

Minimal docker-compose (Temporal auto-setup):

```yaml
version: '3.7'
services:
  temporal:
    image: temporalio/auto-setup:1.21.0
    ports:
      - "7233:7233"
    environment:
      - TEMPORAL_BROADCAST_ADDRESS=0.0.0.0

  # Optional: add Temporal Web UI if desired
  # web:
  #   image: temporal/web:latest
  #   ports:
  #     - "8088:8088"
```

How to run

1. Start Temporal locally:

```bash
docker compose up -d
```

2. Export env vars the worker needs (example):

```bash
export TEMPORAL_ENDPOINT=localhost:7233
export TASK_QUEUE=annie-task-queue
export ANNIE_API_BASE=https://example-backend.local
# optional for auth
export ANNIE_API_AUTH_HEADER="Bearer <token>"
```

3. Run the callback checker worker locally (this registers workflows/activities with Temporal and runs them):

```bash
python callback_checker.py
```

4. Register a cron callback checker (optional):

```bash
python callback_checker.py create_callback_checker --org 6 --interval 5 --start-now --replace
```

Notes
- The docker-compose snippet uses the Temporal quickstart image `temporalio/auto-setup`. Pick a version compatible with your Temporal SDK version.
- If you run Temporal in a remote environment, set `TEMPORAL_ENDPOINT` accordingly.
