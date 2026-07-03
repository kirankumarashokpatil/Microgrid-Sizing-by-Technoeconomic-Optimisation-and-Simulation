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
from core.site import parcel_capacity, area_for_capacity, _SOLAR_MW_PER_HA  # noqa: E402
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
    """Overlay Phase-2 economics on a run result (Phase 1 output). Thin wrapper that
    picks the curve/point frame, then hands off to _economics_block."""
    eco = _eco_from_request(req)
    curve = result.table
    is_point = curve is None or curve.empty
    if is_point:
        curve = _point_to_curve_df(result)
    if curve is None or curve.empty or CurveCols.BESS_MWH not in curve.columns:
        return None
    return _economics_block(curve, eco, profiles_df, dt_hours, req.economic_objective,
                            solar_mw=solar_mw, wind_mw=wind_mw, is_point=is_point)


def _economics_block(curve, eco, profiles_df: pd.DataFrame, dt_hours: float,
                     objective: str, solar_mw: Optional[float] = None,
                     wind_mw: float = 0.0, is_point: bool = False) -> Optional[dict]:
    """The Phase-2 costing itself: cost every point of a sizing curve, pick the
    recommended one, and compute value vs a 100%-grid baseline. No LP re-solve —
    this is a fast overlay on an already-sized curve. All numbers come from
    core.economic_overlay; savings/IRR are stated against the grid baseline."""
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

    rec = find_optimal_point(costed, objective=objective)
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
        "objective":          objective,
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


class EconomicsRequest(BaseModel):
    """Phase-2 overlay inputs: the Phase-1 sized points + the economic assumptions.
    Re-costs the curve WITHOUT re-solving the LP, so CAPEX tweaks are instant."""
    points: list[dict] = Field(default_factory=list)   # from a prior run's economics.points
    profile_path: Optional[str] = None
    load_peak_mw: Optional[float] = None
    solar_mw: Optional[float] = None
    wind_mw: float = 0.0
    economic_objective: str = "knee"
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


@app.post("/economics")
def economics(req: EconomicsRequest) -> dict:
    """Phase 2 — overlay economics on an already-sized curve. Fast (no LP re-solve):
    the SIZE step runs the LP once, then this re-prices the curve every time the
    CAPEX assumptions change and re-picks the recommended commercial design."""
    if not req.points:
        return {"economics": None}
    rows = [{
        CurveCols.SCENARIO:         "econ",
        CurveCols.PV_MW:            float(p.get("pv_mw") or 0),
        CurveCols.WIND_MW:          float(req.wind_mw or 0),
        CurveCols.BESS_MW:          float(p.get("bess_mw") or 0),
        CurveCols.BESS_MWH:         float(p.get("bess_mwh") or 0),
        CurveCols.PEAK_GC_MW:       float(p.get("gc_mw") or 0),
        CurveCols.ACHIEVED_SSR_PCT: float(p.get("ssr_pct") or 0),
        CurveCols.OP_SSR_PCT:       float(p.get("ssr_pct") or 0),
        CurveCols.ACHIEVED_SCR_PCT: float(p.get("scr_pct") or 0),
        CurveCols.FEASIBLE:         True,
    } for p in req.points]
    curve = pd.DataFrame(rows)
    eco = _eco_from_request(req)   # duck-typed: same cost field names as RunRequest

    path = req.profile_path or str(_DEFAULT_PROFILE)
    if not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"Profile file not found: {path}")
    df, load_np, pv_np, dt_hours = _load_profile(path)
    if req.load_peak_mw and req.load_peak_mw > 0:
        cur = float(df["load_mw"].max())
        if cur > 1e-9:
            df = df.copy(); df["load_mw"] = df["load_mw"] * (req.load_peak_mw / cur)

    block = _economics_block(curve, eco, df, dt_hours, req.economic_objective,
                             solar_mw=req.solar_mw, wind_mw=req.wind_mw, is_point=len(rows) == 1)
    return {"economics": block}


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
    #     The LP/dispatch core is unchanged. Solar/wind are kept separate for
    #     economics so each is costed at its own rate.
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

    # 6. Optional Phase-2 economics overlay (CAPEX/OPEX/NPV/LCOE + vs-grid savings).
    if req.with_economics:
        try:
            out["economics"] = _attach_economics(result, req, df, dt_hours,
                                                 solar_mw=solar_mw, wind_mw=eff_wind_mw)
        except Exception as exc:   # economics must never sink a valid sizing run
            out["economics"] = None
            out["economics_error"] = str(exc)
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
    with_economics: bool = False
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
    econ = out.get("economics") if isinstance(out.get("economics"), dict) else None
    rec = (econ or {}).get("recommended") or {}
    row = {
        "label": label, "config": config,
        "feasible": out.get("feasible"),
        "grid_peak_mw": kpis.get("GCmin Peak (MW)", design.get("gc_mw")),
        "pv_mw": design.get("pv_mw"), "wind_mw": wind_mw,
        "bess_mw": design.get("bess_mw"), "bess_mwh": design.get("bess_mwh"),
        "duration_h": design.get("duration_h"),
        "capex_m": rec.get("capex_m") if isinstance(rec, dict) else None,
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
            with_economics=req.with_economics,
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
    # Cost inputs — mirror EconomicParams so the ranking matches the Phase-2 overlay.
    cost_pv_mw: float = 700_000.0
    cost_wind_mw: float = 1_300_000.0
    cost_bess_mw: float = 150_000.0
    cost_bess_mwh: float = 300_000.0
    grid_connection_cost_mw: float = 250_000.0
    grid_cost_mwh: float = 150.0
    fixed_opex_per_mwh_year: float = 8_000.0
    project_lifespan_years: float = 20.0


@app.post("/optimise-split")
def optimise_split(req: OptimiseSplitRequest) -> dict:
    """Sweep PV over [0, ρ_s·A_avail], size BESS for the SSR target at each point,
    cost each, and return the cost-optimal split + the frontier."""
    pv_ceiling = _SOLAR_MW_PER_HA * max(0.0, req.available_land_ha)
    steps = max(2, min(int(req.steps), 20))
    pv_points = [round(pv_ceiling * i / (steps - 1), 2) for i in range(steps)]

    frontier = []
    for pv in pv_points:
        try:
            out = run(RunRequest(
                scenario_id="S11_BTM_SSR_TARGET_BESS", profile_path=req.profile_path,
                load_peak_mw=req.load_peak_mw, pv_mw=pv, wind_mw=req.wind_mw,
                target_ssr_pct=req.target_ssr_pct, site_topology=req.site_topology,
            ))
        except Exception:
            continue
        if not out.get("feasible"):
            continue
        d = out.get("design") or {}
        k = out.get("kpis") or {}
        bess_mw = d.get("bess_mw", 0.0) or 0.0
        bess_mwh = d.get("bess_mwh", 0.0) or 0.0
        grid_mw = k.get("GCmin Peak (MW)", d.get("gc_mw", 0.0)) or 0.0
        grid_import = k.get("Total Grid Import (MWh)", 0.0) or 0.0

        capex = (pv * req.cost_pv_mw + req.wind_mw * req.cost_wind_mw
                 + bess_mw * req.cost_bess_mw + bess_mwh * req.cost_bess_mwh
                 + grid_mw * req.grid_connection_cost_mw)
        annual = grid_import * req.grid_cost_mwh + bess_mwh * req.fixed_opex_per_mwh_year
        lifetime = capex + req.project_lifespan_years * annual

        frontier.append({
            "pv_mw": pv, "wind_mw": req.wind_mw,
            "pv_land_ha": round(pv / _SOLAR_MW_PER_HA, 1) if _SOLAR_MW_PER_HA else None,
            "bess_mw": round(bess_mw, 1), "bess_mwh": round(bess_mwh, 1),
            "grid_mw": round(grid_mw, 2), "ssr_pct": k.get("SSR (%)"),
            "capex_m": round(capex / 1e6, 2),
            "lifetime_cost_m": round(lifetime / 1e6, 2),
        })

    best = min(frontier, key=lambda r: r["lifetime_cost_m"]) if frontier else None
    return {
        "inputs": {
            "available_land_ha": req.available_land_ha, "load_peak_mw": req.load_peak_mw,
            "target_ssr_pct": req.target_ssr_pct, "wind_mw": req.wind_mw,
        },
        "pv_ceiling_mw": round(pv_ceiling, 1),
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
                    initial_soc_pct=req.initial_soc_pct,
                    dispatch_priority=tuple(req.dispatch_priority or ()),
                    allow_grid_charge=req.allow_grid_charge)

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
