"""
core/land_split.py — whole-site land allocation across an SSR sweep
==================================================================
The developer's real question: given ONE site of `total_area_ha`, at each
self-sufficiency (SSR) level, how should the land be carved up between the four
things that need it, and what grid connection does that buy?

    total_area_ha  =  DC land  +  solar land  +  wind land  +  battery land

Every use consumes land through its own density:

    DC land      = load_MW  × 1500 m²/MW  / 10,000   (fixed by the load — carved out first)
    solar land   = solar_MW / 0.9   (MW/ha)          (LP variable)
    wind  land   = wind_MW  / 0.26  (MW/ha)          (LP variable)
    battery land = bess_MWh × 50 m²/MWh / 10,000     (LP variable — storage costs land too)

so the coupling constraint the optimiser respects at every SSR point is

    solar_ha + wind_ha + battery_ha  ≤  total_area_ha − DC_ha

For each SSR target we size the LEAST battery that meets it within the land, and
read back the grid connection — sweeping SSR gives the frontier where each row is
one complete land-allocation decision (grid + DC/solar/wind/battery land + sizes).

Self-contained LP (does NOT touch core/sizing_engine.py).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pyomo.environ as pyo

from core.params import PhysicalParams
from core.solver import create_highs_solver

# Build densities. Solar/wind match core/site.py; battery & DC are developer inputs.
SOLAR_MW_PER_HA = 0.9            # GCR × module efficiency
WIND_MW_PER_HA = 48.0 / 184.0    # ≈ 0.26, calibrated to the reference site
BATTERY_MWH_PER_HA = 200.0       # ≈ 50 m²/MWh (40–60 typical: PCS, cooling, NFPA-855 clearances)
DC_M2_PER_MW = 1500.0            # data-centre total land per MW of load (≈ 1500–2000 m²/MW)


def solve_land_split(
    df: pd.DataFrame,
    params: PhysicalParams,
    *,
    total_area_ha: float,
    ssr_target_pct: float,
    objective: str = "min_bess",
    bess_mwh_cap: float | None = None,
    dc_ha: float = 0.0,
    battery_mwh_per_ha: float = BATTERY_MWH_PER_HA,
    solar_density_mw_per_ha: float = SOLAR_MW_PER_HA,
    wind_density_mw_per_ha: float = WIND_MW_PER_HA,
    return_flows: bool = False,
    tiebreak: float = 1e-4,
    time_limit: int = 120,
) -> dict:
    """Allocate the site (solar/wind/battery within total − DC) and size storage.

    objective="min_bess" (default): least battery to hit ssr_target_pct — the SSR
      sweep; grid connection is read back.
    objective="min_grid": least grid connection (battery up to bess_mwh_cap, if set)
      — the grid↔battery trade-off; SSR is read back.
    """
    dt = params.dt_hours
    n = len(df)
    solar_pu = df["pv_pu"].to_numpy()
    wind_pu = df["wind_pu"].to_numpy() if "wind_pu" in df.columns else np.zeros(n)
    load = df["load_mw"].to_numpy()
    total_demand = float(load.sum()) * dt
    avail_ha = max(0.0, total_area_ha - dc_ha)          # land left after the DC footprint
    if total_demand <= 0 or avail_ha <= 0:
        return {"ssr_target_pct": round(float(ssr_target_pct), 1), "feasible": False}

    m = pyo.ConcreteModel()
    m.T = pyo.RangeSet(0, n - 1)

    # ── Hardware decision variables (each bounded by the land it could take) ──
    m.solar_mw = pyo.Var(within=pyo.NonNegativeReals, bounds=(0, avail_ha * solar_density_mw_per_ha))
    m.wind_mw = pyo.Var(within=pyo.NonNegativeReals, bounds=(0, avail_ha * wind_density_mw_per_ha))
    m.bess_mw = pyo.Var(within=pyo.NonNegativeReals, bounds=(0, params.site_max_bess_mw))
    bess_ub = min(params.site_max_bess_mwh, avail_ha * battery_mwh_per_ha)   # land also caps energy
    if bess_mwh_cap is not None:                                            # trade-off sweep cap
        bess_ub = min(bess_ub, max(0.0, bess_mwh_cap))
    m.bess_mwh = pyo.Var(within=pyo.NonNegativeReals, bounds=(0, bess_ub))
    m.peak_gc = pyo.Var(within=pyo.NonNegativeReals)     # grid-connection tracker

    # ── Time-series variables ─────────────────────────────────────────────
    m.p_gen = pyo.Var(m.T, within=pyo.NonNegativeReals)
    m.p_grid = pyo.Var(m.T, within=pyo.NonNegativeReals)
    m.p_chg = pyo.Var(m.T, within=pyo.NonNegativeReals)
    m.p_dchg = pyo.Var(m.T, within=pyo.NonNegativeReals)
    m.p_curt = pyo.Var(m.T, within=pyo.NonNegativeReals)
    m.e_bess = pyo.Var(m.T, within=pyo.NonNegativeReals)

    # ── Four-way land budget: solar + wind + battery ≤ site − DC ──────────
    m.land_con = pyo.Constraint(expr=(
        m.solar_mw / solar_density_mw_per_ha
        + m.wind_mw / wind_density_mw_per_ha
        + m.bess_mwh / battery_mwh_per_ha) <= avail_ha)

    # ── Power balance (grid meets any residual; no unmet, no export) ──────
    m.balance = pyo.Constraint(m.T, rule=lambda m, t:
                               m.p_gen[t] + m.p_grid[t] + m.p_dchg[t] == load[t] + m.p_chg[t])
    m.gen_avail = pyo.Constraint(m.T, rule=lambda m, t:
                                 m.p_gen[t] + m.p_curt[t] == solar_pu[t] * m.solar_mw + wind_pu[t] * m.wind_mw)
    m.peak_track = pyo.Constraint(m.T, rule=lambda m, t: m.p_grid[t] <= m.peak_gc)
    m.grid_ceiling = pyo.Constraint(m.T, rule=lambda m, t: m.p_grid[t] <= params.site_max_grid_mw)

    # ── BESS power, duration, SOC ────────────────────────────────────────
    m.chg_lim = pyo.Constraint(m.T, rule=lambda m, t: m.p_chg[t] <= m.bess_mw)
    m.dchg_lim = pyo.Constraint(m.T, rule=lambda m, t: m.p_dchg[t] <= m.bess_mw)
    if params.min_bess_duration_h > 0:
        m.dur_min = pyo.Constraint(expr=m.bess_mwh >= m.bess_mw * params.min_bess_duration_h)
    if params.max_bess_duration_h < 1e6:
        m.dur_max = pyo.Constraint(expr=m.bess_mwh <= m.bess_mw * params.max_bess_duration_h)
    min_f, max_f = params.min_soc_pct / 100.0, params.max_soc_pct / 100.0
    m.soc_min = pyo.Constraint(m.T, rule=lambda m, t: m.e_bess[t] >= m.bess_mwh * min_f)
    m.soc_max = pyo.Constraint(m.T, rule=lambda m, t: m.e_bess[t] <= m.bess_mwh * max_f)
    ec, ed, i0 = params.eff_charge, params.eff_discharge, params.initial_soc_pct / 100.0

    def _soc(m, t):
        prior = i0 * m.bess_mwh if t == 0 else m.e_bess[t - 1]
        return m.e_bess[t] == prior + (m.p_chg[t] * ec - m.p_dchg[t] / ed) * dt
    m.soc_dyn = pyo.Constraint(m.T, rule=_soc)

    # ── SSR target: grid-import energy ≤ (1 − SSR) · demand ──────────────
    m.ssr_con = pyo.Constraint(
        expr=sum(m.p_grid[t] for t in m.T) * dt <= (1.0 - ssr_target_pct / 100.0) * total_demand)

    # ── Objective ────────────────────────────────────────────────────────
    if objective == "min_grid":
        m.obj = pyo.Objective(expr=m.peak_gc + tiebreak * m.bess_mwh, sense=pyo.minimize)
    else:  # min_bess
        m.obj = pyo.Objective(expr=m.bess_mwh + tiebreak * m.peak_gc, sense=pyo.minimize)

    solver = create_highs_solver(time_limit_seconds=time_limit)
    try:
        results = solver.solve(m, tee=False, load_solutions=False)
        ok = (results.solver.status == pyo.SolverStatus.ok
              and results.solver.termination_condition == pyo.TerminationCondition.optimal)
    except Exception as exc:
        print(f"   ✗ land-allocation solver exception at SSR {ssr_target_pct}%: {exc}")
        ok = False
    if not ok:
        return {"ssr_target_pct": round(float(ssr_target_pct), 1), "feasible": False}
    m.solutions.load_from(results)

    solar_mw = max(0.0, pyo.value(m.solar_mw))
    wind_mw = max(0.0, pyo.value(m.wind_mw))
    bess_mw = max(0.0, pyo.value(m.bess_mw))
    bess_mwh = max(0.0, pyo.value(m.bess_mwh))
    peak_gc = max(0.0, pyo.value(m.peak_gc))
    grid_energy = sum(max(0.0, pyo.value(m.p_grid[t])) for t in m.T) * dt
    achieved_ssr = (1.0 - grid_energy / total_demand) * 100.0
    gen_total = float((solar_pu * solar_mw + wind_pu * wind_mw).sum()) * dt
    curtailed = sum(max(0.0, pyo.value(m.p_curt[t])) for t in m.T) * dt
    solar_ha = solar_mw / solar_density_mw_per_ha
    wind_ha = wind_mw / wind_density_mw_per_ha
    battery_ha = bess_mwh / battery_mwh_per_ha

    # Hourly dispatch — the "why" behind the design: when generation serves the load,
    # charges the battery, is curtailed, and when the grid steps in.
    flows = None
    if return_flows:
        p_gen = np.array([pyo.value(m.p_gen[t]) for t in m.T])
        p_grid = np.array([pyo.value(m.p_grid[t]) for t in m.T])
        p_chg = np.array([pyo.value(m.p_chg[t]) for t in m.T])
        p_dchg = np.array([pyo.value(m.p_dchg[t]) for t in m.T])
        p_curt = np.array([pyo.value(m.p_curt[t]) for t in m.T])
        e_bess = np.array([pyo.value(m.e_bess[t]) for t in m.T])
        flows = pd.DataFrame({
            "Timestamp": df["timestamp"].values if "timestamp" in df.columns else np.arange(n),
            "Load (MW)": load.round(3),
            "Solar available (MW)": (solar_pu * solar_mw).round(3),
            "Wind available (MW)": (wind_pu * wind_mw).round(3),
            "Generation used (MW)": p_gen.round(3),
            "Curtailed (MW)": p_curt.round(3),
            "BESS charge (MW)": p_chg.round(3),
            "BESS discharge (MW)": p_dchg.round(3),
            "Grid import (MW)": p_grid.round(3),
            "BESS SOC (MWh)": e_bess.round(2),
            "CHECK served − load": (p_gen + p_grid + p_dchg - p_chg - load).round(3),
        })

    out = {
        "ssr_target_pct": round(float(ssr_target_pct), 1),
        "feasible": True,
        "achieved_ssr_pct": round(achieved_ssr, 2),
        "gcmin_mw": round(peak_gc, 2),
        "dc_ha": round(dc_ha, 1),
        "solar_ha": round(solar_ha, 1),
        "solar_mw": round(solar_mw, 2),
        "wind_ha": round(wind_ha, 1),
        "wind_mw": round(wind_mw, 2),
        "battery_ha": round(battery_ha, 2),
        "bess_mw": round(bess_mw, 2),
        "bess_mwh": round(bess_mwh, 2),
        "land_used_ha": round(solar_ha + wind_ha + battery_ha + dc_ha, 1),
        "land_spare_ha": round(max(0.0, total_area_ha - (solar_ha + wind_ha + battery_ha + dc_ha)), 1),
        "solar_share_pct": round(100.0 * solar_ha / (solar_ha + wind_ha), 1) if (solar_ha + wind_ha) > 1e-9 else 0.0,
        "curtailment_pct": round(100.0 * curtailed / gen_total, 1) if gen_total > 1e-9 else 0.0,
    }
    if flows is not None:
        out["flows"] = flows
    return out


def land_allocation_sweep(
    df: pd.DataFrame,
    params: PhysicalParams,
    *,
    total_area_ha: float,
    dc_ha: float = 0.0,
    ssr_targets_pct=(50.0, 60.0, 70.0, 80.0, 90.0),
    battery_mwh_per_ha: float = BATTERY_MWH_PER_HA,
    solar_density_mw_per_ha: float = SOLAR_MW_PER_HA,
    wind_density_mw_per_ha: float = WIND_MW_PER_HA,
    return_flows: bool = False,
    time_limit: int = 120,
):
    """Sweep SSR targets → one row per target with the whole-site land allocation,
    battery sizing and grid connection. Infeasible targets are kept (feasible=False).
    With return_flows, also returns {label: hourly-flows DataFrame} per point."""
    rows, point_flows = [], {}
    for s in ssr_targets_pct:
        r = solve_land_split(df, params, total_area_ha=total_area_ha, ssr_target_pct=float(s),
                             dc_ha=dc_ha, battery_mwh_per_ha=battery_mwh_per_ha,
                             solar_density_mw_per_ha=solar_density_mw_per_ha,
                             wind_density_mw_per_ha=wind_density_mw_per_ha,
                             return_flows=return_flows, time_limit=time_limit)
        f = r.pop("flows", None)
        rows.append(r)
        if return_flows and f is not None:
            point_flows[f"SSR {int(round(float(s)))}pct"] = f
    cols = ["ssr_target_pct", "feasible", "achieved_ssr_pct", "gcmin_mw",
            "dc_ha", "solar_ha", "solar_mw", "wind_ha", "wind_mw",
            "battery_ha", "bess_mw", "bess_mwh", "land_used_ha", "land_spare_ha",
            "solar_share_pct", "curtailment_pct"]
    df_out = pd.DataFrame(rows, columns=cols)
    return (df_out, point_flows) if return_flows else df_out


def grid_battery_tradeoff(
    df: pd.DataFrame,
    params: PhysicalParams,
    *,
    total_area_ha: float,
    battery_mwh_steps,
    dc_ha: float = 0.0,
    battery_mwh_per_ha: float = BATTERY_MWH_PER_HA,
    solar_density_mw_per_ha: float = SOLAR_MW_PER_HA,
    wind_density_mw_per_ha: float = WIND_MW_PER_HA,
    time_limit: int = 120,
) -> pd.DataFrame:
    """For each battery-energy cap, co-optimise the four-way land split to MINIMISE
    the grid connection. Shows how much battery buys how much grid reduction."""
    rows = []
    for cap in battery_mwh_steps:
        r = solve_land_split(df, params, total_area_ha=total_area_ha, ssr_target_pct=0.0,
                             objective="min_grid", bess_mwh_cap=float(cap), dc_ha=dc_ha,
                             battery_mwh_per_ha=battery_mwh_per_ha,
                             solar_density_mw_per_ha=solar_density_mw_per_ha,
                             wind_density_mw_per_ha=wind_density_mw_per_ha, time_limit=time_limit)
        r["battery_cap_mwh"] = round(float(cap), 1)
        rows.append(r)
    cols = ["battery_cap_mwh", "feasible", "gcmin_mw", "bess_mw", "bess_mwh",
            "dc_ha", "solar_ha", "wind_ha", "battery_ha", "achieved_ssr_pct", "curtailment_pct"]
    return pd.DataFrame(rows, columns=cols)
