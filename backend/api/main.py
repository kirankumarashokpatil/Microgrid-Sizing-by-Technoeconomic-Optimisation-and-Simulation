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

import httpx
import io
import math
import tempfile
import uuid
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

from core.params import PhysicalParams, EconomicParams       # noqa: E402
from core.profile_loader import (                            # noqa: E402
    load_profiles, load_bess_input, compute_annual_energy,
)
from core.economic_overlay import (                          # noqa: E402
    evaluate_costs, find_optimal_point,
)
from core.resolver import resolve_scenario                   # noqa: E402
from core.site import parcel_capacity                        # noqa: E402
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

    # ── Economics (Phase-2 overlay). When `with_economics` is true and the run
    #    produces a sizing curve/point, the response gains an `economics` block:
    #    per-point CAPEX/OPEX/NPV/LCOE, a recommended design, and savings vs a
    #    100%-grid baseline. Defaults mirror EconomicParams. ──────────────────
    with_economics: bool = False
    economic_objective: str = "knee"       # knee | min_cost | lcoe | capex | ssr
    cost_pv_mw: float = 700_000.0
    cost_wind_mw: float = 1_300_000.0
    cost_bess_mw: float = 150_000.0
    cost_bess_mwh: float = 300_000.0
    grid_connection_cost_mw: float = 250_000.0
    grid_cost_mwh: float = 150.0
    fixed_opex_per_mwh_year: float = 8_000.0
    cycle_life: int = 5000
    replacement_cost_mwh: float = 300_000.0
    nominal_discount_rate_pct: float = 8.0
    inflation_rate_pct: float = 2.5
    project_lifespan_years: float = 20.0
    off_take_tariff_mwh: float = 0.0


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
    """Make one value JSON-safe: numpy → python, datetimes → ISO string,
    NaN/inf → None. Timestamps matter because forward_eval flows carry a
    ``timestamp`` column, and the DB path serialises with plain ``json.dumps``
    (no FastAPI encoder) when storing JSONB."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    # pandas Timestamp subclasses datetime, so this catches both.
    if isinstance(v, datetime):
        return v.isoformat()
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


def _eco_from_request(req: "RunRequest") -> EconomicParams:
    """Build the engine's EconomicParams from the request's economic fields."""
    return EconomicParams(
        cost_pv_mw=req.cost_pv_mw,
        cost_wind_mw=req.cost_wind_mw,
        cost_bess_mw=req.cost_bess_mw,
        cost_bess_mwh=req.cost_bess_mwh,
        grid_connection_cost_mw=req.grid_connection_cost_mw,
        grid_cost_mwh=req.grid_cost_mwh,
        fixed_opex_per_mwh_year=req.fixed_opex_per_mwh_year,
        cycle_life=req.cycle_life,
        replacement_cost_mwh=req.replacement_cost_mwh,
        nominal_discount_rate_pct=req.nominal_discount_rate_pct,
        inflation_rate_pct=req.inflation_rate_pct,
        project_lifespan_years=req.project_lifespan_years,
        off_take_tariff_mwh=req.off_take_tariff_mwh,
    )


def _point_to_curve_df(result) -> Optional[pd.DataFrame]:
    """Synthesize a one-row CurveCols frame from a point/forward_eval result so
    the same economics overlay that costs curves can also cost a single design."""
    d = result.design or {}
    k = result.kpis or {}
    if not d:
        return None
    row = {
        CurveCols.SCENARIO:         result.id,
        CurveCols.PV_MW:            d.get("pv_mw", 0.0) or 0.0,
        CurveCols.BESS_MW:          d.get("bess_mw", 0.0) or 0.0,
        CurveCols.BESS_MWH:         d.get("bess_mwh", 0.0) or 0.0,
        CurveCols.PEAK_GC_MW:       d.get("gc_mw", k.get(KpiKeys.GCMIN_PEAK, 0.0)) or 0.0,
        CurveCols.ACHIEVED_SSR_PCT: k.get(KpiKeys.SSR, 0.0) or 0.0,
        CurveCols.OP_SSR_PCT:       k.get(KpiKeys.SSR, 0.0) or 0.0,
        CurveCols.ACHIEVED_SCR_PCT: k.get(KpiKeys.SCR, 0.0) or 0.0,
        CurveCols.FEASIBLE:         bool(result.feasible),
    }
    return pd.DataFrame([row])


def _simple_irr(capex: float, annual_benefit: float, years: int) -> Optional[float]:
    """IRR of an even-cashflow project: [-capex, +benefit × years]. Returns the
    rate as a percentage, or None when there is no sign change (no solution).
    Honest about its assumption — a flat annual benefit, not a year-by-year model."""
    if capex <= 0 or annual_benefit <= 0:
        return None
    def npv(r):
        return -capex + sum(annual_benefit / (1 + r) ** t for t in range(1, years + 1))
    lo, hi = -0.9, 5.0
    if npv(lo) < 0 and npv(hi) < 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        v = npv(mid)
        if abs(v) < 1.0:
            break
        if v > 0:
            lo = mid
        else:
            hi = mid
    return round(mid * 100.0, 2)


def _attach_economics(result, req: "RunRequest", profiles_df: pd.DataFrame,
                      dt_hours: float, solar_mw: Optional[float] = None,
                      wind_mw: float = 0.0) -> Optional[dict]:
    """Overlay Phase-2 economics on a run result. Works for curve/surface/frontier
    tables (cost every point, pick a recommended one) and for point designs (cost
    the single design). Returns a JSON-safe dict, or None when nothing is costable.

    Everything here is computed by optimizer.economic_overlay from the real sizing
    output — no synthetic numbers. Savings/IRR are stated against a 100%-grid
    baseline so the 'value vs grid-default' figures are explicit and auditable."""
    eco = _eco_from_request(req)

    # 1. Get a CurveCols frame to cost: the table for curves, else a 1-row point.
    curve = result.table
    is_point = curve is None or curve.empty
    if is_point:
        curve = _point_to_curve_df(result)
    if curve is None or curve.empty or CurveCols.BESS_MWH not in curve.columns:
        return None

    # When wind is used, the engine sized against a COMBINED generation profile, so
    # its PV_MW column is the combined nameplate. For COSTING, substitute the true
    # fixed solar/wind nameplates so each is priced at its own rate (PV here is a
    # fixed input, so overriding the column is safe).
    if wind_mw and wind_mw > 0:
        curve = curve.copy()
        if solar_mw is not None:
            curve[CurveCols.PV_MW] = solar_mw
        curve[CurveCols.WIND_MW] = wind_mw

    costed = evaluate_costs(curve, eco, profiles_df, dt_hours)
    if costed is None or costed.empty:
        return None

    rec = find_optimal_point(costed, objective=req.economic_objective)
    if rec is None or rec.empty:
        return None

    # 2. Baseline: 100% grid import, grid connection sized to peak load.
    total_demand_mwh = float(profiles_df["load_mw"].sum() * dt_hours)
    peak_load_mw     = float(profiles_df["load_mw"].max())
    base_grid_cost   = total_demand_mwh * eco.grid_cost_mwh                  # €/yr
    base_grid_conn   = peak_load_mw * eco.grid_connection_cost_mw            # € capex

    # 3. Pull the recommended design's economics (already in € and €M).
    g = lambda key, default=0.0: float(rec.get(key, default) or 0.0)
    total_capex   = g("Total CAPEX (€M)") * 1e6
    annual_opex   = g("Total Annual OPEX (€M/yr)") * 1e6
    annual_grid   = g("Annual Grid Cost (€M/yr)") * 1e6
    rec_gc_mw     = g(CurveCols.PEAK_GC_MW)

    annual_savings   = max(0.0, base_grid_cost - annual_opex)               # vs 100%-grid opex
    avoided_grid_conn = max(0.0, (peak_load_mw - rec_gc_mw) * eco.grid_connection_cost_mw)
    payback_years    = round(total_capex / annual_savings, 1) if annual_savings > 0 else None
    irr_pct          = _simple_irr(total_capex, annual_savings, int(eco.project_lifespan_years))
    npv_savings      = -total_capex + annual_savings * eco.pv_factor        # NPV of the saving stream

    def _ssr(row):
        v = row.get(CurveCols.OP_SSR_PCT, None)
        if v is None or pd.isna(v):
            v = row.get(CurveCols.ACHIEVED_SSR_PCT, 0.0)
        return _clean(v)

    # 4. Per-point list for the frontier/comparison views.
    points = [{
        "ssr_pct":   _ssr(r),
        "gc_mw":     _clean(r.get(CurveCols.PEAK_GC_MW, 0.0)),
        "pv_mw":     _clean(r.get(CurveCols.PV_MW, 0.0)),
        "bess_mw":   _clean(r.get("Costed BESS Power (MW)", r.get(CurveCols.BESS_MW, 0.0))),
        "bess_mwh":  _clean(r.get("Costed BESS Energy (MWh)", r.get(CurveCols.BESS_MWH, 0.0))),
        "capex_m":   _clean(r.get("Total CAPEX (€M)", 0.0)),
        "opex_m_yr": _clean(r.get("Total Annual OPEX (€M/yr)", 0.0)),
        "lcoe":      _clean(r.get("LCOE (€/MWh)", 0.0)),
        "npv_cost_m":_clean(r.get("NPV Costs (€M)", 0.0)),
    } for _, r in costed.iterrows()]

    return _clean_dict({
        "objective":          req.economic_objective,
        "is_point":           is_point,
        "recommended": _clean_dict({
            "ssr_pct":   _ssr(rec),
            "scr_pct":   rec.get(CurveCols.ACHIEVED_SCR_PCT, None),
            "gc_mw":     rec_gc_mw,
            "pv_mw":     rec.get(CurveCols.PV_MW, 0.0),
            "wind_mw":   wind_mw,
            "bess_mw":   rec.get("Costed BESS Power (MW)", rec.get(CurveCols.BESS_MW, 0.0)),
            "bess_mwh":  rec.get("Costed BESS Energy (MWh)", rec.get(CurveCols.BESS_MWH, 0.0)),
            "capex_m":          total_capex / 1e6,
            "capex_pv_m":       g("CAPEX PV (€M)"),
            "capex_wind_m":     g("CAPEX Wind (€M)"),
            "capex_bess_mw_m":  g("CAPEX BESS MW (€M)"),
            "capex_bess_mwh_m": g("CAPEX BESS MWh (€M)"),
            "capex_grid_m":     g("CAPEX Grid Connection (€M)"),
            "opex_m_yr":        annual_opex / 1e6,
            "lcoe":             g("LCOE (€/MWh)"),
            "npv_cost_m":       g("NPV Costs (€M)"),
        }),
        "vs_grid_default": _clean_dict({
            "baseline_grid_conn_mw":   peak_load_mw,
            "baseline_annual_cost_m":  base_grid_cost / 1e6,
            "baseline_grid_conn_capex_m": base_grid_conn / 1e6,
            "annual_savings_m":        annual_savings / 1e6,
            "avoided_grid_conn_m":     avoided_grid_conn / 1e6,
            "payback_years":           payback_years,
            "irr_pct":                 irr_pct,
            "npv_savings_m":           npv_savings / 1e6,
        }),
        "points": points,
    })


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

# ──────────────────────────────────────────────────────────────────────────────
# Persistence layer wiring. The stateless endpoints above work WITHOUT a database;
# the DB-backed endpoints (projects / profiles / runs) further below need one.
# Imports are kept here (not at module top) so an unreachable DB never stops the
# engine endpoints from importing and serving.
# ──────────────────────────────────────────────────────────────────────────────
from contextlib import asynccontextmanager                       # noqa: E402
from fastapi import BackgroundTasks, Depends                     # noqa: E402
from sqlalchemy import select                                    # noqa: E402
from sqlalchemy.exc import IntegrityError                        # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession                  # noqa: E402

from db import models as dbm                                     # noqa: E402
from db.session import SessionLocal, engine as _db_engine, get_session  # noqa: E402
from services.profile_store import build_envelope               # noqa: E402
from services.gen_client import fetch_generation                # noqa: E402
from services.scenario_seed import seed_scenarios               # noqa: E402
from services import run_orchestrator                            # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Mirror the code scenario registry into the DB read-model. Non-fatal when the
    # DB is down so the stateless engine endpoints still serve.
    try:
        async with SessionLocal() as session:
            n = await seed_scenarios(session)
            print(f"[startup] seeded {n} scenarios into the read-model")
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] scenario seed skipped (DB unreachable?): {exc}")
    yield
    await _db_engine.dispose()


app = FastAPI(
    title="Scenario Engine API",
    description="The kitchen window over the DIP sizing optimiser. "
                "Any front end (Streamlit today, React later) calls these URLs.",
    version="1.0.0",
    lifespan=lifespan,
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


def execute_run(req: "RunRequest", df: pd.DataFrame, pv_np: float,
                dt_hours: float, wind_np: float = 0.0) -> dict:
    """Core engine call, shared by the stateless ``POST /run`` endpoint and the
    persisted (DB-backed) run orchestrator so both paths are byte-identical.

    Given a validated request, a profiles DataFrame (columns: timestamp, load_mw,
    pv_pu, wind_pu) and its native timestep, this builds the engine params +
    context, runs the scenario, and returns the JSON-safe result dict (design +
    KPIs + table + flows, plus an optional economics overlay). It performs NO I/O
    and raises no HTTPException — callers translate engine errors to their own
    transport (500 for the endpoint, a failed run row for the orchestrator).

    ``df`` is treated as owned by the caller; load scaling copies before mutating.
    """
    # Scale the REAL load shape to the combined Step-1 peak (multi-load → sizing).
    # Magnitude from the user's loads, temporal shape from the dataset — the same
    # per-unit×nameplate technique the engine uses for generation. No synthetic
    # shapes. Skipped when not supplied or when it would be a no-op.
    if req.load_peak_mw and req.load_peak_mw > 0:
        cur_peak = float(df["load_mw"].max())
        if cur_peak > 1e-9 and abs(cur_peak - req.load_peak_mw) > 1e-6:
            df = df.copy()
            df["load_mw"] = df["load_mw"] * (req.load_peak_mw / cur_peak)

    # Build the engine's PhysicalParams. Topology is authoritative from the request
    # when supplied (the front-end Step-2 selection); otherwise it falls back to the
    # scenario's own registry topology. Unknown values are ignored rather than
    # allowed to mis-constrain the LP. dt_hours is the file's native resolution so
    # SOC integrates over the real timestep (15-min data must not be solved hourly).
    spec = get_scenario(req.scenario_id)
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
    )

    # Wind (opt-in). The engine consumes ONE generation array; when a wind nameplate
    # is supplied and the dataset carries a wind profile, combine solar + wind here —
    # generation = pv_pu×solar_mw + wind_pu×wind_mw — and feed the combined series as
    # the engine's single "pv" generation. The LP/dispatch core is unchanged.
    # Solar/wind are kept separate for economics so each is costed at its own rate.
    # Area-fit sizing: an omitted nameplate defaults to the profile's stored,
    # parcel-fit capacity (pv_np / wind_np); an explicit request value still wins.
    solar_mw = req.pv_mw if req.pv_mw is not None else pv_np
    wind_mw  = float(req.wind_mw if req.wind_mw is not None else wind_np)
    gen_df, eng_pv_mw, eff_wind_mw = df, solar_mw, 0.0
    if wind_mw > 0 and "wind_pu" in df.columns and float(df["wind_pu"].max()) > 1e-9:
        combined = df["pv_pu"].to_numpy() * solar_mw + df["wind_pu"].to_numpy() * wind_mw
        peak = float(combined.max())
        if peak > 1e-9:
            gen_df = df.copy()
            gen_df["pv_pu"] = combined / peak     # normalised combined shape (0–1)
            eng_pv_mw = peak                       # nameplate s.t. pv_pu×nameplate = combined
            eff_wind_mw = wind_mw                  # wind actually contributed (and is costed)

    # Assemble the context. PV defaults to the file's nameplate when omitted.
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

    result = run_scenario(req.scenario_id, ctx)
    out = _result_to_json(result)
    out["dt_hours"] = dt_hours   # so the UI can scale energy (MW × dt = MWh)

    # Optional Phase-2 economics overlay (CAPEX/OPEX/NPV/LCOE + vs-grid savings).
    if req.with_economics:
        try:
            out["economics"] = _attach_economics(result, req, df, dt_hours,
                                                 solar_mw=solar_mw, wind_mw=eff_wind_mw)
        except Exception as exc:   # economics must never sink a valid sizing run
            out["economics"] = None
            out["economics_error"] = str(exc)
    return out


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

    # 3. Hand off to the shared engine core (identical to the persisted path).
    try:
        return execute_run(req, df, pv_np, dt_hours)
    except Exception as exc:  # surface engine errors as a clean 500, not a stack dump
        raise HTTPException(status_code=500, detail=f"Engine error: {exc}")


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
    vs_grid_default: dict = Field(default_factory=dict)
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
        if req.vs_grid_default:
            rec.update({f"vs_grid: {k}": v for k, v in req.vs_grid_default.items()})
        if rec:
            _kv_sheet(rec).to_excel(xw, sheet_name="Recommended", index=False)
        if req.points:
            pd.DataFrame(req.points).to_excel(xw, sheet_name="Frontier", index=False)
        if req.kpis:
            _kv_sheet(req.kpis).to_excel(xw, sheet_name="KPIs", index=False)
        if req.assumptions:
            _kv_sheet(req.assumptions).to_excel(xw, sheet_name="Assumptions", index=False)

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
                    initial_soc_pct=req.initial_soc_pct)

                splits = []
                pts = [p for p in req.points if (p.get("bess_mwh") or 0) >= 0][: req.max_point_sheets]
                for i, p in enumerate(pts):
                    _k, flows = verify_sizing_with_rule(
                        df, params, pv_mw=eng_pv,
                        bess_mw=float(p.get("bess_mw") or 0), bess_mwh=float(p.get("bess_mwh") or 0),
                        target_type="ssr", grid_ceiling_mw=req.site_max_grid_mw)
                    tag = (f"SSR{round(float(p.get('ssr_pct')))}" if p.get("ssr_pct") is not None
                           else f"GC{round(float(p.get('gc_mw', 0)))}")
                    hourly_flows(flows).to_excel(xw, sheet_name=f"P{i+1}_{tag}"[:31], index=False)
                    splits.append({"Point": f"P{i+1} {tag}",
                                   "BESS (MW)": p.get("bess_mw"), "BESS (MWh)": p.get("bess_mwh"),
                                   **energy_split(flows, dt_hours)})
                if splits:
                    pd.DataFrame(splits).to_excel(xw, sheet_name="Per-point energy", index=False)
                    # monthly breakdown of the recommended (last-ish / representative)
                    monthly(flows, dt_hours).to_excel(xw, sheet_name="Monthly (last point)", index=False)
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


# ══════════════════════════════════════════════════════════════════════════════
# Persistence endpoints — projects, profiles, and DB-backed async runs.
# These require Postgres (see db/config.py + schema.sql). The stateless engine
# endpoints above keep working without a database.
# ══════════════════════════════════════════════════════════════════════════════

class ProjectCreate(BaseModel):
    """Create a project (the owner of runs). Only project_name is required."""
    model_config = {"extra": "forbid"}
    project_name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    user_oid: Optional[str] = None
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    area_ha: Optional[float] = Field(default=None, gt=0)


class ProfileCreate(BaseModel):
    """Register a reusable dataset from a readable Excel path (defaults to the
    bundled 8760). Stored as one JSONB row and referenced by runs via profile_id."""
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=255)
    profile_path: Optional[str] = None


class RunLaunch(BaseModel):
    """Launch a persisted run. `request` is the same contract as POST /run;
    `profile_id` (optional) points at a stored dataset, else the bundled default."""
    model_config = {"extra": "forbid"}
    profile_id: Optional[int] = None
    request: RunRequest


def _project_out(p: "dbm.Project") -> dict:
    return {
        "project_id": p.project_id, "project_name": p.project_name,
        "description": p.description, "user_oid": str(p.user_oid) if p.user_oid else None,
        "latitude": p.latitude, "longitude": p.longitude, "area_ha": p.area_ha,
        "is_active": p.is_active,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _run_out(r: "dbm.Run") -> dict:
    return {
        "run_id": r.run_id, "project_id": r.project_id, "profile_id": r.profile_id,
        "scenario_id": r.scenario_id, "status": r.status, "message": r.message,
        "dt_hours": r.dt_hours, "result": r.result,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }


@app.post("/projects", status_code=201)
async def create_project(body: ProjectCreate, session: AsyncSession = Depends(get_session)) -> dict:
    """Create a project."""
    p = dbm.Project(
        project_name=body.project_name, description=body.description,
        user_oid=body.user_oid, latitude=body.latitude, longitude=body.longitude,
        area_ha=body.area_ha,
    )
    session.add(p)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="A project with that name already exists for this user.")
    await session.refresh(p)
    return _project_out(p)


@app.get("/projects")
async def list_projects(session: AsyncSession = Depends(get_session)) -> list[dict]:
    """List projects (newest first)."""
    rows = (await session.execute(
        select(dbm.Project).order_by(dbm.Project.created_at.desc())
    )).scalars().all()
    return [_project_out(p) for p in rows]


@app.get("/projects/{project_id}")
async def get_project(project_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    p = await session.get(dbm.Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return _project_out(p)


@app.post("/profiles", status_code=201)
async def create_profile(body: ProfileCreate, session: AsyncSession = Depends(get_session)) -> dict:
    """Register a dataset as a single JSONB row from a readable Excel path."""
    path = body.profile_path or str(_DEFAULT_PROFILE)
    if not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"Profile file not found: {path}")
    df, load_np, pv_np, dt_hours = _load_profile(path)
    envelope = build_envelope(
        df, dt_hours=dt_hours, name=body.name, source=path,
        load_nameplate_mw=load_np, pv_nameplate_mw=pv_np,
    )
    prof = dbm.Profile(**envelope)
    session.add(prof)
    await session.commit()
    await session.refresh(prof)
    return {
        "profile_id": prof.profile_id, "name": prof.name, "source": prof.source,
        "dt_hours": prof.dt_hours, "n_steps": prof.n_steps,
        "load_nameplate_mw": prof.load_nameplate_mw, "pv_nameplate_mw": prof.pv_nameplate_mw,
    }


@app.get("/profiles/{profile_id}")
async def get_profile(profile_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """Profile metadata (without the full arrays)."""
    prof = await session.get(dbm.Profile, profile_id)
    if prof is None:
        raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found")
    return {
        "profile_id": prof.profile_id, "name": prof.name, "source": prof.source,
        "dt_hours": prof.dt_hours, "n_steps": prof.n_steps,
        "load_nameplate_mw": prof.load_nameplate_mw, "pv_nameplate_mw": prof.pv_nameplate_mw,
        "created_at": prof.created_at.isoformat() if prof.created_at else None,
    }


@app.post("/projects/{project_id}/runs", status_code=202)
async def launch_run(
    project_id: int, body: RunLaunch, background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Launch a persisted async run for a project. Validates the scenario is
    runnable, records a pending run, schedules the background solve, and returns
    the run id immediately. Poll GET /runs/{id} for status + result."""
    project = await session.get(dbm.Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    # Validate the scenario exists and is runnable before recording anything.
    try:
        spec = get_scenario(body.request.scenario_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown scenario '{body.request.scenario_id}'")
    if spec.status != Status.READY:
        raise HTTPException(status_code=409, detail=f"Scenario '{spec.id}' is not runnable yet — {spec.needs}")

    if body.profile_id is not None:
        prof = await session.get(dbm.Profile, body.profile_id)
        if prof is None:
            raise HTTPException(status_code=404, detail=f"Profile {body.profile_id} not found")

    run = dbm.Run(
        project_id=project_id, profile_id=body.profile_id,
        scenario_id=body.request.scenario_id, status=dbm.RUN_PENDING,
        request=body.request.model_dump(),
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)

    background_tasks.add_task(run_orchestrator.run_optimization, run.run_id)
    return {"run_id": run.run_id, "status": run.status, "scenario_id": run.scenario_id}


@app.get("/runs/{run_id}")
async def get_run(run_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """Run status and (when completed) the full result JSON."""
    run = await session.get(dbm.Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return _run_out(run)


@app.get("/runs/{run_id}/flows")
async def get_run_flows(run_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """The per-timestep energy-flows table for a run, when the scenario produced one."""
    flows = await session.get(dbm.RunFlows, run_id)
    if flows is None:
        raise HTTPException(status_code=404, detail=f"No flows stored for run {run_id}")
    return {"run_id": run_id, "n_steps": flows.n_steps, "flows": flows.series}


# ── Site profiles: build a dataset from the generation API + a load series ─────
# The gen API (wind now, solar later) turns a coordinate + area into per-unit
# generation shapes; we pair them with a load series (bundled default for now)
# and store one reusable profiles row. The parcel-fit nameplates are saved in
# `meta` so a run's wind_mw/pv_mw default to them (area-fit sizing).

class WindParams(BaseModel):
    model_config = {"extra": "forbid"}
    turbine: Optional[str] = None
    hub_m: Optional[float] = Field(default=None, gt=0)


class SolarParams(BaseModel):
    model_config = {"extra": "forbid"}
    # Forwarded to /solar-profile; all optional (orientation auto-optimised if omitted).
    model_id: Optional[int] = Field(default=None, ge=1)   # panel spec (1–14)
    tilt_deg: Optional[float] = Field(default=None, ge=0, le=90)
    azimuth_deg: Optional[float] = Field(default=None, ge=0, le=360)  # 180 = south
    dc_ac_ratio: Optional[float] = Field(default=None, gt=0.8, le=2.0)
    system_loss: Optional[float] = Field(default=None, ge=0, lt=1)
    albedo: Optional[float] = Field(default=None, ge=0, le=1)
    irradiance_bias: Optional[float] = Field(default=None, gt=0)


class SiteProfileCreate(BaseModel):
    """Build a profile from a coordinate + area via the generation API."""
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=255)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    area_km2: float = Field(gt=0, le=2000)
    wind: Optional[WindParams] = None
    solar: Optional[SolarParams] = None
    # Optional: scale the bundled load shape to this peak (MW). None → as-is.
    load_peak_mw: Optional[float] = Field(default=None, gt=0)


class ProjectProfileCreate(BaseModel):
    """Build a site profile using the project's own coordinate + area."""
    model_config = {"extra": "forbid"}
    name: Optional[str] = None
    wind: Optional[WindParams] = None
    solar: Optional[SolarParams] = None
    load_peak_mw: Optional[float] = Field(default=None, gt=0)


async def _build_site_profile(session, name, lat, lon, area_km2,
                              wind, solar, load_peak_mw) -> "dbm.Profile":
    """Fetch generation for the site, pair it with the bundled load, and persist
    one profiles row. Generation is fetched off the event loop is not needed — the
    client is already async. Raises on wind-fetch failure (fail loud)."""
    # 1. Load series (bundled default for now — swap-in point for future demand logic).
    load_df, _load_np, _pv_np, dt_hours = _load_profile(str(_DEFAULT_PROFILE))
    load_mw = load_df["load_mw"].to_numpy().astype(float)
    if load_peak_mw and load_peak_mw > 0:
        cur = float(load_mw.max())
        if cur > 1e-9:
            load_mw = load_mw * (load_peak_mw / cur)
    n = len(load_mw)

    # 2. Generation shapes from the gen API, resampled to the load grid length.
    gen = await fetch_generation(
        lat, lon, area_km2, target_len=n,
        wind_params=(wind.model_dump(exclude_none=True) if wind else None),
        solar_params=(solar.model_dump(exclude_none=True) if solar else None),
    )

    # 3. Assemble the engine frame and store it as one JSONB row.
    frame = pd.DataFrame({
        "timestamp": load_df["timestamp"].to_numpy(),
        "load_mw": load_mw,
        "pv_pu": gen["solar_pu"],
        "wind_pu": gen["wind_pu"],
    })
    envelope = build_envelope(
        frame, dt_hours=dt_hours, name=name,
        source=f"gen_api@{lat},{lon} area={area_km2}km2",
        load_nameplate_mw=(load_peak_mw or float(load_mw.max())),
        pv_nameplate_mw=gen["solar_nameplate_mw"],   # area-fit solar nameplate
    )
    # Stash the area-fit nameplates + provenance (nameplates drive area-fit sizing).
    envelope["meta"] = {
        "wind_nameplate_mw": gen["wind_nameplate_mw"],
        "solar_nameplate_mw": gen["solar_nameplate_mw"],
        "site": {"latitude": lat, "longitude": lon, "area_km2": area_km2},
        "wind": gen["wind_meta"],
        "solar": gen["solar_meta"],
    }
    prof = dbm.Profile(**envelope)
    session.add(prof)
    await session.commit()
    await session.refresh(prof)
    return prof


def _site_profile_out(prof: "dbm.Profile") -> dict:
    meta = prof.meta or {}
    return {
        "profile_id": prof.profile_id, "name": prof.name, "source": prof.source,
        "dt_hours": prof.dt_hours, "n_steps": prof.n_steps,
        "wind_nameplate_mw": meta.get("wind_nameplate_mw"),
        "solar_nameplate_mw": meta.get("solar_nameplate_mw"),
        "wind": meta.get("wind"), "solar": meta.get("solar"),
    }


@app.post("/profiles/from-site", status_code=201)
async def create_site_profile(body: SiteProfileCreate,
                              session: AsyncSession = Depends(get_session)) -> dict:
    """Build a reusable profile from a coordinate + area via the generation API."""
    try:
        prof = await _build_site_profile(
            session, body.name, body.latitude, body.longitude, body.area_km2,
            body.wind, body.solar, body.load_peak_mw,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Generation API error: {exc}")
    return _site_profile_out(prof)


@app.post("/projects/{project_id}/profile", status_code=201)
async def create_project_profile(project_id: int, body: ProjectProfileCreate,
                                 session: AsyncSession = Depends(get_session)) -> dict:
    """Build a site profile from the project's own coordinate + area (ha → km²)."""
    project = await session.get(dbm.Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    if project.latitude is None or project.longitude is None or project.area_ha is None:
        raise HTTPException(
            status_code=422,
            detail="Project must have latitude, longitude and area_ha set to build a site profile.",
        )
    area_km2 = float(project.area_ha) / 100.0   # 1 km² = 100 ha
    name = body.name or f"{project.project_name} — site profile"
    try:
        prof = await _build_site_profile(
            session, name, project.latitude, project.longitude, area_km2,
            body.wind, body.solar, body.load_peak_mw,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Generation API error: {exc}")
    return _site_profile_out(prof)
