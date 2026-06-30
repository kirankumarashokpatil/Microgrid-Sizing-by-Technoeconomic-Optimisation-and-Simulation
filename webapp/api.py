"""
FastAPI backend — "the kitchen window"
======================================
This is the ONLY thing that talks to the optimiser engine (optimizer/…). It does
three jobs and nothing else:

    1. List the scenarios the engine can answer        →  GET  /scenarios
    2. Describe one scenario's full contract           →  GET  /scenarios/{id}
    3. Run one scenario and hand back a plain-JSON     →  POST /run
       result (design + KPIs + table + flows)

Why this layer exists
---------------------
The engine speaks Python objects (DataFrames, dataclasses). Browsers speak JSON.
This file is the translator in the middle: it turns an incoming JSON request into
a ScenarioContext, calls optimizer.scenarios.run_scenario(), and turns the
ScenarioResult back into JSON. Nothing in optimizer/ changes.

Because the engine is reached only through these URLs, ANY front end — today's
Streamlit screen, a future React app, even curl — uses the exact same backend.
Swapping the dining room never touches the kitchen.

Run it:
    pip install -r webapp/requirements.txt
    uvicorn webapp.api:app --reload --port 8000
Then open http://localhost:8000/docs for an auto-generated API playground.
"""

from __future__ import annotations

import json
import math
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ── Reach the engine. The repo root is one level up from webapp/. ──────────────
import sys
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from optimizer.params import PhysicalParams                       # noqa: E402
from optimizer.profile_loader import (                            # noqa: E402
    load_profiles, load_bess_input, compute_annual_energy,
)
from optimizer.scenarios import (                                 # noqa: E402
    REGISTRY, Status, ScenarioContext, coverage, describe,
    get_scenario, run_scenario,
)

# Default bundled dataset, so the API works out-of-the-box with real profiles.
_DEFAULT_PROFILE = _REPO_ROOT / "8760_PV&Load Profiles.xlsx"

# Where uploaded files land (a private temp dir, cleaned by the OS).
_UPLOAD_DIR = Path(tempfile.gettempdir()) / "scenario_uploads"
_UPLOAD_DIR.mkdir(exist_ok=True)

_WIZARD_PATH = Path(__file__).resolve().parent / "wizard.json"


def _load_wizard() -> dict:
    """Load the question-tree config and FAIL LOUD if any leaf points at a
    scenario the engine doesn't have, or any 'next' points at a missing question.
    This mirrors scenarios._check_registry_doc_consistency(): the tree can never
    silently drift from the registry."""
    tree = json.loads(_WIZARD_PATH.read_text())
    questions = tree.get("questions", {})
    known_scenarios = {s.id for s in REGISTRY}
    bad_leaf, bad_next = [], []
    for qid, q in questions.items():
        for opt in q.get("options", []):
            if "leaf" in opt and opt["leaf"] not in known_scenarios:
                bad_leaf.append(f"{qid} → {opt['leaf']}")
            if "next" in opt and opt["next"] not in questions:
                bad_next.append(f"{qid} → {opt['next']}")
    if tree.get("start") not in questions:
        bad_next.append(f"start → {tree.get('start')}")
    problems = []
    if bad_leaf:
        problems.append("leaves with no registry scenario: " + ", ".join(bad_leaf))
    if bad_next:
        problems.append("options pointing to a missing question: " + ", ".join(bad_next))
    if problems:
        raise AssertionError("wizard.json is inconsistent with the registry:\n  - "
                             + "\n  - ".join(problems))
    return tree


# Loaded once at import → a broken tree stops the server at boot, not mid-request.
_WIZARD = _load_wizard()


# ──────────────────────────────────────────────────────────────────────────────
# Profile loading (cached — the Excel only needs reading once per file)
# ──────────────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=8)
def _load_profile(path: str) -> tuple[pd.DataFrame, float, float, float]:
    """Load + cache a profile file, auto-detecting which of the two known layouts
    it is. Returns (df[timestamp, load_mw, pv_pu], load_nameplate, pv_nameplate,
    dt_hours) where dt_hours is the data's NATIVE timestep (e.g. 0.25 for 15-min),
    derived from the timestamps so the engine integrates SOC over the real step.

    The engine only understands two real shapes:
      • the '8760' sheet  → load_profiles()       (datacenter_load_8760)
      • the BESS_Input    → load_bess_input()      (Energy Timeseries)
    We try the first, fall back to the second, and fail loud if it is neither —
    so an unknown spreadsheet gets a clear error, not a wrong answer."""
    try:
        df, load_np, pv_np = load_profiles(xlsx_path=path)
    except Exception as first_err:
        try:
            df, load_np, pv_np = load_bess_input(xlsx_path=path)
        except Exception as second_err:
            raise ValueError(
                "Could not read this Excel in either known format. "
                f"As an 8760 profile sheet: {first_err}. "
                f"As a BESS_Input sheet: {second_err}."
            )
    # Native timestep from the timestamps (median gap). 1.0 h fallback for tiny
    # frames; rounded so 900.0001 s of float jitter still reads as 0.25 h.
    if len(df) >= 3:
        dt_hours = round(df["timestamp"].diff().dropna().median().total_seconds() / 3600.0, 4)
    else:
        dt_hours = 1.0
    return df, load_np, pv_np, dt_hours


# ──────────────────────────────────────────────────────────────────────────────
# Request / response shapes  (Pydantic = the form contract, validated for free)
# ──────────────────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    """Everything a scenario run might need. Every field is optional with a
    sensible default, so the front end only sends what its chosen scenario uses."""
    scenario_id: str = Field(..., description="e.g. 'S21_BTM_SSR_CURVE'")

    # Which profile dataset to run against (defaults to the bundled 8760 file).
    profile_path: Optional[str] = None

    # Design / target inputs (mirror ScenarioContext fields)
    pv_mw: Optional[float] = None          # None ⇒ use the file's PV nameplate
    target_ssr_pct: float = 60.0
    target_gc_mw: Optional[float] = None
    target_firmness_pct: float = 99.0
    bess_mw: float = 0.0
    bess_mwh: float = 0.0
    grid_ceiling_mw: Optional[float] = None
    deliverable_target: str = "ssr"        # "ssr" | "gc"
    pv_sweep_mw: list[float] = Field(default_factory=list)
    ssr_targets_pct: list[float] = Field(default_factory=list)
    solver_time_limit: int = 120

    # Physical assumptions (defaults match main.py's Phase-1 run)
    eff_charge: float = 0.95
    eff_discharge: float = 0.95
    min_soc_pct: float = 10.0
    max_soc_pct: float = 90.0
    initial_soc_pct: float = 50.0
    min_bess_duration_h: float = 2.0
    max_bess_duration_h: float = 8.0
    site_max_grid_mw: float = 200.0
    site_max_bess_mw: float = 500.0
    site_max_bess_mwh: float = 4000.0
    export_limit_mw: float = 0.0
    eol_capacity_retention_pct: float = 80.0


# ──────────────────────────────────────────────────────────────────────────────
# JSON-safe conversion helpers (DataFrames + numpy + NaN → plain JSON)
# ──────────────────────────────────────────────────────────────────────────────

def _clean(v: Any) -> Any:
    """Make one value JSON-safe: numpy → python, NaN/inf → None."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if hasattr(v, "item"):          # numpy scalar
        try:
            v = v.item()
        except Exception:
            return v
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _clean_dict(d: dict) -> dict:
    """JSON-safe a flat dict of (possibly numpy) scalars."""
    return {str(k): _clean(v) for k, v in d.items()}


def _df_to_records(df: Optional[pd.DataFrame], max_rows: int = 2000) -> Optional[dict]:
    """Turn a DataFrame into {columns, rows} JSON, capped so a year of 15-min
    flows can't blow up the payload."""
    if df is None or df.empty:
        return None
    truncated = len(df) > max_rows
    view = df.head(max_rows)
    cols = [str(c) for c in view.columns]
    rows = [[_clean(v) for v in rec] for rec in view.itertuples(index=False, name=None)]
    return {"columns": cols, "rows": rows, "n_total": int(len(df)), "truncated": truncated}


def _result_to_json(result) -> dict:
    return {
        "id": result.id,
        "name": result.name,
        "answer_mode": result.answer_mode,
        "status": result.status,
        "feasible": bool(result.feasible),
        "design": {k: _clean(v) for k, v in (result.design or {}).items()},
        "kpis": {str(k): _clean(v) for k, v in (result.kpis or {}).items()},
        "table": _df_to_records(result.table),
        # Flows drive the carpet heatmap / annual Sankey, which need the whole
        # year — allow a full 8760 (hourly) or 35040 (15-min) through.
        "flows": _df_to_records(result.flows, max_rows=40000),
        "notes": result.notes,
        "summary": result.summary(),
    }


# ──────────────────────────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Scenario Engine API",
    description="The kitchen window over the DIP sizing optimiser. "
                "Any front end (Streamlit today, React later) calls these URLs.",
    version="1.0.0",
)

# Allow a browser front end (Streamlit / React on another port) to call us.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    """Quick check that the server is up and the default dataset is present."""
    return {"status": "ok", "default_profile_exists": _DEFAULT_PROFILE.exists()}


@app.get("/coverage")
def get_coverage() -> dict:
    """How many scenarios are ready vs still backlog."""
    return coverage()


@app.get("/scenarios")
def list_all() -> list[dict]:
    """Every scenario as a lightweight row — what a menu / dropdown needs."""
    return [
        {
            "id": s.id, "name": s.name, "stage": s.stage, "topology": s.topology,
            "family": s.family, "answer_mode": s.answer_mode, "status": s.status,
            "ready": s.status == Status.READY, "needs": s.needs,
        }
        for s in REGISTRY
    ]


@app.get("/wizard")
def get_wizard() -> dict:
    """The guided question tree, with each leaf annotated live from the registry
    (name, status, runnable, needs) so the front end shows accurate state without
    duplicating registry knowledge."""
    by_id = {s.id: s for s in REGISTRY}
    questions = {}
    for qid, q in _WIZARD["questions"].items():
        opts = []
        for opt in q["options"]:
            o = {"label": opt["label"]}
            if "next" in opt:
                o["next"] = opt["next"]
            if "leaf" in opt:
                sid = opt["leaf"]
                spec = by_id[sid]
                o["leaf"] = {
                    "scenario_id": sid,
                    "name": spec.name,
                    "status": spec.status,
                    "runnable": spec.status == Status.READY,
                    "needs": spec.needs,
                }
            opts.append(o)
        questions[qid] = {"text": q["text"], "options": opts}
    return {"start": _WIZARD["start"], "name": _WIZARD["name"], "questions": questions}


@app.get("/scenarios/{scenario_id}")
def scenario_detail(scenario_id: str) -> dict:
    """Full contract for one scenario: proper name + Inputs / Outputs / How."""
    try:
        return describe(scenario_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown scenario '{scenario_id}'")


@app.get("/profile-summary")
def profile_summary(profile_path: Optional[str] = None) -> dict:
    """Annual stats + nameplates for a dataset, so the UI can show sensible
    defaults (e.g. pre-fill PV MW with the file's nameplate)."""
    path = profile_path or str(_DEFAULT_PROFILE)
    if not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"Profile file not found: {path}")
    df, load_np, pv_np, dt_hours = _load_profile(path)
    stats = compute_annual_energy(df, dt_hours=dt_hours)
    return _clean_dict({
        "profile_path": path,
        "load_nameplate_mw": load_np,
        "pv_nameplate_mw": pv_np,
        "dt_hours": dt_hours,
        **stats,
    })


@app.post("/upload-profile")
async def upload_profile(file: UploadFile = File(...)) -> dict:
    """Accept an uploaded .xlsx, validate it loads in one of the two known
    formats, and return a `profile_path` the front end then passes to /run.

    The file is saved under a private temp dir with a unique name so concurrent
    uploads don't clash. Validation happens here (by actually loading it) so the
    user learns immediately if the spreadsheet is the wrong shape."""
    name = file.filename or "upload.xlsx"
    if not name.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Please upload an .xlsx / .xls file.")

    dest = _UPLOAD_DIR / f"{uuid.uuid4().hex}_{Path(name).name}"
    dest.write_bytes(await file.read())

    # Validate by loading (auto-detects format); clean up + 400 on failure.
    try:
        df, load_np, pv_np, dt_hours = _load_profile(str(dest))
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc))

    stats = compute_annual_energy(df, dt_hours=dt_hours)
    return _clean_dict({
        "profile_path": str(dest),
        "filename": name,
        "load_nameplate_mw": load_np,
        "pv_nameplate_mw": pv_np,
        "dt_hours": dt_hours,
        **stats,
    })


@app.post("/run")
def run(req: RunRequest) -> dict:
    """Run one scenario and return its result as JSON. This is the one endpoint
    a front end really needs."""
    # 1. Validate the scenario exists and is runnable.
    try:
        spec = get_scenario(req.scenario_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown scenario '{req.scenario_id}'")
    if spec.status != Status.READY:
        raise HTTPException(
            status_code=409,
            detail=f"Scenario '{req.scenario_id}' is not runnable yet — {spec.needs}",
        )

    # 2. Load profiles (real data from the bundled Excel by default).
    path = req.profile_path or str(_DEFAULT_PROFILE)
    if not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"Profile file not found: {path}")
    df, load_np, pv_np, dt_hours = _load_profile(path)

    # 3. Build the engine's PhysicalParams from the request (topology comes from
    #    the scenario itself — the runner already forces PV=0 for sub-scenarios).
    #    dt_hours is the file's native resolution so SOC integrates over the real
    #    timestep (15-min data must not be solved as if it were hourly).
    params = PhysicalParams(
        dt_hours=dt_hours,
        eff_charge=req.eff_charge, eff_discharge=req.eff_discharge,
        min_soc_pct=req.min_soc_pct, max_soc_pct=req.max_soc_pct,
        initial_soc_pct=req.initial_soc_pct,
        min_bess_duration_h=req.min_bess_duration_h,
        max_bess_duration_h=req.max_bess_duration_h,
        site_max_bess_mw=req.site_max_bess_mw, site_max_bess_mwh=req.site_max_bess_mwh,
        site_max_grid_mw=req.site_max_grid_mw,
        export_limit_mw=req.export_limit_mw,
        eol_capacity_retention_pct=req.eol_capacity_retention_pct,
        site_topology=spec.topology,
    )

    # 4. Assemble the context. PV defaults to the file's nameplate when omitted.
    ctx = ScenarioContext(
        profiles_df=df,
        params=params,
        pv_mw=req.pv_mw if req.pv_mw is not None else pv_np,
        target_ssr_pct=req.target_ssr_pct,
        target_gc_mw=req.target_gc_mw,
        target_firmness_pct=req.target_firmness_pct,
        bess_mw=req.bess_mw,
        bess_mwh=req.bess_mwh,
        grid_ceiling_mw=req.grid_ceiling_mw,
        deliverable_target=req.deliverable_target,
        pv_sweep_mw=tuple(req.pv_sweep_mw),
        ssr_targets_pct=tuple(req.ssr_targets_pct),
        solver_time_limit=req.solver_time_limit,
    )

    # 5. Run and translate to JSON.
    try:
        result = run_scenario(req.scenario_id, ctx)
    except Exception as exc:  # surface engine errors as a clean 500, not a stack dump
        raise HTTPException(status_code=500, detail=f"Engine error: {exc}")
    out = _result_to_json(result)
    out["dt_hours"] = dt_hours   # so the UI can scale energy (MW × dt = MWh)
    return out
