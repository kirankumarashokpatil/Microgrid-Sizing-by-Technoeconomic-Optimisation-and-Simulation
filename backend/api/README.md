# API — the FastAPI service over the engine

This is the only layer that talks to the optimisation engine (`core/`). It turns
HTTP/JSON requests into engine calls and back, and changes **nothing** inside
`core/`. Any front end (the React app, curl, a future client) uses these URLs.

`main.py` does three jobs: list the scenarios the engine can answer, describe one
scenario's contract, and run one scenario — returning design + KPIs + table +
flows (+ optional economics) as plain JSON.

## Run it (from `backend/`)

```bash
pip install -r requirements.txt          # first time only
uvicorn api.main:app --reload --port 8000
```

Open <http://localhost:8000/docs> for an auto-generated, clickable API playground.

## Endpoints

| Method & URL | What it does |
|---|---|
| `GET /health` | Is the server up? |
| `GET /scenarios` | List every scenario (id, name, status) |
| `GET /scenarios/{id}` | Full contract for one scenario (Inputs/Outputs/How) |
| `GET /coverage` | How many scenarios are ready vs backlog |
| `GET /profile-summary` | Dataset stats + nameplates (for sensible defaults) |
| `POST /upload-profile` | Upload an `.xlsx`; validated + saved, returns a `profile_path` |
| `POST /resolve` | **Detect** the scenario from the supplied inputs (+ the reason) — no question wizard |
| `POST /run` | **Run** a scenario → design + KPIs + table + flows (+ economics) as JSON |

### Scenario auto-detection (`/resolve`)

Instead of asking the user what they're modelling, the inputs themselves declare
intent. `/resolve` maps the supplied inputs to a scenario id and explains why:

- generation present / absent, grid allowed / off-grid, export-limited → topology
- `bess_mw` + `bess_mwh` given → evaluate a fixed design; otherwise size it
- PV given as a fixed `pv_mw` vs a unit profile to size → PV-fixed vs PV-variable
- an SSR target vs a grid-connection cap vs both → the sizing target
- a single target vs a swept range → point vs curve / surface

The same logic lives in `core/scenario_resolver.py`, so the engine, the API, and
any client agree on the mapping. The front end shows the detected scenario and
lets the user override it — smart, but transparent and traceable.

### Uploading your own Excel

The engine reads two known layouts, auto-detected on upload:

- the **8760 PV+Load** sheet (`datacenter_load_8760`) — hourly, like the bundled file
- the **BESS_Input** sheet (`Energy Timeseries`) — 15-minute solar+wind+load

The native timestep is detected from the timestamps and fed to the solver, so
15-min data is sized over the real 15-min step. Any other spreadsheet is rejected
with a clear message rather than solved wrongly. Leave it empty to use the bundled
dataset.

Everything runs through `run_scenario()` in `core/scenarios.py`, so the API stays
thin: it translates JSON ⇄ Python and never duplicates engine logic.
