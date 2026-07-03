# DIP web-app API — persistence layer

Adds Postgres-backed **projects → runs** on top of the stateless scenario engine,
with generation/load **profiles stored as single JSONB rows** and expanded to
DataFrames at run time. The engine (`core/`) is unchanged; the stateless
`POST /run` still works with no database.

## Setup

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env            # edit DIP_DB_* to point at your Postgres
psql "<your conn>" -f schema.sql   # creates schema + tables (idempotent)
uvicorn api.main:app --reload --port 8000
```

On startup the API seeds the `scenarios` read-model from the code registry
(non-fatal if the DB is down). Open http://localhost:8000/docs.

## Data model (schema `gigapark_bess_designer`, see `schema.sql`)

| Table        | Purpose |
|--------------|---------|
| `projects`   | Owner of runs — name, optional site (lat/long/area_ha). |
| `profiles`   | A reusable dataset as **one JSONB row** (`series = {load_mw[], pv_pu[], wind_pu[]}`); timestamps derived from `start_ts + i·dt_hours` when regular. |
| `scenarios`  | Read-model of the code registry (`S00…S123`); FK-validates a run's `scenario_id`. Auto-seeded. |
| `runs`       | One execution — `request` and `result` both **JSONB**, so the shape stays flexible across answer modes (point/curve/surface/…). |
| `run_flows`  | Per-timestep energy-flows table (JSONB), only for scenarios that emit one; kept off `runs.result` so the summary row stays small. |

## Endpoints (persistence)

| Method + path | Purpose |
|---|---|
| `POST /projects` | Create a project. |
| `GET /projects`, `GET /projects/{id}` | List / fetch. |
| `POST /profiles` | Register a dataset from an Excel path → one JSONB row; returns `profile_id`. |
| `GET /profiles/{id}` | Profile metadata. |
| `POST /projects/{id}/runs` | Launch an async run (`{profile_id?, request:{…RunRequest}}`); 202 + `run_id`. |
| `GET /runs/{id}` | Status + full result JSON. |
| `GET /runs/{id}/flows` | Per-timestep flows, when present. |

The stateless engine endpoints (`/run`, `/resolve`, `/scenarios`, `/ssr-range`,
`/export`, `/health`, …) are unchanged.

## How a run flows

`POST /projects/{id}/runs` validates the scenario is `READY`, records a `pending`
run (storing the request verbatim), and schedules a background task. The
orchestrator (`services/run_orchestrator.py`) reconstructs the profiles frame
(from the stored `profile_id`, else the bundled default), runs the **shared**
engine core (`api.main.execute_run` — the exact function `POST /run` uses, so
results are identical), then writes `result` + `run_flows` and flips the run to
`completed`/`failed`. The Pyomo/HiGHS solve runs in a worker thread.

## Notes / follow-ups

- `min_grid` in the UI's run payload is a **`/resolve`** signal, not a `RunRequest`
  field — it belongs on `POST /resolve` (scenario detection), not on a run.
- Profiles are stored via JSONB today; if datasets grow large (15-min × multi-year)
  the only change is swapping the `series` column to `bytea` (parquet/npz) — the
  envelope shape stays the same.
- Alembic is not wired yet; `schema.sql` is the source of truth for the DDL.
