# Web layer — backend + front end, kept separate

This folder adds a **website** on top of your optimiser **without changing one
line** inside `optimizer/`. It is split into two halves on purpose.

## The mental model (read this first)

Think of a restaurant:

| Part | Restaurant | This project | File |
|------|-----------|--------------|------|
| Kitchen | cooks the food | the optimiser — does the math | `optimizer/` (unchanged) |
| Kitchen window | food passes through | the **API** (backend) | `webapp/api.py` |
| Dining room | where guests sit | the **front end** | `webapp/streamlit_app.py` |

The dining room **never reaches into the kitchen**. It orders through the
window. That is the whole reason for this design: when you later want a fancier
dining room (React), you build it against the **same window**. The kitchen and
the window don't change. Today's Streamlit dining room would simply be retired.

`webapp/streamlit_app.py` does not `import optimizer` anywhere — it only makes
HTTP calls to the API. That is your proof the two are truly separate.

## How to run it (two terminals)

**Terminal 1 — start the backend (the kitchen window):**

```bash
cd "/Users/kirankumarpatil/Desktop/Data Centre"
pip install -r webapp/requirements.txt          # first time only
uvicorn webapp.api:app --reload --port 8000
```

Check it works: open <http://localhost:8000/docs> — an auto-generated page where
you can click any endpoint and try it. Useful even with no front end at all.

**Terminal 2 — start the front end (the dining room):**

```bash
cd "/Users/kirankumarpatil/Desktop/Data Centre"
streamlit run webapp/streamlit_app.py
```

It opens a browser tab. Pick a scenario, fill the inputs, press **Run**.

## What the backend offers (the "window" menu)

| Method & URL | What it does |
|---|---|
| `GET /health` | Is the server up? |
| `GET /coverage` | How many scenarios are ready vs backlog |
| `GET /scenarios` | List every scenario (id, name, status) — feeds the dropdown |
| `GET /scenarios/{id}` | Full contract for one scenario (Inputs/Outputs/How) |
| `GET /profile-summary` | Dataset stats + nameplates (for sensible defaults) |
| `GET /wizard` | The guided question tree (leaves annotated live with registry status) |
| `POST /upload-profile` | Upload an `.xlsx`; it's validated + saved, returns a `profile_path` to use in `/run` |
| `POST /run` | **The important one** — run a scenario, get design + KPIs + table + flows as JSON |

### The guided wizard (question tree)

The front end has two ways in: **🧭 Guided wizard** (business questions that narrow
to one scenario) and **📋 Pick a scenario directly** (the full list). Both end at
the same scenario form.

The tree itself is plain config: [`wizard.json`](wizard.json). Each option either
points to the `next` question or ends at a `leaf` scenario. **Leaves carry only a
`scenario_id`** — the backend fills in the name/status/needs from the live
registry when serving `GET /wizard`. So the wizard can never claim a scenario is
runnable when the engine says otherwise.

Safety net: at startup the API validates every leaf against the registry and every
`next` against the questions. A typo (e.g. a leaf pointing at a scenario that
doesn't exist) **stops the server at boot** with a clear message, the same way
`scenarios.py` guards its own spec table. To edit the flow, just edit
`wizard.json` — no Python change needed.

### Uploading your own Excel

The front end has an **Upload profiles** box in the sidebar. The engine only reads
two known layouts, so the upload auto-detects which one it is:

- the **8760 PV+Load** sheet (`datacenter_load_8760`) — hourly, like the bundled file
- the **BESS_Input** sheet (`Energy Timeseries`) — 15-minute solar+wind+load

The native timestep (hourly vs 15-min) is detected from the timestamps and fed to
the solver, so 15-min data is sized over the real 15-min step. Any other
spreadsheet is rejected with a clear message rather than solved wrongly. Leave the
box empty to use the bundled dataset.

Everything goes through `run_scenario()` in `optimizer/scenarios.py`, so the API
is thin: it just translates JSON ⇄ Python and never duplicates engine logic.

## Moving to React later (when you're ready)

Nothing here gets thrown away. You would:

1. Keep `webapp/api.py` exactly as is.
2. Build a React app that calls the same `GET /scenarios` and `POST /run` URLs
   (with `fetch()` instead of Python `requests`).
3. Stop running `streamlit_app.py`.

The kitchen (`optimizer/`) and window (`api.py`) never change. That's the payoff
of doing the split now.

## Adding a new scenario to the form

1. Make the scenario `READY` in `optimizer/scenarios.py` (engine side).
2. Add one line to the `FIELDS` map in `streamlit_app.py` listing which inputs it
   needs. If you skip this, it falls back to showing the common inputs.

No backend change needed — the API already runs any `READY` scenario by id.
