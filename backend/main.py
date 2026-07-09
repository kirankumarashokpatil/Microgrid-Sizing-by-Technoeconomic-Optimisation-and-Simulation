"""
Microgrid Optimizer — Main Entry Point
----------------------------------------
Physical sizing (the only mode):
      Reads the 8760 PV+Load profile Excel.
      Generates physical sizing curves for all scenarios.
      Output: Phase1_Sizing_Curves.xlsx

Usage:
  python main.py --profiles "data/8760_PV&Load Profiles.xlsx"
  python main.py --profiles "data/8760_PV&Load Profiles.xlsx" --pv-mw 150 --load-mw 100
  python main.py --ssr-step 5 --gc-step 5 --scenarios A B C D
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

from core.schema import CurveCols

# ── Shared defaults ────────────────────────────────────────────────────────────
_DEFAULT_PROFILES  = Path(__file__).parent / "data" / "8760_PV&Load Profiles.xlsx"
_DEFAULT_P1_OUTPUT = Path(__file__).with_name("Phase1_Sizing_Curves.xlsx")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description="Microgrid Optimizer — DIP Italy")

    # Phase 1 is the only mode now
    p.add_argument("--phase1", action="store_true", default=True,
                   help="Run Phase 1: physical sizing curves (default)")

    # ── Phase 1 args ──────────────────────────────────────────────────────
    p.add_argument("--profiles", default=str(_DEFAULT_PROFILES),
                   help="Path to the 8760 PV+Load profiles Excel file")
    p.add_argument("--pv-mw", type=float, default=None,
                   help="PV nameplate (MW). Reads from file if not set.")
    p.add_argument("--load-mw", type=float, default=None,
                   help="Load nameplate (MW). Reads from file if not set.")
    p.add_argument("--output", default=str(_DEFAULT_P1_OUTPUT),
                   help="Output Excel file for Phase 1 curves")
    p.add_argument("--ssr-step", type=float, default=5.0,
                   help="SSR sweep step size (%%). Default: 5")
    p.add_argument("--gc-step", type=float, default=5.0,
                   help="Peak-shaving GC sweep step (MW). Default: 5")
    p.add_argument("--scenarios", nargs="+", default=["A", "B", "C", "D", "E", "F", "G"],
                   help="Which scenarios to run: A B C D E F G (default: all)")
    p.add_argument("--pv-surface-points", nargs="+", type=float, default=None,
                   help="PV nameplate values (MW) for Scenario C surface.")
    p.add_argument("--solver-timeout", type=int, default=120,
                   help="HiGHS solver time limit per LP solve (seconds). Default: 120")
    p.add_argument("--resample-15min", action="store_true",
                   help="Run at 15-min resolution. Requires NATIVE 15-min profiles; "
                        "errors on hourly data (peaks are not fabricated by interpolation).")
    p.add_argument("--eol-retention", type=float, default=80.0,
                   help="BESS usable-capacity retained at ~20yr end of life (%%). "
                        "Deliverable sizes are grossed up by 1/this so the target "
                        "still holds at EoL. Default: 80")



    return p.parse_args()


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1 — Physical Sizing Curves
# ══════════════════════════════════════════════════════════════════════════════

def run_phase1(args):
    from core.profile_loader import load_profiles, compute_annual_energy
    from core.params import PhysicalParams
    from core.curve_runner import (
        run_ssr_curve,
        run_peak_shaving_curve,
        run_pv_bess_surface,
        run_sub_peak_shaving_curve,
        run_co_opt_scenario,
        run_off_grid_curve,
        run_standalone_export_curve,
    )
    from core.curve_reporter import save_phase1_report

    profiles_path = Path(args.profiles)
    if not profiles_path.exists():
        print(f"❌ Profile file not found: {profiles_path}")
        sys.exit(1)

    print("\n" + "=" * 65)
    print("  MICROGRID OPTIMIZER — PHASE 1: PHYSICAL SIZING CURVES")
    print("  DIP Italy — Behind-the-Meter Data Centre")
    print("=" * 65)

    print(f"\n📂 Loading profiles: {profiles_path.name}")
    df, load_nameplate, pv_nameplate = load_profiles(
        xlsx_path=profiles_path,
        load_mw_nameplate=args.load_mw,
        pv_mw_nameplate=args.pv_mw,
        dt_hours=0.25 if args.resample_15min else 1.0,
    )
    dt_h = 0.25 if args.resample_15min else 1.0
    annual = compute_annual_energy(df, dt_hours=dt_h)
    print(f"\n   Annual energy stats:")
    print(f"   Total load   : {annual['total_load_mwh']:,.0f} MWh/yr")
    print(f"   Peak load    : {annual['peak_load_mw']:.2f} MW")
    print(f"   PV CF (p.u.) : {annual['pv_capacity_factor']:.4f}  "
          f"→  {annual['pv_capacity_factor'] * pv_nameplate * 8760:.0f} MWh/yr at {pv_nameplate:.0f} MW")

    params = PhysicalParams(
        dt_hours=dt_h,
        eff_charge=0.95, eff_discharge=0.95,
        min_soc_pct=10.0, max_soc_pct=90.0, initial_soc_pct=50.0,
        min_bess_duration_h=2.0, max_bess_duration_h=8.0,
        site_max_bess_mw=500.0, site_max_bess_mwh=4000.0, site_max_grid_mw=200.0,
        ssr_sweep_step_pct=args.ssr_step,
        gc_sweep_step_mw=args.gc_step,
        eol_capacity_retention_pct=args.eol_retention,
    )

    import copy
    scenarios = [s.upper() for s in args.scenarios]
    timeout   = args.solver_timeout
    curve_a = curve_b = curve_c = curve_d = curve_e = curve_f = curve_g = None

    if "A" in scenarios:
        curve_a = run_ssr_curve(df, params, pv_mw_fixed=pv_nameplate,
                                scenario_label="Main · SSR Target · BESS Sizing",
                                solver_time_limit=timeout)
    if "B" in scenarios:
        curve_b = run_peak_shaving_curve(df, params, pv_mw_fixed=pv_nameplate,
                                         scenario_label="Main · Peak Shaving · BESS Sizing",
                                         solver_time_limit=timeout)
    if "C" in scenarios:
        pv_pts    = args.pv_surface_points or [50.0, 100.0, 150.0, 200.0, 250.0]
        ssr_step  = args.ssr_step
        ssr_tgts  = list(range(int(ssr_step), 90, int(ssr_step)))
        curve_c = run_pv_bess_surface(df, params, pv_sweep_mw=pv_pts,
                                       ssr_targets_pct=ssr_tgts,
                                       scenario_label="Main · PV+BESS Co-Sizing Surface",
                                       solver_time_limit=timeout)
    if "D" in scenarios:
        curve_d = run_sub_peak_shaving_curve(df, params,
                                              scenario_label="Sub-Scenario · BESS+Load (no PV)",
                                              solver_time_limit=timeout)
    if "E" in scenarios:
        ssr_step  = args.ssr_step
        ssr_tgts  = list(range(int(ssr_step), 90, int(ssr_step)))
        # BTM-01 / DC-01 use cases require hitting a target SSR and a target GC simultaneously.
        # Let's fix the GC target to 80% of peak load to demonstrate the co-optimization.
        peak_load = df["load_mw"].max()
        target_gc = max(1.0, round(peak_load * 0.8, 0))
        curve_e = run_co_opt_scenario(df, params, pv_mw_fixed=pv_nameplate,
                                      ssr_targets_pct=ssr_tgts,
                                      target_gc_mw=target_gc,
                                      scenario_label="Co-Opt · SSR + GC Limit",
                                      solver_time_limit=timeout)
    if "F" in scenarios:
        p_off = copy.copy(params)
        p_off.site_topology = "off_grid"
        pv_sweep = args.pv_surface_points or [150.0, 200.0, 250.0, 300.0, 400.0]
        curve_f = run_off_grid_curve(df, p_off, firmness_target_pct=99.0,
                                     pv_sweep_mw=pv_sweep,
                                     scenario_label="Off-Grid · 99% Firmness",
                                     solver_time_limit=timeout)
    if "G" in scenarios:
        p_std = copy.copy(params)
        p_std.site_topology = "standalone_gen"
        p_std.export_limit_mw = 50.0  # e.g., 50 MW export limit
        curt_tgts = [0.0, 5.0, 10.0, 15.0, 20.0]
        curve_g = run_standalone_export_curve(df, p_std, pv_mw_fixed=pv_nameplate,
                                              curtailment_targets_pct=curt_tgts,
                                              scenario_label="Standalone Gen · Curtailment",
                                              solver_time_limit=timeout)

    # ── Operational verification (Model R) on every curve point ───────────────
    # Attach the causal-rule SSR/SCR/peak/unmet next to the LP lower bound, plus
    # the O−R gap, so each curve carries both numbers (docs/DESIGN_REVIEW.md §1).
    from core.rule_dispatch import attach_operational_kpis, attach_rule_sizing
    print("\n  [Model R] Verifying every curve point under the causal rule …")
    # Grid-connected BTM scenarios only. F (off-grid) and G (standalone export)
    # have different grid semantics and keep LP columns only.
    curve_a = attach_operational_kpis(curve_a, df, params)
    curve_b = attach_operational_kpis(curve_b, df, params)
    curve_c = attach_operational_kpis(curve_c, df, params)
    curve_d = attach_operational_kpis(curve_d, df, params)
    curve_e = attach_operational_kpis(curve_e, df, params)

    # ── Deliverable sizing (Model R) + end-of-life gross-up ───────────────────
    # The LP size is an optimistic lower bound; re-size each point to actually
    # meet its target under the causal rule, then oversize for ~20yr fade so the
    # target still holds at end of life. These are the numbers to buy.
    print(f"  [Model R] Sizing deliverable BESS + EoL gross-up "
          f"(retention {params.eol_capacity_retention_pct:.0f}%) …")
    curve_a = attach_rule_sizing(curve_a, df, params)
    curve_b = attach_rule_sizing(curve_b, df, params)
    curve_c = attach_rule_sizing(curve_c, df, params)
    curve_d = attach_rule_sizing(curve_d, df, params)
    curve_e = attach_rule_sizing(curve_e, df, params)

    save_phase1_report(
        output_path=args.output,
        profiles_df=df,
        curve_a=curve_a, curve_b=curve_b,
        curve_c=curve_c, curve_d=curve_d,
        curve_e=curve_e, curve_f=curve_f,
        curve_g=curve_g,
    )

    from core.plotter import plot_sizing_curves
    plot_sizing_curves(Path(args.output).parent, curve_ssr=curve_a, curve_ps=curve_b, surface=curve_c)


def _print_optimum(label, opt):
    if opt is None or (hasattr(opt, "empty") and opt.empty):
        print(f"   ✗ {label}: no feasible optimum found")
        return
    bmw  = opt.get(CurveCols.BESS_MW,  0) or 0
    bmwh = opt.get(CurveCols.BESS_MWH, 0) or 0
    bh   = opt.get(CurveCols.BESS_DURATION_H, 0) or 0
    gc   = opt.get(CurveCols.PEAK_GC_MW, 0) or 0
    ssr  = opt.get(CurveCols.ACHIEVED_SSR_PCT, 0) or 0
    op_ssr = opt.get(CurveCols.OP_SSR_PCT, None)
    op_scr = opt.get(CurveCols.OP_SCR_PCT, None)
    scr  = op_scr if (op_scr is not None and not pd.isna(op_scr)) else (opt.get(CurveCols.ACHIEVED_SCR_PCT, 0) or 0)
    # Operational (Model R) SSR is the honest number; LP value shown in parens.
    ssr_txt = f"SSR(R) {op_ssr:.1f}% (LP {ssr:.1f}%)" if op_ssr is not None and not pd.isna(op_ssr) else f"SSR {ssr:.1f}%"
    print(f"   ✓ {label}: BESS {bmw:.1f} MW / {bmwh:.1f} MWh ({bh:.1f}h) | "
          f"GC {gc:.1f} MW | {ssr_txt} | SCR {scr:.1f}%")


# ══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    args = parse_args()
    run_phase1(args)


if __name__ == "__main__":
    main()

