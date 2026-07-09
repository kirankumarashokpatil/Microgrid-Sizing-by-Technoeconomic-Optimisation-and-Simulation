"""
Curve Runner Module (Phase 1)
------------------------------
The sweep orchestrator. Runs hundreds of LP solves to trace the full
sizing curves defined in the DIP Phase 1 specification:

  Scenario A : Main · SSR Target · BESS Sizing
               PV fixed → sweep SSR target → BESS MW + MWh curve

  Scenario B : Main · Peak Shaving · BESS Sizing
               PV fixed → sweep GC target → BESS MW + MWh curve

  Scenario C : Main · SSR Target · PV+BESS Co-Sizing (surface)
               Sweep PV size × SSR target → BESS MW + MWh surface

  Scenario D : Sub-Scenario · Peak Shaving (no PV)
               PV = 0 → sweep GC target → BESS MW + MWh curve

Each scenario calls solve_sizing_point() once per sweep step, collects
the results, and returns a DataFrame (the "sizing curve").
"""

from __future__ import annotations

import time
from typing import Sequence

import numpy as np
import pandas as pd

from core.params import PhysicalParams, SizingResult
from core.schema import CurveCols
from core.sizing_engine import (
    find_gc_min,
    find_ssr_max,
    solve_sizing_point,
)


# ──────────────────────────────────────────────────────────────────────────────
# Scenario A — Main · SSR Target · BESS Sizing
# ──────────────────────────────────────────────────────────────────────────────

def run_ssr_curve(
    df: pd.DataFrame,
    params: PhysicalParams,
    pv_mw_fixed: float,
    scenario_label: str = "Main · SSR Target",
    solver_time_limit: int = 120,
    ssr_start_pct: float | None = None,
    n_points: int | None = None,
) -> pd.DataFrame:
    """
    Sweep SSR targets from ssr_start (default = one step) up to ssr_max and find
    the minimum BESS (MWh, tie-broken by MW) for each target.

    ssr_start_pct lets the caller skip the trivial low-SSR region (where the
    no-battery generation already meets the target, so BESS = 0) and spend the
    sweep budget where the battery actually does work. Pass the no-battery
    baseline SSR to start right where storage begins to matter.

    n_points, if given, places exactly that many targets EVENLY across the
    feasible band [ssr_start, ssr_max] — so every scenario is sampled at the same
    resolution regardless of how wide its band is (the defensible default). Else
    a fixed step (ssr_sweep_step_pct) is used.

    Returns
    -------
    pd.DataFrame with CurveCols columns.
    """
    start = ssr_start_pct if ssr_start_pct is not None else params.ssr_sweep_step_pct
    mode = f"{n_points} points across band" if n_points else f"step {params.ssr_sweep_step_pct:.1f}%"
    print(f"\n{'='*65}")
    print(f"  Scenario: {scenario_label}")
    print(f"  PV fixed at {pv_mw_fixed:.1f} MW  |  Sweep {start:.0f}% → max, {mode}")
    print(f"{'='*65}")

    # Step 1: find the maximum reachable SSR with this PV
    print("\n  [Step 1/2] Finding SSR_max …")
    t0 = time.time()
    ssr_max = find_ssr_max(df, params, pv_mw_fixed, solver_time_limit)
    print(f"  → SSR_max = {ssr_max:.1f}%  ({time.time()-t0:.1f}s)")

    if ssr_max < start:
        print(f"  ⚠ SSR_max ({ssr_max:.1f}%) < sweep start ({start:.0f}%) — no feasible SSR targets.")
        return pd.DataFrame(columns=_curve_columns())

    # Step 2: build the target list — either N points across the feasible band
    # (consistent resolution per scenario) or a fixed step up to the ceiling.
    if n_points and n_points >= 2:
        targets = list(np.linspace(start, ssr_max, n_points))
    else:
        targets = list(np.arange(start, ssr_max, params.ssr_sweep_step_pct))
    targets = sorted(set(round(t, 1) for t in targets))
    if not targets or abs(targets[-1] - ssr_max) > 0.2:
        targets.append(round(ssr_max, 1))

    print(f"\n  [Step 2/2] Sweeping {len(targets)} SSR targets: "
          f"{targets[0]:.0f}% … {targets[-1]:.0f}%")

    rows = []
    for i, ssr_target in enumerate(targets):
        t0 = time.time()
        result = solve_sizing_point(
            df=df,
            params=params,
            pv_mw_fixed=pv_mw_fixed,
            target_type="ssr",
            target_value=ssr_target,
            scenario_label=f"{scenario_label} | SSR={ssr_target:.0f}%",
            solver_time_limit=solver_time_limit,
        )
        elapsed = time.time() - t0
        status = "✓" if result.feasible else "✗"
        print(
            f"  {status} [{i+1:2d}/{len(targets)}] SSR {ssr_target:5.1f}%  →  "
            f"BESS {result.bess_mw:6.1f} MW / {result.bess_mwh:7.1f} MWh  "
            f"({result.bess_duration_h:.1f}h)  peak_gc={result.peak_grid_mw:.1f} MW  "
            f"[{elapsed:.1f}s]"
        )
        rows.append(_result_to_row(result, scenario_label))

    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Scenario B — Main · Peak Shaving · BESS Sizing
# ──────────────────────────────────────────────────────────────────────────────

def run_peak_shaving_curve(
    df: pd.DataFrame,
    params: PhysicalParams,
    pv_mw_fixed: float,
    scenario_label: str = "Main · Peak Shaving",
    solver_time_limit: int = 120,
) -> pd.DataFrame:
    """
    Sweep grid connection targets from peak_demand down to gc_min and find
    the minimum BESS (MW, tie-broken by MWh) for each target.

    Returns
    -------
    pd.DataFrame with CurveCols columns.
    """
    print(f"\n{'='*65}")
    print(f"  Scenario: {scenario_label}")
    print(f"  PV fixed at {pv_mw_fixed:.1f} MW  |  Sweep step: {params.gc_sweep_step_mw:.0f} MW")
    print(f"{'='*65}")

    peak_demand = float(df["load_mw"].max())
    print(f"\n  Peak demand in profiles = {peak_demand:.2f} MW")

    # Step 1: find the minimum reachable GC with max BESS + this PV
    print("\n  [Step 1/2] Finding GC_min …")
    t0 = time.time()
    gc_min = find_gc_min(df, params, pv_mw_fixed, solver_time_limit)
    # Round gc_min down to nearest gc step to avoid missing the minimum
    gc_min = max(1.0, round(gc_min, 0))
    print(f"  → GC_min = {gc_min:.1f} MW  ({time.time()-t0:.1f}s)")

    # Step 2: sweep targets from peak_demand down to gc_min
    # (no battery at peak_demand, maximum battery at gc_min)
    targets = np.arange(peak_demand, gc_min, -params.gc_sweep_step_mw)
    targets = [round(t, 1) for t in targets if t >= gc_min]
    # Always include gc_min as the last point
    if not targets or abs(targets[-1] - gc_min) > 0.5:
        targets.append(gc_min)

    print(f"\n  [Step 2/2] Sweeping {len(targets)} GC targets: "
          f"{targets[0]:.0f} MW … {targets[-1]:.0f} MW")

    rows = []
    for i, gc_target in enumerate(targets):
        t0 = time.time()
        result = solve_sizing_point(
            df=df,
            params=params,
            pv_mw_fixed=pv_mw_fixed,
            target_type="peak_shaving",
            target_value=gc_target,
            scenario_label=f"{scenario_label} | GC={gc_target:.0f}MW",
            solver_time_limit=solver_time_limit,
        )
        elapsed = time.time() - t0
        status = "✓" if result.feasible else "✗"
        print(
            f"  {status} [{i+1:2d}/{len(targets)}] GC ≤ {gc_target:5.1f} MW  →  "
            f"BESS {result.bess_mw:6.1f} MW / {result.bess_mwh:7.1f} MWh  "
            f"({result.bess_duration_h:.1f}h)  peak_gc={result.peak_grid_mw:.1f} MW  "
            f"[{elapsed:.1f}s]"
        )
        rows.append(_result_to_row(result, scenario_label))

    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Scenario C — Main · PV + BESS Co-Sizing Surface
# ──────────────────────────────────────────────────────────────────────────────

def run_pv_bess_surface(
    df: pd.DataFrame,
    params: PhysicalParams,
    pv_sweep_mw: Sequence[float],
    ssr_targets_pct: Sequence[float],
    scenario_label: str = "Main · PV+BESS Surface",
    solver_time_limit: int = 120,
) -> pd.DataFrame:
    """
    Run a grid of LP solves over (PV nameplate × SSR target) to build
    the 3D sizing surface: PV_MW × SSR → BESS_MWh.

    For each PV size, we first check the maximum reachable SSR and skip
    any target above that bound.

    Returns
    -------
    pd.DataFrame with CurveCols columns.
    One row per (pv_mw, ssr_target) combination.
    """
    print(f"\n{'='*65}")
    print(f"  Scenario: {scenario_label}")
    print(f"  PV sweep: {list(pv_sweep_mw)} MW")
    print(f"  SSR targets: {list(ssr_targets_pct)} %")
    total = len(pv_sweep_mw) * len(ssr_targets_pct)
    print(f"  Total LP solves (max): {total}")
    print(f"{'='*65}")

    rows = []
    solve_n = 0
    for pv_mw in pv_sweep_mw:
        print(f"\n  ── PV = {pv_mw:.0f} MW ──")
        ssr_max = find_ssr_max(df, params, pv_mw, solver_time_limit)
        print(f"     SSR_max = {ssr_max:.1f}%")

        for ssr in ssr_targets_pct:
            solve_n += 1
            if ssr > ssr_max:
                print(f"     ✗ SSR {ssr:.0f}% > SSR_max ({ssr_max:.1f}%) — skipped (infeasible)")
                # Still record it as infeasible so the surface is complete
                rows.append({
                    CurveCols.SCENARIO:         scenario_label,
                    CurveCols.TARGET_TYPE:      "ssr",
                    CurveCols.TARGET_SSR_PCT:   ssr,
                    CurveCols.TARGET_GC_MW:     None,
                    CurveCols.PV_MW:            pv_mw,
                    CurveCols.BESS_MW:          None,
                    CurveCols.BESS_MWH:         None,
                    CurveCols.BESS_DURATION_H:  None,
                    CurveCols.PEAK_GC_MW:       None,
                    CurveCols.ACHIEVED_SSR_PCT: None,
                    CurveCols.ACHIEVED_SCR_PCT: None,
                    CurveCols.FEASIBLE:         False,
                })
                continue

            t0 = time.time()
            result = solve_sizing_point(
                df=df,
                params=params,
                pv_mw_fixed=pv_mw,
                target_type="ssr",
                target_value=ssr,
                scenario_label=f"{scenario_label} | PV={pv_mw:.0f}MW SSR={ssr:.0f}%",
                solver_time_limit=solver_time_limit,
            )
            elapsed = time.time() - t0
            status = "✓" if result.feasible else "✗"
            print(
                f"     {status} SSR {ssr:5.1f}%  →  "
                f"BESS {result.bess_mw:6.1f} MW / {result.bess_mwh:7.1f} MWh  "
                f"[{elapsed:.1f}s]"
            )
            rows.append(_result_to_row(result, scenario_label))

    print(f"\n  Surface complete: {solve_n} points ({sum(r[CurveCols.FEASIBLE] for r in rows)} feasible)")
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Scenario D — Sub-Scenario · Peak Shaving (no PV)
# ──────────────────────────────────────────────────────────────────────────────

def run_sub_peak_shaving_curve(
    df: pd.DataFrame,
    params: PhysicalParams,
    scenario_label: str = "Sub-Scenario · BESS+Load (no PV)",
    solver_time_limit: int = 120,
) -> pd.DataFrame:
    """
    BESS + Load only — no on-site generation.
    PV is set to 0. The BESS charges from the grid and time-shifts
    energy to flatten the load profile.

    Identical logic to run_peak_shaving_curve with pv_mw_fixed=0.

    Returns
    -------
    pd.DataFrame with CurveCols columns.
    """
    return run_peak_shaving_curve(
        df=df,
        params=params,
        pv_mw_fixed=0.0,
        scenario_label=scenario_label,
        solver_time_limit=solver_time_limit,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Scenario E — Co-Optimization (SSR Target + GC Limit)
# ──────────────────────────────────────────────────────────────────────────────

def run_co_opt_scenario(
    df: pd.DataFrame,
    params: PhysicalParams,
    pv_mw_fixed: float,
    ssr_targets_pct: Sequence[float],
    target_gc_mw: float,
    scenario_label: str = "Co-Optimization · SSR + GC Limit",
    solver_time_limit: int = 120,
) -> pd.DataFrame:
    """
    Simultaneous Co-Optimization.
    Fixes the grid connection limit (target_gc_mw) and sweeps SSR targets.
    Finds the minimum BESS required to hit the SSR target without breaching the GC limit.
    """
    print(f"\n{'='*65}")
    print(f"  Scenario: {scenario_label}")
    print(f"  PV fixed: {pv_mw_fixed:.1f} MW  |  GC fixed limit: {target_gc_mw:.1f} MW")
    print(f"  Sweeping {len(ssr_targets_pct)} SSR targets")
    print(f"{'='*65}")

    rows = []
    for i, ssr in enumerate(ssr_targets_pct):
        t0 = time.time()
        result = solve_sizing_point(
            df=df,
            params=params,
            pv_mw_fixed=pv_mw_fixed,
            target_type="co_opt",
            target_value=ssr,
            target_gc_mw=target_gc_mw,
            scenario_label=f"{scenario_label} | SSR={ssr:.0f}% GC≤{target_gc_mw:.0f}MW",
            solver_time_limit=solver_time_limit,
        )
        elapsed = time.time() - t0
        status = "✓" if result.feasible else "✗"
        print(
            f"  {status} [{i+1:2d}/{len(ssr_targets_pct)}] SSR {ssr:5.1f}%  →  "
            f"BESS {result.bess_mw:6.1f} MW / {result.bess_mwh:7.1f} MWh  "
            f"[{elapsed:.1f}s]"
        )
        rows.append(_result_to_row(result, scenario_label))

    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Scenario F — Off-Grid (Islanded)
# ──────────────────────────────────────────────────────────────────────────────

def run_off_grid_curve(
    df: pd.DataFrame,
    params: PhysicalParams,
    firmness_target_pct: float,
    pv_sweep_mw: Sequence[float],
    scenario_label: str = "Off-Grid · Firmness Target",
    solver_time_limit: int = 120,
) -> pd.DataFrame:
    """
    Off-grid system. Sweeps PV capacities and finds the minimum BESS required
    to achieve the firmness target (e.g. 99% of load met).
    """
    print(f"\n{'='*65}")
    print(f"  Scenario: {scenario_label}")
    print(f"  Target Firmness: {firmness_target_pct:.1f}%")
    print(f"{'='*65}")

    rows = []
    for i, pv_mw in enumerate(pv_sweep_mw):
        t0 = time.time()
        result = solve_sizing_point(
            df=df,
            params=params,
            pv_mw_fixed=pv_mw,
            target_type="firmness",
            target_value=firmness_target_pct,
            scenario_label=f"{scenario_label} | PV={pv_mw:.0f}MW",
            solver_time_limit=solver_time_limit,
        )
        elapsed = time.time() - t0
        status = "✓" if result.feasible else "✗"
        print(
            f"  {status} [{i+1:2d}/{len(pv_sweep_mw)}] PV {pv_mw:5.1f} MW  →  "
            f"BESS {result.bess_mw:6.1f} MW / {result.bess_mwh:7.1f} MWh  "
            f"[{elapsed:.1f}s]"
        )
        rows.append(_result_to_row(result, scenario_label))

    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Scenario G — Standalone Generation (No Load)
# ──────────────────────────────────────────────────────────────────────────────

def run_standalone_export_curve(
    df: pd.DataFrame,
    params: PhysicalParams,
    pv_mw_fixed: float,
    curtailment_targets_pct: Sequence[float],
    scenario_label: str = "Standalone Gen · Curtailment Target",
    solver_time_limit: int = 120,
) -> pd.DataFrame:
    """
    Standalone PV + BESS (Front-of-Meter). Sweeps allowed curtailment targets.
    Finds the minimum BESS required to capture the generation given the
    export_limit_mw defined in params.
    """
    print(f"\n{'='*65}")
    print(f"  Scenario: {scenario_label}")
    print(f"  PV Fixed: {pv_mw_fixed:.1f} MW | Export Limit: {params.export_limit_mw:.1f} MW")
    print(f"{'='*65}")

    rows = []
    for i, curt_target in enumerate(curtailment_targets_pct):
        t0 = time.time()
        result = solve_sizing_point(
            df=df,
            params=params,
            pv_mw_fixed=pv_mw_fixed,
            target_type="curtailment",
            target_value=curt_target,
            scenario_label=f"{scenario_label} | MaxCurt={curt_target:.1f}%",
            solver_time_limit=solver_time_limit,
        )
        elapsed = time.time() - t0
        status = "✓" if result.feasible else "✗"
        print(
            f"  {status} [{i+1:2d}/{len(curtailment_targets_pct)}] Max Curt {curt_target:5.1f}%  →  "
            f"BESS {result.bess_mw:6.1f} MW / {result.bess_mwh:7.1f} MWh  "
            f"[{elapsed:.1f}s]"
        )
        rows.append(_result_to_row(result, scenario_label))

    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _curve_columns() -> list[str]:
    return [
        CurveCols.SCENARIO,
        CurveCols.TARGET_TYPE,
        CurveCols.TARGET_SSR_PCT,
        CurveCols.TARGET_GC_MW,
        CurveCols.PV_MW,
        CurveCols.BESS_MW,
        CurveCols.BESS_MWH,
        CurveCols.BESS_DURATION_H,
        CurveCols.PEAK_GC_MW,
        CurveCols.PEAK_EXPORT_MW,
        CurveCols.ACHIEVED_SSR_PCT,
        CurveCols.ACHIEVED_SCR_PCT,
        CurveCols.ACHIEVED_OSR_PCT,
        CurveCols.EXPORTED_MWH,
        CurveCols.CURTAILED_MWH,
        CurveCols.FEASIBLE,
    ]


def _result_to_row(result: SizingResult, scenario_label: str) -> dict:
    return {
        CurveCols.SCENARIO:         scenario_label,
        CurveCols.TARGET_TYPE:      result.target_type,
        CurveCols.TARGET_SSR_PCT:   result.target_value if result.target_type in ("ssr", "co_opt", "firmness", "curtailment") else None,
        CurveCols.TARGET_GC_MW:     result.target_gc_mw if result.target_type == "co_opt" else (result.target_value if result.target_type == "peak_shaving" else None),
        CurveCols.PV_MW:            result.pv_mw,
        CurveCols.BESS_MW:          result.bess_mw if result.feasible else None,
        CurveCols.BESS_MWH:         result.bess_mwh if result.feasible else None,
        CurveCols.BESS_DURATION_H:  result.bess_duration_h if result.feasible else None,
        CurveCols.PEAK_GC_MW:       result.peak_grid_mw if result.feasible else None,
        CurveCols.PEAK_EXPORT_MW:   result.peak_export_mw if result.feasible else None,
        CurveCols.ACHIEVED_SSR_PCT: result.achieved_ssr_pct if result.feasible else None,
        CurveCols.ACHIEVED_SCR_PCT: result.achieved_scr_pct if result.feasible else None,
        CurveCols.ACHIEVED_OSR_PCT: result.achieved_osr_pct if result.feasible else None,
        CurveCols.EXPORTED_MWH:     result.exported_mwh if result.feasible else None,
        CurveCols.CURTAILED_MWH:    result.total_curtailed_mwh if result.feasible else None,
        CurveCols.FEASIBLE:         result.feasible,
    }
