"""
Rolling-Horizon Dispatch — Model W
----------------------------------
The realistic middle between the two existing models:

    Model O  (solver.py / LP)     — perfect foresight over the whole year.
                                    Optimistic LOWER BOUND on the battery needed.
    Model W  (this module)        — LIMITED foresight: at each window start, look
                                    ahead `horizon_h`, optimise dispatch, COMMIT the
                                    first `commit_h`, roll forward carrying SOC.
                                    What a real forecast-driven controller achieves.
    Model R  (rule_dispatch.py)   — zero foresight, causal merit order.
                                    Conservative, fully auditable truth.

Model W operates a FIXED design (already-sized PV + BESS). Like Model R it emits a
canonical FlowsFrame (schema.FlowCols), so every KPI/validation/export path reuses
unchanged — SSR/SCR/GCmin/OSR all come from compute_flow_kpis(), never redefined.

Each window is a small linear program (≈7 vars/timestep) solved with SciPy's HiGHS.
The per-window matrices are built ONCE per window length and reused across the year;
only the right-hand sides (load, PV, initial SOC) change per solve, so a full 8760
year is thousands of tiny warm LP solves rather than one giant model.

Objective (per window), minimise:
    Σ grid_import                       — grid drawn is the thing SSR minimises
  + M · Σ unmet                         — load shed is heavily penalised (M ≫ 1)
  + ε · Σ (charge + discharge)          — tiny, breaks ties against pointless cycling
  − λ · soc[end]                        — terminal value of stored energy, so the
                                          battery is not myopically dumped at the
                                          window edge (λ < 1 ⇒ never hoards over
                                          serving a real deficit).

Because unmet is penalised and the grid connection is a hard cap, the horizon LP
will RESERVE battery energy ahead of a forecast peak above the ceiling — that
peak-shaving foresight is exactly the value Model W captures over Model R.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import linprog
from scipy.sparse import csr_matrix

from core.params import PhysicalParams
from core.schema import FlowCols


# Objective weights (see module docstring).
_UNMET_PENALTY = 1.0e5     # M — must dominate grid import so load is served first
_CYCLE_EPS     = 1.0e-4    # ε — discourage round-tripping with no benefit
_TERMINAL_LAM  = 0.9       # λ — value of stored energy at the window edge (< 1)

# Variable layout within one timestep of a window (7 vars/step):
_PVL, _PVC, _G, _D, _U, _S, _GC = range(7)
_NV = 7


def _build_window_template(
    h: int,
    *,
    dt: float,
    eta_c: float,
    eta_d: float,
    bess_mw: float,
    soc_min: float,
    soc_max: float,
    ceiling: float,
    allow_grid_charge: bool,
):
    """Build the parts of a length-`h` window LP that DON'T change across windows:
    A_eq, A_ub, the b_ub entries that are constant, the variable bounds, and the
    cost vector. Returns everything the per-window solve needs; only the load, PV
    availability and the initial SOC vary between calls (fed into _solve_window)."""
    n = _NV * h

    # ── Equalities: load balance (h rows) + SOC dynamics (h rows) ──────────────
    eq_rows, eq_cols, eq_vals = [], [], []
    for i in range(h):
        b = _NV * i
        # Load balance: pv_load + discharge + grid + unmet = load[i]
        for off, coef in ((_PVL, 1.0), (_D, 1.0), (_G, 1.0), (_U, 1.0)):
            eq_rows.append(i); eq_cols.append(b + off); eq_vals.append(coef)
        # SOC dynamics: soc[i] - soc[i-1] - eta_c·dt·(pvc+gc) + dt/eta_d·dis = soc_start·[i==0]
        r = h + i
        eq_rows.append(r); eq_cols.append(b + _S);  eq_vals.append(1.0)
        eq_rows.append(r); eq_cols.append(b + _PVC); eq_vals.append(-eta_c * dt)
        eq_rows.append(r); eq_cols.append(b + _GC);  eq_vals.append(-eta_c * dt)
        eq_rows.append(r); eq_cols.append(b + _D);   eq_vals.append(dt / eta_d if eta_d > 0 else 0.0)
        if i > 0:
            pb = _NV * (i - 1)
            eq_rows.append(r); eq_cols.append(pb + _S); eq_vals.append(-1.0)
    A_eq = csr_matrix((eq_vals, (eq_rows, eq_cols)), shape=(2 * h, n))

    # ── Inequalities (≤): PV avail (h) + grid ceiling (h) + charge power (h) ────
    ub_rows, ub_cols, ub_vals = [], [], []
    for i in range(h):
        b = _NV * i
        # pv_load + pv_charge ≤ pv_avail[i]     (rhs varies → set per window)
        ub_rows += [i, i];         ub_cols += [b + _PVL, b + _PVC]; ub_vals += [1.0, 1.0]
        # grid + grid_charge ≤ ceiling
        ub_rows += [h + i, h + i]; ub_cols += [b + _G, b + _GC];    ub_vals += [1.0, 1.0]
        # pv_charge + grid_charge ≤ bess_mw     (charge power cap)
        ub_rows += [2 * h + i, 2 * h + i]; ub_cols += [b + _PVC, b + _GC]; ub_vals += [1.0, 1.0]
    A_ub = csr_matrix((ub_vals, (ub_rows, ub_cols)), shape=(3 * h, n))
    b_ub_const = np.empty(3 * h)
    b_ub_const[h:2 * h]     = ceiling      # grid ceiling rows (constant)
    b_ub_const[2 * h:3 * h] = bess_mw      # charge power rows (constant)
    # rows [0:h] (PV avail) are filled per window from the PV slice.

    # ── Bounds per variable ────────────────────────────────────────────────────
    gc_hi = bess_mw if allow_grid_charge else 0.0
    bounds = []
    for _ in range(h):
        bounds += [
            (0.0, None),            # pv_load
            (0.0, bess_mw),         # pv_charge
            (0.0, None),            # grid
            (0.0, bess_mw),         # discharge
            (0.0, None),            # unmet
            (soc_min, soc_max),     # soc
            (0.0, gc_hi),           # grid_charge
        ]

    # ── Cost vector ────────────────────────────────────────────────────────────
    c = np.zeros(n)
    for i in range(h):
        b = _NV * i
        c[b + _G]  = 1.0                 # grid to load  → import
        c[b + _GC] = 1.0                 # grid to bess  → import
        c[b + _U]  = _UNMET_PENALTY
        c[b + _PVC] = _CYCLE_EPS
        c[b + _D]   = _CYCLE_EPS
    c[_NV * (h - 1) + _S] -= _TERMINAL_LAM   # value terminal stored energy

    return A_eq, A_ub, b_ub_const, bounds, c


def _solve_window(template, load, pv_avail, soc_start):
    """Solve one horizon LP given the prebuilt template and this window's data.
    Returns the flat solution vector, or None if the solve failed."""
    A_eq, A_ub, b_ub_const, bounds, c = template
    h = len(load)
    b_eq = np.zeros(2 * h)
    b_eq[:h] = load                      # load-balance RHS
    b_eq[h] = soc_start                  # first SOC row carries the entry SOC
    b_ub = b_ub_const.copy()
    b_ub[:h] = pv_avail                  # PV-availability RHS
    res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                  bounds=bounds, method="highs")
    return res.x if res.success else None


def run_rolling_horizon_dispatch(
    profiles_df: pd.DataFrame,
    params: PhysicalParams,
    *,
    pv_mw: float,
    bess_mw: float,
    bess_mwh: float,
    grid_ceiling_mw: float,
    horizon_h: float = 24.0,
    commit_h: float = 1.0,
    allow_grid_charge: bool | None = None,
) -> pd.DataFrame:
    """
    Operate a FIXED design under rolling-horizon foresight (Model W) and return a
    canonical FlowsFrame.

    horizon_h : look-ahead window length (hours). Default 24 (day-ahead).
    commit_h  : how much of each window is locked in before re-optimising (hours).
                Default 1. commit_h ≤ horizon_h.
    allow_grid_charge : None ⇒ off (grid as last resort, SSR default). True lets the
                horizon LP valley-fill the battery from the grid below the ceiling.

    The FlowsFrame columns and sign conventions match run_rule_dispatch exactly, so
    compute_flow_kpis / validate_flows / the Excel export consume it unchanged.
    """
    dt    = params.dt_hours
    eta_c = params.eff_charge
    eta_d = params.eff_discharge
    gc    = bool(allow_grid_charge) if allow_grid_charge is not None else False

    soc_min = bess_mwh * params.min_soc_pct / 100.0
    soc_max = bess_mwh * params.max_soc_pct / 100.0
    soc     = min(max(bess_mwh * params.initial_soc_pct / 100.0, soc_min), soc_max)

    load_arr = profiles_df[FlowCols.LOAD_MW].to_numpy(dtype=float)
    pv_arr   = profiles_df["pv_pu"].to_numpy(dtype=float) * pv_mw
    n = len(profiles_df)

    H = max(1, int(round(horizon_h / dt)))
    C = max(1, min(int(round(commit_h / dt)), H))

    out = {k: np.zeros(n) for k in ("pv_used", "grid", "charge", "discharge",
                                    "soc", "curtail", "unmet")}

    templates: dict[int, tuple] = {}   # cache LP templates by window length

    t0 = 0
    while t0 < n:
        h = min(H, n - t0)
        if h not in templates:
            templates[h] = _build_window_template(
                h, dt=dt, eta_c=eta_c, eta_d=eta_d, bess_mw=bess_mw,
                soc_min=soc_min, soc_max=soc_max, ceiling=grid_ceiling_mw,
                allow_grid_charge=gc)
        x = _solve_window(templates[h], load_arr[t0:t0 + h], pv_arr[t0:t0 + h], soc)

        c_commit = min(C, h)
        if x is None:
            # Infeasible/failed solve (should not happen — unmet is always a valve).
            # Fail safe: import to the ceiling, shed the rest, hold SOC.
            for j in range(c_commit):
                t = t0 + j
                served_grid = min(load_arr[t], grid_ceiling_mw)
                out["grid"][t]  = served_grid
                out["unmet"][t] = max(0.0, load_arr[t] - served_grid)
                out["curtail"][t] = pv_arr[t]
                out["soc"][t]   = soc
            t0 += c_commit
            continue

        for j in range(c_commit):
            t = t0 + j
            b = _NV * j
            pvl = x[b + _PVL]; pvc = x[b + _PVC]; g = x[b + _G]
            d = x[b + _D]; u = x[b + _U]; s = x[b + _S]; gch = x[b + _GC]
            # Snap sub-tolerance numerical dust and any (never-optimal) simultaneous
            # charge/discharge so the physics invariants hold cleanly.
            charge_tot = pvc + gch
            if charge_tot > 1e-7 and d > 1e-7:
                m = min(charge_tot, d)
                charge_tot -= m; d -= m; pvc = max(0.0, charge_tot - gch)
            out["pv_used"][t]   = pvl + pvc
            out["grid"][t]      = g + gch          # TOTAL grid import (load + charge)
            out["charge"][t]    = charge_tot
            out["discharge"][t] = d
            out["unmet"][t]     = max(0.0, u)
            out["curtail"][t]   = max(0.0, pv_arr[t] - pvl - pvc)
            out["soc"][t]       = s
            soc = s                                # carry SOC to the next window
        t0 += c_commit

    return pd.DataFrame({
        FlowCols.TIMESTAMP:    profiles_df[FlowCols.TIMESTAMP].to_numpy(),
        FlowCols.LOAD_MW:      load_arr,
        FlowCols.PV_AVAIL_MW:  pv_arr,
        FlowCols.PV_USED_MW:   out["pv_used"],
        FlowCols.GRID_IMP_MW:  out["grid"],
        FlowCols.CHARGE_MW:    out["charge"],
        FlowCols.DISCHARGE_MW: out["discharge"],
        FlowCols.SOC_MWH:      out["soc"],
        FlowCols.CURTAIL_MW:   out["curtail"],
        FlowCols.UNMET_MW:     out["unmet"],
        FlowCols.EXPORT_MW:    np.zeros(n),   # BTM import-only: never exports
    })


def size_under_rolling(
    profiles_df: pd.DataFrame,
    params: PhysicalParams,
    *,
    pv_mw: float,
    target_type: str,            # "ssr" | "peak_shaving"
    target_value: float,         # SSR % (ssr) | grid ceiling MW (peak_shaving)
    duration_h: float = 4.0,
    grid_ceiling_mw: float | None = None,
    max_bess_mw: float | None = None,
    horizon_h: float = 24.0,
    tol_mw: float = 0.5,
    max_iter: int = 25,
):
    """
    Minimum BESS that meets the target when operated under rolling-horizon foresight
    (Model W) — the "forecast-deliverable" size, the W analogue of Model R's
    size_by_bisection. Returns a RuleSizingResult (pv/bess_mw/bess_mwh/kpis/feasible).

    Energy-driven for both objectives here (MWh = MW·duration_h); the feasibility
    pre-check at max BESS returns fast when the target is unreachable (no fine scan).

    Speed: the search runs in BLOCK mode (commit == horizon). Because the within-window
    "forecast" is the actual data, block mode reproduces the fine-commit KPIs (verified)
    while being ~20× faster, so a full bisection is a few seconds, not minutes.
    """
    from core.rule_dispatch import (
        compute_flow_kpis, _smallest_meeting, RuleSizingResult, KpiKeys as _KK,
    )

    max_bess_mw = max_bess_mw if max_bess_mw is not None else params.site_max_bess_mw
    ceiling = grid_ceiling_mw if grid_ceiling_mw is not None else params.site_max_grid_mw
    if target_type == "peak_shaving":
        ceiling = target_value
    gc_override = getattr(params, "allow_grid_charge", None)
    grid_charge = gc_override if gc_override is not None else (target_type == "peak_shaving")

    def meets(mw: float):
        mwh = mw * duration_h
        flows = run_rolling_horizon_dispatch(
            profiles_df, params, pv_mw=pv_mw, bess_mw=mw, bess_mwh=mwh,
            grid_ceiling_mw=ceiling, horizon_h=horizon_h, commit_h=horizon_h,  # block mode
            allow_grid_charge=grid_charge)
        k = compute_flow_kpis(flows, params.dt_hours)
        ok = (k[_KK.SSR] >= target_value) if target_type == "ssr" \
            else (k[_KK.TOTAL_UNMET_LOAD] <= 1e-6)
        return ok, k

    ok_hi, k_hi = meets(max_bess_mw)
    if not ok_hi:   # unreachable even at the site's max battery
        return RuleSizingResult(pv_mw, max_bess_mw, max_bess_mw * duration_h,
                                ceiling, k_hi, feasible=False)
    ok_lo, k_lo = meets(0.0)
    if ok_lo:       # target met with no battery at all
        return RuleSizingResult(pv_mw, 0.0, 0.0, ceiling, k_lo, feasible=True)
    mw, k_best = _smallest_meeting(lambda x: meets(x), 0.0, max_bess_mw, tol_mw, max_iter)
    return RuleSizingResult(pv_mw, mw, mw * duration_h, ceiling, k_best, feasible=True)


def verify_sizing_with_rolling(
    profiles_df: pd.DataFrame,
    params: PhysicalParams,
    *,
    pv_mw: float,
    bess_mw: float,
    bess_mwh: float,
    target_type: str,
    grid_ceiling_mw: float,
    horizon_h: float = 24.0,
    commit_h: float = 1.0,
) -> tuple[dict, pd.DataFrame]:
    """
    Operate a fixed design under Model W and return (kpis, flows), validated against
    the physics invariants — the Model-W analogue of verify_sizing_with_rule.
    Grid-charging follows the same policy: on only for peak-shaving.
    """
    from core.rule_dispatch import compute_flow_kpis, validate_flows

    gc_override = getattr(params, "allow_grid_charge", None)
    grid_charge = gc_override if gc_override is not None else (target_type == "peak_shaving")
    flows = run_rolling_horizon_dispatch(
        profiles_df, params, pv_mw=pv_mw, bess_mw=bess_mw, bess_mwh=bess_mwh,
        grid_ceiling_mw=grid_ceiling_mw, horizon_h=horizon_h, commit_h=commit_h,
        allow_grid_charge=grid_charge,
    )
    validate_flows(flows, params, bess_mwh=bess_mwh, export_limit_mw=params.export_limit_mw)
    return compute_flow_kpis(flows, params.dt_hours), flows
