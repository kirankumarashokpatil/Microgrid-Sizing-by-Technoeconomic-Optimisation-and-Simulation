"""
Rule-Based Causal Dispatch — Model R
------------------------------------
The auditable behind-the-meter controller the CEO brief mandates: a hardcoded
priority order applied at every timestep, with NO foresight.

    Merit order ("grid as last resort"):
      1. Generation → Load        (direct self-consumption)
      2. Generation → BESS        (charge from SURPLUS generation only)
      3. BESS → Load              (discharge to cover residual load)
      4. Grid → Load              (residual import, capped by the connection)

Policy decision (DESIGN_REVIEW.md §1): the rule NEVER charges the BESS from the
grid. The battery charges only from surplus generation. Any load that cannot be
served within the grid connection ceiling is reported as unmet — never hidden.

This module is the single source of truth for operational SSR/SCR/GCmin:
  - run_rule_dispatch()  → a canonical FlowsFrame
  - compute_flow_kpis()  → KPIs computed ONLY from a FlowsFrame
  - validate_flows()     → physics invariants that must hold on every FlowsFrame
  - size_by_bisection()  → minimum BESS that meets a target UNDER THE RULE
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from optimizer.params import PhysicalParams
from optimizer.schema import (
    FLOW_REQUIRED_COLUMNS,
    CurveCols,
    FlowCols,
    KpiKeys,
    require_columns,
)


# ──────────────────────────────────────────────────────────────────────────────
# Model R — causal rule dispatch
# ──────────────────────────────────────────────────────────────────────────────

def run_rule_dispatch(
    profiles_df: pd.DataFrame,
    params: PhysicalParams,
    *,
    pv_mw: float,
    bess_mw: float,
    bess_mwh: float,
    grid_ceiling_mw: float,
    mode: str = "self_sufficiency",
    allow_grid_charge: bool = False,
) -> pd.DataFrame:
    """
    Simulate the causal BTM merit order over the full profile and return a
    canonical FlowsFrame (see optimizer.schema.FlowCols).

    Parameters
    ----------
    profiles_df : DataFrame with columns 'timestamp', 'load_mw', 'pv_pu'.
    params      : PhysicalParams (dt, efficiencies, SOC limits, initial SOC).
    pv_mw       : PV nameplate (MW). 0 for the BESS-only sub-scenario.
    bess_mw     : BESS power rating (MW).
    bess_mwh    : BESS energy capacity (MWh).
    grid_ceiling_mw : grid connection ceiling (MW). Load above this that the
                      BESS cannot cover is shed (reported as unmet).
    mode :
        "self_sufficiency" → discharge whenever there is residual load
                             (maximise SSR). Used for SSR-target scenarios.
        "peak_shaving"     → discharge ONLY for the portion of load above the
                             grid ceiling, reserving charge for the peaks.
    allow_grid_charge :
        If True, the BESS may charge from the grid using the headroom below the
        ceiling (Grid → BESS). Per DESIGN_REVIEW.md §1 this is enabled ONLY for
        the no-PV sub-scenario; the main scenarios keep it False ("grid as last
        resort"). Only meaningful with mode="peak_shaving".
    """
    dt    = params.dt_hours
    eta_c = params.eff_charge
    eta_d = params.eff_discharge

    soc_min = bess_mwh * params.min_soc_pct / 100.0
    soc_max = bess_mwh * params.max_soc_pct / 100.0
    soc     = min(max(bess_mwh * params.initial_soc_pct / 100.0, soc_min), soc_max)

    load_arr = profiles_df[FlowCols.LOAD_MW].to_numpy(dtype=float)
    pv_arr   = profiles_df["pv_pu"].to_numpy(dtype=float) * pv_mw
    n = len(profiles_df)

    pv_used   = np.zeros(n)
    grid_imp  = np.zeros(n)
    charge    = np.zeros(n)
    discharge = np.zeros(n)
    soc_out   = np.zeros(n)
    curtail   = np.zeros(n)
    unmet     = np.zeros(n)

    def _max_charge(soc_now, already):
        """Charge power headroom (MW) given SOC and power already committed."""
        by_energy = (soc_max - soc_now) / (eta_c * dt) if eta_c * dt > 0 else 0.0
        return max(0.0, min(bess_mw - already, by_energy))

    for t in range(n):
        load     = load_arr[t]
        pv_avail = pv_arr[t]

        # 1. Generation → Load
        pv_to_load    = min(pv_avail, load)
        residual_load = load - pv_to_load
        pv_surplus    = pv_avail - pv_to_load

        c = d = c_grid = 0.0
        if pv_surplus > 0.0:
            # 2. Generation → BESS (surplus only)
            c = min(pv_surplus, _max_charge(soc, 0.0))
            soc += c * eta_c * dt
            curtail[t] = pv_surplus - c
        elif residual_load > 0.0:
            # 3. BESS → Load. In peak-shaving mode only shave above the ceiling,
            #    reserving stored energy for the peaks (causal — no foresight).
            want_d = residual_load if mode != "peak_shaving" else max(0.0, residual_load - grid_ceiling_mw)
            avail_energy    = soc - soc_min
            max_d_by_energy = avail_energy * eta_d / dt if dt > 0 else 0.0
            d = min(want_d, bess_mw, max(0.0, max_d_by_energy))
            soc -= d / eta_d * dt
            residual_load -= d

        # 4. Grid → Load (capped by the connection ceiling; rest is shed)
        g = min(residual_load, grid_ceiling_mw)
        shed = residual_load - g

        # 4b. Grid → BESS (gated): charge from the headroom below the ceiling.
        #     Only when not discharging this step (load below the ceiling).
        if allow_grid_charge and d == 0.0 and pv_surplus <= 0.0:
            headroom = grid_ceiling_mw - g
            if headroom > 0.0:
                c_grid = min(headroom, _max_charge(soc, c))
                soc += c_grid * eta_c * dt

        pv_used[t]   = pv_to_load + c
        charge[t]    = c + c_grid
        discharge[t] = d
        grid_imp[t]  = g + c_grid
        unmet[t]     = shed
        soc_out[t]   = soc

    return pd.DataFrame({
        FlowCols.TIMESTAMP:    profiles_df[FlowCols.TIMESTAMP].to_numpy(),
        FlowCols.LOAD_MW:      load_arr,
        FlowCols.PV_AVAIL_MW:  pv_arr,
        FlowCols.PV_USED_MW:   pv_used,
        FlowCols.GRID_IMP_MW:  grid_imp,
        FlowCols.CHARGE_MW:    charge,
        FlowCols.DISCHARGE_MW: discharge,
        FlowCols.SOC_MWH:      soc_out,
        FlowCols.CURTAIL_MW:   curtail,
        FlowCols.UNMET_MW:     unmet,
        FlowCols.EXPORT_MW:    np.zeros(n),   # BTM import-only: never exports
    })


# ──────────────────────────────────────────────────────────────────────────────
# Unified KPIs — computed ONLY from a FlowsFrame (single source of truth)
# ──────────────────────────────────────────────────────────────────────────────

def compute_flow_kpis(flows: pd.DataFrame, dt_hours: float) -> dict:
    """
    Compute SSR / SCR / GCmin / OSR and energy totals from a FlowsFrame.
    This is the ONLY place these KPIs are defined — every caller routes here.
    """
    require_columns(flows, FLOW_REQUIRED_COLUMNS, "FlowsFrame")

    total_demand   = flows[FlowCols.LOAD_MW].sum()     * dt_hours
    total_grid     = flows[FlowCols.GRID_IMP_MW].sum() * dt_hours
    total_unmet    = flows[FlowCols.UNMET_MW].sum()    * dt_hours
    total_pv_avail = flows[FlowCols.PV_AVAIL_MW].sum() * dt_hours
    total_pv_used  = flows[FlowCols.PV_USED_MW].sum()  * dt_hours
    total_curtail  = flows[FlowCols.CURTAIL_MW].sum()  * dt_hours

    served_onsite = total_demand - total_grid - total_unmet
    served_load   = total_demand - total_unmet

    ssr = (served_onsite / total_demand * 100.0) if total_demand > 0 else 0.0
    scr = (total_pv_used / total_pv_avail * 100.0) if total_pv_avail > 1e-9 else 0.0
    osr = (total_curtail / total_pv_avail * 100.0) if total_pv_avail > 1e-9 else 0.0
    reliability = (served_load / total_demand * 100.0) if total_demand > 0 else 0.0

    return {
        KpiKeys.SSR:               round(ssr, 2),
        KpiKeys.SCR:               round(scr, 2),
        KpiKeys.OSR:               round(osr, 2),
        KpiKeys.GCMIN_P95:         round(flows[FlowCols.GRID_IMP_MW].quantile(0.95), 2),
        KpiKeys.GCMIN_PEAK:        round(flows[FlowCols.GRID_IMP_MW].max(), 2),
        KpiKeys.TOTAL_DEMAND:      round(total_demand, 1),
        KpiKeys.TOTAL_GRID_IMPORT: round(total_grid, 1),
        KpiKeys.TOTAL_UNMET_LOAD:  round(total_unmet, 1),
        KpiKeys.SERVED_LOAD:       round(served_load, 1),
        KpiKeys.RELIABILITY:       round(reliability, 2),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Invariants — physics checks that MUST hold on every FlowsFrame
# ──────────────────────────────────────────────────────────────────────────────

def validate_flows(
    flows: pd.DataFrame,
    params: PhysicalParams,
    *,
    bess_mwh: float,
    export_limit_mw: float = 0.0,
    tol: float = 1e-6,
) -> None:
    """
    Assert the FlowsFrame is physically consistent. Raises ValueError listing
    every violated invariant. Cheap enough to run after every dispatch.
    """
    require_columns(flows, FLOW_REQUIRED_COLUMNS, "FlowsFrame")
    f = flows
    issues: list[str] = []

    # 1. Energy balance at every step:
    #    pv_used + grid + discharge + unmet == load + charge + export
    lhs = f[FlowCols.PV_USED_MW] + f[FlowCols.GRID_IMP_MW] + f[FlowCols.DISCHARGE_MW] + f[FlowCols.UNMET_MW]
    rhs = f[FlowCols.LOAD_MW] + f[FlowCols.CHARGE_MW] + f[FlowCols.EXPORT_MW]
    bal = (lhs - rhs).abs()
    if (bal > tol).any():
        issues.append(f"energy balance violated at {(bal > tol).sum()} steps (max {bal.max():.2e})")

    # 2. Non-negativity
    for col in (FlowCols.GRID_IMP_MW, FlowCols.CHARGE_MW, FlowCols.DISCHARGE_MW,
                FlowCols.CURTAIL_MW, FlowCols.UNMET_MW, FlowCols.PV_USED_MW):
        if (f[col] < -tol).any():
            issues.append(f"{col} has negative values (min {f[col].min():.2e})")

    # 3. SOC within [min, max]
    soc_min = bess_mwh * params.min_soc_pct / 100.0
    soc_max = bess_mwh * params.max_soc_pct / 100.0
    if (f[FlowCols.SOC_MWH] < soc_min - tol).any() or (f[FlowCols.SOC_MWH] > soc_max + tol).any():
        issues.append(f"SOC out of [{soc_min:.3f}, {soc_max:.3f}] MWh band")

    # 4. No simultaneous charge & discharge
    both = (f[FlowCols.CHARGE_MW] > tol) & (f[FlowCols.DISCHARGE_MW] > tol)
    if both.any():
        issues.append(f"simultaneous charge+discharge at {both.sum()} steps")

    # 5. Export within ceiling
    if (f[FlowCols.EXPORT_MW] > export_limit_mw + tol).any():
        issues.append(f"export exceeds limit {export_limit_mw} MW")

    # 6. Curtailment accounting: pv_used + curtailed == pv_available
    pv_resid = (f[FlowCols.PV_USED_MW] + f[FlowCols.CURTAIL_MW] - f[FlowCols.PV_AVAIL_MW]).abs()
    if (pv_resid > tol).any():
        issues.append(f"PV accounting (used+curtailed != available) off at {(pv_resid > tol).sum()} steps")

    if issues:
        raise ValueError("FlowsFrame invariants violated:\n  - " + "\n  - ".join(issues))


# ──────────────────────────────────────────────────────────────────────────────
# Bisection sizing — minimum BESS that meets a target UNDER THE RULE (Model R)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RuleSizingResult:
    pv_mw: float
    bess_mw: float
    bess_mwh: float
    grid_ceiling_mw: float
    kpis: dict
    feasible: bool


def size_by_bisection(
    profiles_df: pd.DataFrame,
    params: PhysicalParams,
    *,
    pv_mw: float,
    target_type: str,            # "ssr" | "peak_shaving"
    target_value: float,         # SSR % (ssr) | grid ceiling MW (peak_shaving)
    duration_h: float = 4.0,     # E/P ratio used to derive MWh from MW
    grid_ceiling_mw: float | None = None,   # for ssr: the import ceiling (site max)
    max_bess_mw: float | None = None,
    tol_mw: float = 0.5,
    max_iter: int = 30,
) -> RuleSizingResult:
    """
    Find the minimum BESS power (MW, at fixed E/P duration) that meets the target
    when operated by the causal rule. BESS size → SSR (and → lower peak) is
    monotone, so a clean bisection converges in ~log2(range/tol) rule-sims.

    This is the operationally-honest sizing number (Model R), to be reported
    alongside the LP lower bound (Model O).
    """
    max_bess_mw = max_bess_mw if max_bess_mw is not None else params.site_max_bess_mw
    ceiling = grid_ceiling_mw if grid_ceiling_mw is not None else params.site_max_grid_mw
    if target_type == "peak_shaving":
        ceiling = target_value

    # Dispatch policy: SSR targets maximise self-sufficiency; peak-shaving targets
    # reserve charge for peaks. The no-PV sub-scenario must grid-charge to shave
    # at all (DESIGN_REVIEW.md §1, open item #4) — gated ONLY there.
    rule_mode = "peak_shaving" if target_type == "peak_shaving" else "self_sufficiency"
    grid_charge = (rule_mode == "peak_shaving") and (pv_mw <= 1e-9)

    def meets(bess_mw: float) -> tuple[bool, dict]:
        flows = run_rule_dispatch(
            profiles_df, params,
            pv_mw=pv_mw, bess_mw=bess_mw, bess_mwh=bess_mw * duration_h,
            grid_ceiling_mw=ceiling, mode=rule_mode, allow_grid_charge=grid_charge,
        )
        k = compute_flow_kpis(flows, params.dt_hours)
        if target_type == "ssr":
            return k[KpiKeys.SSR] >= target_value, k
        elif target_type == "peak_shaving":
            # peak shaving holds when no load is shed under the ceiling
            return k[KpiKeys.TOTAL_UNMET_LOAD] <= 1e-6, k
        raise ValueError(f"unknown target_type {target_type!r}")

    # Is the target even reachable at max BESS?
    ok_hi, k_hi = meets(max_bess_mw)
    if not ok_hi:
        return RuleSizingResult(pv_mw, max_bess_mw, max_bess_mw * duration_h, ceiling, k_hi, feasible=False)

    # Does it need any BESS at all?
    ok_lo, k_lo = meets(0.0)
    if ok_lo:
        return RuleSizingResult(pv_mw, 0.0, 0.0, ceiling, k_lo, feasible=True)

    lo, hi = 0.0, max_bess_mw
    k_best = k_hi
    for _ in range(max_iter):
        if hi - lo <= tol_mw:
            break
        mid = 0.5 * (lo + hi)
        ok, k = meets(mid)
        if ok:
            hi, k_best = mid, k
        else:
            lo = mid

    return RuleSizingResult(pv_mw, hi, hi * duration_h, ceiling, k_best, feasible=True)


# ──────────────────────────────────────────────────────────────────────────────
# Verification — run a FIXED design under the rule (Model R) and report KPIs
# ──────────────────────────────────────────────────────────────────────────────

def _mode_for(target_type: str) -> str:
    return "peak_shaving" if target_type == "peak_shaving" else "self_sufficiency"


def verify_sizing_with_rule(
    profiles_df: pd.DataFrame,
    params: PhysicalParams,
    *,
    pv_mw: float,
    bess_mw: float,
    bess_mwh: float,
    target_type: str,
    grid_ceiling_mw: float,
) -> tuple[dict, pd.DataFrame]:
    """
    Operate a fixed (already-sized) design under the causal rule and return
    (kpis, flows). The flows are validated against the physics invariants.

    Mode/grid-charge follow the same policy as size_by_bisection: SSR designs
    run self-sufficiency; peak-shaving designs reserve charge for peaks; the
    no-PV sub-scenario is allowed to grid-charge (and nothing else is).
    """
    mode = _mode_for(target_type)
    grid_charge = (mode == "peak_shaving") and (pv_mw <= 1e-9)
    flows = run_rule_dispatch(
        profiles_df, params,
        pv_mw=pv_mw, bess_mw=bess_mw, bess_mwh=bess_mwh,
        grid_ceiling_mw=grid_ceiling_mw, mode=mode, allow_grid_charge=grid_charge,
    )
    validate_flows(flows, params, bess_mwh=bess_mwh, export_limit_mw=params.export_limit_mw)
    return compute_flow_kpis(flows, params.dt_hours), flows


def attach_operational_kpis(
    curve_df: pd.DataFrame,
    profiles_df: pd.DataFrame,
    params: PhysicalParams,
) -> pd.DataFrame:
    """
    For every feasible row of a Phase-1 sizing curve (Model O / LP), re-run the
    design under the causal rule (Model R) and attach the operational KPIs plus
    the SSR gap. Cheap (~5 ms/row), so it runs on the whole curve.

    Adds columns: Operational SSR/SCR/Peak Grid/Unmet, and SSR Gap O−R (pp).

    The causal rule models grid-connected BTM. Off-grid (F) and standalone-export
    (G) topologies have different grid semantics, so they are left unverified
    (LP columns only) rather than given misleading operational numbers.
    """
    if curve_df is None or curve_df.empty:
        return curve_df
    if getattr(params, "site_topology", "grid_connected_btm") != "grid_connected_btm":
        return curve_df

    op_ssr, op_scr, op_peak, op_grid, op_unmet, op_gap = [], [], [], [], [], []
    for _, row in curve_df.iterrows():
        feasible = bool(row.get(CurveCols.FEASIBLE, False))
        bess_mwh = row.get(CurveCols.BESS_MWH, None)
        if not feasible or bess_mwh is None or pd.isna(bess_mwh):
            op_ssr.append(None); op_scr.append(None); op_peak.append(None)
            op_grid.append(None); op_unmet.append(None); op_gap.append(None)
            continue

        target_type = row.get(CurveCols.TARGET_TYPE, "ssr")
        pv_mw   = float(row.get(CurveCols.PV_MW, 0) or 0)
        bess_mw = float(row.get(CurveCols.BESS_MW, 0) or 0)
        # Grid ceiling: peak-shaving designs operate against their GC target;
        # SSR designs run open (site grid limit) so we measure SSR, not shed.
        if _mode_for(target_type) == "peak_shaving":
            ceiling = float(row.get(CurveCols.TARGET_GC_MW, None)
                            or row.get(CurveCols.PEAK_GC_MW, params.site_max_grid_mw))
        else:
            ceiling = params.site_max_grid_mw

        kpis, _ = verify_sizing_with_rule(
            profiles_df, params,
            pv_mw=pv_mw, bess_mw=bess_mw, bess_mwh=float(bess_mwh),
            target_type=target_type, grid_ceiling_mw=ceiling,
        )
        lp_ssr = float(row.get(CurveCols.ACHIEVED_SSR_PCT, 0) or 0)
        op_ssr.append(kpis[KpiKeys.SSR])
        op_scr.append(kpis[KpiKeys.SCR])
        op_peak.append(kpis[KpiKeys.GCMIN_PEAK])
        op_grid.append(kpis[KpiKeys.TOTAL_GRID_IMPORT])
        op_unmet.append(kpis[KpiKeys.TOTAL_UNMET_LOAD])
        op_gap.append(round(lp_ssr - kpis[KpiKeys.SSR], 2))

    out = curve_df.copy()
    out[CurveCols.OP_SSR_PCT]    = op_ssr
    out[CurveCols.OP_SCR_PCT]    = op_scr
    out[CurveCols.OP_PEAK_GC_MW] = op_peak
    out[CurveCols.OP_GRID_MWH]   = op_grid
    out[CurveCols.OP_UNMET_MWH]  = op_unmet
    out[CurveCols.OP_SSR_GAP_PP] = op_gap
    return out
