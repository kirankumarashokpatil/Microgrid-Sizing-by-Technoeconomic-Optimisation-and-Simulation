"""
Workbook I/O — the single-file Excel contract for the backend
=============================================================
This module is the ONE place that knows how the engine talks to a spreadsheet.
It gives the backend a self-contained "Excel in → Excel out" path with no
frontend and no JSON:

    one workbook  ──read_inputs──►  {inputs}  ──build_context──►  ScenarioContext
                                                                        │
                                                              core.run_scenario
                                                                        │
    same workbook  ◄──write_results──  ScenarioResult  ◄────────────────┘

Design rules (kept deliberately simple so the code stays easy to follow):

  • FIELDS below is the SINGLE source of truth for every input. The template
    writer, the reader, and the "resolved inputs" echo all derive from it — so
    there is exactly one list to edit when an input is added or renamed.
  • The engine itself (core/scenarios.py, core/sizing_engine.py, …) is NOT
    touched. This module only translates spreadsheet ⇆ engine objects, exactly
    like api/main.py does for JSON. A future frontend keeps using the API; this
    file is its offline, spreadsheet-driven twin.

The workbook layout
-------------------
  Sheet "Inputs"   — three columns: Parameter | Value | Description.
                     `Parameter` is the machine key (e.g. pv_mw); `Value` is what
                     you edit; `Description` is guidance. Blank Value ⇒ use the
                     engine default. This is the only sheet you fill in.
  Results are written back into the SAME workbook as extra sheets:
  Sheet "Run Info" — scenario chosen, feasibility, notes, profile used, timestamp.
  Sheet "Design"   — the sized hardware (PV / BESS MW·MWh / grid).
  Sheet "KPIs"     — SSR / SCR / GCmin / unmet, etc.
  Sheet "Frontier" — the curve/surface table, when the scenario produces one.
  Sheet "Flows"    — the per-timestep dispatch (when a point design is evaluated).
  Sheet "Inputs (resolved)" — every input with the value actually applied, so the
                     run is fully reproducible from the output file alone.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

import numpy as np
import pandas as pd

from core.params import PhysicalParams
from core.profile_loader import load_profiles
from core.resolver import resolve_scenario
from core.scenarios import ScenarioContext, run_scenario
from core.site import parcel_capacity

# Bundled default dataset — used when the Inputs sheet leaves `profile_path` blank.
DEFAULT_PROFILE = Path(__file__).resolve().parent.parent / "data" / "Energy Timeseries.xlsx"

# Engine topology vocabulary, and how the resolver's short signals map onto it.
_VALID_TOPOLOGIES = {"grid_connected_btm", "bess_load_only", "off_grid", "standalone_gen"}
_RESOLVER_TOPO_TO_ENGINE = {
    "btm": "grid_connected_btm",
    "backup": "bess_load_only",
    "off_grid": "off_grid",
    "standalone": "standalone_gen",
}


# ──────────────────────────────────────────────────────────────────────────────
# FIELDS — the single source of truth for every workbook input.
#   key    : machine name (the Parameter cell, and the RunRequest/context field)
#   type   : how the raw cell is coerced — float|int|bool|str|list_float|list_str
#   default: engine default (echoed into a fresh template; applied when blank)
#   group  : section header, for a readable template
#   help   : one-line guidance shown in the Description column
# ──────────────────────────────────────────────────────────────────────────────
FIELDS: list[dict] = [
    # ── Project ──
    dict(key="project_name", type="str", default="DIP Project", group="Project",
         help="Free-text name, echoed into the output."),
    dict(key="location", type="str", default="", group="Project",
         help="Site location / coordinates (informational, echoed to output)."),
    dict(key="scenario_id", type="str", default="", group="Project",
         help="Leave blank to auto-detect the scenario from the inputs below."),
    dict(key="profile_path", type="str", default="", group="Project",
         help="Path to an 8760 profiles .xlsx. Blank ⇒ bundled dataset."),

    # ── Generation & design ──
    dict(key="pv_mw", type="float", default="", group="Generation & design",
         help="Fixed PV nameplate (MW). Blank ⇒ the profile file's PV nameplate."),
    dict(key="wind_mw", type="float", default=0.0, group="Generation & design",
         help="Opt-in wind nameplate (MW). Needs a wind profile in the dataset."),
    dict(key="load_peak_mw", type="float", default="", group="Generation & design",
         help="Combined consumer peak (MW). Blank ⇒ the file's load peak. Scales the real shape."),
    dict(key="bess_mw", type="float", default=0.0, group="Generation & design",
         help="Fixed BESS power (MW), for fixed-design evaluation. 0 ⇒ let the engine size it."),
    dict(key="bess_mwh", type="float", default=0.0, group="Generation & design",
         help="Fixed BESS energy (MWh). 0 ⇒ let the engine size it."),

    # ── Objective — declared by WHICH targets/flags you set (blank ⇒ not chosen).
    #    This is how the scenario is detected: set an SSR target for an SSR run, a
    #    GC target for a grid-connection run, both for the SSR+GC co-opt, etc. Leave
    #    the others blank so they don't shadow your intent.
    dict(key="target_ssr_pct", type="float", default="", group="Objective",
         help="Self-sufficiency target (%). SET THIS for an SSR run; blank ⇒ not an SSR objective."),
    dict(key="target_gc_mw", type="float", default="", group="Objective",
         help="Grid-connection cap (MW). SET THIS for a grid-connection run; blank ⇒ not a GC objective."),
    dict(key="min_grid", type="bool", default=False, group="Objective",
         help="true ⇒ find the LOWEST achievable grid connection (GCmin), not a fixed target."),
    dict(key="size_pv", type="bool", default=False, group="Objective",
         help="true ⇒ size PV as a variable too (PV+BESS surface), instead of a fixed nameplate."),
    dict(key="verify", type="bool", default=False, group="Objective",
         help="true ⇒ operationally verify a fixed design (needs bess_mw & bess_mwh set)."),
    dict(key="target_firmness_pct", type="float", default="", group="Objective",
         help="Firmness target (%) for off-grid sizing. Blank ⇒ engine default (99) when needed."),
    dict(key="target_curtailment_pct", type="float", default="", group="Objective",
         help="Max curtailment (%) for standalone export. Blank ⇒ engine default (5) when needed."),
    dict(key="grid_ceiling_mw", type="float", default="", group="Objective",
         help="Hard grid ceiling threaded into firmness sizing. Blank ⇒ site max."),
    dict(key="deliverable_target", type="str", default="ssr", group="Objective",
         help="Which target the deliverable size must hit: ssr | gc."),
    dict(key="sizing_basis", type="str", default="lp", group="Objective",
         help="Sizing basis: lp (LP lower-bound), deliverable (causal-rule bisection), eol (deliverable + EoL gross-up)."),

    # ── Land (whole-site allocation across the SSR sweep) ──
    #    Set total_area_ha to run the land-allocation SSR sweep: each SSR point carves
    #    the site into DC / solar / wind / battery land and reports the grid connection.
    dict(key="total_area_ha", type="float", default="", group="Land",
         help="Total buildable site (ha), split across DC + solar + wind + battery. Set this to run the land-allocation SSR sweep. Blank ⇒ largest Parcels area."),
    dict(key="dc_land_m2_per_mw", type="float", default=1500.0, group="Land",
         help="Data-centre land per MW of load (m²/MW), incl. cooling, generators, substation, clearances (~1500–2000). DC land = load_MW × this ÷ 10,000 ha."),
    dict(key="battery_land_m2_per_mwh", type="float", default=50.0, group="Land",
         help="Battery land per MWh (m²/MWh), incl. PCS, cooling, NFPA-855 fire clearances (~40–60). Battery land = BESS_MWh × this ÷ 10,000 ha."),

    # ── Topology & site limits ──
    dict(key="site_topology", type="str", default="", group="Topology & site limits",
         help="grid_connected_btm | bess_load_only | off_grid | standalone_gen. Blank ⇒ auto."),
    dict(key="site_max_grid_mw", type="float", default=200.0, group="Topology & site limits",
         help="Hard grid-connection limit (MW)."),
    dict(key="site_max_bess_mw", type="float", default=500.0, group="Topology & site limits",
         help="Site cap on BESS power (MW)."),
    dict(key="site_max_bess_mwh", type="float", default=4000.0, group="Topology & site limits",
         help="Site cap on BESS energy (MWh)."),
    dict(key="export_limit_mw", type="float", default=0.0, group="Topology & site limits",
         help="Max export to grid (MW). 0 ⇒ no export."),
    dict(key="allow_grid_import", type="bool", default=True, group="Topology & site limits",
         help="Allow importing from the grid. false ⇒ islanded (grid import forced to 0)."),
    dict(key="allow_grid_export", type="bool", default=False, group="Topology & site limits",
         help="Allow exporting to the grid. true ⇒ export up to export_limit_mw (or grid cap if 0)."),

    # ── BESS physics ──
    dict(key="eff_charge", type="float", default=0.95, group="BESS physics",
         help="One-way charging efficiency (0–1)."),
    dict(key="eff_discharge", type="float", default=0.95, group="BESS physics",
         help="One-way discharging efficiency (0–1)."),
    dict(key="min_soc_pct", type="float", default=10.0, group="BESS physics",
         help="Minimum state of charge (%)."),
    dict(key="max_soc_pct", type="float", default=90.0, group="BESS physics",
         help="Maximum state of charge (%)."),
    dict(key="initial_soc_pct", type="float", default=50.0, group="BESS physics",
         help="Starting state of charge (%)."),
    dict(key="min_bess_duration_h", type="float", default=2.0, group="BESS physics",
         help="Minimum BESS duration (MWh/MW)."),
    dict(key="max_bess_duration_h", type="float", default=8.0, group="BESS physics",
         help="Maximum BESS duration (MWh/MW)."),
    dict(key="eol_capacity_retention_pct", type="float", default=80.0, group="BESS physics",
         help="Usable capacity retained at ~20yr end of life (%). Sizes are grossed up by 1/this."),

    # ── Dispatch policy ──
    dict(key="dispatch_priority", type="list_str", default="", group="Dispatch policy",
         help="Comma-separated causal merit order. Blank ⇒ engine default for the objective."),
    dict(key="allow_grid_charge", type="bool", default="", group="Dispatch policy",
         help="true/false to force grid-charging of the battery. Blank ⇒ infer from objective."),

    # ── Sweeps (curve / surface scenarios) ──
    dict(key="pv_sweep_mw", type="list_float", default="", group="Sweeps",
         help="Comma-separated PV nameplates (MW) for a PV–BESS surface."),
    dict(key="ssr_targets_pct", type="list_float", default="", group="Sweeps",
         help="Comma-separated SSR targets (%) for a curve/surface."),

    # ── Solver ──
    dict(key="solver_time_limit", type="int", default=120, group="Solver",
         help="HiGHS time limit per LP solve (seconds)."),
]

_FIELD_BY_KEY = {f["key"]: f for f in FIELDS}


# ──────────────────────────────────────────────────────────────────────────────
# Coercion — raw spreadsheet cell → typed Python value
# ──────────────────────────────────────────────────────────────────────────────

def _is_blank(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and pd.isna(v):
        return True
    return isinstance(v, str) and v.strip() == ""


def _coerce(kind: str, raw: Any) -> Any:
    """Turn a raw cell into the field's Python type. Blank ⇒ None (use default)."""
    if _is_blank(raw):
        return None
    if kind == "float":
        return float(raw)
    if kind == "int":
        return int(float(raw))
    if kind == "str":
        return str(raw).strip()
    if kind == "bool":
        s = str(raw).strip().lower()
        if s in ("true", "yes", "y", "1"):
            return True
        if s in ("false", "no", "n", "0"):
            return False
        raise ValueError(f"Expected true/false, got {raw!r}")
    if kind in ("list_float", "list_str"):
        parts = [p.strip() for p in str(raw).split(",") if p.strip()]
        return [float(p) for p in parts] if kind == "list_float" else parts
    raise ValueError(f"Unknown field kind {kind!r}")


# ──────────────────────────────────────────────────────────────────────────────
# Read — workbook "Inputs" sheet → {key: value} (only the values actually supplied)
# ──────────────────────────────────────────────────────────────────────────────

def read_inputs(source: Union[str, Path, bytes, io.BytesIO], sheet: str = "Inputs") -> dict:
    """Read the Inputs sheet into a dict of provided (non-blank) values.

    Accepts a file path or in-memory bytes (so the API can pass an upload straight
    through). Unknown Parameter rows are ignored; blank Values are skipped so the
    engine default applies. Raises ValueError with the parameter name on a bad cell.
    """
    buf = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    raw = pd.read_excel(buf, sheet_name=sheet, header=0)
    cols = {str(c).strip().lower(): c for c in raw.columns}
    if "parameter" not in cols or "value" not in cols:
        raise ValueError(
            f"'{sheet}' sheet must have 'Parameter' and 'Value' columns "
            f"(found: {list(raw.columns)})."
        )
    pcol, vcol = cols["parameter"], cols["value"]

    out: dict = {}
    for _, row in raw.iterrows():
        key = row[pcol]
        if _is_blank(key):
            continue
        key = str(key).strip()
        field = _FIELD_BY_KEY.get(key)
        if field is None:
            continue  # ignore comments / unknown rows
        try:
            val = _coerce(field["type"], row[vcol])
        except (ValueError, TypeError) as e:
            raise ValueError(f"Input '{key}': {e}") from e
        if val is not None:
            out[key] = val
    return out


def _effective(inputs: dict) -> dict:
    """Overlay the supplied inputs onto the field defaults → the values actually used.
    Blank-defaulted optional fields (e.g. pv_mw) stay None so downstream logic can
    fall back to the profile file."""
    eff: dict = {}
    for f in FIELDS:
        default = f["default"]
        eff[f["key"]] = None if default == "" else default
    eff.update(inputs)
    return eff


# ──────────────────────────────────────────────────────────────────────────────
# Land parcels — the 'Parcels' sheet. Area (+ location) → max installable MW.
# This is the land-first entry point: you design from the land you have, and the
# engine derives the nameplate caps via core/site.py (≈0.9 MWp/ha solar, ≈0.26
# MW/ha wind). Columns: tech | area_ha | lat | lon | density_mw_per_ha (optional).
# ──────────────────────────────────────────────────────────────────────────────
PARCEL_COLUMNS = ["tech", "area_ha", "lat", "lon", "density_mw_per_ha"]


def _sheet_names(path: Union[str, Path]) -> set:
    return set(pd.ExcelFile(path).sheet_names)


def read_parcels(source: Union[str, Path], sheet: str = "Parcels") -> dict:
    """Read the optional 'Parcels' sheet → {tech: {area_ha, lat, lon, max_mw, cf}}.
    Returns {} when the sheet is absent. `max_mw` is derived from the area (an
    explicit density_mw_per_ha overrides the site.py rule of thumb)."""
    if sheet not in _sheet_names(source):
        return {}
    raw = pd.read_excel(source, sheet_name=sheet)
    cols = {str(c).strip().lower(): c for c in raw.columns}
    if "tech" not in cols or "area_ha" not in cols:
        raise ValueError(f"'{sheet}' sheet needs at least 'tech' and 'area_ha' columns.")

    out: dict = {}
    for _, row in raw.iterrows():
        tech = str(row[cols["tech"]]).strip().lower()
        if tech not in ("solar", "wind") or _is_blank(row[cols["area_ha"]]):
            continue
        area = float(row[cols["area_ha"]])
        lat = float(row[cols["lat"]]) if "lat" in cols and not _is_blank(row[cols["lat"]]) else 51.96
        lon = float(row[cols["lon"]]) if "lon" in cols and not _is_blank(row[cols["lon"]]) else 1.35
        density = (float(row[cols["density_mw_per_ha"]])
                   if "density_mw_per_ha" in cols and not _is_blank(row[cols["density_mw_per_ha"]])
                   else None)
        cap = parcel_capacity(area, lat, lon)          # authoritative land calc
        max_mw = area * density if density else (cap["max_solar_mw"] if tech == "solar" else cap["max_wind_mw"])
        out[tech] = {"area_ha": area, "lat": lat, "lon": lon,
                     "max_mw": round(max_mw, 3),
                     "solar_cf_pct": cap["solar_cf_pct"] if tech == "solar" else None}
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Consumers — the 'Loads' sheet. Rows of {name, load_type, peak_mw, baseline_mw};
# the combined peak scales the load shape (the engine sizes against ONE series).
# ──────────────────────────────────────────────────────────────────────────────
LOAD_COLUMNS = ["name", "load_type", "peak_mw", "baseline_mw"]


def read_loads(source: Union[str, Path], sheet: str = "Loads") -> tuple[list, float]:
    """Read the optional 'Loads' sheet → (rows, combined_peak_mw). Returns ([], 0.0)
    when absent. baseline_mw is captured for reference; the combined peak is what
    scales the demand shape."""
    if sheet not in _sheet_names(source):
        return [], 0.0
    raw = pd.read_excel(source, sheet_name=sheet)
    cols = {str(c).strip().lower(): c for c in raw.columns}
    if "peak_mw" not in cols:
        raise ValueError(f"'{sheet}' sheet needs at least a 'peak_mw' column.")

    rows, total = [], 0.0
    for _, r in raw.iterrows():
        if _is_blank(r[cols["peak_mw"]]):
            continue
        peak = float(r[cols["peak_mw"]])
        rows.append({
            "name": str(r[cols["name"]]).strip() if "name" in cols and not _is_blank(r[cols["name"]]) else "Load",
            "load_type": str(r[cols["load_type"]]).strip() if "load_type" in cols and not _is_blank(r[cols["load_type"]]) else "generic",
            "peak_mw": peak,
            "baseline_mw": float(r[cols["baseline_mw"]]) if "baseline_mw" in cols and not _is_blank(r[cols["baseline_mw"]]) else 0.0,
        })
        total += peak
    return rows, round(total, 3)


# ──────────────────────────────────────────────────────────────────────────────
# Profiles — the 'Energy Timeseries' sheet, read flexibly. Generation may be given
# as absolute MW (solar_power_mw / wind_power_mw) OR as per-unit shape (solar_pu /
# wind_pu ∈ [0,1]); load as 'Data center MW' / load_mw (absolute) or load_pu.
# Per-unit mode decouples the SHAPE from the nameplate, so the nameplate can come
# from the land (Parcels) or a sweep — the land-first way.
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class ProfileBundle:
    df: pd.DataFrame                 # timestamp, load_mw, pv_pu, wind_pu
    load_np: Optional[float]         # load nameplate hint (data peak), None if load_pu
    solar_np: Optional[float]        # solar nameplate hint (data peak), None if solar_pu
    wind_np: Optional[float]
    dt_hours: float
    solar_mode: str                  # "absolute" | "pu" | "none"
    wind_mode: str
    load_mode: str


def _gen_shape(raw: pd.DataFrame, cols: dict, pu_key: str, abs_key: str):
    """Return (pu_array 0–1, nameplate_hint or None, mode) for a generation column."""
    if pu_key in cols:
        s = pd.to_numeric(raw[cols[pu_key]], errors="coerce").fillna(0.0).clip(0.0, 1.0)
        return s.to_numpy(), None, "pu"
    if abs_key in cols:
        s = pd.to_numeric(raw[cols[abs_key]], errors="coerce").fillna(0.0).clip(lower=0.0)
        peak = float(s.max())
        pu = (s / peak).clip(0.0, 1.0) if peak > 1e-9 else s * 0.0
        return pu.to_numpy(), (peak if peak > 1e-9 else None), "absolute"
    return np.zeros(len(raw)), None, "none"


def _load_shape(raw: pd.DataFrame, cols: dict):
    """Return (load_series, nameplate_hint or None, mode) for the demand column."""
    for k in ("data center mw", "load_mw"):
        if k in cols:
            s = pd.to_numeric(raw[cols[k]], errors="coerce").fillna(0.0).clip(lower=0.0)
            return s.to_numpy(), float(np.ceil(s.max())), "absolute"
    if "load_pu" in cols:
        s = pd.to_numeric(raw[cols["load_pu"]], errors="coerce").fillna(0.0).clip(0.0, 1.0)
        return s.to_numpy(), None, "pu"
    raise ValueError(
        "'Energy Timeseries' needs a demand column: 'Data center MW' or 'load_mw' "
        "(absolute) or 'load_pu' (0–1, scaled by the Loads/ load_peak_mw)."
    )


def read_profiles(source: Union[str, Path]) -> ProfileBundle:
    """Load a profiles workbook (absolute or per-unit) into a ProfileBundle.
    Dispatches on the sheet name so the pipeline is format-agnostic:
      • 'Energy Timeseries'    → named columns (absolute MW and/or per-unit).
      • 'datacenter_load_8760' → the legacy positional layout (load_profiles).
    """
    sheets = _sheet_names(source)
    if "Energy Timeseries" in sheets:
        raw = pd.read_excel(source, sheet_name="Energy Timeseries")
        cols = {str(c).strip().lower(): c for c in raw.columns}
        if "date_time" not in cols:
            raise ValueError("'Energy Timeseries' needs a 'date_time' column.")
        solar_pu, solar_np, solar_mode = _gen_shape(raw, cols, "solar_pu", "solar_power_mw")
        wind_pu, wind_np, wind_mode = _gen_shape(raw, cols, "wind_pu", "wind_power_mw")
        load, load_np, load_mode = _load_shape(raw, cols)
        df = pd.DataFrame({
            "timestamp": pd.to_datetime(raw[cols["date_time"]], errors="coerce"),
            "load_mw": load, "pv_pu": solar_pu, "wind_pu": wind_pu,
        }).dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        return ProfileBundle(df, load_np, solar_np, wind_np, _native_dt_hours(df),
                             solar_mode, wind_mode, load_mode)

    if "datacenter_load_8760" in sheets:
        df, load_np, pv_np = load_profiles(xlsx_path=Path(source))
        if "wind_pu" not in df.columns:
            df = df.assign(wind_pu=0.0)
        return ProfileBundle(df[["timestamp", "load_mw", "pv_pu", "wind_pu"]],
                             load_np, pv_np, None, _native_dt_hours(df),
                             "absolute", "none", "absolute")

    raise ValueError(
        f"Unrecognised profile format. Expected a sheet named 'Energy Timeseries' "
        f"or 'datacenter_load_8760'; found {sorted(sheets)}."
    )


def _apply_grid_flags(inputs: dict) -> None:
    """Translate the allow_grid_import / allow_grid_export booleans onto the numeric
    grid limits the engine understands (mutates `inputs` in place)."""
    eff = _effective(inputs)
    if not eff["allow_grid_import"]:
        inputs["site_max_grid_mw"] = 0.0            # islanded — no grid import
    if not eff["allow_grid_export"]:
        inputs["export_limit_mw"] = 0.0             # no export
    elif not eff["export_limit_mw"] or eff["export_limit_mw"] <= 0:
        inputs["export_limit_mw"] = eff["site_max_grid_mw"]  # export up to the grid cap


# ──────────────────────────────────────────────────────────────────────────────
# Build — {inputs} + profiles → (scenario_id, ScenarioContext, meta)
# Mirrors the assembly in api/main.py `/run` so the spreadsheet and the API agree.
# ──────────────────────────────────────────────────────────────────────────────

def build_context(
    inputs: dict,
    df: pd.DataFrame,
    load_np: float,
    pv_np: float,
    dt_hours: float,
) -> tuple[str, ScenarioContext, dict]:
    """Translate supplied inputs + a loaded profile into an engine-ready context.

    Returns (scenario_id, context, meta) where meta records the choices made
    (effective PV/wind/topology and the resolver's reason) for the Run-Info sheet.
    """
    eff = _effective(inputs)

    # Effective solar nameplate: explicit, else the file's PV nameplate.
    solar_mw = eff["pv_mw"] if eff["pv_mw"] is not None else pv_np
    wind_mw = float(eff["wind_mw"] or 0.0)

    # Scale the real load SHAPE to the requested combined peak (magnitude from the
    # user, shape from the data) — identical to the /run technique, no synthetic shapes.
    if eff["load_peak_mw"] and eff["load_peak_mw"] > 0:
        cur_peak = float(df["load_mw"].max())
        if cur_peak > 1e-9 and abs(cur_peak - eff["load_peak_mw"]) > 1e-6:
            df = df.copy()
            df["load_mw"] = df["load_mw"] * (eff["load_peak_mw"] / cur_peak)

    # Wind is opt-in: fold solar+wind into the engine's single generation array.
    gen_df, eng_pv_mw = df, solar_mw
    if wind_mw > 0 and "wind_pu" in df.columns and float(df["wind_pu"].max()) > 1e-9:
        combined = df["pv_pu"].to_numpy() * solar_mw + df["wind_pu"].to_numpy() * wind_mw
        peak = float(combined.max())
        if peak > 1e-9:
            gen_df = df.copy()
            gen_df["pv_pu"] = combined / peak
            eng_pv_mw = peak

    # Decide the scenario purely from intent. The rule the resolver was built on:
    # a target/flag counts ONLY when it is set (blank ⇒ None ⇒ not chosen), so the
    # objective you fill in is the objective you get — no target silently shadows
    # another. size_pv presents PV as a profile to SIZE (→ PV+BESS surface);
    # min_grid asks for the lowest grid connection; verify checks a fixed design.
    size_pv = bool(eff["size_pv"])
    resolver_inputs = {
        "pv_mw": None if size_pv else eng_pv_mw,   # size_pv ⇒ no fixed nameplate
        "pv_unit_profile": size_pv,
        "bess_mw": eff["bess_mw"], "bess_mwh": eff["bess_mwh"],
        "off_grid": eff["site_topology"] == "off_grid",
        "standalone": eff["site_topology"] == "standalone_gen",
        "target_ssr_pct": eff["target_ssr_pct"],
        "target_gc_mw": eff["target_gc_mw"],
        "target_firmness_pct": eff["target_firmness_pct"],
        "target_curtailment_pct": eff["target_curtailment_pct"],
        "min_grid": bool(eff["min_grid"]),
        "verify": bool(eff["verify"]),
        "pv_sweep_mw": eff["pv_sweep_mw"] or [],
        "ssr_targets_pct": eff["ssr_targets_pct"] or [],
    }
    resolution = resolve_scenario(resolver_inputs)
    scenario_id = eff["scenario_id"] or resolution.fallback_id or resolution.scenario_id

    # Topology: explicit if valid, else map from the resolver's detected signal.
    if eff["site_topology"] in _VALID_TOPOLOGIES:
        topology = eff["site_topology"]
    else:
        topology = _RESOLVER_TOPO_TO_ENGINE.get(resolution.signals.get("topology"), "grid_connected_btm")

    params = PhysicalParams(
        dt_hours=dt_hours,
        eff_charge=eff["eff_charge"], eff_discharge=eff["eff_discharge"],
        min_soc_pct=eff["min_soc_pct"], max_soc_pct=eff["max_soc_pct"],
        initial_soc_pct=eff["initial_soc_pct"],
        min_bess_duration_h=eff["min_bess_duration_h"],
        max_bess_duration_h=eff["max_bess_duration_h"],
        site_max_bess_mw=eff["site_max_bess_mw"], site_max_bess_mwh=eff["site_max_bess_mwh"],
        site_max_grid_mw=eff["site_max_grid_mw"],
        export_limit_mw=eff["export_limit_mw"],
        eol_capacity_retention_pct=eff["eol_capacity_retention_pct"],
        site_topology=topology,
        dispatch_priority=tuple(eff["dispatch_priority"] or ()),
        allow_grid_charge=eff["allow_grid_charge"],
    )

    # For the RUN itself, a scenario that needs a target still needs a concrete
    # number — fall back to the engine defaults (60 / 99 / 5) when the cell is blank.
    ctx = ScenarioContext(
        profiles_df=gen_df, params=params, pv_mw=eng_pv_mw,
        target_ssr_pct=eff["target_ssr_pct"] if eff["target_ssr_pct"] is not None else 60.0,
        target_gc_mw=eff["target_gc_mw"],
        target_firmness_pct=eff["target_firmness_pct"] if eff["target_firmness_pct"] is not None else 99.0,
        target_curtailment_pct=eff["target_curtailment_pct"] if eff["target_curtailment_pct"] is not None else 5.0,
        bess_mw=eff["bess_mw"], bess_mwh=eff["bess_mwh"],
        grid_ceiling_mw=eff["grid_ceiling_mw"],
        deliverable_target=eff["deliverable_target"],
        sizing_basis=eff["sizing_basis"],
        pv_sweep_mw=tuple(eff["pv_sweep_mw"] or ()),
        ssr_targets_pct=tuple(eff["ssr_targets_pct"] or ()),
        solver_time_limit=eff["solver_time_limit"],
    )

    meta = {
        "scenario_reason": resolution.reason,
        "scenario_auto_detected": not bool(eff["scenario_id"]),
        "effective_pv_mw": eng_pv_mw,
        "effective_wind_mw": wind_mw,
        "topology": topology,
        "dt_hours": dt_hours,
        "load_peak_mw": float(gen_df["load_mw"].max()),
    }
    return scenario_id, ctx, meta


# ──────────────────────────────────────────────────────────────────────────────
# Run — the whole spreadsheet→engine→spreadsheet pipeline in one call
# ──────────────────────────────────────────────────────────────────────────────

def run_workbook(
    in_path: Union[str, Path],
    out_path: Union[str, Path, None] = None,
    *,
    profiles_override: Union[str, Path, None] = None,
) -> Path:
    """Read a project workbook, run the engine, write results back to `out_path`
    (defaults to `<in>_out.xlsx`). Returns the output path."""
    in_path = Path(in_path)
    out_path = Path(out_path) if out_path else in_path.with_name(in_path.stem + "_out.xlsx")

    inputs = read_inputs(in_path)
    parcels = read_parcels(in_path)                 # land (optional)
    loads, loads_peak = read_loads(in_path)         # consumers (optional)

    # Profiles source: explicit override / profile_path wins; otherwise if the input
    # workbook itself carries a profiles sheet use it (single-file mode), else the
    # bundled dataset.
    profile_src = profiles_override or inputs.get("profile_path")
    if not profile_src:
        embedded = {"Energy Timeseries", "datacenter_load_8760"} & _sheet_names(in_path)
        profile_src = in_path if embedded else DEFAULT_PROFILE
    profile_path = Path(profile_src)
    if not profile_path.exists():
        raise FileNotFoundError(f"Profile file not found: {profile_path}")
    prof = read_profiles(profile_path)

    # ── Land-allocation SSR sweep: when a total site area is given (or Parcels
    #    describe the land), each SSR point carves the site into DC / solar / wind /
    #    battery and reports the grid connection (core/land_split.py).
    _total_area = inputs.get("total_area_ha") or max(
        [p["area_ha"] for p in parcels.values() if p.get("area_ha")] or [0.0])
    if _total_area and _total_area > 0:
        return _run_land_split(out_path, inputs, parcels, loads, loads_peak, prof,
                               total_area_ha=float(_total_area), profile_used=str(profile_path))

    # ── Land-first derivation: parcels set the nameplates when not given explicitly.
    if parcels.get("solar") and inputs.get("pv_mw") is None:
        inputs["pv_mw"] = parcels["solar"]["max_mw"]
    if parcels.get("wind") and not inputs.get("wind_mw") and prof.wind_mode != "none":
        inputs["wind_mw"] = parcels["wind"]["max_mw"]

    # ── Multiple consumers: their combined peak scales the demand shape.
    if loads and inputs.get("load_peak_mw") is None:
        inputs["load_peak_mw"] = loads_peak

    # ── PV-sizing (size_pv): a PV+BESS surface needs a PV sweep and target list.
    #    Derive them from the land you have when not supplied — sweep PV up to the
    #    parcel's max, over a spread of SSR targets (unless a GC target is set).
    if bool(inputs.get("size_pv")):
        max_pv = parcels.get("solar", {}).get("max_mw") or inputs.get("pv_mw") or prof.solar_np or 0.0
        if max_pv and not inputs.get("pv_sweep_mw"):
            inputs["pv_sweep_mw"] = [round(max_pv * f, 1) for f in (0.4, 0.6, 0.8, 1.0)]
        if not inputs.get("ssr_targets_pct") and inputs.get("target_gc_mw") is None:
            inputs["ssr_targets_pct"] = [50.0, 60.0, 70.0, 80.0]

    # ── Grid flags → the numeric limits the engine understands.
    _apply_grid_flags(inputs)

    # ── Nameplate fallback for the profile: data peak, else land, else 0.
    pv_np = prof.solar_np if prof.solar_np is not None else parcels.get("solar", {}).get("max_mw", 0.0)
    if prof.solar_mode == "pu" and not (inputs.get("pv_mw") or pv_np):
        raise ValueError("solar_pu supplied but no nameplate — set pv_mw or add a solar Parcel.")
    if prof.load_mode == "pu" and not inputs.get("load_peak_mw"):
        raise ValueError("load_pu supplied but no peak — set load_peak_mw or add a Loads sheet.")

    scenario_id, ctx, meta = build_context(inputs, prof.df, prof.load_np or 0.0, pv_np, prof.dt_hours)

    # Enrich meta so the Run-Info / echo sheets can show the land-first choices.
    e = _effective(inputs)
    meta.update({
        "loads_peak_mw": loads_peak,
        "solar_mode": prof.solar_mode, "wind_mode": prof.wind_mode, "load_mode": prof.load_mode,
        "allow_grid_import": e["allow_grid_import"], "allow_grid_export": e["allow_grid_export"],
        "grid_import_limit_mw": e["site_max_grid_mw"], "grid_export_limit_mw": e["export_limit_mw"],
    })

    result = run_scenario(scenario_id, ctx)
    write_results(out_path, inputs, result, meta, profile_used=str(profile_path),
                  parcels=parcels, loads=loads)
    return out_path


def _native_dt_hours(df: pd.DataFrame) -> float:
    if len(df) < 3:
        return 1.0
    return round(df["timestamp"].diff().dropna().median().total_seconds() / 3600.0, 6)


def _parcel_density(parcel: Optional[dict], default: float) -> float:
    """MW/ha implied by a parcel (max_mw ÷ area_ha), else the site default."""
    if parcel and parcel.get("area_ha") and parcel.get("max_mw"):
        return round(parcel["max_mw"] / parcel["area_ha"], 4)
    return default


def _run_land_split(out_path, inputs, parcels, loads, loads_peak, prof, *, total_area_ha, profile_used="") -> Path:
    """Land-allocation SSR sweep: for each SSR target, carve the whole site across
    DC / solar / wind / battery and report the grid connection — one integrated
    Frontier ('SSR Sweep' sheet). Uses the RAW (unfolded) solar/wind profiles so
    each is an independent LP variable."""
    from core.land_split import (land_allocation_sweep, grid_battery_tradeoff, solve_land_split,
                                 SOLAR_MW_PER_HA, WIND_MW_PER_HA, BATTERY_MWH_PER_HA, DC_M2_PER_MW)
    eff = _effective(inputs)

    # Scale the load shape to the DC's peak (Loads sheet or load_peak_mw).
    df = prof.df.copy()
    peak = eff["load_peak_mw"] or loads_peak or None
    if peak and float(df["load_mw"].max()) > 1e-9:
        df["load_mw"] = df["load_mw"] * (float(peak) / float(df["load_mw"].max()))
    load_peak = float(df["load_mw"].max())

    # Data-centre land carved out first, dynamic with the load (m²/MW → ha).
    dc_m2_per_mw = eff["dc_land_m2_per_mw"] or DC_M2_PER_MW
    dc_ha = round(load_peak * dc_m2_per_mw / 10000.0, 2)
    # Battery land per MWh (m²/MWh) → the engine's MWh/ha density.
    batt_m2_per_mwh = eff["battery_land_m2_per_mwh"] or (10000.0 / BATTERY_MWH_PER_HA)
    batt_density = 10000.0 / batt_m2_per_mwh if batt_m2_per_mwh > 0 else BATTERY_MWH_PER_HA
    solar_rho = _parcel_density(parcels.get("solar"), SOLAR_MW_PER_HA)
    wind_rho = _parcel_density(parcels.get("wind"), WIND_MW_PER_HA)
    ssr_targets = eff["ssr_targets_pct"] or [50.0, 60.0, 70.0, 80.0, 90.0]

    params = PhysicalParams(
        dt_hours=prof.dt_hours,
        eff_charge=eff["eff_charge"], eff_discharge=eff["eff_discharge"],
        min_soc_pct=eff["min_soc_pct"], max_soc_pct=eff["max_soc_pct"],
        initial_soc_pct=eff["initial_soc_pct"],
        min_bess_duration_h=eff["min_bess_duration_h"], max_bess_duration_h=eff["max_bess_duration_h"],
        site_max_bess_mw=eff["site_max_bess_mw"], site_max_bess_mwh=eff["site_max_bess_mwh"],
        site_max_grid_mw=eff["site_max_grid_mw"],
        site_topology="grid_connected_btm",
    )
    land_kw = dict(dc_ha=dc_ha, battery_mwh_per_ha=batt_density,
                   solar_density_mw_per_ha=solar_rho, wind_density_mw_per_ha=wind_rho,
                   time_limit=eff["solver_time_limit"])

    # 1) SSR sweep + per-point hourly flows (the "why" behind each design).
    table, point_flows = land_allocation_sweep(df, params, total_area_ha=total_area_ha,
                                               ssr_targets_pct=tuple(ssr_targets),
                                               return_flows=True, **land_kw)

    # 2) Recommended design — the lowest grid connection this site+battery can reach
    #    (least battery that gets there) — with its own hourly flows.
    best = solve_land_split(df, params, total_area_ha=total_area_ha, ssr_target_pct=0.0,
                            objective="min_grid", return_flows=True, **land_kw)
    best_flows = best.pop("flows", None)
    if best_flows is not None:
        point_flows = {"Recommended": best_flows, **point_flows}

    # 3) Grid ↔ battery trade-off — how much battery buys how much grid reduction,
    #    from 0 up to the battery the recommended design uses.
    max_batt = best.get("bess_mwh", 0.0) if best.get("feasible") else eff["site_max_bess_mwh"]
    steps = sorted({round(max_batt * f, 1) for f in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)}) \
        if (max_batt and max_batt > 1e-6) else [0.0]
    tradeoff = grid_battery_tradeoff(df, params, total_area_ha=total_area_ha,
                                     battery_mwh_steps=steps, **land_kw)

    meta = {
        "total_area_ha": total_area_ha, "dc_ha": dc_ha, "dc_m2_per_mw": dc_m2_per_mw,
        "battery_m2_per_mwh": round(batt_m2_per_mwh, 1), "avail_ha": round(total_area_ha - dc_ha, 1),
        "solar_density_mw_per_ha": solar_rho, "wind_density_mw_per_ha": wind_rho,
        "load_peak_mw": load_peak, "dt_hours": prof.dt_hours,
    }
    _write_land_split(out_path, inputs, table, best, tradeoff, point_flows, meta, parcels, loads,
                      profile_used=profile_used)
    return Path(out_path)


def _write_land_split(out_path, inputs, table, best, tradeoff, point_flows, meta, parcels, loads,
                      *, profile_used="") -> None:
    """Write the integrated land workbook: Inputs · Run Info · Recommended Design ·
    SSR Sweep (+charts) · Grid vs Battery (+charts) · per-point Flows · Parcels · Loads."""
    out_path = Path(out_path)
    eff = _effective(inputs)
    has_rows = table is not None and not table.empty
    n_feasible = int(table["feasible"].sum()) if (has_rows and "feasible" in table) else 0
    _chart_sheets = ("SSR Sweep", "Grid vs Battery")
    _flow_sheets = set()

    with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
        _input_template_frame(values=eff).to_excel(xw, sheet_name="Inputs", index=False)
        info = {
            "Project": eff.get("project_name") or "DIP Project",
            "Location": eff.get("location") or "",
            "Analysis": "Land-allocation SSR sweep (DC + solar + wind + battery)",
            "Total site (ha)": meta.get("total_area_ha"),
            "Data-centre land (ha)": f"{meta.get('dc_ha')}  (= {round(meta.get('load_peak_mw', 0), 1)} MW × {meta.get('dc_m2_per_mw')} m²/MW)",
            "Land for gen + storage (ha)": meta.get("avail_ha"),
            "Solar density (MW/ha)": meta.get("solar_density_mw_per_ha"),
            "Wind density (MW/ha)": meta.get("wind_density_mw_per_ha"),
            "Battery land (m²/MWh)": meta.get("battery_m2_per_mwh"),
            "DC load peak (MW)": round(meta.get("load_peak_mw", 0.0), 3),
            "SSR points solved": n_feasible,
            "Profile used": profile_used,
            "Generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        _kv({k: _text(v) for k, v in info.items()}).to_excel(xw, sheet_name="Run Info", index=False)

        # Headline: the recommended (lowest achievable grid connection) design.
        if best is not None and best.get("feasible"):
            rec = {
                "Lowest grid connection (MW)": best["gcmin_mw"],
                "Achieved SSR (%)": best["achieved_ssr_pct"],
                "Data-centre land (ha)": best["dc_ha"],
                "Solar (ha)": best["solar_ha"], "Solar (MW)": best["solar_mw"],
                "Wind (ha)": best["wind_ha"], "Wind (MW)": best["wind_mw"],
                "Battery (ha)": best["battery_ha"], "Battery (MW)": best["bess_mw"], "Battery (MWh)": best["bess_mwh"],
                "Land used (ha)": best["land_used_ha"], "Land spare (ha)": best["land_spare_ha"],
                "Curtailment (%)": best["curtailment_pct"],
            }
            _kv({k: _round(v) for k, v in rec.items()}).to_excel(xw, sheet_name="Recommended Design", index=False)

        if has_rows:
            table.to_excel(xw, sheet_name="SSR Sweep", index=False)
            _add_land_split_charts(xw.sheets["SSR Sweep"], len(table))

        if tradeoff is not None and not tradeoff.empty:
            tradeoff.to_excel(xw, sheet_name="Grid vs Battery", index=False)
            _add_tradeoff_charts(xw.sheets["Grid vs Battery"], len(tradeoff))

        # Per-point hourly dispatch — the timeseries "why" behind each design.
        for label, fdf in (point_flows or {}).items():
            if fdf is None or fdf.empty:
                continue
            sheet = f"Flows {label}"[:31]
            fdf.to_excel(xw, sheet_name=sheet, index=False)
            _flow_sheets.add(sheet)

        if parcels:
            pd.DataFrame([
                {"tech": t, "area_ha": p["area_ha"], "lat": p["lat"], "lon": p["lon"],
                 "max_mw": p["max_mw"], "solar_cf_pct": p["solar_cf_pct"]}
                for t, p in parcels.items()
            ]).to_excel(xw, sheet_name="Parcels", index=False)
        if loads:
            pd.DataFrame(loads).to_excel(xw, sheet_name="Loads", index=False)
        for ws in xw.book.worksheets:
            if ws.title in _flow_sheets:
                ws.freeze_panes = "B2"          # header + timestamp; skip slow autofit on 8760 rows
            else:
                _autofit(ws, freeze=("B2" if ws.title in _chart_sheets else "A2"))


def _add_tradeoff_charts(ws, n_rows: int) -> None:
    """Grid↔battery trade-off charts: grid connection vs battery (scatter/line) and the
    four-way land split by battery (stacked). Columns match grid_battery_tradeoff()."""
    if n_rows < 1:
        return
    from openpyxl.chart import BarChart, ScatterChart, Reference, Series
    from openpyxl.chart.shapes import GraphicalProperties
    from openpyxl.chart.marker import Marker
    from openpyxl.drawing.line import LineProperties

    DC, SOLAR, WIND, BATT, GRID = "8894A0", "EDA100", "2A78D6", "1F8F1F", "4A3AA7"
    r1 = n_rows + 1
    # cols: 1 battery_cap · 3 gcmin · 6 dc_ha · 7 solar_ha · 8 wind_ha · 9 battery_ha

    def paint(series, hexc, line=False):
        gp = GraphicalProperties()
        if line:
            gp.line = LineProperties(solidFill=hexc, w=28000)
        else:
            gp.solidFill = hexc
        series.graphicalProperties = gp

    sc = ScatterChart(); sc.title = "Grid connection vs battery"
    sc.x_axis.title = "Battery (MWh)"; sc.y_axis.title = "Grid connection (MW)"
    sc.height, sc.width, sc.legend = 8, 15, None
    s = Series(Reference(ws, min_col=3, min_row=1, max_row=r1),
               Reference(ws, min_col=1, min_row=2, max_row=r1), title_from_data=True)
    paint(s, GRID, line=True); s.marker = Marker(symbol="circle", size=6)
    sc.series.append(s); ws.add_chart(sc, "M2")

    bar = BarChart(); bar.type = "col"; bar.grouping = "stacked"; bar.overlap = 100
    bar.title = "Land split by battery"; bar.y_axis.title = "hectares"; bar.x_axis.title = "Battery cap (MWh)"
    bar.height, bar.width = 8, 15
    cats = Reference(ws, min_col=1, min_row=2, max_row=r1)
    for col in (6, 7, 8, 9):                        # dc_ha, solar_ha, wind_ha, battery_ha
        bar.add_data(Reference(ws, min_col=col, min_row=1, max_row=r1), titles_from_data=True)
    bar.set_categories(cats)
    for i, c in enumerate((DC, SOLAR, WIND, BATT)):
        paint(bar.series[i], c)
    ws.add_chart(bar, "M20")


def _add_land_split_charts(ws, n_rows: int) -> None:
    """Embed native Excel charts on the SSR Sweep sheet: the four-way land allocation
    (DC + solar + wind + battery, stacked), the grid connection, and battery size —
    all across the SSR sweep. Column order matches land_allocation_sweep()."""
    if n_rows < 1:
        return
    from openpyxl.chart import BarChart, LineChart, Reference
    from openpyxl.chart.shapes import GraphicalProperties
    from openpyxl.drawing.line import LineProperties

    DC, SOLAR, WIND, BATT, GRID = "8894A0", "EDA100", "2A78D6", "1F8F1F", "4A3AA7"
    r1 = n_rows + 1
    # SSR Sweep cols (1-based): 1 ssr · 4 gcmin · 5 dc_ha · 6 solar_ha · 8 wind_ha ·
    #                           10 battery_ha · 12 bess_mwh
    cats = Reference(ws, min_col=1, min_row=2, max_row=r1)

    def paint(series, hexc, line=False):
        gp = GraphicalProperties()
        if line:
            gp.line = LineProperties(solidFill=hexc, w=28000)
        else:
            gp.solidFill = hexc
        series.graphicalProperties = gp

    # 1) four-way land allocation (stacked ha) — DC / solar / wind / battery
    land = BarChart(); land.type = "col"; land.grouping = "stacked"; land.overlap = 100
    land.title = "Land allocation by self-sufficiency"; land.y_axis.title = "hectares"; land.x_axis.title = "SSR %"
    land.height, land.width = 8, 16
    for col in (5, 6, 8, 10):                       # dc_ha, solar_ha, wind_ha, battery_ha
        land.add_data(Reference(ws, min_col=col, min_row=1, max_row=r1), titles_from_data=True)
    land.set_categories(cats)
    for i, c in enumerate((DC, SOLAR, WIND, BATT)):
        paint(land.series[i], c)
    ws.add_chart(land, "R2")

    # 2) grid connection (line)
    grid = LineChart(); grid.legend = None
    grid.title = "Grid connection by self-sufficiency"; grid.y_axis.title = "MW"; grid.x_axis.title = "SSR %"
    grid.height, grid.width = 7.5, 16
    grid.add_data(Reference(ws, min_col=4, min_row=1, max_row=r1), titles_from_data=True)
    grid.set_categories(cats); paint(grid.series[0], GRID, line=True)
    ws.add_chart(grid, "R19")

    # 3) battery capacity (MWh bar)
    batt = BarChart(); batt.type = "col"; batt.legend = None
    batt.title = "Battery by self-sufficiency"; batt.y_axis.title = "MWh"; batt.x_axis.title = "SSR %"
    batt.height, batt.width = 7.5, 16
    batt.add_data(Reference(ws, min_col=12, min_row=1, max_row=r1), titles_from_data=True)
    batt.set_categories(cats); paint(batt.series[0], BATT)
    ws.add_chart(batt, "R35")


# ──────────────────────────────────────────────────────────────────────────────
# Write — ScenarioResult (+ echoed inputs) → sheets appended to the workbook
# ──────────────────────────────────────────────────────────────────────────────

def _kv(d: dict) -> pd.DataFrame:
    return pd.DataFrame({"Field": list(d.keys()), "Value": list(d.values())})


def _text(v: Any) -> str:
    """Render a value as unambiguous text for the informational Run-Info sheet.
    Keeps booleans and numbers from sharing one column (which makes pandas coerce
    a numeric 1 into True on read)."""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v)


def _autofit(ws, max_w: int = 48, freeze: str = "A2") -> None:
    # `freeze` pins everything above/left of that cell while scrolling. "A2" pins
    # the header row; "B2" also pins the first column (handy for wide time-series).
    ws.freeze_panes = freeze
    for col in ws.columns:
        letter = col[0].column_letter
        width = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[letter].width = min(max_w, max(10, width + 2))


def write_results(
    out_path: Union[str, Path],
    inputs: dict,
    result,
    meta: dict,
    *,
    profile_used: str = "",
    parcels: Optional[dict] = None,
    loads: Optional[list] = None,
    max_flow_rows: int = 40000,
) -> None:
    """Write the run outcome into a fresh workbook. Keeps the original Inputs sheet
    (so the file is round-trippable) and appends Run Info / Design / KPIs / Frontier
    / Flows / resolved-inputs sheets."""
    out_path = Path(out_path)
    eff = _effective(inputs)

    with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
        # 1. Original Inputs, so the output file can be re-run as-is.
        _input_template_frame(values=eff).to_excel(xw, sheet_name="Inputs", index=False)

        # 2. Run Info — what actually happened. Values are rendered as text so the
        #    Yes/No flags never coerce the numeric cells (see _text).
        info = {
            "Project": eff.get("project_name") or "DIP Project",
            "Location": eff.get("location") or "",
            "Scenario": f"{result.id} — {result.name}",
            "Scenario auto-detected": bool(meta.get("scenario_auto_detected")),
            "Why this scenario": meta.get("scenario_reason", ""),
            "Feasible": bool(result.feasible),
            "Answer mode": result.answer_mode,
            "Topology": meta.get("topology", ""),
            "Effective PV (MW)": round(meta.get("effective_pv_mw", 0.0), 3),
            "Effective wind (MW)": round(meta.get("effective_wind_mw", 0.0), 3),
            "Load peak (MW)": round(meta.get("load_peak_mw", 0.0), 3),
            "Generation input mode": f"solar={meta.get('solar_mode', '?')}, wind={meta.get('wind_mode', '?')}",
            "Timestep (h)": meta.get("dt_hours", 1.0),
        }
        for tech in ("solar", "wind"):
            p = (parcels or {}).get(tech)
            if p:
                info[f"{tech.capitalize()} land"] = f"{p['area_ha']} ha → {p['max_mw']} MW"
        if loads:
            info["Consumers"] = f"{len(loads)} → {round(meta.get('loads_peak_mw', 0.0), 3)} MW combined peak"
        info["Grid import (MW)"] = (f"{meta.get('grid_import_limit_mw', 0.0)}"
                                    + ("" if meta.get("allow_grid_import", True) else " (islanded)"))
        info["Grid export (MW)"] = (f"{meta.get('grid_export_limit_mw', 0.0)}"
                                    + (" (allowed)" if meta.get("allow_grid_export") else " (blocked)"))
        info["Profile used"] = profile_used
        info["Notes"] = result.notes
        info["Generated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        _kv({k: _text(v) for k, v in info.items()}).to_excel(xw, sheet_name="Run Info", index=False)

        # 3. Design + KPIs (scalars).
        if result.design:
            _kv({k: _round(v) for k, v in result.design.items()}).to_excel(
                xw, sheet_name="Design", index=False)
        if result.kpis:
            _kv({str(k): _round(v) for k, v in result.kpis.items()}).to_excel(
                xw, sheet_name="KPIs", index=False)

        # 4. Curve/surface table and per-slot flows, when the scenario produced them.
        if result.table is not None and not result.table.empty:
            result.table.to_excel(xw, sheet_name="Frontier", index=False)
        if result.flows is not None and not result.flows.empty:
            result.flows.head(max_flow_rows).to_excel(xw, sheet_name="Flows", index=False)
        point_freezes: dict = {}
        if getattr(result, "point_flows", None) and isinstance(result.point_flows, dict):
            point_freezes = _write_point_flows_sheets(xw, result.point_flows, meta, max_flow_rows=max_flow_rows)

        # 5. Echo the land parcels and consumers that shaped the run.
        if parcels:
            pd.DataFrame([
                {"tech": t, "area_ha": p["area_ha"], "lat": p["lat"], "lon": p["lon"],
                 "max_mw": p["max_mw"], "solar_cf_pct": p["solar_cf_pct"]}
                for t, p in parcels.items()
            ]).to_excel(xw, sheet_name="Parcels", index=False)
        if loads:
            pd.DataFrame(loads).to_excel(xw, sheet_name="Loads", index=False)

        # Freeze the header row everywhere; also pin the first column on the wide
        # time-series sheets, and pin the hourly-table header on per-point sheets.
        for ws in xw.book.worksheets:
            if ws.title in point_freezes:
                _autofit(ws, freeze=point_freezes[ws.title])
            elif ws.title in ("Flows", "Frontier"):
                _autofit(ws, freeze="B2")
            else:
                _autofit(ws)


def _write_point_flows_sheets(
    xw: pd.ExcelWriter,
    point_flows: dict,
    meta: dict,
    max_flow_rows: int = 40000,
) -> dict:
    """Write individual flow detail tabs for each point along a sizing curve/sweep.

    Returns {sheet_name: freeze_cell} so the caller can pin the HOURLY table's own
    header row (not the block title) while scrolling the 8760 rows."""
    from core.report import hourly_flows, energy_split, monthly
    dt_hours = float(meta.get("dt_hours", 1.0) or 1.0) if meta else 1.0
    freezes: dict = {}
    for sheet_name, f_df in point_flows.items():
        if f_df is None or f_df.empty:
            continue
        # Excel limits sheet names to 31 characters
        s_name = str(sheet_name)[:31]
        try:
            es = energy_split(f_df, dt_hours)
            bal = pd.DataFrame({
                "Energy flow (MWh/yr)": ["Total demand (load)", "  served by PV directly", "  served by BESS discharge",
                                         "  imported from grid", "  unmet (shed)", "PV available", "PV used on-site", "PV curtailed"],
                "MWh/yr": [es.get("Load (MWh)", 0), es.get("PV → Load (MWh)", 0), es.get("BESS → Load (MWh)", 0),
                           es.get("Grid → Load (MWh)", 0), es.get("Unmet (MWh)", 0), es.get("PV available (MWh)", 0),
                           es.get("PV used (MWh)", 0), es.get("PV curtailed (MWh)", 0)]
            })
            m_df = monthly(f_df, dt_hours)
            hf = hourly_flows(f_df).head(max_flow_rows)

            def _write_block(title: str, dfb: pd.DataFrame, cur: int) -> int:
                dfb.to_excel(xw, sheet_name=s_name, index=False, startrow=cur + 1)
                xw.sheets[s_name].cell(row=cur + 1, column=1, value=title)
                return cur + 1 + 1 + len(dfb) + 1

            cur = 0
            cur = _write_block("ANNUAL ENERGY BALANCE", bal, cur)
            cur = _write_block("MONTHLY BREAKDOWN", m_df, cur)
            hourly_cur = cur                       # remember where the hourly block starts
            cur = _write_block("HOURLY ENERGY FLOWS  (merit order; CHECK = Load)", hf, cur)
            # Hourly header lands at Excel row hourly_cur+2 (title at +1). Pin rows
            # 1..header + the first (timestamp) column so both stay visible on scroll.
            freezes[s_name] = f"B{hourly_cur + 3}"
        except Exception:
            try:
                hourly_flows(f_df).head(max_flow_rows).to_excel(xw, sheet_name=s_name, index=False)
            except Exception:
                f_df.head(max_flow_rows).to_excel(xw, sheet_name=s_name, index=False)
            freezes[s_name] = "B2"
    return freezes


def _round(v: Any) -> Any:
    return round(v, 4) if isinstance(v, float) else v


# ──────────────────────────────────────────────────────────────────────────────
# Template — write a ready-to-edit starter workbook
# ──────────────────────────────────────────────────────────────────────────────

def _input_template_frame(values: Optional[dict] = None) -> pd.DataFrame:
    """The Inputs sheet as a DataFrame. `values` overrides the shown Value per key
    (used to echo the resolved inputs into the output)."""
    rows = []
    for f in FIELDS:
        shown = "" if f["default"] == "" else f["default"]
        if values is not None and f["key"] in values and values[f["key"]] is not None:
            v = values[f["key"]]
            shown = ", ".join(str(x) for x in v) if isinstance(v, list) else v
        if isinstance(shown, bool):                 # render flags as true/false, not 1/0
            shown = "true" if shown else "false"
        rows.append({
            "Group": f["group"],
            "Parameter": f["key"],
            "Value": shown,
            "Description": f["help"],
        })
    return pd.DataFrame(rows, columns=["Group", "Parameter", "Value", "Description"])


def build_template(out_path: Union[str, Path]) -> Path:
    """Write a starter workbook: Inputs (scalars) + Parcels (land) + Loads
    (consumers) + a Read me. Profiles stay in the referenced dataset."""
    out_path = Path(out_path)
    readme = pd.DataFrame({
        "How to use this workbook": [
            "1. Edit the Value column on the 'Inputs' sheet. Blank ⇒ engine default.",
            "2. 'Parcels' sheet (optional): land per technology (area_ha + lat/lon). The",
            "   engine derives max solar/wind MW from the area, so you design from land.",
            "   A parcel sets the nameplate when pv_mw / wind_mw are left blank.",
            "3. 'Loads' sheet (optional): one row per consumer; the combined peak scales",
            "   the demand shape (leave load_peak_mw blank to use it).",
            "4. Leave 'scenario_id' blank to let the engine detect the scenario from inputs.",
            "5. 'profile_path' blank ⇒ bundled dataset (data/Energy Timeseries.xlsx). That",
            "   file may carry absolute MW (solar_power_mw/wind_power_mw) OR per-unit shape",
            "   (solar_pu/wind_pu, 0–1) — per-unit lets the nameplate come from the land.",
            "6. Run:  python run_workbook.py --in <this file> --out <result file>",
            "7. Output adds: Run Info · Design · KPIs · Frontier · Flows (+ echoed Parcels/Loads).",
        ]
    })
    parcels = pd.DataFrame(
        [{"tech": "solar", "area_ha": 184, "lat": 51.96, "lon": 1.35, "density_mw_per_ha": "",
          "note": "blank density ⇒ site.py rule (~0.9 MWp/ha); max MW derived from area"},
         {"tech": "wind", "area_ha": 184, "lat": 51.96, "lon": 1.35, "density_mw_per_ha": "",
          "note": "blank density ⇒ ~0.26 MW/ha"}],
        columns=PARCEL_COLUMNS + ["note"])
    loads = pd.DataFrame(
        [{"name": "Data Centre", "load_type": "data_centre", "peak_mw": 91, "baseline_mw": 52}],
        columns=LOAD_COLUMNS)
    with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
        readme.to_excel(xw, sheet_name="Read me", index=False)
        _input_template_frame().to_excel(xw, sheet_name="Inputs", index=False)
        parcels.to_excel(xw, sheet_name="Parcels", index=False)
        loads.to_excel(xw, sheet_name="Loads", index=False)
        for ws in xw.book.worksheets:
            _autofit(ws)
    return out_path


def build_example(out_path: Union[str, Path],
                  profiles_source: Union[str, Path] = DEFAULT_PROFILE) -> Path:
    """Write a fully-filled, single-file input workbook that mirrors the inputs the
    frontend used to collect — Inputs + Parcels + Loads + the embedded profiles — so
    the optimiser can detect the scenario and run from this one file. Nothing else
    is needed: `python run_workbook.py --in <this file>`.
    """
    out_path = Path(out_path)
    # The frontend's default project, expressed as workbook inputs.
    values = {
        "project_name": "Felixstowe Port — DC Colocation",
        "location": "Felixstowe, UK",
        "target_ssr_pct": 95,                 # frontend default SSR target
        "site_topology": "grid_connected_btm",
    }
    parcels = pd.DataFrame(
        [{"tech": "solar", "area_ha": 184, "lat": 51.96, "lon": 1.35, "density_mw_per_ha": "",
          "note": "184 ha of solar land → ~166 MWp (site.py). Nameplate comes from here."},
         {"tech": "wind", "area_ha": 184, "lat": 51.96, "lon": 1.35, "density_mw_per_ha": "",
          "note": "184 ha of wind land → ~48 MW. Its night generation makes SSR 95% reachable."}],
        columns=PARCEL_COLUMNS + ["note"])
    loads = pd.DataFrame(
        [{"name": "Data Centre", "load_type": "data_centre", "peak_mw": 24, "baseline_mw": 14}],
        columns=LOAD_COLUMNS)
    # Embed the profiles so this is a genuine one-file input.
    profiles = pd.read_excel(profiles_source, sheet_name="Energy Timeseries")

    with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
        _input_template_frame(values=values).to_excel(xw, sheet_name="Inputs", index=False)
        parcels.to_excel(xw, sheet_name="Parcels", index=False)
        loads.to_excel(xw, sheet_name="Loads", index=False)
        profiles.to_excel(xw, sheet_name="Energy Timeseries", index=False)
        for ws in xw.book.worksheets:
            if ws.title == "Energy Timeseries":
                ws.freeze_panes = "B2"              # pin header + date col (skip slow autofit)
            else:
                _autofit(ws)
    return out_path
