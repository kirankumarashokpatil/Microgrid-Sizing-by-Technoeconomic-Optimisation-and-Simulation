"""
Microgrid Optimizer — Main Entry Point
----------------------------------------
Supports two execution modes:

  --phase1  (default)
      Reads the 8760 PV+Load profile Excel.
      Generates physical sizing curves (no economics) for all scenarios.
      Output: Phase1_Sizing_Curves.xlsx

  --phase2
      Reads Phase 1 curves and overlays economics.
      Finds the techno-economic optimum on each curve.
      Runs seasonal shifting, grid services, site-area analyses.
      Output: Phase2_TechnoEconomic.xlsx

Phase 1 usage:
  python main.py --phase1 --profiles "8760_PV&Load Profiles.xlsx"
  python main.py --phase1 --profiles "8760_PV&Load Profiles.xlsx" --pv-mw 150 --load-mw 100
  python main.py --phase1 --ssr-step 5 --gc-step 5 --scenarios A B C D

Phase 2 usage:
  python main.py --phase2
  python main.py --phase2 --curves Phase1_Sizing_Curves.xlsx --site-area 500000
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

from optimizer.schema import CurveCols

# ── Shared defaults ────────────────────────────────────────────────────────────
_DEFAULT_PROFILES  = Path(__file__).with_name("8760_PV&Load Profiles.xlsx")
_DEFAULT_CURVES    = Path(__file__).with_name("Phase1_Sizing_Curves.xlsx")
_DEFAULT_P1_OUTPUT = Path(__file__).with_name("Phase1_Sizing_Curves.xlsx")
_DEFAULT_P2_OUTPUT = Path(__file__).with_name("Phase2_TechnoEconomic.xlsx")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description="Microgrid Optimizer — DIP Italy")

    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--phase1", action="store_true", default=True,
                      help="Run Phase 1: physical sizing curves (default)")
    mode.add_argument("--phase2", action="store_true",
                      help="Run Phase 2: techno-economic overlay on Phase 1 curves")

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

    # ── Phase 2 args ──────────────────────────────────────────────────────
    p.add_argument("--curves", default=str(_DEFAULT_CURVES),
                   help="Path to Phase 1 Sizing Curves Excel (input to Phase 2)")
    p.add_argument("--p2-output", default=str(_DEFAULT_P2_OUTPUT),
                   help="Output Excel file for Phase 2 results")
    p.add_argument("--verify-dispatch", action="store_true",
                   help="Run Phase 2 optimum through rolling-horizon dispatch verification")

    # Economics (Phase 2 overrides — all have sensible defaults)
    p.add_argument("--cost-pv-mw",      type=float, default=700_000,
                   help="PV CAPEX (€/MW). Default: 700,000")
    p.add_argument("--cost-bess-mw",    type=float, default=150_000,
                   help="BESS power CAPEX (€/MW). Default: 150,000")
    p.add_argument("--cost-bess-mwh",   type=float, default=300_000,
                   help="BESS energy CAPEX (€/MWh). Default: 300,000")
    p.add_argument("--cost-gc-mw",      type=float, default=250_000,
                   help="Grid connection CAPEX (€/MW). Default: 250,000")
    p.add_argument("--grid-price",      type=float, default=150.0,
                   help="Grid energy import price (€/MWh). Default: 150")
    p.add_argument("--discount-rate",   type=float, default=8.0,
                   help="Nominal discount rate (%%). Default: 8")
    p.add_argument("--lifespan",        type=float, default=20.0,
                   help="Project lifespan (years). Default: 20")
    p.add_argument("--site-area",       type=float, default=None,
                   help="Available site area (m²). Default: no constraint")
    p.add_argument("--gs-reserved-soc", type=float, default=20.0,
                   help="Grid services reserved SOC (%%). Default: 20")
    p.add_argument("--gs-price",        type=float, default=200.0,
                   help="Grid services energy price (€/MWh). Default: 200")

    return p.parse_args()


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1 — Physical Sizing Curves
# ══════════════════════════════════════════════════════════════════════════════

def run_phase1(args):
    from optimizer.profile_loader import load_profiles, compute_annual_energy
    from optimizer.params import PhysicalParams
    from optimizer.curve_runner import (
        run_ssr_curve,
        run_peak_shaving_curve,
        run_pv_bess_surface,
        run_sub_peak_shaving_curve,
        run_co_opt_scenario,
        run_off_grid_curve,
        run_standalone_export_curve,
    )
    from optimizer.curve_reporter import save_phase1_report

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
    # the O−R gap, so each curve carries both numbers (DESIGN_REVIEW.md §1).
    from optimizer.rule_dispatch import attach_operational_kpis, attach_rule_sizing
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

    from optimizer.plotter import plot_sizing_curves
    plot_sizing_curves(Path(args.output).parent, curve_ssr=curve_a, curve_ps=curve_b, surface=curve_c)


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 — Techno-Economic Overlay
# ══════════════════════════════════════════════════════════════════════════════

def run_phase2(args):
    from optimizer.params import EconomicParams
    from optimizer.economic_overlay import (
        load_phase1_curves,
        evaluate_costs,
        find_optimal_point,
        cooptimise_ssr_gc,
        analyse_seasonal_shifting,
        analyse_grid_services,
        check_site_area,
    )
    from optimizer.phase2_reporter import save_phase2_report

    curves_path = Path(args.curves)
    if not curves_path.exists():
        print(f"❌ Phase 1 curves file not found: {curves_path}")
        print(f"   Run Phase 1 first: python main.py --phase1")
        sys.exit(1)

    print("\n" + "=" * 65)
    print("  MICROGRID OPTIMIZER — PHASE 2: TECHNO-ECONOMIC OVERLAY")
    print("  DIP Italy — Behind-the-Meter Data Centre")
    print("=" * 65)

    # ── Build economic parameters ──────────────────────────────────────────
    eco = EconomicParams(
        cost_pv_mw              = args.cost_pv_mw,
        cost_bess_mw            = args.cost_bess_mw,
        cost_bess_mwh           = args.cost_bess_mwh,
        grid_connection_cost_mw = args.cost_gc_mw,
        grid_cost_mwh           = args.grid_price,
        nominal_discount_rate_pct = args.discount_rate,
        project_lifespan_years  = args.lifespan,
        fixed_opex_per_mwh_year = 8_000.0,
        cycle_life              = 5000,
        replacement_cost_mwh    = 300_000.0,
        inflation_rate_pct      = 2.5,
    )
    print(f"\n   Economic assumptions:")
    print(f"   PV CAPEX:        €{eco.cost_pv_mw/1e3:.0f}k/MW")
    print(f"   BESS CAPEX:      €{eco.cost_bess_mw/1e3:.0f}k/MW + €{eco.cost_bess_mwh/1e3:.0f}k/MWh")
    print(f"   Grid connection: €{eco.grid_connection_cost_mw/1e3:.0f}k/MW")
    print(f"   Grid energy:     €{eco.grid_cost_mwh:.0f}/MWh")
    print(f"   Discount rate:   {eco.nominal_discount_rate_pct:.1f}%  |  "
          f"Real: {eco.real_discount_rate:.2f}%  |  Lifespan: {eco.project_lifespan_years:.0f} yr")
    print(f"   Degradation cost: €{eco.real_deg_cost:.2f}/MWh discharged")

    # ── Load Phase 1 curves ────────────────────────────────────────────────
    print(f"\n📂 Loading Phase 1 curves: {curves_path.name}")
    curves = load_phase1_curves(curves_path)
    profiles_df = curves["profiles"]
    dt_hours = 1.0

    # ── Overlay economics on each scenario ────────────────────────────────
    costed_ssr = costed_ps = costed_sub = None
    optimal_ssr = optimal_ps = optimal_sub = None

    # Scenario A — SSR Curve
    if not curves["ssr"].empty:
        print(f"\n  [A] SSR Curve — overlaying economics …")
        costed_ssr  = evaluate_costs(curves["ssr"], eco, profiles_df, dt_hours)
        optimal_ssr = find_optimal_point(costed_ssr, objective="knee")
        _print_optimum("Main · SSR Target", optimal_ssr)

    # Scenario B — Peak Shaving Curve
    if not curves["peak_shaving"].empty:
        print(f"\n  [B] Peak Shaving Curve — overlaying economics …")
        costed_ps  = evaluate_costs(curves["peak_shaving"], eco, profiles_df, dt_hours)
        optimal_ps = find_optimal_point(costed_ps, objective="knee")
        _print_optimum("Main · Peak Shaving", optimal_ps)

    # Scenario D — Sub-Scenario Curve
    if not curves["sub"].empty:
        print(f"\n  [D] Sub-Scenario Curve — overlaying economics …")
        costed_sub  = evaluate_costs(curves["sub"], eco, profiles_df, dt_hours)
        optimal_sub = find_optimal_point(costed_sub, objective="knee")
        _print_optimum("Sub · No PV", optimal_sub)

    # ── Scenario C — Co-Optimisation (PV+BESS surface) ────────────────────
    cooptimised_surface = None
    if not curves["surface"].empty:
        print(f"\n  [C] Co-Optimisation Surface — overlaying economics …")
        cooptimised_surface = cooptimise_ssr_gc(
            curves["surface"], eco, profiles_df, dt_hours
        )

    # ── Seasonal grid time-shifting analysis ──────────────────────────────
    seasonal_df = None
    # Use the optimal with highest BESS for seasonal / grid-services analysis
    # (seasonal shifting requires an actual GC limit below peak demand)
    ref_optimal = optimal_ps if (optimal_ps is not None and not optimal_ps.empty
                                  and float(optimal_ps.get(CurveCols.BESS_MW, 0) or 0) > 0.1) \
                 else (optimal_ssr if optimal_ssr is not None and not optimal_ssr.empty else None)
    # Fall back to SSR-max point if knee-optimal has no BESS
    if ref_optimal is not None and not ref_optimal.empty:
        ref_bess = float(ref_optimal.get(CurveCols.BESS_MW, 0) or 0)
        if ref_bess < 0.1 and costed_ssr is not None and not costed_ssr.empty:
            # Use the highest-SSR feasible point instead
            ref_optimal = find_optimal_point(costed_ssr, objective="ssr")
    if ref_optimal is not None and not ref_optimal.empty and not profiles_df.empty:
        print(f"\n  [Seasonal] Analysing seasonal shifting potential …")
        seasonal_df = analyse_seasonal_shifting(profiles_df, ref_optimal, eco, dt_hours)
        if seasonal_df is not None and not seasonal_df.empty:
            total_shift = seasonal_df["shifting_potential_mwh"].sum()
            total_value = seasonal_df["shifting_value_€k"].sum()
            print(f"   → Annual shifting potential: {total_shift:,.0f} MWh/yr  "
                  f"(estimated value: €{total_value:.0f}k/yr)")

    # ── Grid services overlay ─────────────────────────────────────────────
    grid_services_dict = None
    if ref_optimal is not None and not ref_optimal.empty:
        print(f"\n  [Grid Services] Estimating revenue from BESS grid services …")
        grid_services_dict = analyse_grid_services(
            optimal_sizing=ref_optimal,
            eco=eco,
            reserved_soc_pct=args.gs_reserved_soc,
            service_hours_per_day=4.0,
            service_price_mwh=args.gs_price,
        )
        net = grid_services_dict.get("annual_net_revenue_€k", 0)
        print(f"   → Annual net grid services revenue: €{net:.0f}k/yr")

    # ── Site-area feasibility check ───────────────────────────────────────
    site_area_df = None
    if args.site_area and costed_ssr is not None:
        print(f"\n  [Site Area] Checking feasibility for "
              f"{args.site_area/10000:.1f} ha site …")
        site_area_df = check_site_area(
            costed_df=costed_ssr,
            max_area_m2=args.site_area,
            pv_area_m2_per_mw=10_000.0,
            bess_area_m2_per_mwh=15.0,
        )

    # ── Save Phase 2 report ───────────────────────────────────────────────
    save_phase2_report(
        output_path=args.p2_output,
        optimal_ssr=optimal_ssr,   costed_ssr=costed_ssr,
        optimal_ps=optimal_ps,     costed_ps=costed_ps,
        optimal_sub=optimal_sub,   costed_sub=costed_sub,
        cooptimised_surface=cooptimised_surface,
        seasonal_df=seasonal_df,
        grid_services_dict=grid_services_dict,
        site_area_df=site_area_df,
    )

    from optimizer.plotter import plot_sizing_curves
    plot_sizing_curves(Path(args.p2_output).parent, curve_ssr=costed_ssr, curve_ps=costed_ps, surface=cooptimised_surface)

    # ── Dispatch Verification (Model R — causal rule, the contract truth) ──
    if args.verify_dispatch and ref_optimal is not None and not ref_optimal.empty:
        print(f"\n" + "=" * 65)
        print(f"  PHASE 2 — DISPATCH VERIFICATION (CAUSAL RULE, Model R)")
        print("=" * 65)
        from optimizer.params import PhysicalParams
        from optimizer.rule_dispatch import verify_sizing_with_rule
        from optimizer.schema import KpiKeys

        phys_params = PhysicalParams(
            dt_hours=dt_hours, eff_charge=0.95, eff_discharge=0.95,
            min_soc_pct=10.0, max_soc_pct=90.0, initial_soc_pct=50.0,
            min_bess_duration_h=2.0, max_bess_duration_h=8.0,
            site_max_bess_mw=500.0, site_max_bess_mwh=4000.0, site_max_grid_mw=200.0,
        )

        target_type = ref_optimal.get(CurveCols.TARGET_TYPE, "ssr")
        pv_mw   = float(ref_optimal.get(CurveCols.PV_MW, 0) or 0)
        bess_mw = float(ref_optimal.get(CurveCols.BESS_MW, 0) or 0)
        bess_mwh= float(ref_optimal.get(CurveCols.BESS_MWH, 0) or 0)
        lp_ssr  = float(ref_optimal.get(CurveCols.ACHIEVED_SSR_PCT, 0) or 0)
        # Peak-shaving designs verify against their GC target; SSR designs run open.
        if target_type == "peak_shaving":
            ceiling = float(ref_optimal.get(CurveCols.TARGET_GC_MW, None)
                            or ref_optimal.get(CurveCols.PEAK_GC_MW, phys_params.site_max_grid_mw))
        else:
            ceiling = phys_params.site_max_grid_mw

        print(f"\n  Operating: PV {pv_mw:.0f} MW | BESS {bess_mw:.1f} MW / {bess_mwh:.1f} MWh "
              f"| grid ceiling {ceiling:.1f} MW | target={target_type}")
        kpis, flows = verify_sizing_with_rule(
            profiles_df, phys_params,
            pv_mw=pv_mw, bess_mw=bess_mw, bess_mwh=bess_mwh,
            target_type=target_type, grid_ceiling_mw=ceiling,
        )

        print(f"  [Model R] SSR={kpis[KpiKeys.SSR]:.1f}% (LP bound {lp_ssr:.1f}%, "
              f"gap {lp_ssr - kpis[KpiKeys.SSR]:+.1f}pp)  |  SCR={kpis[KpiKeys.SCR]:.1f}%  "
              f"|  peak grid={kpis[KpiKeys.GCMIN_PEAK]:.1f} MW  |  unmet={kpis[KpiKeys.TOTAL_UNMET_LOAD]:.1f} MWh")
        if target_type == "peak_shaving":
            verdict = "✓ holds" if kpis[KpiKeys.TOTAL_UNMET_LOAD] <= 1e-6 else "✗ sheds load"
            print(f"  Peak-shaving check: peak {kpis[KpiKeys.GCMIN_PEAK]:.1f} MW vs ceiling {ceiling:.1f} MW → {verdict}")
        else:
            verdict = "✓ meets" if kpis[KpiKeys.SSR] >= lp_ssr - 1.0 else "✗ below LP bound"
            print(f"  SSR check: operational {kpis[KpiKeys.SSR]:.1f}% vs LP bound {lp_ssr:.1f}% → {verdict}")

        d_out = Path(args.p2_output).parent / "Phase2_Dispatch_Verification.xlsx"
        flows.to_excel(d_out, index=False)
        print(f"  ✓ Dispatch verification (Model R flows) saved to: {d_out.name}")


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
    cap  = opt.get("Total CAPEX (€M)", "?")
    lcoe = opt.get("LCOE (€/MWh)", "?")
    # Operational (Model R) SSR is the honest number; LP value shown in parens.
    ssr_txt = f"SSR(R) {op_ssr:.1f}% (LP {ssr:.1f}%)" if op_ssr is not None and not pd.isna(op_ssr) else f"SSR {ssr:.1f}%"
    print(f"   ✓ {label}: BESS {bmw:.1f} MW / {bmwh:.1f} MWh ({bh:.1f}h) | "
          f"GC {gc:.1f} MW | {ssr_txt} | SCR {scr:.1f}% | CAPEX €{cap:.1f}M | LCOE €{lcoe:.1f}/MWh")


# ══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    args = parse_args()
    if args.phase2:
        run_phase2(args)
    else:
        run_phase1(args)


if __name__ == "__main__":
    main()
