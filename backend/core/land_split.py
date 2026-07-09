"""
core/land_split.py — solar ↔ wind land co-optimisation (shared land budget)
===========================================================================
The Phase-1 engine sizes BESS for a single PV nameplate. This module answers a
different, land-first question:

    Given ONE shared parcel of `total_area_ha`, for a target SSR, how much of the
    land should go to SOLAR vs WIND (and how big a BESS) to MINIMISE the grid
    connection (peak import), with least BESS as the tiebreaker?

Both `solar_mw` and `wind_mw` are LP decision variables, drawing on the same land
via their build densities (MW/ha), so the optimiser trades one against the other:

    solar_mw / ρ_solar  +  wind_mw / ρ_wind  ≤  total_area_ha

It is a self-contained LP (it does NOT import or modify core/sizing_engine.py), so
the existing scenarios are untouched. Sweep `land_split_frontier` over SSR targets
to get the "for each SSR, how much land to whom" table.

Objective (per SSR target):   min  peak_grid  +  ε · bess_mwh
i.e. the smallest grid connection that still hits the SSR, and among equal-grid
solutions, the smallest battery.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pyomo.environ as pyo

from core.params import PhysicalParams
from core.solver import create_highs_solver

# Default build densities (MW per hectare), matching core/site.py.
SOLAR_MW_PER_HA = 0.9      # GCR × module efficiency
WIND_MW_PER_HA = 48.0 / 184.0   # ≈ 0.26, calibrated to the reference site


def solve_land_split(
    df: pd.DataFrame,
    params: PhysicalParams,
    *,
    total_area_ha: float,
    ssr_target_pct: float,
    objective: str = "min_bess",
    bess_mwh_cap: float | None = None,
    solar_density_mw_per_ha: float = SOLAR_MW_PER_HA,
    wind_density_mw_per_ha: float = WIND_MW_PER_HA,
    tiebreak: float = 1e-4,
    time_limit: int = 120,
) -> dict:
    """Co-optimise the solar/wind land split + BESS at a given SSR target.

    objective:
      "min_bess" (default) — least battery to hit the SSR; the grid connection is
        read back. Gives a MEANINGFUL per-SSR frontier (higher SSR ⇒ more BESS).
      "min_grid" — least grid connection; note this degenerates to full islanding
        (grid → 0, SSR → 100%) whenever land+BESS allow, so it does NOT vary with
        the SSR target — use it as a single "how small can the grid be" question.

    Returns a plain dict (feasible flag + the design)."""
    dt = params.dt_hours
    n = len(df)
    solar_pu = df["pv_pu"].to_numpy()
    wind_pu = df["wind_pu"].to_numpy() if "wind_pu" in df.columns else np.zeros(n)
    load = df["load_mw"].to_numpy()
    total_demand = float(load.sum()) * dt
    if total_demand <= 0:
        return {"ssr_target_pct": ssr_target_pct, "feasible": False}

    m = pyo.ConcreteModel()
    m.T = pyo.RangeSet(0, n - 1)

    # ── Hardware decision variables ───────────────────────────────────────
    # Each generation type is bounded by what the WHOLE parcel could hold if given
    # entirely to it; the shared-land constraint below couples them.
    m.solar_mw = pyo.Var(within=pyo.NonNegativeReals, bounds=(0, total_area_ha * solar_density_mw_per_ha))
    m.wind_mw = pyo.Var(within=pyo.NonNegativeReals, bounds=(0, total_area_ha * wind_density_mw_per_ha))
    m.bess_mw = pyo.Var(within=pyo.NonNegativeReals, bounds=(0, params.site_max_bess_mw))
    # A battery-energy cap lets the grid↔battery trade-off sweep the storage budget.
    bess_mwh_ub = params.site_max_bess_mwh if bess_mwh_cap is None \
        else min(params.site_max_bess_mwh, max(0.0, bess_mwh_cap))
    m.bess_mwh = pyo.Var(within=pyo.NonNegativeReals, bounds=(0, bess_mwh_ub))
    m.peak_gc = pyo.Var(within=pyo.NonNegativeReals)          # grid-connection tracker

    # ── Time-series variables ─────────────────────────────────────────────
    m.p_gen = pyo.Var(m.T, within=pyo.NonNegativeReals)       # generation used (to load or BESS)
    m.p_grid = pyo.Var(m.T, within=pyo.NonNegativeReals)      # grid import
    m.p_chg = pyo.Var(m.T, within=pyo.NonNegativeReals)       # BESS charge
    m.p_dchg = pyo.Var(m.T, within=pyo.NonNegativeReals)      # BESS discharge
    m.p_curt = pyo.Var(m.T, within=pyo.NonNegativeReals)      # curtailed generation
    m.e_bess = pyo.Var(m.T, within=pyo.NonNegativeReals)      # BESS state of charge

    # ── Shared land budget: solar_ha + wind_ha ≤ total_area_ha ────────────
    m.land_con = pyo.Constraint(
        expr=m.solar_mw / solar_density_mw_per_ha + m.wind_mw / wind_density_mw_per_ha <= total_area_ha)

    # ── Power balance (grid meets any residual; no unmet, no export) ──────
    m.balance = pyo.Constraint(m.T, rule=lambda m, t:
                               m.p_gen[t] + m.p_grid[t] + m.p_dchg[t] == load[t] + m.p_chg[t])

    # ── Generation availability: used + curtailed == solar + wind output ──
    m.gen_avail = pyo.Constraint(m.T, rule=lambda m, t:
                                 m.p_gen[t] + m.p_curt[t] == solar_pu[t] * m.solar_mw + wind_pu[t] * m.wind_mw)

    # ── Grid-connection tracker + hard site ceiling ──────────────────────
    m.peak_track = pyo.Constraint(m.T, rule=lambda m, t: m.p_grid[t] <= m.peak_gc)
    m.grid_ceiling = pyo.Constraint(m.T, rule=lambda m, t: m.p_grid[t] <= params.site_max_grid_mw)

    # ── BESS power, duration and SOC dynamics ────────────────────────────
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

    # ── SSR target: grid import energy ≤ (1 − SSR) · demand ──────────────
    m.ssr_con = pyo.Constraint(
        expr=sum(m.p_grid[t] for t in m.T) * dt <= (1.0 - ssr_target_pct / 100.0) * total_demand)

    # ── Objective ────────────────────────────────────────────────────────
    #   min_bess: least battery to hit the SSR (grid connection reported) — this is
    #             the frontier that varies with SSR and reveals the land split.
    #   min_grid: least grid connection (BESS as tiebreak) — degenerates to islanding.
    if objective == "min_grid":
        m.obj = pyo.Objective(expr=m.peak_gc + tiebreak * m.bess_mwh, sense=pyo.minimize)
    else:  # "min_bess"
        m.obj = pyo.Objective(expr=m.bess_mwh + tiebreak * m.peak_gc, sense=pyo.minimize)

    solver = create_highs_solver(time_limit_seconds=time_limit)
    try:
        results = solver.solve(m, tee=False, load_solutions=False)
        ok = (results.solver.status == pyo.SolverStatus.ok
              and results.solver.termination_condition == pyo.TerminationCondition.optimal)
    except Exception as exc:
        print(f"   ✗ land-split solver exception at SSR {ssr_target_pct}%: {exc}")
        ok = False
    if not ok:
        return {"ssr_target_pct": ssr_target_pct, "feasible": False}
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

    return {
        "ssr_target_pct": round(float(ssr_target_pct), 1),
        "feasible": True,
        "solar_mw": round(solar_mw, 2),
        "wind_mw": round(wind_mw, 2),
        "solar_ha": round(solar_ha, 1),
        "wind_ha": round(wind_ha, 1),
        "total_land_ha": round(solar_ha + wind_ha, 1),
        "solar_share_pct": round(100.0 * solar_ha / (solar_ha + wind_ha), 1) if (solar_ha + wind_ha) > 1e-9 else 0.0,
        "bess_mw": round(bess_mw, 2),
        "bess_mwh": round(bess_mwh, 2),
        "gcmin_mw": round(peak_gc, 2),
        "achieved_ssr_pct": round(achieved_ssr, 2),
        "curtailed_mwh": round(curtailed, 1),
        "curtailment_pct": round(100.0 * curtailed / gen_total, 1) if gen_total > 1e-9 else 0.0,
    }


def land_split_frontier(
    df: pd.DataFrame,
    params: PhysicalParams,
    *,
    total_area_ha: float,
    ssr_targets_pct=(50.0, 60.0, 70.0, 80.0, 90.0),
    objective: str = "min_bess",
    solar_density_mw_per_ha: float = SOLAR_MW_PER_HA,
    wind_density_mw_per_ha: float = WIND_MW_PER_HA,
    time_limit: int = 120,
) -> pd.DataFrame:
    """Sweep SSR targets → one row per target with the optimal land split, BESS and
    grid connection. Infeasible targets are KEPT (feasible=False) so a missing target
    is visible, not silently dropped."""
    objective = objective if objective in ("min_bess", "min_grid") else "min_bess"
    # min_grid degenerates to full islanding — identical for every SSR — so solve it
    # ONCE (at the strictest requested SSR as a floor) instead of repeating the row.
    targets = [max(ssr_targets_pct)] if objective == "min_grid" else list(ssr_targets_pct)
    rows = [solve_land_split(df, params, total_area_ha=total_area_ha, ssr_target_pct=float(s),
                             objective=objective,
                             solar_density_mw_per_ha=solar_density_mw_per_ha,
                             wind_density_mw_per_ha=wind_density_mw_per_ha, time_limit=time_limit)
            for s in targets]
    cols = ["ssr_target_pct", "feasible", "achieved_ssr_pct", "gcmin_mw", "solar_mw", "wind_mw",
            "solar_ha", "wind_ha", "total_land_ha", "solar_share_pct",
            "bess_mw", "bess_mwh", "curtailed_mwh", "curtailment_pct"]
    return pd.DataFrame(rows, columns=cols)


def grid_battery_tradeoff(
    df: pd.DataFrame,
    params: PhysicalParams,
    *,
    total_area_ha: float,
    battery_mwh_steps,
    solar_density_mw_per_ha: float = SOLAR_MW_PER_HA,
    wind_density_mw_per_ha: float = WIND_MW_PER_HA,
    time_limit: int = 120,
) -> pd.DataFrame:
    """For each battery-energy cap, co-optimise the solar/wind split to MINIMISE the
    grid connection. Reveals how much battery buys how much grid reduction (a small
    battery often cuts most of the grid; islanding to 0 costs a big one)."""
    rows = []
    for cap in battery_mwh_steps:
        r = solve_land_split(df, params, total_area_ha=total_area_ha, ssr_target_pct=0.0,
                             objective="min_grid", bess_mwh_cap=float(cap),
                             solar_density_mw_per_ha=solar_density_mw_per_ha,
                             wind_density_mw_per_ha=wind_density_mw_per_ha, time_limit=time_limit)
        r["battery_cap_mwh"] = round(float(cap), 1)
        rows.append(r)
    cols = ["battery_cap_mwh", "feasible", "gcmin_mw", "bess_mw", "bess_mwh",
            "solar_ha", "wind_ha", "solar_share_pct", "achieved_ssr_pct", "curtailment_pct"]
    return pd.DataFrame(rows, columns=cols)
