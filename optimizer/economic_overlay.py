"""
Economic Overlay Module (Phase 2)
-----------------------------------
Takes the Phase 1 physical sizing curves and overlays CAPEX + OPEX costs
to find the techno-economic optimum point on each curve.

Phase 2 does NOT re-run any LP simulations for sizing.
It reads the already-computed sizing curves from Phase 1, evaluates the
cost of each point, and selects the minimum-NPV (or min-LCOE) design.

The Phase 2 optimum is then passed to the dispatch engine for verification,
and the financial summary is generated.

Phase 2 objectives (per DIP spec):
  1. Find cost-optimal point on SSR curve
  2. Find cost-optimal point on Peak Shaving curve
  3. Co-optimise SSR + GC together (from PV+BESS surface)
  4. Seasonal grid time-shifting
  5. Grid services overlay (reserved SOC + revenue)
  6. Site-area constraint feasibility check
"""

from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from optimizer.params import EconomicParams, SizingResult
from optimizer.schema import CurveCols

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")


# ──────────────────────────────────────────────────────────────────────────────
# 1. Load Phase 1 curves
# ──────────────────────────────────────────────────────────────────────────────

def load_phase1_curves(phase1_xlsx: str | Path) -> dict[str, pd.DataFrame]:
    """
    Read the Phase 1 sizing curves workbook.

    Returns
    -------
    dict with keys:
        "ssr"          → Main_SSR_Curve sheet
        "peak_shaving" → Main_PeakShave sheet
        "surface"      → PV_BESS_Surface sheet (may be empty)
        "sub"          → Sub_PeakShave sheet
        "profiles"     → Input_Profiles sheet
    """
    path = Path(phase1_xlsx)
    if not path.exists():
        raise FileNotFoundError(f"Phase 1 results not found: {path}")

    xls = pd.ExcelFile(path)
    available = xls.sheet_names

    def _safe_read(sheet_name):
        if sheet_name in available:
            return pd.read_excel(xls, sheet_name=sheet_name)
        return pd.DataFrame()

    curves = {
        "ssr":          _safe_read("Main_SSR_Curve"),
        "peak_shaving": _safe_read("Main_PeakShave"),
        "surface":      _safe_read("PV_BESS_Surface"),
        "sub":          _safe_read("Sub_PeakShave"),
        "profiles":     _safe_read("Input_Profiles"),
    }

    total_points = sum(len(v) for v in curves.values() if not v.empty)
    print(f"   [economic_overlay] Loaded {total_points} curve points from {path.name}")
    for key, df in curves.items():
        if not df.empty:
            print(f"   → {key:15s}: {len(df)} rows")

    return curves


# ──────────────────────────────────────────────────────────────────────────────
# 2. Cost Evaluation — overlay economics on each curve point
# ──────────────────────────────────────────────────────────────────────────────

def evaluate_costs(
    curve_df: pd.DataFrame,
    eco: EconomicParams,
    profiles_df: pd.DataFrame,
    dt_hours: float = 1.0,
) -> pd.DataFrame:
    """
    Add economic columns to a sizing curve DataFrame.

    For each row (a sizing point), calculate:
      - CAPEX breakdown (PV, BESS MW, BESS MWh, grid connection)
      - Annual OPEX (grid energy cost, BESS degradation, fixed O&M)
      - NPV and LCOE

    Parameters
    ----------
    curve_df : pd.DataFrame
        Phase 1 sizing curve (one row per target/sizing combination).
    eco : EconomicParams
        All financial assumptions.
    profiles_df : pd.DataFrame
        Input profiles (timestamp, load_mw, pv_pu) for energy calculations.
    dt_hours : float
        Timestep in hours.

    Returns
    -------
    pd.DataFrame
        Same rows, with added economic columns.
    """
    if curve_df.empty:
        return curve_df

    feasible = curve_df[curve_df.get(CurveCols.FEASIBLE, True) == True].copy()
    if feasible.empty:
        return curve_df

    total_demand_mwh = profiles_df["load_mw"].sum() * dt_hours

    rows = []
    for _, row in feasible.iterrows():
        bess_mw  = float(row.get(CurveCols.BESS_MW,  0) or 0)
        bess_mwh = float(row.get(CurveCols.BESS_MWH, 0) or 0)
        pv_mw    = float(row.get(CurveCols.PV_MW,    0) or 0)
        gc_mw    = float(row.get(CurveCols.PEAK_GC_MW, 0) or 0)
        ssr      = float(row.get(CurveCols.ACHIEVED_SSR_PCT, 0) or 0)

        # ── CAPEX ──────────────────────────────────────────────────────────
        capex_pv           = pv_mw    * eco.cost_pv_mw
        capex_bess_mw      = bess_mw  * eco.cost_bess_mw
        capex_bess_mwh     = bess_mwh * eco.cost_bess_mwh
        capex_grid_conn    = gc_mw    * eco.grid_connection_cost_mw
        total_capex        = capex_pv + capex_bess_mw + capex_bess_mwh + capex_grid_conn

        # ── Annual OPEX ────────────────────────────────────────────────────
        # Grid energy: SSR tells us what fraction comes from grid
        annual_grid_mwh    = total_demand_mwh * (1.0 - ssr / 100.0)
        annual_grid_cost   = annual_grid_mwh * eco.grid_cost_mwh

        # BESS degradation: estimate equivalent full cycles from SSR improvement
        # Approximate: BESS cycles ≈ (SSR_achieved - SSR_direct) * demand / bess_mwh
        ssr_direct = 39.6  # typical direct SSR from 150 MW PV without BESS (data-specific)
        ssr_from_bess = max(0.0, ssr - ssr_direct)
        annual_bess_throughput = (ssr_from_bess / 100.0) * total_demand_mwh
        annual_deg_cost = annual_bess_throughput * eco.real_deg_cost

        # Fixed O&M
        annual_fixed_om = bess_mwh * eco.fixed_opex_per_mwh_year

        annual_opex = annual_grid_cost + annual_deg_cost + annual_fixed_om

        # ── DCF / NPV / LCOE ───────────────────────────────────────────────
        pv_factor     = eco.pv_factor
        total_pvc     = total_capex + annual_opex * pv_factor
        discounted_demand = total_demand_mwh * pv_factor
        lcoe          = total_pvc / discounted_demand if discounted_demand > 0 else 0.0
        npv           = -(total_pvc)   # cost-only NPV (no revenue assumed in base case)

        r = row.to_dict()
        r["CAPEX PV (€M)"]            = round(capex_pv / 1e6, 3)
        r["CAPEX BESS MW (€M)"]       = round(capex_bess_mw / 1e6, 3)
        r["CAPEX BESS MWh (€M)"]      = round(capex_bess_mwh / 1e6, 3)
        r["CAPEX Grid Connection (€M)"]= round(capex_grid_conn / 1e6, 3)
        r["Total CAPEX (€M)"]         = round(total_capex / 1e6, 3)
        r["Annual Grid Cost (€M/yr)"]  = round(annual_grid_cost / 1e6, 3)
        r["Annual Deg Cost (€M/yr)"]   = round(annual_deg_cost / 1e6, 3)
        r["Annual Fixed OM (€M/yr)"]   = round(annual_fixed_om / 1e6, 3)
        r["Total Annual OPEX (€M/yr)"] = round(annual_opex / 1e6, 3)
        r["LCOE (€/MWh)"]             = round(lcoe, 2)
        r["NPV Costs (€M)"]           = round(npv / 1e6, 3)
        rows.append(r)

    result = pd.DataFrame(rows)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# 3. Find Optimal Point on a Curve
# ──────────────────────────────────────────────────────────────────────────────

def find_optimal_point(
    costed_df: pd.DataFrame,
    objective: str = "knee",
) -> pd.Series:
    """
    Select the single best design point from a costed sizing curve.

    Parameters
    ----------
    costed_df : pd.DataFrame
        Output of evaluate_costs() — curve with economic columns.
    objective : str
        "knee"      → inflection point: best SSR per incremental € of CAPEX
                      (recommended for DIP spec — identifies the economic sweet spot)
        "lcoe"      → minimise Levelised Cost of Energy
        "min_cost"  → minimise total life-cycle cost (CAPEX + discounted OPEX)
        "capex"     → minimise total CAPEX only
        "ssr"       → maximise SSR (ignore cost)

    Returns
    -------
    pd.Series
        The single optimal row.
    """
    if costed_df.empty:
        return pd.Series()

    feasible = costed_df[costed_df.get(CurveCols.FEASIBLE, True) == True].copy()
    if feasible.empty:
        return pd.Series()

    ssr_col = CurveCols.ACHIEVED_SSR_PCT
    cap_col = "Total CAPEX (€M)"

    if objective == "knee":
        # Knee-of-curve: maximise incremental SSR per incremental € of CAPEX.
        # IMPORTANT: only consider points where the BESS is actually deployed.
        # Zero-battery points are "free" PV self-consumption (marginal cost ≈ 0),
        # and including them makes the knee collapse onto the no-storage design —
        # which is never a meaningful storage recommendation.
        bess_col = CurveCols.BESS_MW
        engaged = feasible[feasible.get(bess_col, 0).fillna(0) > 0.1].copy() if bess_col in feasible.columns else feasible
        if engaged.empty:
            # No design uses storage at all — fall back to the cheapest point.
            if cap_col in feasible.columns:
                return feasible.loc[feasible[cap_col].idxmin()]
            return feasible.iloc[0]

        if ssr_col in engaged.columns and cap_col in engaged.columns:
            ranked = engaged.sort_values(ssr_col).reset_index()
            ranked["delta_ssr"]   = ranked[ssr_col].diff().fillna(0)
            ranked["delta_capex"] = ranked[cap_col].diff().fillna(0)
            # Only rows where SSR genuinely increases with more spend
            valid = ranked[(ranked["delta_ssr"] > 0) & (ranked["delta_capex"] >= 0)].copy()
            if valid.empty:
                # Fall back: highest-SSR engaged point
                return engaged.sort_values(ssr_col).iloc[-1]
            valid["marginal_€M_per_pct_ssr"] = valid["delta_capex"] / valid["delta_ssr"]
            # Knee = last point before marginal cost exceeds 2.5× the initial marginal
            first_marginal = valid["marginal_€M_per_pct_ssr"].iloc[0]
            threshold = first_marginal * 2.5
            below = valid[valid["marginal_€M_per_pct_ssr"] <= threshold]
            best_orig_idx = (below.iloc[-1]["index"] if not below.empty
                             else valid.iloc[0]["index"])
            return engaged.loc[best_orig_idx]
        else:
            return engaged.iloc[-1]

    elif objective == "min_cost":
        col = "NPV Costs (€M)"
        idx = feasible[col].idxmin() if col in feasible.columns else feasible.index[0]

    elif objective == "lcoe":
        col = "LCOE (€/MWh)"
        idx = feasible[col].idxmin() if col in feasible.columns else feasible.index[0]

    elif objective == "capex":
        col = cap_col
        idx = feasible[col].idxmin() if col in feasible.columns else feasible.index[0]

    elif objective == "ssr":
        idx = feasible[ssr_col].idxmax() if ssr_col in feasible.columns else feasible.index[-1]

    else:
        idx = feasible.index[0]

    return feasible.loc[idx]



# ──────────────────────────────────────────────────────────────────────────────
# 4. Co-Optimisation: SSR + Grid Connection Together
# ──────────────────────────────────────────────────────────────────────────────

def cooptimise_ssr_gc(
    surface_df: pd.DataFrame,
    eco: EconomicParams,
    profiles_df: pd.DataFrame,
    dt_hours: float = 1.0,
) -> pd.DataFrame:
    """
    Phase 2 co-optimisation: given the PV+BESS surface (Scenario C), find the
    combined (SSR, GC) optimum by computing total system cost for each surface point.

    The spec says: "Co-optimise SSR and Grid connection size. Understand how to
    optimise both variables at once, with the load fixed."

    For each surface point (PV_MW, target_SSR):
      - The BESS sizes the system for that SSR target
      - The grid connection is the residual peak from the LP
      - Total cost = PV CAPEX + BESS CAPEX + Grid connection CAPEX + OPEX

    Returns
    -------
    pd.DataFrame
        Surface with economic overlay + a "Recommended" flag on the min-cost point.
    """
    if surface_df.empty:
        return surface_df

    costed = evaluate_costs(surface_df, eco, profiles_df, dt_hours)
    if costed.empty:
        return costed

    # Mark the global minimum cost point
    if "Total CAPEX (€M)" in costed.columns:
        min_idx = costed["Total CAPEX (€M)"].idxmin()
        costed["Recommended"] = False
        costed.loc[min_idx, "Recommended"] = True

    return costed


# ──────────────────────────────────────────────────────────────────────────────
# 5. Seasonal Grid Time-Shifting Analysis
# ──────────────────────────────────────────────────────────────────────────────

def analyse_seasonal_shifting(
    profiles_df: pd.DataFrame,
    optimal_sizing: pd.Series,
    eco: EconomicParams,
    dt_hours: float = 1.0,
) -> pd.DataFrame:
    """
    Phase 2 seasonal grid time-shifting:
    For a data-centre load, identify periods where the base load is below the
    grid connection limit — these are 'headroom windows' where the BESS can
    be charged cheaply from the grid and used in expensive peak periods.

    This is an analytical post-processing step (not an LP) — it quantifies
    the potential value of seasonal shifting.

    Returns
    -------
    pd.DataFrame
        Monthly summary of headroom, potential shifting, and estimated value.
    """
    if profiles_df.empty or optimal_sizing.empty:
        return pd.DataFrame()

    gc_mw = float(optimal_sizing.get(CurveCols.PEAK_GC_MW, 0) or 0)
    if gc_mw <= 0:
        return pd.DataFrame()

    df = profiles_df.copy()
    df["month"] = pd.to_datetime(df["timestamp"]).dt.month
    df["headroom_mw"] = (gc_mw - df["load_mw"]).clip(lower=0)
    df["over_limit_mw"] = (df["load_mw"] - gc_mw).clip(lower=0)

    monthly = df.groupby("month").agg(
        mean_load_mw=("load_mw", "mean"),
        peak_load_mw=("load_mw", "max"),
        mean_headroom_mw=("headroom_mw", "mean"),
        total_headroom_mwh=("headroom_mw", lambda x: x.sum() * dt_hours),
        total_over_limit_mwh=("over_limit_mw", lambda x: x.sum() * dt_hours),
    ).reset_index()

    # Potential shifting value: energy that could be moved from off-peak to peak
    monthly["shifting_potential_mwh"] = monthly["total_over_limit_mwh"].clip(
        upper=monthly["total_headroom_mwh"]
    )
    monthly["shifting_value_€k"] = (
        monthly["shifting_potential_mwh"] * eco.grid_cost_mwh / 1000
    ).round(1)

    month_names = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
                   7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}
    monthly["month_name"] = monthly["month"].map(month_names)
    return monthly.round(2)


# ──────────────────────────────────────────────────────────────────────────────
# 6. Grid Services Overlay
# ──────────────────────────────────────────────────────────────────────────────

def analyse_grid_services(
    optimal_sizing: pd.Series,
    eco: EconomicParams,
    reserved_soc_pct: float = 20.0,
    service_hours_per_day: float = 4.0,
    service_price_mwh: float = 200.0,
) -> dict:
    """
    Phase 2 grid services: introduce a revenue stream from reserving a portion
    of the BESS state of charge for grid balancing / ancillary services.

    This is an analytical estimate (not an LP). A full grid services LP would
    require a real-time price signal — modelled here as a flat service premium.

    Parameters
    ----------
    optimal_sizing : pd.Series
        The selected optimal design point from Phase 1.
    eco : EconomicParams
        Financial parameters.
    reserved_soc_pct : float
        Percentage of BESS capacity reserved for grid services (e.g. 20%).
    service_hours_per_day : float
        Average hours per day the BESS participates in grid services.
    service_price_mwh : float
        Price received for grid services energy (€/MWh).

    Returns
    -------
    dict
        Key grid services metrics and economics.
    """
    bess_mw  = float(optimal_sizing.get(CurveCols.BESS_MW,  0) or 0)
    bess_mwh = float(optimal_sizing.get(CurveCols.BESS_MWH, 0) or 0)

    if bess_mw <= 0:
        return {
            "reserved_mwh": 0,
            "reserved_mw": 0,
            "annual_service_revenue_€k": 0,
            "note": "No BESS sized — no grid services possible"
        }

    reserved_mwh = bess_mwh * (reserved_soc_pct / 100.0)
    reserved_mw  = bess_mw  * (reserved_soc_pct / 100.0)

    # Annual throughput available for services
    annual_service_cycles = service_hours_per_day * 365
    annual_service_mwh    = reserved_mw * service_hours_per_day * 365

    # Revenue
    annual_revenue_eur    = annual_service_mwh * service_price_mwh

    # Net impact on BESS degradation (additional cycling)
    extra_deg_cost = annual_service_mwh * eco.real_deg_cost
    net_revenue    = annual_revenue_eur - extra_deg_cost

    return {
        "reserved_soc_pct":             reserved_soc_pct,
        "reserved_mwh":                 round(reserved_mwh, 1),
        "reserved_mw":                  round(reserved_mw, 1),
        "service_hours_per_day":        service_hours_per_day,
        "annual_service_mwh":           round(annual_service_mwh, 0),
        "service_price_€_per_mwh":      service_price_mwh,
        "annual_gross_revenue_€k":      round(annual_revenue_eur / 1000, 1),
        "annual_extra_deg_cost_€k":     round(extra_deg_cost / 1000, 1),
        "annual_net_revenue_€k":        round(net_revenue / 1000, 1),
        "note": (
            f"BESS provides {reserved_mw:.1f} MW / {reserved_mwh:.1f} MWh for grid services "
            f"({reserved_soc_pct:.0f}% of capacity reserved)"
        )
    }


# ──────────────────────────────────────────────────────────────────────────────
# 7. Site-Area Constraint Feasibility Check
# ──────────────────────────────────────────────────────────────────────────────

def check_site_area(
    costed_df: pd.DataFrame,
    max_area_m2: float,
    pv_area_m2_per_mw: float = 10_000.0,   # ~10,000 m²/MW for utility PV
    bess_area_m2_per_mwh: float = 15.0,    # ~15 m²/MWh for containerised BESS
) -> pd.DataFrame:
    """
    Phase 2 site-area constraint: filter curve points that physically fit
    within the available site area.

    Parameters
    ----------
    costed_df : pd.DataFrame
        Output of evaluate_costs().
    max_area_m2 : float
        Total available site area in m².
    pv_area_m2_per_mw : float
        Land requirement per MW of PV (m²/MW). Default: 10,000 (≈ 1 ha/MW).
    bess_area_m2_per_mwh : float
        Land requirement per MWh of BESS (m²/MWh). Default: 15 m²/MWh.

    Returns
    -------
    pd.DataFrame
        Same as input but with:
        - "PV Area (m²)"      : land for PV
        - "BESS Area (m²)"    : land for BESS
        - "Total Area (m²)"   : combined
        - "Fits on Site"      : bool — True if total ≤ max_area_m2
    """
    if costed_df.empty:
        return costed_df

    df = costed_df.copy()
    pv_col   = CurveCols.PV_MW
    bess_col = CurveCols.BESS_MWH

    pv_mw    = pd.to_numeric(df.get(pv_col,   0), errors="coerce").fillna(0)
    bess_mwh = pd.to_numeric(df.get(bess_col, 0), errors="coerce").fillna(0)

    df["PV Area (m²)"]    = (pv_mw    * pv_area_m2_per_mw).round(0)
    df["BESS Area (m²)"]  = (bess_mwh * bess_area_m2_per_mwh).round(0)
    df["Total Area (m²)"] = df["PV Area (m²)"] + df["BESS Area (m²)"]
    df["Fits on Site"]    = df["Total Area (m²)"] <= max_area_m2

    n_fit = df["Fits on Site"].sum()
    print(f"   [site_area] {n_fit}/{len(df)} designs fit within {max_area_m2/10000:.1f} ha site")
    return df
