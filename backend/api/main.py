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

Because the engine is reached only through these URLs, ANY front end — the React
app, curl, or a future client — uses the exact same backend. Swapping the dining
room never touches the kitchen.

Run it (from backend/):
    pip install -r requirements.txt
    uvicorn api.main:app --reload --port 8000
Then open http://localhost:8000/docs for an auto-generated API playground.
"""

from __future__ import annotations

import io
import math
import os
import tempfile
import threading
import uuid
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# ── Reach the engine. The backend root is one level up from api/. ──────────────
import sys
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.params import PhysicalParams                       # noqa: E402
from core.profile_loader import (                            # noqa: E402
    load_profiles, load_bess_input, compute_annual_energy,
)
from core.resolver import resolve_scenario                   # noqa: E402
from core.site import parcel_capacity, area_for_capacity, _SOLAR_MW_PER_HA  # noqa: E402
from core.sweep import solve_ssr_point, solve_ssr_point_star  # noqa: E402
from core.boundary import parse_boundary                      # noqa: E402
from core.schema import CurveCols, KpiKeys                   # noqa: E402
from core.scenarios import (                                 # noqa: E402
    REGISTRY, Status, ScenarioContext, coverage, describe,
    get_scenario, run_scenario,
)

# Default bundled dataset, so the API works out-of-the-box with real profiles.
_DEFAULT_PROFILE = _REPO_ROOT / "data" / "8760_PV&Load Profiles.xlsx"

# Where uploaded files land (a private temp dir, cleaned by the OS).
_UPLOAD_DIR = Path(tempfile.gettempdir()) / "scenario_uploads"
_UPLOAD_DIR.mkdir(exist_ok=True)

# NOTE: the old question-tree wizard (wizard.json + GET /wizard) has been replaced
# by input-driven scenario detection — see core/resolver.py and POST /resolve.


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
    pv_mw: Optional[float] = None          # None ⇒ use the file's PV (solar) nameplate
    wind_mw: Optional[float] = None        # opt-in wind nameplate (MW); needs a wind profile
    # Combined consumer peak (sum of the Step-1 loads). When set, the dataset's REAL
    # load shape is scaled so its peak equals this — magnitude from the loads, shape
    # from real data (same per-unit×nameplate idea the engine uses for PV).
    load_peak_mw: Optional[float] = None
    target_ssr_pct: float = 60.0
    target_gc_mw: Optional[float] = None
    target_firmness_pct: float = 99.0
    target_curtailment_pct: float = 5.0
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
    # Network topology — authoritative when supplied, overriding the scenario's
    # default. None ⇒ fall back to the scenario's registry topology (spec.topology).
    site_topology: Optional[str] = None    # grid_connected_btm | bess_load_only | off_grid | standalone_gen

    # ── Dispatch policy (Model R) — frontend-overridable ──────────────────
    # Ordered causal merit order. Empty ⇒ engine uses the mode default. Entries
    # must be from rule_dispatch.DISPATCH_ACTIONS.
    dispatch_priority: list[str] = Field(default_factory=list)
    # Tri-state grid-charging: null ⇒ infer from objective; true/false ⇒ force it.
    allow_grid_charge: Optional[bool] = None




class ResolveRequest(BaseModel):
    """The inputs the resolver inspects to DETECT the scenario. Every field is
    optional — `None`/`False` means 'not chosen', so defaults are never mistaken
    for user intent. Mirrors the discriminators in core/resolver.py.

    `flow_topology` is the optional JSON payload from the FlowDesigner (React).
    When supplied its derived signals take precedence over the individual booleans
    below — so the resolver always sees what the user actually wired up."""
    pv_mw: Optional[float] = None              # fixed PV nameplate (a number ⇒ PV fixed)
    pv_unit_profile: bool = False              # PV supplied as a profile to SIZE
    bess_mw: Optional[float] = None
    bess_mwh: Optional[float] = None
    grid_available: bool = True
    off_grid: bool = False
    standalone: bool = False
    export_limit_mw: Optional[float] = None
    target_ssr_pct: Optional[float] = None
    target_gc_mw: Optional[float] = None
    target_firmness_pct: Optional[float] = None
    target_curtailment_pct: Optional[float] = None
    min_grid: bool = False
    want_curve: bool = False
    pv_sweep_mw: list[float] = Field(default_factory=list)
    ssr_targets_pct: list[float] = Field(default_factory=list)
    gc_targets_mw: list[float] = Field(default_factory=list)
    verify: bool = False
    # Optional: full FlowDesigner topology payload (nodes + flows + derived signals)
    flow_topology: Optional[dict] = None


def _merge_flow_topology(req_dict: dict) -> dict:
    """When a `flow_topology` payload is present, extract its derived signals
    and merge them into the resolver inputs dict — overriding the individual
    booleans that may still reflect stale radio-button state."""
    ft = req_dict.get("flow_topology")
    if not ft:
        return req_dict

    sig = ft.get("signals", {})
    if not sig:
        return req_dict

    merged = dict(req_dict)  # shallow copy

    # Topology booleans — authoritative from the FlowDesigner when present
    if "grid_available" in sig:
        merged["grid_available"] = bool(sig["grid_available"])
    if "off_grid" in sig:
        merged["off_grid"] = bool(sig["off_grid"])
    if "standalone" in sig:
        merged["standalone"] = bool(sig["standalone"])

    # PV nameplate — sum of all solar node rated_mw in the designer
    if sig.get("pv_mw") is not None and merged.get("pv_mw") is None:
        try:
            merged["pv_mw"] = float(sig["pv_mw"])
        except (TypeError, ValueError):
            pass

    # Export limit — from grid node params when standalone
    if sig.get("export_limit_mw") is not None and merged.get("export_limit_mw") is None:
        try:
            merged["export_limit_mw"] = float(sig["export_limit_mw"])
        except (TypeError, ValueError):
            pass

    return merged


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
# App — startup warms the profile cache so the first real request is instant.
# The Excel read (~2 s cold) happens in a daemon thread during server boot,
# not on the first user click. Multi-worker: run with --workers 2 so a slow
# LP solve on one worker never blocks /health or /profile-summary on another.
# ──────────────────────────────────────────────────────────────────────────────

_cache_ready = threading.Event()   # set when the default profile is warm


def _warm_default_profile() -> None:
    """Pre-load the bundled Excel into lru_cache.  Runs in a daemon thread at
    startup so the first real API call finds the data already in memory."""
    try:
        _load_profile(str(_DEFAULT_PROFILE))
        print("[startup] Default profile cached ✓")
    except Exception as exc:
        print(f"[startup] Profile warm-up failed (non-fatal): {exc}")
    finally:
        _cache_ready.set()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    t = threading.Thread(target=_warm_default_profile, daemon=True, name="profile-warmer")
    t.start()
    yield
    # nothing to tear down


app = FastAPI(
    title="Scenario Engine API",
    description="The kitchen window over the DIP sizing optimiser. "
                "Any front end (Streamlit today, React later) calls these URLs.",
    version="1.0.0",
    lifespan=_lifespan,
)

# Allow a browser front end (Streamlit / React on another port) to call us.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    """Always-fast liveness check — never touches the LP or the Excel file.
    `profile_cached` tells the UI whether profile data is ready to serve."""
    return {
        "status": "ok",
        "default_profile_exists": _DEFAULT_PROFILE.exists(),
        "profile_cached": _cache_ready.is_set(),
    }


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


@app.get("/profile-series")
def profile_series(profile_path: Optional[str] = None, points: int = 336) -> dict:
    """The REAL load / solar / wind shapes from the dataset, downsampled for
    charting. The front end plots this instead of a synthetic preview — the demand
    curve is the actual data, not a parametric shape."""
    path = profile_path or str(_DEFAULT_PROFILE)
    if not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"Profile file not found: {path}")
    df, load_np, pv_np, dt_hours = _load_profile(path)
    n = len(df)
    stride = max(1, n // max(1, points))
    view = df.iloc[::stride]
    series = {
        "timestamps": [str(t) for t in view["timestamp"]],
        "load_mw":    [_clean(v) for v in view["load_mw"]],
        "pv_pu":      [_clean(v) for v in view["pv_pu"]],
        "wind_pu":    [_clean(v) for v in (view["wind_pu"] if "wind_pu" in view else [0.0] * len(view))],
    }
    return {"profile_path": path, "dt_hours": dt_hours, "n_total": n,
            "load_nameplate_mw": _clean(load_np), "pv_nameplate_mw": _clean(pv_np), **series}


class ParcelRequest(BaseModel):
    """A buildable parcel — its area (and location) determine available generation."""
    area_ha: float = 184.0
    lat: float = 51.96
    lon: float = 1.35


@app.post("/parcel-capacity")
def parcel_capacity_endpoint(req: ParcelRequest) -> dict:
    """Available generation a land parcel can host — max solar/wind MW + yields.
    The engineering calculation lives in core/site.py, so the UI never computes it."""
    return _clean_dict(parcel_capacity(req.area_ha, req.lat, req.lon))


class CapacityFromMwRequest(BaseModel):
    """The reciprocal of a parcel: a target nameplate → the land it needs."""
    mw: float = 50.0
    tech: str = "solar"          # solar | wind
    lat: float = 51.96
    lon: float = 1.35


@app.post("/capacity-from-mw")
def capacity_from_mw_endpoint(req: CapacityFromMwRequest) -> dict:
    """Land a target generation (MW) needs, at the tech's build density — the
    inverse of /parcel-capacity, powering the UI's 'size by generation' toggle."""
    return _clean_dict(area_for_capacity(req.mw, req.tech, req.lat, req.lon))


@app.post("/parse-boundary")
async def parse_boundary_endpoint(file: UploadFile = File(...)) -> dict:
    """Accept a GeoJSON / KMZ / KML boundary export and return the land parcel
    rings it contains — each as {latlngs, area_ha, centroid, name}.

    This lets a developer start from real GIS data instead of hand-drawing the
    boundary on the map. The geometry work lives in core/boundary.py so the API
    stays a thin translator, exactly like /parcel-capacity."""
    name = file.filename or "boundary"
    if not name.lower().endswith((".geojson", ".json", ".kml", ".kmz")):
        raise HTTPException(status_code=400, detail="Please upload a .geojson, .json, .kml or .kmz file.")
    try:
        rings = parse_boundary(name, await file.read())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read boundary file: {exc}")
    if not rings:
        raise HTTPException(status_code=400, detail="No polygon boundary found in the file.")
    return {"filename": name, "rings": rings}


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


@app.post("/resolve")
def resolve(req: ResolveRequest) -> dict:
    """Detect the scenario from the supplied inputs (no question wizard). Returns
    the scenario id, a plain-English reason, the discriminator signals, and the
    runnable status — enriched with the scenario's name so the UI can show it.
    Advisory only: the front end displays this and lets the user override.

    When `flow_topology` is supplied (from the FlowDesigner React component), its
    derived topology signals override the individual boolean fields so the resolver
    always reflects the actual wired-up topology, not just button selections."""
    inputs = _merge_flow_topology(req.model_dump())
    res = resolve_scenario(inputs)
    out = res.as_dict()
    # Enrich with display name + the fallback's name, from the live registry.
    try:
        out["name"] = get_scenario(res.scenario_id).name
    except KeyError:
        out["name"] = res.scenario_id
    if res.fallback_id:
        try:
            out["fallback_name"] = get_scenario(res.fallback_id).name
        except KeyError:
            out["fallback_name"] = res.fallback_id
    # Echo back the merged inputs so the UI can show exactly what was used
    out["merged_signals"] = inputs
    return out


class SsrRangeRequest(BaseModel):
    """Inputs to probe the FEASIBLE SSR band before the user picks a target."""
    profile_path: Optional[str] = None
    load_peak_mw: Optional[float] = None
    pv_mw: Optional[float] = None
    wind_mw: Optional[float] = None
    site_topology: str = "grid_connected_btm"
    site_max_bess_mw: float = 500.0
    site_max_bess_mwh: float = 4000.0
    site_max_grid_mw: float = 200.0


@app.post("/ssr-range")
def ssr_range(req: SsrRangeRequest) -> dict:
    """The feasible SSR band for these inputs: SSR_min (no battery, PV-direct) and
    SSR_max (the site's maximum battery). Two fast forward-evals — no LP sweep — so
    the UI can show the achievable range before a target is chosen."""
    from core.params import PhysicalParams
    from core.rule_dispatch import verify_sizing_with_rule
    from core.schema import KpiKeys

    path = req.profile_path or str(_DEFAULT_PROFILE)
    if not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"Profile file not found: {path}")
    df, load_np, pv_np, dt_hours = _load_profile(path)
    if req.load_peak_mw and req.load_peak_mw > 0:
        cur = float(df["load_mw"].max())
        if cur > 1e-9:
            df = df.copy(); df["load_mw"] = df["load_mw"] * (req.load_peak_mw / cur)
    solar_mw = req.pv_mw if req.pv_mw is not None else pv_np
    wind_mw  = float(req.wind_mw or 0.0)
    eng_pv = solar_mw
    if wind_mw > 0 and "wind_pu" in df.columns and float(df["wind_pu"].max()) > 1e-9:
        comb = df["pv_pu"].to_numpy() * solar_mw + df["wind_pu"].to_numpy() * wind_mw
        peak = float(comb.max())
        if peak > 1e-9:
            df = df.copy(); df["pv_pu"] = comb / peak; eng_pv = peak
    params = PhysicalParams(dt_hours=dt_hours, site_topology=req.site_topology,
                            site_max_grid_mw=req.site_max_grid_mw)

    def ssr_at(bess_mw, bess_mwh):
        k, _ = verify_sizing_with_rule(df, params, pv_mw=eng_pv, bess_mw=bess_mw,
                                       bess_mwh=bess_mwh, target_type="ssr",
                                       grid_ceiling_mw=req.site_max_grid_mw)
        return float(k.get(KpiKeys.SSR, 0.0) or 0.0)

    ssr_min = ssr_at(0.0, 0.0)                                   # no storage → PV-direct floor
    ssr_max = ssr_at(req.site_max_bess_mw, req.site_max_bess_mwh)  # max storage → ceiling
    return _clean_dict({"ssr_min": round(ssr_min, 1), "ssr_max": round(ssr_max, 1),
                        "has_generation": eng_pv > 0})





@app.post("/run")
def run(req: RunRequest) -> dict:
    """Run one scenario and return its result as JSON. This is the one endpoint
    a front end really needs."""
    # 1. Validate the scenario exists and is runnable.
    try:
        spec = get_scenario(req.scenario_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown scenario '{req.scenario_id}'")

    # Validate the dispatch-priority override up front (client error, not a 500).
    if req.dispatch_priority:
        from core.rule_dispatch import resolve_priority
        try:
            resolve_priority("self_sufficiency", req.dispatch_priority)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
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

    # 2b. Scale the REAL load shape to the combined Step-1 peak (multi-load → sizing).
    #     Magnitude from the user's loads, temporal shape from the dataset — the same
    #     per-unit×nameplate technique the engine uses for generation. No synthetic
    #     shapes. Skipped when not supplied or when it would be a no-op.
    if req.load_peak_mw and req.load_peak_mw > 0:
        cur_peak = float(df["load_mw"].max())
        if cur_peak > 1e-9 and abs(cur_peak - req.load_peak_mw) > 1e-6:
            df = df.copy()
            df["load_mw"] = df["load_mw"] * (req.load_peak_mw / cur_peak)

    # 3. Build the engine's PhysicalParams. Topology is authoritative from the
    #    request when supplied (the front-end Step-2 selection); otherwise it
    #    falls back to the scenario's own registry topology. Unknown values are
    #    ignored rather than allowed to mis-constrain the LP.
    #    dt_hours is the file's native resolution so SOC integrates over the real
    #    timestep (15-min data must not be solved as if it were hourly).
    _VALID_TOPOLOGIES = {"grid_connected_btm", "bess_load_only", "off_grid", "standalone_gen"}
    topology = req.site_topology if req.site_topology in _VALID_TOPOLOGIES else spec.topology
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
        site_topology=topology,
        dispatch_priority=tuple(req.dispatch_priority or ()),
        allow_grid_charge=req.allow_grid_charge,
    )

    # 3b. Wind (opt-in). The engine consumes ONE generation array; when a wind
    #     nameplate is supplied and the dataset carries a wind profile, combine
    #     solar + wind here — generation = pv_pu×solar_mw + wind_pu×wind_mw —
    #     and feed the combined series as the engine's single "pv" generation.
    #     The LP/dispatch core is unchanged.
    solar_mw = req.pv_mw if req.pv_mw is not None else pv_np
    wind_mw  = float(req.wind_mw or 0.0)
    gen_df, eng_pv_mw, eff_wind_mw = df, solar_mw, 0.0
    if wind_mw > 0 and "wind_pu" in df.columns and float(df["wind_pu"].max()) > 1e-9:
        combined = df["pv_pu"].to_numpy() * solar_mw + df["wind_pu"].to_numpy() * wind_mw
        peak = float(combined.max())
        if peak > 1e-9:
            gen_df = df.copy()
            gen_df["pv_pu"] = combined / peak     # normalised combined shape (0–1)
            eng_pv_mw = peak                       # nameplate s.t. pv_pu×nameplate = combined
            eff_wind_mw = wind_mw                  # wind actually contributed (and is costed)

    # 4. Assemble the context. PV defaults to the file's nameplate when omitted.
    ctx = ScenarioContext(
        profiles_df=gen_df,
        params=params,
        pv_mw=eng_pv_mw,
        target_ssr_pct=req.target_ssr_pct,
        target_gc_mw=req.target_gc_mw,
        target_firmness_pct=req.target_firmness_pct,
        target_curtailment_pct=req.target_curtailment_pct,
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


# ──────────────────────────────────────────────────────────────────────────────
# Rolling-horizon dispatch (Model W) — operate a fixed design under limited foresight
# ──────────────────────────────────────────────────────────────────────────────
class RollingDispatchRequest(BaseModel):
    """Operate an already-sized design (PV + BESS) under rolling-horizon dispatch
    (Model W) and return its KPIs + the Model-R comparison — the value of foresight
    for the SAME battery. Inputs mirror /run so the front end reuses its config."""
    profile_path: Optional[str] = None
    load_peak_mw: Optional[float] = None
    pv_mw: Optional[float] = None
    wind_mw: Optional[float] = None
    bess_mw: float = 0.0
    bess_mwh: float = 0.0
    target_type: str = "ssr"                 # "ssr" | "peak_shaving"
    site_topology: str = "grid_connected_btm"
    site_max_grid_mw: float = 200.0
    grid_ceiling_mw: Optional[float] = None  # defaults to site_max_grid_mw
    horizon_h: float = 24.0
    commit_h: float = 1.0
    eff_charge: float = 0.95
    eff_discharge: float = 0.95
    min_soc_pct: float = 10.0
    max_soc_pct: float = 90.0
    initial_soc_pct: float = 50.0
    allow_grid_charge: Optional[bool] = None
    include_flows: bool = False              # return the full per-slot table too
    # Optional deliverable-size comparison: minimum BESS to hit `target_value` under
    # BOTH Model R and Model W. Factual — the two are typically similar (foresight's
    # value is operational, not smaller hardware), so this is for transparency, not a
    # "buy less" claim. target_value = SSR % (ssr) or grid ceiling MW (peak_shaving).
    compare_sizes: bool = False
    target_value: Optional[float] = None
    duration_h: float = 4.0


# A full-year W dispatch is ~8 s, so memoise the endpoint by its exact inputs: the
# UI re-requests the same comparison whenever Step 6 re-renders or the user revisits.
_ROLLING_CACHE: dict = {}
_ROLLING_CACHE_MAX = 128


@app.post("/dispatch-rolling")
def dispatch_rolling(req: RollingDispatchRequest) -> dict:
    """Run Model W (rolling horizon) on a fixed design and return KPIs for W and R
    side by side, so a caller sees exactly what limited foresight buys."""
    from core.rolling_dispatch import run_rolling_horizon_dispatch, size_under_rolling
    from core.rule_dispatch import (
        run_rule_dispatch, compute_flow_kpis, _mode_for, size_by_bisection,
    )

    # Cache hit? Identical inputs ⇒ identical result; skip the expensive re-solve.
    _sig = tuple(sorted(req.model_dump().items(), key=lambda kv: kv[0]))
    _hit = _ROLLING_CACHE.get(_sig)
    if _hit is not None:
        return _hit

    path = req.profile_path or str(_DEFAULT_PROFILE)
    if not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"Profile file not found: {path}")
    df, load_np, pv_np, dt_hours = _load_profile(path)

    # Same load-scaling + generation-combine as /run, so the design is comparable.
    if req.load_peak_mw and req.load_peak_mw > 0:
        cur = float(df["load_mw"].max())
        if cur > 1e-9:
            df = df.copy(); df["load_mw"] = df["load_mw"] * (req.load_peak_mw / cur)
    solar_mw = req.pv_mw if req.pv_mw is not None else pv_np
    wind_mw  = float(req.wind_mw or 0.0)
    eng_pv = solar_mw
    if wind_mw > 0 and "wind_pu" in df.columns and float(df["wind_pu"].max()) > 1e-9:
        comb = df["pv_pu"].to_numpy() * solar_mw + df["wind_pu"].to_numpy() * wind_mw
        peak = float(comb.max())
        if peak > 1e-9:
            df = df.copy(); df["pv_pu"] = comb / peak; eng_pv = peak

    ceiling = req.grid_ceiling_mw if req.grid_ceiling_mw is not None else req.site_max_grid_mw
    params = PhysicalParams(
        dt_hours=dt_hours, site_topology=req.site_topology, site_max_grid_mw=req.site_max_grid_mw,
        eff_charge=req.eff_charge, eff_discharge=req.eff_discharge,
        min_soc_pct=req.min_soc_pct, max_soc_pct=req.max_soc_pct,
        initial_soc_pct=req.initial_soc_pct, allow_grid_charge=req.allow_grid_charge)

    # Guard against a pathologically fine series × tiny commit (would hang the request).
    H = max(1, round(req.horizon_h / dt_hours))
    C = max(1, min(round(req.commit_h / dt_hours), H))
    if (len(df) / C) * H > 2.0e6:
        raise HTTPException(status_code=400, detail=(
            f"Series too fine ({len(df)} steps) for horizon {req.horizon_h:g}h / "
            f"commit {req.commit_h:g}h. Increase commit_h."))

    mode = _mode_for(req.target_type)
    grid_charge = req.allow_grid_charge if req.allow_grid_charge is not None else (mode == "peak_shaving")
    try:
        w_flows = run_rolling_horizon_dispatch(
            df, params, pv_mw=eng_pv, bess_mw=req.bess_mw, bess_mwh=req.bess_mwh,
            grid_ceiling_mw=ceiling, horizon_h=req.horizon_h, commit_h=req.commit_h,
            allow_grid_charge=grid_charge)
        r_flows = run_rule_dispatch(
            df, params, pv_mw=eng_pv, bess_mw=req.bess_mw, bess_mwh=req.bess_mwh,
            grid_ceiling_mw=ceiling, mode=mode, allow_grid_charge=grid_charge)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Dispatch error: {exc}")

    kW = compute_flow_kpis(w_flows, dt_hours)
    kR = compute_flow_kpis(r_flows, dt_hours)
    out = {
        "dt_hours": dt_hours, "horizon_h": req.horizon_h, "commit_h": req.commit_h,
        "grid_ceiling_mw": ceiling,
        "model_w": _clean_dict(kW), "model_r": _clean_dict(kR),
    }

    # Optional deliverable-size comparison (min BESS to hit the target under each model).
    if req.compare_sizes and req.target_value is not None:
        try:
            rs = size_by_bisection(df, params, pv_mw=eng_pv, target_type=req.target_type,
                                   target_value=req.target_value, duration_h=req.duration_h,
                                   grid_ceiling_mw=(None if req.target_type == "peak_shaving" else ceiling))
            ws = size_under_rolling(df, params, pv_mw=eng_pv, target_type=req.target_type,
                                    target_value=req.target_value, duration_h=req.duration_h,
                                    grid_ceiling_mw=(None if req.target_type == "peak_shaving" else ceiling),
                                    horizon_h=req.horizon_h)
            out["sizing"] = {
                "target_type": req.target_type, "target_value": req.target_value,
                "model_r": {"bess_mw": round(rs.bess_mw, 1), "bess_mwh": round(rs.bess_mwh, 1),
                            "feasible": bool(rs.feasible)},
                "model_w": {"bess_mw": round(ws.bess_mw, 1), "bess_mwh": round(ws.bess_mwh, 1),
                            "feasible": bool(ws.feasible)},
            }
        except Exception:
            pass   # sizing is advisory; never fail the operational comparison over it

    if req.include_flows:
        out["flows_w"] = _df_to_records(w_flows, max_rows=40000)

    # Store in the bounded cache (clear wholesale when full — simple + safe).
    if len(_ROLLING_CACHE) >= _ROLLING_CACHE_MAX:
        _ROLLING_CACHE.clear()
    _ROLLING_CACHE[_sig] = out
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Grid-connection minimiser — the smallest grid a design can hold + the real levers
# ──────────────────────────────────────────────────────────────────────────────
class MinGridRequest(BaseModel):
    """For a FIXED design (PV + battery + demand), the minimum grid connection that
    still serves all load (zero unmet), plus how that minimum moves with the levers
    that actually matter: battery size, PV size, and grid-charging on/off.

    Dispatch is Model R (causal peak-shaving) — verified to hit the perfect-foresight
    floor for this metric, so the numbers are the true minimum, not a heuristic."""
    profile_path: Optional[str] = None
    load_peak_mw: Optional[float] = None
    pv_mw: Optional[float] = None
    wind_mw: Optional[float] = None
    bess_mw: float = 0.0
    bess_mwh: float = 0.0
    site_topology: str = "grid_connected_btm"
    eff_charge: float = 0.95
    eff_discharge: float = 0.95
    min_soc_pct: float = 10.0
    max_soc_pct: float = 90.0
    initial_soc_pct: float = 50.0
    sweep_points: int = 6                     # points per lever sweep (battery, PV)


_MINGRID_CACHE: dict = {}


def _min_grid_connection(df, params, *, pv_mw, bess_mw, bess_mwh, allow_grid_charge,
                         iters: int = 18) -> float:
    """Smallest grid ceiling (MW) that serves ALL load with this fixed design, found
    by bisection on the ceiling under Model R peak-shaving. Zero unmet is the test."""
    from core.rule_dispatch import run_rule_dispatch, compute_flow_kpis
    peak = float(df["load_mw"].max())
    lo, hi = 0.0, peak
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        f = run_rule_dispatch(df, params, pv_mw=pv_mw, bess_mw=bess_mw, bess_mwh=bess_mwh,
                              grid_ceiling_mw=mid, mode="peak_shaving",
                              allow_grid_charge=allow_grid_charge)
        unmet = compute_flow_kpis(f, params.dt_hours)["Total Unmet Load (MWh)"]
        if unmet <= 1e-3:
            hi = mid
        else:
            lo = mid
    return round(hi, 1)


def _binding_shortage(df, params, *, pv_mw, bess_mw, bess_mwh, ceiling, allow_grid_charge) -> dict:
    """At a given ceiling, characterise the LONGEST sustained above-ceiling stretch —
    the constraint that sets the minimum grid connection. Returns its duration, the
    energy above the connection (what the battery must cover), and the battery SOC
    entering it (how full it managed to get). This is why the connection can't go
    lower: an energy-limited shortage, not controller cleverness."""
    from core.rule_dispatch import run_rule_dispatch
    from core.schema import FlowCols
    import numpy as np

    dt = params.dt_hours
    f = run_rule_dispatch(df, params, pv_mw=pv_mw, bess_mw=bess_mw, bess_mwh=bess_mwh,
                          grid_ceiling_mw=ceiling, mode="peak_shaving",
                          allow_grid_charge=allow_grid_charge)
    load = df["load_mw"].to_numpy()
    soc = f[FlowCols.SOC_MWH].to_numpy()
    above = load > ceiling - 1e-6

    runs, start = [], None
    for i, a in enumerate(above):
        if a and start is None:
            start = i
        elif not a and start is not None:
            runs.append((start, i)); start = None
    if start is not None:
        runs.append((start, len(above)))
    if not runs:
        return {"hours": 0.0, "deficit_mwh": 0.0, "soc_entering_pct": None}

    s, e = max(runs, key=lambda r: r[1] - r[0])
    deficit = float(np.sum(load[s:e] - ceiling) * dt)
    soc_max = bess_mwh * params.max_soc_pct / 100.0
    soc_in = float(soc[s - 1]) if s > 0 else float(soc[0])
    return {
        "hours": round((e - s) * dt, 1),
        "deficit_mwh": round(deficit, 1),
        "soc_entering_pct": round(100.0 * soc_in / soc_max, 0) if soc_max > 1e-9 else None,
    }


def _grid_minimiser_report(df, params, *, pv_mw, bess_mw, bess_mwh, sweep_points=6) -> dict:
    """The minimum grid connection a fixed design can hold + sensitivity to the real
    levers (battery power, battery energy/duration, PV, grid-charging) + the binding
    shortage. Shared by the /min-grid-connection endpoint and the Excel export so both
    tell the identical story."""
    peak_load = round(float(df["load_mw"].max()), 1)
    duration = (bess_mwh / bess_mw) if bess_mw > 1e-9 else 5.0

    def mg(pv, bmw, bmwh, gc):
        return _min_grid_connection(df, params, pv_mw=pv, bess_mw=bmw, bess_mwh=bmwh,
                                    allow_grid_charge=gc)

    cur_off = mg(pv_mw, bess_mw, bess_mwh, False)
    cur_on  = mg(pv_mw, bess_mw, bess_mwh, True)

    n = max(3, min(int(sweep_points), 10))
    # Battery power sweep (grid-charging ON, PV fixed): 0 → 2× the current power.
    bmax = max(bess_mw * 2.0, bess_mw + 20.0, 40.0)
    battery_sweep = [
        {"bess_mw": round(b, 1), "bess_mwh": round(b * duration, 1),
         "min_grid_mw": mg(pv_mw, b, b * duration, True)}
        for b in (bmax * i / (n - 1) for i in range(n))
    ]
    # PV sweep (grid-charging ON, current battery): current → 2× (or up to a sensible cap).
    pvmax = max(pv_mw * 2.0, pv_mw + 50.0, 100.0)
    pv_sweep = [
        {"pv_mw": round(p, 1), "min_grid_mw": mg(p, bess_mw, bess_mwh, True)}
        for p in (pvmax * i / (n - 1) for i in range(n))
    ]
    # Battery ENERGY (duration) sweep — fix POWER, vary hours: the lever for a long
    # sustained shortage. Only meaningful with a battery present; grid-charging ON.
    duration_sweep = []
    if bess_mw > 1e-6:
        dmax = max(12.0, duration * 1.5)
        durations = [2.0 + (dmax - 2.0) * i / (n - 1) for i in range(n)]
        duration_sweep = [
            {"duration_h": round(d, 1), "bess_mwh": round(bess_mw * d, 1),
             "min_grid_mw": mg(pv_mw, bess_mw, bess_mw * d, True)}
            for d in durations
        ]

    shortage = _binding_shortage(df, params, pv_mw=pv_mw, bess_mw=bess_mw,
                                 bess_mwh=bess_mwh, ceiling=cur_on, allow_grid_charge=True)
    return {
        "no_battery_mw": peak_load,
        "current": {
            "pv_mw": round(pv_mw, 1), "bess_mw": round(bess_mw, 1),
            "bess_mwh": round(bess_mwh, 1), "duration_h": round(duration, 1),
            "min_grid_off": cur_off, "min_grid_on": cur_on,
            "grid_charge_saving_mw": round(cur_off - cur_on, 1),
        },
        "battery_sweep": battery_sweep,
        "duration_sweep": duration_sweep,
        "pv_sweep": pv_sweep,
        "binding_shortage": shortage,
    }


@app.post("/min-grid-connection")
def min_grid_connection(req: MinGridRequest) -> dict:
    """The minimum grid connection a design can hold + sensitivity to the real levers.
    Returns the no-battery baseline, the current design's minimum (grid-charging off vs
    on), battery/energy/PV sweeps, and the binding shortage."""
    _sig = tuple(sorted(req.model_dump().items(), key=lambda kv: kv[0]))
    hit = _MINGRID_CACHE.get(_sig)
    if hit is not None:
        return hit

    path = req.profile_path or str(_DEFAULT_PROFILE)
    if not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"Profile file not found: {path}")
    df, load_np, pv_np, dt_hours = _load_profile(path)
    if req.load_peak_mw and req.load_peak_mw > 0:
        cur = float(df["load_mw"].max())
        if cur > 1e-9:
            df = df.copy(); df["load_mw"] = df["load_mw"] * (req.load_peak_mw / cur)
    solar_mw = req.pv_mw if req.pv_mw is not None else pv_np
    wind_mw  = float(req.wind_mw or 0.0)
    eng_pv = solar_mw
    if wind_mw > 0 and "wind_pu" in df.columns and float(df["wind_pu"].max()) > 1e-9:
        comb = df["pv_pu"].to_numpy() * solar_mw + df["wind_pu"].to_numpy() * wind_mw
        peak = float(comb.max())
        if peak > 1e-9:
            df = df.copy(); df["pv_pu"] = comb / peak; eng_pv = peak

    params = PhysicalParams(
        dt_hours=dt_hours, site_topology=req.site_topology,
        eff_charge=req.eff_charge, eff_discharge=req.eff_discharge,
        min_soc_pct=req.min_soc_pct, max_soc_pct=req.max_soc_pct,
        initial_soc_pct=req.initial_soc_pct)

    out = _grid_minimiser_report(df, params, pv_mw=eng_pv, bess_mw=req.bess_mw,
                                 bess_mwh=req.bess_mwh, sweep_points=req.sweep_points)
    if len(_MINGRID_CACHE) >= 128:
        _MINGRID_CACHE.clear()
    _MINGRID_CACHE[_sig] = out
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Grid-charging trade-off — the battery the target needs WITH vs WITHOUT grid-charging
# ──────────────────────────────────────────────────────────────────────────────
class SizeTradeoffRequest(BaseModel):
    """Size the battery to hit a target under BOTH grid-charging policies, so the user
    sees what allowing grid pre-charge saves in hardware. Grid-charging lets a smaller
    battery hold a grid connection (big saving for the grid objective); for a self-
    sufficiency target it makes no difference (importing to charge lowers SSR)."""
    profile_path: Optional[str] = None
    load_peak_mw: Optional[float] = None
    pv_mw: Optional[float] = None
    wind_mw: Optional[float] = None
    target_type: str = "ssr"                 # "ssr" | "peak_shaving"
    target_value: float = 90.0               # SSR % (ssr) | grid ceiling MW (peak_shaving)
    duration_h: float = 4.0
    site_topology: str = "grid_connected_btm"
    site_max_grid_mw: float = 200.0
    site_max_bess_mw: float = 500.0
    site_max_bess_mwh: float = 4000.0
    eff_charge: float = 0.95
    eff_discharge: float = 0.95
    min_soc_pct: float = 10.0
    max_soc_pct: float = 90.0
    initial_soc_pct: float = 50.0


_TRADEOFF_CACHE: dict = {}


@app.post("/size-tradeoff")
def size_tradeoff(req: SizeTradeoffRequest) -> dict:
    """Battery sized to hit the target with grid-charging ON vs OFF + the saving."""
    from core.rule_dispatch import size_by_bisection

    _sig = tuple(sorted(req.model_dump().items(), key=lambda kv: kv[0]))
    hit = _TRADEOFF_CACHE.get(_sig)
    if hit is not None:
        return hit

    path = req.profile_path or str(_DEFAULT_PROFILE)
    if not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"Profile file not found: {path}")
    df, load_np, pv_np, dt_hours = _load_profile(path)
    if req.load_peak_mw and req.load_peak_mw > 0:
        cur = float(df["load_mw"].max())
        if cur > 1e-9:
            df = df.copy(); df["load_mw"] = df["load_mw"] * (req.load_peak_mw / cur)
    solar_mw = req.pv_mw if req.pv_mw is not None else pv_np
    wind_mw  = float(req.wind_mw or 0.0)
    eng_pv = solar_mw
    if wind_mw > 0 and "wind_pu" in df.columns and float(df["wind_pu"].max()) > 1e-9:
        comb = df["pv_pu"].to_numpy() * solar_mw + df["wind_pu"].to_numpy() * wind_mw
        peak = float(comb.max())
        if peak > 1e-9:
            df = df.copy(); df["pv_pu"] = comb / peak; eng_pv = peak

    def _size(gc: bool) -> dict:
        params = PhysicalParams(
            dt_hours=dt_hours, site_topology=req.site_topology,
            site_max_grid_mw=req.site_max_grid_mw,
            site_max_bess_mw=req.site_max_bess_mw, site_max_bess_mwh=req.site_max_bess_mwh,
            eff_charge=req.eff_charge, eff_discharge=req.eff_discharge,
            min_soc_pct=req.min_soc_pct, max_soc_pct=req.max_soc_pct,
            initial_soc_pct=req.initial_soc_pct, allow_grid_charge=gc)
        r = size_by_bisection(
            df, params, pv_mw=eng_pv, target_type=req.target_type,
            target_value=req.target_value, duration_h=req.duration_h,
            grid_ceiling_mw=(None if req.target_type == "peak_shaving" else req.site_max_grid_mw))
        return {"bess_mw": round(r.bess_mw, 1), "bess_mwh": round(r.bess_mwh, 1),
                "feasible": bool(r.feasible)}

    on, off = _size(True), _size(False)
    saving_mw = round(off["bess_mw"] - on["bess_mw"], 1)
    saving_mwh = round(off["bess_mwh"] - on["bess_mwh"], 1)
    out = {
        "target_type": req.target_type, "target_value": req.target_value,
        "grid_charge_on": on, "grid_charge_off": off,
        "saving_mw": saving_mw, "saving_mwh": saving_mwh,
        # For SSR the two are equal (grid-charging hurts self-sufficiency); flag it so
        # the UI can say "no effect for this objective — keep grid-charging off".
        "matters": abs(saving_mw) > 0.5 or abs(saving_mwh) > 1.0,
    }
    if len(_TRADEOFF_CACHE) >= 128:
        _TRADEOFF_CACHE.clear()
    _TRADEOFF_CACHE[_sig] = out
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Land-first evaluation core (Phase 1) — one land/demand input → scenario compare
# ──────────────────────────────────────────────────────────────────────────────
class DesignRequest(BaseModel):
    """Land-first design input. From the available land + the data-centre demand +
    the (pre-allotted / built) generation, produce the canonical scenario comparison
    so a developer sees what a battery actually buys — without picking a scenario id.

    This is the Phase-1 core: it ORCHESTRATES existing ready scenarios rather than
    changing the engine. Later phases promote generation to a decision variable
    under the land budget (see GIGA_PARK_CHANGES.md)."""
    profile_path: Optional[str] = None
    load_peak_mw: Optional[float] = None       # data-centre demand peak (MW) — the anchor
    pv_mw: float = 0.0                          # allotted / built solar nameplate (MW)
    wind_mw: float = 0.0                        # allotted / built wind nameplate (MW)
    target_ssr_pct: float = 90.0               # self-sufficiency target for the BESS design
    site_topology: Optional[str] = "grid_connected_btm"

    available_land_ha: Optional[float] = None  # informational now; constrains later phases


_DESIGN_KPI = {
    "ssr_pct": "SSR (%)", "scr_pct": "SCR (%)",
    "curtailment_pct": "OSR / Curtailment (%)",
    "grid_p95_mw": "GCmin P95 (MW)", "grid_import_mwh": "Total Grid Import (MWh)",
}


def _design_summary(label: str, config: str, out: dict, wind_mw: float) -> dict:
    """Normalise one /run result into a comparison row."""
    design = out.get("design") or {}
    kpis = out.get("kpis") or {}
    row = {
        "label": label, "config": config,
        "feasible": out.get("feasible"),
        "grid_peak_mw": kpis.get("GCmin Peak (MW)", design.get("gc_mw")),
        "pv_mw": design.get("pv_mw"), "wind_mw": wind_mw,
        "bess_mw": design.get("bess_mw"), "bess_mwh": design.get("bess_mwh"),
        "duration_h": design.get("duration_h"),
    }
    for out_key, kpi_key in _DESIGN_KPI.items():
        row[out_key] = kpis.get(kpi_key)
    return row


@app.post("/design")
def design(req: DesignRequest) -> dict:
    """Phase-1 evaluation core: one land/demand input → a scenario comparison.

    Orchestrates three READY scenarios on the same inputs:
      • Grid-only baseline    — S00, pv=0, bess=0
      • Generation, no BESS   — S00, generation fixed, bess=0
      • Generation + BESS     — S11, size BESS for the target SSR
    Returns a comparison array so the front end shows what the battery buys."""
    def _run(scenario_id: str, pv: float, wind: float) -> dict:
        return run(RunRequest(
            scenario_id=scenario_id, profile_path=req.profile_path,
            load_peak_mw=req.load_peak_mw, pv_mw=pv, wind_mw=wind,
            target_ssr_pct=req.target_ssr_pct, site_topology=req.site_topology,

        ))

    scenarios = [
        _design_summary("Grid-only", "no gen · no BESS",
                        _run("S00_FIXED_DESIGN_EVAL", 0.0, 0.0), 0.0),
        _design_summary("Generation, no BESS", "B = 0 forced",
                        _run("S00_FIXED_DESIGN_EVAL", req.pv_mw, req.wind_mw), req.wind_mw),
        _design_summary("Generation + BESS", f"BESS sized for SSR {req.target_ssr_pct:g}%",
                        _run("S11_BTM_SSR_TARGET_BESS", req.pv_mw, req.wind_mw), req.wind_mw),
    ]
    return {
        "inputs": {
            "load_peak_mw": req.load_peak_mw, "pv_mw": req.pv_mw, "wind_mw": req.wind_mw,
            "target_ssr_pct": req.target_ssr_pct, "available_land_ha": req.available_land_ha,
        },
        "scenarios": scenarios,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Split optimiser (Phase 3, sweep) — cheapest PV/BESS split within the land
# ──────────────────────────────────────────────────────────────────────────────
class OptimiseSplitRequest(BaseModel):
    """Land-first split optimiser. Sweep the solar allocation across the land the
    developer owns, size BESS for the SSR target at each point (existing LP), cost
    each with the same inputs as the Phase-2 overlay, and return the cheapest split
    plus the frontier. 'The tool decides the split' — with cost as the arbiter.

    Solar-only sweep for now (wind fixed); the coupled cost-LP is the end-state."""
    profile_path: Optional[str] = None
    load_peak_mw: Optional[float] = None
    available_land_ha: float = 184.0        # land available to GENERATION (net of load/reserved)
    target_ssr_pct: float = 90.0
    wind_mw: float = 0.0                     # fixed wind nameplate (MW)
    steps: int = 8                           # sweep resolution (PV points)
    site_topology: Optional[str] = "grid_connected_btm"



@app.post("/optimise-split")
def optimise_split(req: OptimiseSplitRequest) -> dict:
    """Sweep PV over [0, ρ_s·A_avail], size BESS for the SSR target at each point,
    cost each, and return the cost-optimal split + the frontier."""
    pv_ceiling = _SOLAR_MW_PER_HA * max(0.0, req.available_land_ha)
    steps = max(2, min(int(req.steps), 20))
    pv_points = [round(pv_ceiling * i / (steps - 1), 2) for i in range(steps)]

    # Analytic feasibility floor: a battery only SHIFTS energy, never creates it, so
    # solar must generate at least the self-supplied share of demand. PV below this
    # can't reach the target — skip the (slow) solve instead of proving it infeasible.
    pv_floor = 0.0
    try:
        df0, _, _, dt0 = _load_profile(req.profile_path or str(_DEFAULT_PROFILE))
        load0 = df0["load_mw"]
        if req.load_peak_mw and float(load0.max()) > 1e-9:
            load0 = load0 * (req.load_peak_mw / float(load0.max()))
        pv_energy_per_mw = float(df0["pv_pu"].sum()) * dt0          # MWh/yr per MW solar
        wind_energy = req.wind_mw * float(df0.get("wind_pu", pd.Series([0])).sum()) * dt0
        need = (req.target_ssr_pct / 100.0) * float(load0.sum()) * dt0 - wind_energy
        pv_floor = max(0.0, need / pv_energy_per_mw) if pv_energy_per_mw > 1e-9 else 0.0
    except Exception:
        pv_floor = 0.0

    # Each PV point is an independent full-year LP. The appsi/HiGHS solver is
    # in-process and NOT thread-safe, so fan the points out across PROCESSES.
    path = req.profile_path or str(_DEFAULT_PROFILE)
    todo = [pv for pv in pv_points if pv + 1e-6 >= pv_floor]   # prune infeasible-by-energy
    args = [(path, req.load_peak_mw, pv, req.wind_mw, req.target_ssr_pct, req.site_topology)
            for pv in todo]

    results: list[dict] = []
    if args:
        try:
            workers = max(1, min(len(args), os.cpu_count() or 2))
            with ProcessPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(solve_ssr_point_star, args))
        except Exception:
            results = [solve_ssr_point(*a) for a in args]   # fallback: sequential, never break

    def _physical_row(r: dict) -> dict:
        pv = r["pv_mw"]; bess_mw = r["bess_mw"]; bess_mwh = r["bess_mwh"]
        lp_grid = r["grid_mw"]; lp_ssr = r["ssr_pct"]
        # Headline = operational (Model R, causal) truth when the rule verified this
        # design; the LP (Model O) numbers are the optimistic bound, kept for context.
        verified = r.get("op_ssr_pct") is not None
        ssr  = r["op_ssr_pct"] if verified else lp_ssr
        gc   = r["op_gc_mw"] if verified else lp_grid
        scr  = r.get("op_scr_pct") if verified else r.get("scr_pct")
        curt = r.get("op_curtailment_pct") if verified else r.get("curtailment_pct")
        return {
            "pv_mw": pv, "wind_mw": req.wind_mw,
            "pv_land_ha": round(pv / _SOLAR_MW_PER_HA, 1) if _SOLAR_MW_PER_HA else None,
            "bess_mw": round(bess_mw, 1), "bess_mwh": round(bess_mwh, 1),
            "duration_h": round(bess_mwh / bess_mw, 1) if bess_mw > 1e-9 else 0.0,
            # grid_mw is the internal sort key; gc_mw is the app-wide name the UI reads.
            "grid_mw": round(gc, 2), "gc_mw": round(gc, 2),
            "ssr_pct": round(ssr, 1),
            "scr_pct": round(scr, 1) if scr is not None else None,
            "curtailment_pct": round(curt, 1) if curt is not None else None,
            # Transparency: the optimistic LP bound and the cost of no foresight.
            "verified": verified,
            "lp_ssr_pct": round(lp_ssr, 1),
            "lp_gc_mw": round(lp_grid, 2),
            "ssr_gap_pp": round(lp_ssr - r["op_ssr_pct"], 1) if verified else None,
            "unmet_mwh": round(r.get("op_unmet_mwh") or 0.0, 1) if verified else None,
        }

    frontier = sorted((_physical_row(r) for r in results if r and r.get("feasible")),
                      key=lambda x: x["pv_mw"])
    # Recommend the split with the smallest grid connection. gc_mw is the honest
    # (Model R) peak when verified, so the pick is ranked on the deliverable, not
    # the optimistic LP bound.
    best = min(frontier, key=lambda r: r["gc_mw"]) if frontier else None
    return {
        "inputs": {
            "available_land_ha": req.available_land_ha, "load_peak_mw": req.load_peak_mw,
            "target_ssr_pct": req.target_ssr_pct, "wind_mw": req.wind_mw,
        },
        "pv_ceiling_mw": round(pv_ceiling, 1),
        "pv_floor_mw": round(pv_floor, 1),
        "recommended": best,
        "frontier": frontier,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Excel export — build a downloadable .xlsx from a run result (no re-solve)
# ──────────────────────────────────────────────────────────────────────────────

class ExportRequest(BaseModel):
    """Everything needed to build a rich Excel workbook. The summary numbers come
    from the result the UI holds; the per-point dispatch time-series are re-derived
    by forward-evaluating each sized point (fast — no LP re-solve)."""
    project_name: str = "DIP Project"
    scenario_id: str = ""
    scenario_name: str = ""
    recommended: dict = Field(default_factory=dict)

    points: list[dict] = Field(default_factory=list)     # each: pv_mw,bess_mw,bess_mwh,ssr_pct,gc_mw,…
    kpis: dict = Field(default_factory=dict)
    assumptions: dict = Field(default_factory=dict)
    # Inputs to reproduce each point's dispatch (per-slot detail):
    profile_path: Optional[str] = None
    load_peak_mw: Optional[float] = None
    pv_mw: Optional[float] = None            # fixed solar nameplate across the curve
    wind_mw: Optional[float] = None
    site_topology: str = "grid_connected_btm"
    site_max_grid_mw: float = 200.0
    eff_charge: float = 0.95
    eff_discharge: float = 0.95
    min_soc_pct: float = 10.0
    max_soc_pct: float = 90.0
    initial_soc_pct: float = 50.0
    include_timeseries: bool = True
    max_point_sheets: int = 12               # cap per-point sheets so files stay sane
    # "Everything in detail" mode: echo the raw input profiles, drop the per-point
    # sheet cap, and add per-point KPIs, the SSR feasibility band, and an annual
    # energy-balance reconciliation. Defaults on — the export is meant to be complete.
    full_detail: bool = True
    site_max_bess_mw: float = 500.0          # site BESS ceiling (for the SSR band)
    site_max_bess_mwh: float = 4000.0
    # Rolling-horizon dispatch (Model W): operate the recommended design under
    # limited foresight and compare against Model R (the value of a forecast).
    rolling_window: bool = True
    rolling_horizon_h: float = 24.0          # look-ahead per window (hours)
    rolling_commit_h: float = 1.0            # committed block before re-optimising
    # Dispatch policy (Model R) so the re-derived per-slot flows match the run.
    dispatch_priority: list[str] = Field(default_factory=list)
    allow_grid_charge: Optional[bool] = None


def _kv_sheet(d: dict) -> pd.DataFrame:
    return pd.DataFrame({"Field": list(d.keys()), "Value": list(d.values())})


def _autofit(ws, max_w: int = 42) -> None:
    """Freeze the header row and set sensible column widths."""
    ws.freeze_panes = "A2"
    for col in ws.columns:
        letter = col[0].column_letter
        width = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[letter].width = min(max_w, max(10, width + 2))


@app.post("/export")
def export_xlsx(req: ExportRequest):
    """Return a rich .xlsx: Summary, Recommended, Frontier, per-point energy split,
    Assumptions, and — for each sized point — a full per-slot dispatch sheet showing
    what served the load every timestep (like the old reports/ builders)."""
    from core.params import PhysicalParams
    from core.rule_dispatch import verify_sizing_with_rule
    from core.report import hourly_flows, energy_split, monthly

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        _kv_sheet({
            "Project": req.project_name,
            "Scenario": f"{req.scenario_id} — {req.scenario_name}".strip(" —"),
            "Generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "Topology": req.site_topology,
        }).to_excel(xw, sheet_name="Summary", index=False)

        rec = {**req.recommended}
        if rec:
            _kv_sheet(rec).to_excel(xw, sheet_name="Recommended", index=False)
        if req.points:
            pd.DataFrame(req.points).to_excel(xw, sheet_name="Frontier", index=False)
        if req.kpis:
            _kv_sheet(req.kpis).to_excel(xw, sheet_name="KPIs", index=False)
        if req.assumptions:
            _kv_sheet(req.assumptions).to_excel(xw, sheet_name="Assumptions", index=False)

        # ── Definitions: make the workbook self-describing (every term + sheet). ──
        if req.full_detail:
            pd.DataFrame({
                "Term": [
                    "SSR (%)", "SCR (%)", "OSR / Curtailment (%)", "GCmin (MW)",
                    "Model O", "Model W", "Model R", "PV → Load", "PV → BESS", "BESS → Load",
                    "Grid → Load", "Unmet", "SOC (MWh)", "CHECK served = Load",
                    "Sheet: Input Profiles", "Sheet: Run Parameters",
                    "Sheet: SSR Feasibility Band", "Sheet: Per-point KPIs",
                    "Sheet: Recommended dispatch", "Sheet: Annual Energy Balance",
                ],
                "Meaning": [
                    "Self-Sufficiency Ratio — share of demand met by on-site PV+BESS (not grid).",
                    "Self-Consumption Ratio — share of PV generation used on-site (vs curtailed).",
                    "Curtailment — share of available PV spilled because it couldn't be used or stored.",
                    "Minimum grid connection — the peak grid import the design still requires.",
                    "Perfect-foresight LP (optimistic lower bound on the battery needed).",
                    "Rolling-horizon dispatch — limited foresight (look ahead, commit, roll). Realistic operation, between O and R.",
                    "Causal rule dispatch — no foresight, the buildable truth. All headline KPIs use Model R.",
                    "PV serving load directly this timestep.",
                    "Surplus PV charging the battery this timestep.",
                    "Battery discharging to serve load this timestep.",
                    "Grid import serving load this timestep.",
                    "Load not served (should be 0 for a feasible design).",
                    "Battery state of charge (energy) at each timestep.",
                    "Reconciliation: PV→Load + BESS→Load + Grid→Load + Unmet must equal Load.",
                    "The exact load/PV/wind series the run consumed (raw echo).",
                    "Every parameter driving dispatch — reproduce any number from these.",
                    "Achievable SSR from no battery (floor) to site-max battery (ceiling).",
                    "All computed KPIs for every sized point on the curve.",
                    "Full per-slot dispatch of the recommended design (Model R).",
                    "Annual MWh split with a served-vs-load reconciliation check.",
                ],
            }).to_excel(xw, sheet_name="Definitions", index=False)

        # ── Per-point dispatch: forward-eval each sized point → per-slot sheet ──
        _prof = req.profile_path or str(_DEFAULT_PROFILE)   # default to bundled dataset
        if req.include_timeseries and Path(_prof).exists():
            try:
                df, load_np, pv_np, dt_hours = _load_profile(_prof)
                # reproduce the run's load scaling + generation combine
                if req.load_peak_mw and req.load_peak_mw > 0:
                    cur = float(df["load_mw"].max())
                    if cur > 1e-9:
                        df = df.copy(); df["load_mw"] = df["load_mw"] * (req.load_peak_mw / cur)
                solar_mw = req.pv_mw if req.pv_mw is not None else pv_np
                wind_mw  = float(req.wind_mw or 0.0)
                eng_pv = solar_mw
                if wind_mw > 0 and "wind_pu" in df.columns and float(df["wind_pu"].max()) > 1e-9:
                    comb = df["pv_pu"].to_numpy() * solar_mw + df["wind_pu"].to_numpy() * wind_mw
                    peak = float(comb.max())
                    if peak > 1e-9:
                        df = df.copy(); df["pv_pu"] = comb / peak; eng_pv = peak
                params = PhysicalParams(
                    dt_hours=dt_hours, site_topology=req.site_topology,
                    site_max_grid_mw=req.site_max_grid_mw,
                    eff_charge=req.eff_charge, eff_discharge=req.eff_discharge,
                    min_soc_pct=req.min_soc_pct, max_soc_pct=req.max_soc_pct,
                    initial_soc_pct=req.initial_soc_pct,
                    dispatch_priority=tuple(req.dispatch_priority or ()),
                    allow_grid_charge=req.allow_grid_charge)

                # ── Run Parameters: EVERY value that drives the per-slot dispatch,
                #    so a reader can reproduce every number in this workbook. ──
                if req.full_detail:
                    _order = list(req.dispatch_priority) or ["(engine default merit order)"]
                    _kv_sheet({
                        "Timestep dt (h)": dt_hours,
                        "Topology": req.site_topology,
                        "Grid ceiling (MW)": req.site_max_grid_mw,
                        "Solar nameplate (MW)": round(float(solar_mw), 3),
                        "Wind nameplate (MW)": round(wind_mw, 3),
                        "Engine generation nameplate (MW)": round(float(eng_pv), 3),
                        "Load scaled to peak (MW)": req.load_peak_mw or "(file default)",
                        "Charge efficiency": req.eff_charge,
                        "Discharge efficiency": req.eff_discharge,
                        "Round-trip efficiency": round(req.eff_charge * req.eff_discharge, 4),
                        "SOC min (%)": req.min_soc_pct,
                        "SOC max (%)": req.max_soc_pct,
                        "SOC initial (%)": req.initial_soc_pct,
                        "Usable SOC window (%)": round(req.max_soc_pct - req.min_soc_pct, 1),
                        "Site-max BESS power (MW)": req.site_max_bess_mw,
                        "Site-max BESS energy (MWh)": req.site_max_bess_mwh,
                        "Dispatch merit order": "  →  ".join(_order),
                        "Grid-charging": ("auto (from objective)" if req.allow_grid_charge is None
                                          else ("on" if req.allow_grid_charge else "off")),
                        "Dispatch model": "Model R (causal, no foresight) — the buildable truth",
                    }).to_excel(xw, sheet_name="Run Parameters", index=False)

                # ── Raw input echo: the exact profiles the run consumed, so input
                #    and results live in one file (full 8760 / 15-min series). ──
                if req.full_detail:
                    _wind_pu = (df["wind_pu"].to_numpy() if "wind_pu" in df.columns
                                else [0.0] * len(df))
                    input_echo = pd.DataFrame({
                        "Timestamp": df["timestamp"].to_numpy(),
                        "Load (MW)": df["load_mw"].round(4).to_numpy(),
                        "PV (p.u.)": df["pv_pu"].round(6).to_numpy(),
                        "PV available (MW)": (df["pv_pu"] * eng_pv).round(4).to_numpy(),
                        "Wind (p.u.)": _wind_pu,
                    })
                    input_echo.to_excel(xw, sheet_name="Input Profiles", index=False)
                    # Provenance + native resolution alongside the raw series.
                    _kv_sheet({
                        "Source file": Path(_prof).name,
                        "Rows (timesteps)": len(df),
                        "Native dt (h)": dt_hours,
                        "Load peak (MW)": round(float(df["load_mw"].max()), 3),
                        "Load energy (MWh/yr)": round(float(df["load_mw"].sum() * dt_hours), 1),
                        "Solar nameplate used (MW)": round(float(eng_pv), 3),
                        "Wind nameplate (MW)": round(wind_mw, 3),
                        "Load scaled to peak (MW)": req.load_peak_mw or "(file default)",
                    }).to_excel(xw, sheet_name="Input Summary", index=False)

                # ── SSR feasibility band: what's achievable at this generation, from
                #    no battery (PV-direct floor) to the site's max BESS (ceiling). ──
                if req.full_detail:
                    def _ssr_at(bmw, bmwh):
                        k, _ = verify_sizing_with_rule(
                            df, params, pv_mw=eng_pv, bess_mw=bmw, bess_mwh=bmwh,
                            target_type="ssr", grid_ceiling_mw=req.site_max_grid_mw)
                        return float(k.get(KpiKeys.SSR, 0.0) or 0.0)
                    try:
                        _kv_sheet({
                            "SSR min — no BESS (%)": round(_ssr_at(0.0, 0.0), 1),
                            "SSR max — site-max BESS (%)": round(
                                _ssr_at(req.site_max_bess_mw, req.site_max_bess_mwh), 1),
                            "Site-max BESS power (MW)": req.site_max_bess_mw,
                            "Site-max BESS energy (MWh)": req.site_max_bess_mwh,
                        }).to_excel(xw, sheet_name="SSR Feasibility Band", index=False)
                    except Exception:
                        pass

                # Points may arrive keyed snake_case (pv_mw, …) or by the engine's
                # curve column names ("BESS Power (MW)", …). Read either.
                def _pget(p, *keys):
                    for k in keys:
                        v = p.get(k)
                        if v is not None:
                            return v
                    return None

                # Full detail ⇒ no per-point sheet cap; otherwise honour the cap.
                cap = len(req.points) if req.full_detail else req.max_point_sheets
                splits, point_kpis = [], []
                last_flows = None
                pts = [p for p in req.points
                       if (_pget(p, "bess_mwh", CurveCols.BESS_MWH) or 0) >= 0][: cap]
                for i, p in enumerate(pts):
                    p_bess_mw  = float(_pget(p, "bess_mw", CurveCols.BESS_MW) or 0)
                    p_bess_mwh = float(_pget(p, "bess_mwh", CurveCols.BESS_MWH) or 0)
                    p_ssr      = _pget(p, "ssr_pct", CurveCols.ACHIEVED_SSR_PCT, CurveCols.OP_SSR_PCT)
                    p_gc       = _pget(p, "gc_mw", CurveCols.PEAK_GC_MW, CurveCols.OP_PEAK_GC_MW) or 0
                    k, flows = verify_sizing_with_rule(
                        df, params, pv_mw=eng_pv,
                        bess_mw=p_bess_mw, bess_mwh=p_bess_mwh,
                        target_type="ssr", grid_ceiling_mw=req.site_max_grid_mw)
                    last_flows = flows
                    tag = (f"SSR{round(float(p_ssr))}" if p_ssr is not None
                           else f"GC{round(float(p_gc))}")
                    hourly_flows(flows).to_excel(xw, sheet_name=f"P{i+1}_{tag}"[:31], index=False)
                    splits.append({"Point": f"P{i+1} {tag}",
                                   "BESS (MW)": p_bess_mw, "BESS (MWh)": p_bess_mwh,
                                   **energy_split(flows, dt_hours)})
                    # Every computed KPI for this point (SSR/SCR/GCmin/OSR/…).
                    point_kpis.append({"Point": f"P{i+1} {tag}",
                                       "BESS (MW)": p_bess_mw, "BESS (MWh)": p_bess_mwh,
                                       **_clean_dict(k)})
                if splits:
                    pd.DataFrame(splits).to_excel(xw, sheet_name="Per-point energy", index=False)
                if point_kpis and req.full_detail:
                    pd.DataFrame(point_kpis).to_excel(xw, sheet_name="Per-point KPIs", index=False)

                # ── Recommended design: derive its OWN flows from req.recommended
                #    (do NOT assume it is the last point processed). Falls back to the
                #    last point only when no recommended design was supplied. ──
                rec = req.recommended or {}
                rec_flows = last_flows
                if rec:
                    rec_bess_mw  = float(_pget(rec, "bess_mw", CurveCols.BESS_MW) or 0)
                    rec_bess_mwh = float(_pget(rec, "bess_mwh", CurveCols.BESS_MWH) or 0)
                    _kr, rec_flows = verify_sizing_with_rule(
                        df, params, pv_mw=eng_pv, bess_mw=rec_bess_mw, bess_mwh=rec_bess_mwh,
                        target_type="ssr", grid_ceiling_mw=req.site_max_grid_mw)
                if rec_flows is not None:
                    if req.full_detail:
                        # Full per-slot dispatch of the recommended design + its balance.
                        hourly_flows(rec_flows).to_excel(xw, sheet_name="Recommended dispatch", index=False)
                        bal = energy_split(rec_flows, dt_hours)
                        served = (bal.get("PV → Load (MWh)", 0) + bal.get("BESS → Load (MWh)", 0)
                                  + bal.get("Grid → Load (MWh)", 0))
                        bal["Served (MWh)"] = round(served, 1)
                        bal["Served + Unmet − Load (MWh)"] = round(
                            served + bal.get("Unmet (MWh)", 0) - bal.get("Load (MWh)", 0), 1)
                        _kv_sheet(bal).to_excel(xw, sheet_name="Annual Energy Balance", index=False)
                    monthly(rec_flows, dt_hours).to_excel(xw, sheet_name="Monthly (recommended)", index=False)

                    # ── Model W: rolling-horizon dispatch of the recommended design ──
                    #    Operate the SAME battery under limited foresight and compare
                    #    with Model R — the value a forecast-driven controller adds.
                    #    Guarded by a work proxy so a fine-resolution year can't hang
                    #    the request; skips with a note instead.
                    if req.rolling_window and rec:
                        try:
                            from core.rolling_dispatch import run_rolling_horizon_dispatch
                            from core.rule_dispatch import compute_flow_kpis
                            H = max(1, round(req.rolling_horizon_h / dt_hours))
                            C = max(1, min(round(req.rolling_commit_h / dt_hours), H))
                            work = (len(df) / C) * H       # ≈ solves × window size
                            if work > 2.0e6:
                                _kv_sheet({"Model W skipped":
                                    f"series too fine ({len(df)} steps) for horizon "
                                    f"{req.rolling_horizon_h:g}h / commit {req.rolling_commit_h:g}h; "
                                    "increase commit_h to enable."}).to_excel(
                                    xw, sheet_name="Model W note", index=False)
                            else:
                                rec_bmw  = float(_pget(rec, "bess_mw", CurveCols.BESS_MW) or 0)
                                rec_bmwh = float(_pget(rec, "bess_mwh", CurveCols.BESS_MWH) or 0)
                                w_flows = run_rolling_horizon_dispatch(
                                    df, params, pv_mw=eng_pv, bess_mw=rec_bmw, bess_mwh=rec_bmwh,
                                    grid_ceiling_mw=req.site_max_grid_mw,
                                    horizon_h=req.rolling_horizon_h, commit_h=req.rolling_commit_h,
                                    allow_grid_charge=req.allow_grid_charge)
                                kW = compute_flow_kpis(w_flows, dt_hours)
                                kR = compute_flow_kpis(rec_flows, dt_hours)
                                # Side-by-side R vs W on the same design.
                                comp = pd.DataFrame({
                                    "KPI": list(kR.keys()),
                                    "Model R (no foresight)": [_clean(kR[k]) for k in kR],
                                    "Model W (rolling horizon)": [_clean(kW.get(k)) for k in kR],
                                })
                                comp.to_excel(xw, sheet_name="Dispatch model comparison", index=False)
                                _kv_sheet({
                                    "Horizon (h)": req.rolling_horizon_h,
                                    "Commit (h)": req.rolling_commit_h,
                                    "Model": "W = limited foresight (between O=LP bound and R=causal)",
                                }).to_excel(xw, sheet_name="Model W settings", index=False)
                                if req.full_detail:
                                    hourly_flows(w_flows).to_excel(
                                        xw, sheet_name="Rolling dispatch (W)", index=False)
                        except Exception as exc:
                            _kv_sheet({"Model W error": str(exc)}).to_excel(
                                xw, sheet_name="Model W note", index=False)

                # ── Grid-connection minimiser: the smallest grid the recommended design
                #    can hold + the levers that shrink it (battery power/energy, PV,
                #    grid-charging) + the binding shortage. This is the "reduce the grid
                #    connection" story in the lender pack, not just the UI. ──
                if req.full_detail and rec and req.site_topology == "grid_connected_btm":
                    try:
                        gm_bmw  = float(_pget(rec, "bess_mw", CurveCols.BESS_MW) or 0)
                        gm_bmwh = float(_pget(rec, "bess_mwh", CurveCols.BESS_MWH) or 0)
                        rep = _grid_minimiser_report(df, params, pv_mw=eng_pv,
                                                     bess_mw=gm_bmw, bess_mwh=gm_bmwh)
                        cur = rep["current"]; sh = rep["binding_shortage"]
                        _kv_sheet({
                            "Peak demand — grid with no battery (MW)": rep["no_battery_mw"],
                            "Minimum grid — grid-charging OFF (MW)": cur["min_grid_off"],
                            "Minimum grid — grid-charging ON (MW)": cur["min_grid_on"],
                            "Grid-charging saving (MW)": cur["grid_charge_saving_mw"],
                            "Design PV (MW)": cur["pv_mw"],
                            "Design BESS (MW / MWh / h)":
                                f"{cur['bess_mw']} / {cur['bess_mwh']} / {cur['duration_h']}",
                            "Binding shortage — duration (h)": sh["hours"],
                            "Binding shortage — energy above connection (MWh)": sh["deficit_mwh"],
                            "Binding shortage — battery SOC entering (%)": sh["soc_entering_pct"],
                            "Note": "Model R hits the perfect-foresight floor for grid size; the "
                                    "levers are battery power, battery energy (hours), PV and grid-charging.",
                        }).to_excel(xw, sheet_name="Grid Connection", index=False)
                        # Lever sweeps as one tidy long-format table (min grid per lever step).
                        lever_rows = (
                            [{"Lever": "Battery power", "Value": r["bess_mw"], "Unit": "MW",
                              "Min grid (MW)": r["min_grid_mw"]} for r in rep["battery_sweep"]]
                            + [{"Lever": "Battery duration", "Value": r["duration_h"], "Unit": "h",
                                "Min grid (MW)": r["min_grid_mw"]} for r in rep["duration_sweep"]]
                            + [{"Lever": "PV nameplate", "Value": r["pv_mw"], "Unit": "MW",
                                "Min grid (MW)": r["min_grid_mw"]} for r in rep["pv_sweep"]]
                        )
                        pd.DataFrame(lever_rows).to_excel(xw, sheet_name="Grid levers", index=False)
                    except Exception as exc:
                        _kv_sheet({"Grid minimiser error": str(exc)}).to_excel(
                            xw, sheet_name="Grid note", index=False)
            except Exception as exc:
                _kv_sheet({"time-series error": str(exc)}).to_excel(xw, sheet_name="Dispatch note", index=False)

        for ws in xw.book.worksheets:
            _autofit(ws)

    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    fname = f"DIP_{req.scenario_id or 'result'}_{stamp}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
