# DIP Italy — Microgrid Sizing & Dispatch Optimizer

A behind-the-meter (BTM) sizing and dispatch optimisation platform for data centres
with on-site PV and BESS. A Pyomo + HiGHS linear-programming engine drives a two-phase
method, exposed through a CLI, a FastAPI service, and a React decision-support UI.

- **Phase 1 (Sizing Curves)** — sizes the BESS on physical constraints only to produce
  objective feasibility curves (SSR, peak-shaving, PV+BESS surface).
- **Phase 3 (Land-First Split)** — sweeps solar across available land and sizes BESS to
  find the optimal physical split and trade-off curves.

## Repository structure

```
.
├── backend/                  # everything Python — the engine and its service
│   ├── core/                 # the LP engine (sizing, dispatch rule, resolver)
│   ├── api/                  # FastAPI service (main.py)
│   ├── data/                 # input datasets (8760 profiles, BESS_Input)
│   ├── main.py               # command-line entry point
│   └── requirements.txt      # engine + API dependencies
├── frontend/                 # React + Vite decision-support UI (6-step wizard)
│   └── src/
│       ├── lib/              # api client + chart helpers
│       └── steps/            # one file per wizard step
├── docs/                     # specification, design review, brief
└── archive/                  # superseded / legacy code (not part of the live app)
```

## Quick start

### 1. Backend (engine + API)

```bash
cd backend
python -m venv ../.venv && source ../.venv/bin/activate   # first time only
pip install -r requirements.txt
uvicorn api.main:app --reload --port 8000                 # serves the API on :8000
```

> Requires Python 3.10+. The `highspy` package provides the open-source HiGHS LP solver.

### 2. Frontend (React UI)

```bash
cd frontend
npm install        # first time only
npm run dev        # serves the UI on :5173, proxying /api → :8000
```

Open <http://localhost:5173>. The UI walks six steps — project definition, site,
assumptions, closed-loop sizing, scenario comparison, decision pack — calling the
real engine for every solved number.

## Command-line usage (no server)

All CLI commands run from `backend/`:

```bash
cd backend
```

### Phase 1 — physical sizing curves

```bash
python main.py --phase1 --profiles "data/8760_PV&Load Profiles.xlsx"
```

- `--profiles` — Excel with 8760 hourly load + PV data (defaults to the bundled dataset).
- `--scenarios A B C D` — A: SSR curve · B: peak-shaving · C: PV+BESS surface · D: sub peak-shaving.
- `--resample-15min` — solve at native 15-min resolution (errors on hourly data; peaks are never fabricated).

Outputs: `Phase1_Sizing_Curves.xlsx` + interactive `plot_*.html`.

## Scenario reports

Standalone per-scenario report builders live in `archive/reports/<name>/build.py`.
They predate the web UI (which now drives scenarios directly) and are kept for
reference; generated workbooks/plots are git-ignored.
