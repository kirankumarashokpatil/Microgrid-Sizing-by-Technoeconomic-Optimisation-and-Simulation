# React front end (the new "dining room")

This is a React app that talks to the **same** FastAPI backend (`backend/api/main.py`)
the Streamlit screen used. The optimiser (`optimizer/`) and the API are
unchanged — we only swapped the dining room, exactly as planned.

It does not import any Python. It only calls the API over HTTP (`src/api.js`).

## What it adds over Streamlit

Analyst-grade visuals via **Plotly**, chosen per result shape:

| Result shape | Charts shown |
|---|---|
| Point solve (S11, S12, …) | KPI cards + design table |
| Sizing curve (S21, S22) | **Knee curve** with 3 lines: LP bound · deliverable · EoL-sized + data table |
| Surface (S62) | PV × target **heatmap** |
| Flows (S00, S41, …) | annual energy **Sankey** · **carpet heatmap** (hour × day) · peak-day dispatch · grid duration curve |

Plus the guided **wizard** and the **direct picker**, both driving the same form.

## How to run (three things)

You need the backend running, then the React dev server.

```bash
# Terminal 1 — backend (the kitchen window)
cd "/Users/kirankumarpatil/Desktop/Data Centre/backend"
uvicorn api.main:app --reload --port 8000

# Terminal 2 — React front end
cd "/Users/kirankumarpatil/Desktop/Data Centre/frontend"
npm install          # first time only
npm run dev
```

Open **http://localhost:5173**.

The dev server proxies `/api/*` → `http://localhost:8000` (see `vite.config.js`),
so the browser only talks to one origin — no CORS issues.

## File map (small on purpose)

| File | Role |
|---|---|
| `src/api.js` | every backend call (the only file that knows the API exists) |
| `src/App.jsx` | shell: sidebar (upload + coverage), wizard/direct toggle |
| `src/Wizard.jsx` | walks the `/wizard` question tree to one scenario |
| `src/ScenarioForm.jsx` | conditional form per scenario, runs it |
| `src/Results.jsx` | routes a result to the right charts |
| `src/Charts.jsx` | the Plotly charts (KPI cards, knee curve, Sankey, carpet, …) |
| `src/fields.js` | which inputs each scenario shows (mirrors Streamlit's FIELDS) |

## Production build

```bash
npm run build      # outputs static files to dist/
npm run preview    # serve the build locally
```

`dist/` is plain static files — host them anywhere and point them at a deployed
API (replace the dev proxy with the API's real URL).

## Streamlit is still there

`webapp/streamlit_app.py` still works against the same backend. Keep it as a
fallback / quick-tweak tool, or retire it once you're happy with React.
