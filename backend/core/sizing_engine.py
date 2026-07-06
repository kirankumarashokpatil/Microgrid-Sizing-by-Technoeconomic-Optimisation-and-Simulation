"""
Sizing Engine Module — Phase 1
-------------------------------
Builds and solves ONE pure-physics LP per target point.

No CAPEX. No prices. No discount rates.

The objective is to find the MINIMUM BESS size (MWh or MW, depending on
the sizing objective) that allows the system to meet a given physical target:

  A) SSR target       → Minimize BESS_MWh subject to SSR ≥ target
  B) Peak shaving     → Minimize BESS_MW  subject to peak_grid ≤ target

Both objectives add a small penalty to break degeneracy and satisfy
the spec's tie-breaker rules (§ "Sizing objectives / The trade-off").

Energy flows modelled (BTM, no export):
  PV → Load (direct self-consumption)
  PV → BESS (charge from generation)
  BESS → Load (discharge to serve load)
  Grid → Load (residual import)
  Grid → BESS (allowed only when load < grid_limit — implicit via balance)
  Curtailment when PV > Load + available BESS charging headroom
"""

from __future__ import annotations

import pyomo.environ as pyo
import pandas as pd

from core.params import PhysicalParams, SizingResult
from core.solver import create_highs_solver


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def solve_sizing_point(
    df: pd.DataFrame,
    params: PhysicalParams,
    pv_mw_fixed: float,
    target_type: str,
    target_value: float,
    target_gc_mw: float | None = None,
    scenario_label: str = "",
    solver_time_limit: int = 120,
    pv_variable: bool = False,
    pv_max_mw: float | None = None,
    pv_weight: float = 700.0 / 300.0,
    bess_fixed_mw: float | None = None,
    bess_fixed_mwh: float | None = None,
) -> SizingResult:
    """
    Solve ONE sizing LP for a specific target and return a SizingResult.

    Parameters
    ----------
    df : pd.DataFrame
        Columns: load_mw (MW), pv_pu (0–1 p.u.)
        Rows: one per timestep, ordered chronologically.
    params : PhysicalParams
        Physical site + BESS constraints.
    pv_mw_fixed : float
        PV nameplate size (MW) — fixed input.
        Set to 0.0 for sub-scenario (BESS-only, no generation).
        Ignored when pv_variable=True (PV becomes an LP decision variable).
    pv_variable : bool
        Promote PV nameplate to an LP decision variable (the registry's
        NEEDS_PV_VARIABLE capability). The objective gains pv_weight·pv_mw so
        the LP trades PV against BESS.
    pv_max_mw : float | None
        Upper bound on the PV variable — the land/site ceiling (ρ_s·A_avail).
        None ⇒ a generous default (20× peak load).
    pv_weight : float
        Objective weight of 1 MW PV relative to 1 MWh BESS. Default mirrors the
        default CAPEX ratio (€700k/MW PV ÷ €300k/MWh BESS) so "min PV+BESS"
        approximates min CAPEX; callers can pass their own ratio.
    bess_fixed_mw / bess_fixed_mwh : float | None
        Pin the BESS to a given size (equality constraints) — used by frontier
        sweeps that vary BESS externally.
    target_type : str
        "ssr"           → SSR target sizing (minimise BESS_MWh)
        "peak_shaving"  → Grid connection target (minimise BESS_MW)
        "co_opt"        → Simultaneous SSR & GC constraints
    target_value : float
        For "ssr": target SSR in % (e.g. 60.0).
        For "peak_shaving": target grid connection in MW (e.g. 50.0).
    scenario_label : str
        Human-readable label for console output.
    solver_time_limit : int
        HiGHS time limit in seconds per solve.

    Returns
    -------
    SizingResult
        The minimum-physical-size solution. feasible=False if infeasible.
    """
    _validate_inputs(df, params, pv_mw_fixed, target_type, target_value)

    model = _build_lp(df, params, pv_mw_fixed, target_type, target_value, target_gc_mw,
                      pv_variable=pv_variable, pv_max_mw=pv_max_mw, pv_weight=pv_weight,
                      bess_fixed_mw=bess_fixed_mw, bess_fixed_mwh=bess_fixed_mwh)
    solver = create_highs_solver(time_limit_seconds=solver_time_limit)
    results = None
    try:
        results = solver.solve(model, tee=False, load_solutions=False)
        ok = (
            results.solver.status == pyo.SolverStatus.ok
            and results.solver.termination_condition == pyo.TerminationCondition.optimal
        )
        if ok:
            model.solutions.load_from(results)
    except Exception as e:
        ok = False
        print(f"   ✗ Solver exception: {e}")

    if not ok:
        cond = getattr(results.solver, "termination_condition", "unknown") if results is not None else "exception"
        label = scenario_label or f"{target_type}={target_value}"
        print(f"   ✗ [{label}] Infeasible / solver failed: {cond}")
        return SizingResult(
            scenario_name=scenario_label,
            pv_mw=pv_mw_fixed,
            bess_mw=0.0,
            bess_mwh=0.0,
            peak_grid_mw=0.0,
            target_type=target_type,
            target_value=target_value,
            target_gc_mw=target_gc_mw,
            achieved_ssr_pct=0.0,
            bess_duration_h=0.0,
            exported_mwh=0.0,
            peak_export_mw=0.0,
            feasible=False,
        )

    # Extract solution values
    bess_mw  = max(0.0, pyo.value(model.bess_mw))
    bess_mwh = max(0.0, pyo.value(model.bess_mwh))
    duration = (bess_mwh / bess_mw) if bess_mw > 1e-3 else 0.0
    # Realized PV: the LP's choice when variable, else the fixed input.
    realized_pv = max(0.0, pyo.value(model.pv_mw_var)) if pv_variable else pv_mw_fixed

    # Verify achieved SSR from LP variables
    dt = params.dt_hours
    total_demand_mwh = sum(df["load_mw"].iloc[t] for t in model.T) * dt
    total_grid_mwh   = sum(max(0.0, pyo.value(model.p_grid[t])) for t in model.T) * dt
    total_unmet_mwh  = sum(max(0.0, pyo.value(model.p_unmet[t])) for t in model.T) * dt
    achieved_ssr = (
        (total_demand_mwh - total_grid_mwh - total_unmet_mwh) / total_demand_mwh * 100.0
        if total_demand_mwh > 0 else 0.0
    )

    peak_grid = max(max(0.0, pyo.value(model.p_grid[t])) for t in model.T)

    total_export_mwh = sum(max(0.0, pyo.value(model.p_export[t])) for t in model.T) * dt
    peak_export = max(max(0.0, pyo.value(model.p_export[t])) for t in model.T)

    # Self-consumption ratio (SCR) = PV used on-site / PV available.
    # First-class output per the CEO Strategic Brief (SCR alongside SSR).
    total_pv_avail_mwh = sum(df["pv_pu"].iloc[t] for t in model.T) * realized_pv * dt
    total_pv_used_mwh  = sum(max(0.0, pyo.value(model.p_pv[t])) for t in model.T) * dt
    achieved_scr = (
        total_pv_used_mwh / total_pv_avail_mwh * 100.0
        if total_pv_avail_mwh > 1e-9 else 0.0
    )

    return SizingResult(
        scenario_name=scenario_label,
        pv_mw=round(realized_pv, 4),
        bess_mw=round(bess_mw, 4),
        bess_mwh=round(bess_mwh, 4),
        peak_grid_mw=round(peak_grid, 4),
        target_type=target_type,
        target_value=target_value,
        target_gc_mw=target_gc_mw,
        achieved_ssr_pct=round(achieved_ssr, 2),
        achieved_scr_pct=round(achieved_scr, 2),
        bess_duration_h=round(duration, 2),
        exported_mwh=round(total_export_mwh, 4),
        peak_export_mw=round(peak_export, 4),
        feasible=True,
    )


def find_ssr_max(
    df: pd.DataFrame,
    params: PhysicalParams,
    pv_mw_fixed: float,
    solver_time_limit: int = 120,
) -> float:
    """
    Find the maximum achievable SSR with the given PV size and site limits.
    Runs ONE LP that maximises achieved SSR (by minimising grid + unmet).

    Returns the max achievable SSR as a percentage (0–100).
    """
    model = _build_lp(
        df, params, pv_mw_fixed,
        target_type="find_max_ssr",
        target_value=0.0,       # no target — free to minimise grid
    )
    solver = create_highs_solver(time_limit_seconds=solver_time_limit)
    results = None
    try:
        results = solver.solve(model, tee=False, load_solutions=False)
        ok = (
            results.solver.status == pyo.SolverStatus.ok
            and results.solver.termination_condition == pyo.TerminationCondition.optimal
        )
        if ok:
            model.solutions.load_from(results)
    except Exception:
        ok = False

    if not ok:
        cond = getattr(results.solver, "termination_condition", "unknown") if results is not None else "exception"
        # Fail loud: this value sets the SSR sweep ceiling. A silent default would
        # corrupt the entire curve that flows into an IC pack (docs/DESIGN_REVIEW.md §3).
        raise RuntimeError(f"[find_ssr_max] solver failed ({cond}) — refusing to "
                           f"guess SSR_max; fix the model/data and re-run.")

    dt = params.dt_hours
    total_demand = sum(df["load_mw"].iloc[t] for t in model.T) * dt
    total_grid   = sum(max(0.0, pyo.value(model.p_grid[t])) for t in model.T) * dt
    total_unmet  = sum(max(0.0, pyo.value(model.p_unmet[t])) for t in model.T) * dt
    ssr_max = (total_demand - total_grid - total_unmet) / total_demand * 100.0 if total_demand > 0 else 0.0
    return round(max(0.0, min(100.0, ssr_max)), 1)


def find_gc_min(
    df: pd.DataFrame,
    params: PhysicalParams,
    pv_mw_fixed: float,
    solver_time_limit: int = 120,
    bess_fixed_mw: float | None = None,
    bess_fixed_mwh: float | None = None,
) -> float:
    """
    Find the minimum possible grid connection (MW) with maximum BESS and PV.
    This is the lower bound for the peak-shaving curve sweep.

    Pass bess_fixed_mw/mwh to pin the battery instead (the S55 frontier sweeps
    BESS sizes externally and asks for GCmin at each).

    Returns the minimum achievable peak grid import in MW.
    """
    model = _build_lp(
        df, params, pv_mw_fixed,
        target_type="find_min_gc",
        target_value=0.0,       # no target — free to minimise peak
        bess_fixed_mw=bess_fixed_mw,
        bess_fixed_mwh=bess_fixed_mwh,
    )
    solver = create_highs_solver(time_limit_seconds=solver_time_limit)
    results = None
    try:
        results = solver.solve(model, tee=False, load_solutions=False)
        ok = (
            results.solver.status == pyo.SolverStatus.ok
            and results.solver.termination_condition == pyo.TerminationCondition.optimal
        )
        if ok:
            model.solutions.load_from(results)
    except Exception:
        ok = False

    if not ok:
        cond = getattr(results.solver, "termination_condition", "unknown") if results is not None else "exception"
        # Fail loud: this value sets the peak-shaving sweep floor (docs/DESIGN_REVIEW.md §3).
        raise RuntimeError(f"[find_gc_min] solver failed ({cond}) — refusing to "
                           f"guess GC_min; fix the model/data and re-run.")

    # Read the peak_gc variable that was added by the find_min_gc objective builder
    if hasattr(model, "peak_gc"):
        peak_gc = max(0.0, pyo.value(model.peak_gc))
    else:
        peak_gc = max(max(0.0, pyo.value(model.p_grid[t])) for t in model.T)
    return round(peak_gc, 1)


# ──────────────────────────────────────────────────────────────────────────────
# LP Builder — Internal
# ──────────────────────────────────────────────────────────────────────────────

def _build_lp(
    df: pd.DataFrame,
    params: PhysicalParams,
    pv_mw_fixed: float,
    target_type: str,
    target_value: float,
    target_gc_mw: float | None = None,
    pv_variable: bool = False,
    pv_max_mw: float | None = None,
    pv_weight: float = 700.0 / 300.0,
    bess_fixed_mw: float | None = None,
    bess_fixed_mwh: float | None = None,
) -> pyo.ConcreteModel:
    """
    Build the Pyomo LP model.

    target_type options
    -------------------
    "ssr"           → enforce SSR ≥ target_value (%), minimise BESS_MWh
    "peak_shaving"  → enforce peak_grid ≤ target_value (MW), minimise BESS_MW
    "find_max_ssr"  → no SSR constraint, use max BESS site limits, minimise grid
    "find_min_gc"   → no GC constraint, use max BESS site limits, minimise peak grid
    """
    dt = params.dt_hours
    n  = len(df)

    m   = pyo.ConcreteModel()
    m.T = pyo.RangeSet(0, n - 1)

    # ── Decision variables (hardware — scalars) ───────────────────────────
    m.bess_mw  = pyo.Var(within=pyo.NonNegativeReals, bounds=(0, params.site_max_bess_mw))
    m.bess_mwh = pyo.Var(within=pyo.NonNegativeReals, bounds=(0, params.site_max_bess_mwh))
    # Pin BESS when a frontier sweep sizes it externally.
    if bess_fixed_mw is not None:
        m.bess_mw_fix = pyo.Constraint(expr=m.bess_mw == max(0.0, bess_fixed_mw))
    if bess_fixed_mwh is not None:
        m.bess_mwh_fix = pyo.Constraint(expr=m.bess_mwh == max(0.0, bess_fixed_mwh))

    # ── Decision variables (time-series — one per timestep) ───────────────
    m.p_pv   = pyo.Var(m.T, within=pyo.NonNegativeReals)   # PV power used (MW)
    m.p_grid = pyo.Var(m.T, within=pyo.NonNegativeReals)   # Grid import (MW)
    m.p_chg  = pyo.Var(m.T, within=pyo.NonNegativeReals)   # BESS charge (MW)
    m.p_dchg = pyo.Var(m.T, within=pyo.NonNegativeReals)   # BESS discharge (MW)
    m.p_curt = pyo.Var(m.T, within=pyo.NonNegativeReals)   # Curtailed PV (MW)
    m.p_unmet= pyo.Var(m.T, within=pyo.NonNegativeReals)   # Unmet load (MW)
    m.p_export=pyo.Var(m.T, within=pyo.NonNegativeReals)   # Grid export (MW)
    m.e_bess = pyo.Var(m.T, within=pyo.NonNegativeReals)   # BESS SOC (MWh)

    # ── Precompute data arrays ────────────────────────────────────────────
    if params.site_topology == "standalone_gen":
        load_mw = df["load_mw"].values * 0.0
        total_demand_mwh = 0.0
    else:
        load_mw = df["load_mw"].values
        total_demand_mwh = load_mw.sum() * dt

    pv_pu = df["pv_pu"].values
    pv_avail = pv_pu * pv_mw_fixed                 # available PV (MW) — fixed-PV case

    # ── PV as a decision variable (NEEDS_PV_VARIABLE capability) ──────────
    # pv_pu[t] is a constant coefficient, so pv_pu[t]·pv_mw_var stays linear.
    if pv_variable:
        peak_load = float(load_mw.max()) if n else 0.0
        pv_ub = pv_max_mw if (pv_max_mw is not None and pv_max_mw > 0) \
            else max(1000.0, peak_load * 20.0)
        m.pv_mw_var = pyo.Var(within=pyo.NonNegativeReals, bounds=(0, pv_ub))

    # ── Site Topology Constraints ─────────────────────────────────────────
    if params.site_topology == "off_grid":
        m.off_grid_import_con = pyo.Constraint(m.T, rule=lambda m, t: m.p_grid[t] == 0.0)
        m.off_grid_export_con = pyo.Constraint(m.T, rule=lambda m, t: m.p_export[t] == 0.0)

    # ── Constraint 1: Power balance at every timestep ─────────────────────
    def balance_rule(m, t):
        return (
            m.p_pv[t] + m.p_grid[t] + m.p_dchg[t] + m.p_unmet[t]
            == load_mw[t] + m.p_chg[t] + m.p_export[t]
        )
    m.balance_con = pyo.Constraint(m.T, rule=balance_rule)

    # ── Constraint 2: PV availability + curtailment accounting ────────────
    if pv_variable:
        def pv_avail_rule(m, t):
            return m.p_pv[t] + m.p_curt[t] == pv_pu[t] * m.pv_mw_var
    else:
        def pv_avail_rule(m, t):
            return m.p_pv[t] + m.p_curt[t] == pv_avail[t]
    m.pv_avail_con = pyo.Constraint(m.T, rule=pv_avail_rule)

    # ── Constraint 3: Export Limits ───────────────────────────────────────
    if params.export_limit_mw >= 0:
        m.export_limit_con = pyo.Constraint(m.T, rule=lambda m, t: m.p_export[t] <= params.export_limit_mw)

    # ── Constraint 4: Grid import ceiling ────────────────────────────────
    def grid_ceiling_rule(m, t):
        return m.p_grid[t] <= params.site_max_grid_mw
    m.grid_ceiling_con = pyo.Constraint(m.T, rule=grid_ceiling_rule)

    # ── Constraint 5: BESS power limits ───────────────────────────────────
    m.chg_limit  = pyo.Constraint(m.T, rule=lambda m, t: m.p_chg[t]  <= m.bess_mw)
    m.dchg_limit = pyo.Constraint(m.T, rule=lambda m, t: m.p_dchg[t] <= m.bess_mw)

    # ── Constraint 6: Unmet load bounded by demand ────────────────────────
    m.unmet_limit = pyo.Constraint(m.T, rule=lambda m, t: m.p_unmet[t] <= load_mw[t])

    # ── Constraint 7: BESS SOC limits ─────────────────────────────────────
    min_soc_frac = params.min_soc_pct / 100.0
    max_soc_frac = params.max_soc_pct / 100.0
    m.soc_min_con = pyo.Constraint(m.T, rule=lambda m, t: m.e_bess[t] >= m.bess_mwh * min_soc_frac)
    m.soc_max_con = pyo.Constraint(m.T, rule=lambda m, t: m.e_bess[t] <= m.bess_mwh * max_soc_frac)

    # ── Constraint 8: BESS SOC dynamics ───────────────────────────────────
    eta_c = params.eff_charge
    eta_d = params.eff_discharge
    init_soc_frac = params.initial_soc_pct / 100.0

    def soc_rule(m, t):
        if t == 0:
            return (
                m.e_bess[t]
                == init_soc_frac * m.bess_mwh
                + (m.p_chg[t] * eta_c - m.p_dchg[t] / eta_d) * dt
            )
        return (
            m.e_bess[t]
            == m.e_bess[t - 1]
            + (m.p_chg[t] * eta_c - m.p_dchg[t] / eta_d) * dt
        )
    m.soc_con = pyo.Constraint(m.T, rule=soc_rule)

    # ── Constraint 9: Terminal SOC ≥ Initial SOC ──────────────────────────
    last_t = n - 1
    m.terminal_soc_con = pyo.Constraint(
        expr=m.e_bess[last_t] >= init_soc_frac * m.bess_mwh
    )

    # ── Constraint 10: BESS duration tie-breaker bounds ───────────────────
    if params.min_bess_duration_h > 0:
        m.min_dur_con = pyo.Constraint(
            expr=m.bess_mwh >= m.bess_mw * params.min_bess_duration_h
        )
    if params.max_bess_duration_h < 1e6:
        m.max_dur_con = pyo.Constraint(
            expr=m.bess_mwh <= m.bess_mw * params.max_bess_duration_h
        )

    # ── Scenario-specific target constraint ───────────────────────────────
    if target_type == "ssr":
        allowed_grid_mwh = total_demand_mwh * (1.0 - target_value / 100.0)
        m.ssr_target_con = pyo.Constraint(
            expr=sum((m.p_grid[t] + m.p_unmet[t]) * dt for t in m.T) <= allowed_grid_mwh
        )

    elif target_type == "peak_shaving":
        m.gc_target_con = pyo.Constraint(
            m.T, rule=lambda m, t: m.p_grid[t] <= target_value
        )
        
    elif target_type == "co_opt":
        allowed_grid_mwh = total_demand_mwh * (1.0 - target_value / 100.0)
        m.ssr_target_con = pyo.Constraint(
            expr=sum((m.p_grid[t] + m.p_unmet[t]) * dt for t in m.T) <= allowed_grid_mwh
        )
        if target_gc_mw is not None:
            m.gc_target_con = pyo.Constraint(
                m.T, rule=lambda m, t: m.p_grid[t] <= target_gc_mw
            )

    elif target_type == "firmness":
        # target_value is the firmness target % (e.g. 99.0 means 99% of demand is met)
        unmet_mwh = sum(m.p_unmet[t] for t in m.T) * dt
        max_unmet = (1.0 - target_value / 100.0) * total_demand_mwh
        m.firmness_con = pyo.Constraint(expr=unmet_mwh <= max_unmet)

    elif target_type in ("curtailment", "export_max"):
        # target_value is the allowable curtailment % (e.g. 5.0 means max 5% of PV is curtailed).
        # With PV variable, total PV is a linear expression of pv_mw_var — still an LP.
        curt_mwh = sum(m.p_curt[t] for t in m.T) * dt
        if pv_variable:
            total_pv_expr = float(pv_pu.sum()) * dt * m.pv_mw_var
        else:
            total_pv_expr = sum(pv_avail) * dt
        m.curtailment_con = pyo.Constraint(
            expr=curt_mwh <= (target_value / 100.0) * total_pv_expr)

    # The BESS is allowed to use full site limits.

    # ── Objective ─────────────────────────────────────────────────────────
    # With PV variable, the objective gains pv_weight·pv_mw so the LP trades PV
    # against BESS (weight defaults to the CAPEX ratio ⇒ ≈ min-CAPEX design).
    pv_term = (pv_weight * m.pv_mw_var) if pv_variable else 0.0

    if target_type in ("ssr", "co_opt", "firmness", "curtailment"):
        # Primary: minimise BESS_MWh (+ weighted PV when variable).
        # Tie-breaker: small weight on BESS_MW.
        # Tiny reward for export so solver prefers exporting to curtailing when allowed.
        def obj_rule(m):
            unmet_penalty = sum(m.p_unmet[t] for t in m.T) * 1e6
            anti_proc     = -sum(m.e_bess[t] for t in m.T) * 1e-6
            export_reward = -sum(m.p_export[t] for t in m.T) * 1e-4
            return m.bess_mwh + 0.001 * m.bess_mw + pv_term + unmet_penalty + anti_proc + export_reward
        m.obj = pyo.Objective(rule=obj_rule, sense=pyo.minimize)

    elif target_type == "peak_shaving":
        # Primary: minimise BESS_MW (+ weighted PV when variable).
        def obj_rule(m):
            unmet_penalty = sum(m.p_unmet[t] for t in m.T) * 1e6
            anti_proc     = -sum(m.e_bess[t] for t in m.T) * 1e-6
            export_reward = -sum(m.p_export[t] for t in m.T) * 1e-4
            return m.bess_mw + 0.001 * m.bess_mwh + pv_term + unmet_penalty + anti_proc + export_reward
        m.obj = pyo.Objective(rule=obj_rule, sense=pyo.minimize)

    elif target_type == "export_max":
        # Standalone generation sizing: maximise usable export within the export
        # limit, holding curtailment ≤ target% (constraint above). PV growth is
        # bounded by the curtailment cap, so the LP is bounded.
        def obj_rule(m):
            total_export = sum(m.p_export[t] for t in m.T) * dt
            size_penalty = (m.bess_mwh + m.bess_mw) * 1e-4 + pv_term * 1e-4
            return -total_export + size_penalty
        m.obj = pyo.Objective(rule=obj_rule, sense=pyo.minimize)

    elif target_type == "find_max_ssr":
        # Find the ceiling SSR: minimise total grid import with BESS freely sized.
        # No SSR constraint — solver finds the physical max by minimising grid.
        # Tiny BESS penalty to prefer smaller BESS when equal SSR is achieved.
        def obj_rule(m):
            total_grid    = sum(m.p_grid[t] for t in m.T)
            total_unmet   = sum(m.p_unmet[t] for t in m.T) * 1e6
            bess_penalty  = (m.bess_mwh + m.bess_mw) * 1e-5
            return total_grid + total_unmet + bess_penalty
        m.obj = pyo.Objective(rule=obj_rule, sense=pyo.minimize)

    elif target_type == "find_min_gc":
        # Find the floor grid connection: add peak_gc tracker variable and minimise it.
        # BESS is freely sized up to site limits.
        m.peak_gc = pyo.Var(within=pyo.NonNegativeReals, bounds=(0, params.site_max_grid_mw))
        m.peak_gc_track = pyo.Constraint(
            m.T, rule=lambda m, t: m.p_grid[t] <= m.peak_gc
        )
        def obj_rule(m):
            bess_penalty  = (m.bess_mwh + m.bess_mw) * 1e-5
            unmet_penalty = sum(m.p_unmet[t] for t in m.T) * 1e6
            return m.peak_gc + bess_penalty + unmet_penalty
        m.obj = pyo.Objective(rule=obj_rule, sense=pyo.minimize)

    return m



# ──────────────────────────────────────────────────────────────────────────────
# Validation helpers
# ──────────────────────────────────────────────────────────────────────────────

def _validate_inputs(
    df: pd.DataFrame,
    params: PhysicalParams,
    pv_mw_fixed: float,
    target_type: str,
    target_value: float,
) -> None:
    valid_types = {"ssr", "peak_shaving", "find_max_ssr", "find_min_gc", "co_opt",
                   "firmness", "curtailment", "export_max"}
    if target_type not in valid_types:
        raise ValueError(f"target_type must be one of {valid_types}, got '{target_type}'")
    if "load_mw" not in df.columns or "pv_pu" not in df.columns:
        raise ValueError("df must have columns 'load_mw' and 'pv_pu'")
    if pv_mw_fixed < 0:
        raise ValueError(f"pv_mw_fixed must be ≥ 0, got {pv_mw_fixed}")
    if target_type in ("ssr", "co_opt") and not (0 <= target_value <= 100):
        raise ValueError(f"SSR target must be 0–100%, got {target_value}")
    if target_type == "peak_shaving" and target_value < 0:
        raise ValueError(f"Peak shaving target GC must be ≥ 0 MW, got {target_value}")
