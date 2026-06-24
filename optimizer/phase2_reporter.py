"""
Phase 2 Reporter Module
------------------------
Writes the Phase 2 techno-economic results to a clean Excel workbook.

Output file: Phase2_TechnoEconomic.xlsx
Sheets:
  - Executive_Summary      : top-line numbers per scenario
  - SSR_Curve_Costed       : Scenario A with economic overlay
  - PeakShave_Curve_Costed : Scenario B with economic overlay
  - Sub_Curve_Costed       : Scenario D with economic overlay
  - CoOptimisation         : Scenario C surface with economic overlay
  - Seasonal_Shifting      : monthly shifting analysis
  - Grid_Services          : grid services estimate
  - Site_Area_Check        : feasibility filter by site footprint
  - Optimal_Designs        : the chosen optimum from each scenario
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from optimizer.schema import CurveCols


def save_phase2_report(
    output_path: str | Path,
    optimal_ssr: pd.Series | None        = None,
    costed_ssr: pd.DataFrame | None      = None,
    optimal_ps: pd.Series | None         = None,
    costed_ps: pd.DataFrame | None       = None,
    optimal_sub: pd.Series | None        = None,
    costed_sub: pd.DataFrame | None      = None,
    cooptimised_surface: pd.DataFrame | None = None,
    seasonal_df: pd.DataFrame | None     = None,
    grid_services_dict: dict | None      = None,
    site_area_df: pd.DataFrame | None    = None,
) -> None:
    """
    Write Phase 2 results to Excel.
    All arguments are optional — missing data is simply omitted from output.
    """
    output_path = Path(output_path)
    print(f"\n💾 Saving Phase 2 results → {output_path.name} …")

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:

        # ── Executive Summary ─────────────────────────────────────────────
        summary_rows = []
        for label, opt, costed in [
            ("Main · SSR Target",     optimal_ssr, costed_ssr),
            ("Main · Peak Shaving",   optimal_ps,  costed_ps),
            ("Sub · No PV",           optimal_sub, costed_sub),
        ]:
            if opt is not None and not opt.empty:
                row = {"Scenario": label}
                for col in [
                    CurveCols.PV_MW, CurveCols.BESS_MW, CurveCols.BESS_MWH,
                    CurveCols.BESS_DURATION_H, CurveCols.PEAK_GC_MW,
                    CurveCols.ACHIEVED_SSR_PCT, CurveCols.ACHIEVED_SCR_PCT,
                    "Total CAPEX (€M)", "Total Annual OPEX (€M/yr)", "LCOE (€/MWh)",
                ]:
                    if col in opt.index:
                        row[col] = opt[col]
                summary_rows.append(row)

        if summary_rows:
            pd.DataFrame(summary_rows).to_excel(
                writer, sheet_name="Executive_Summary", index=False)
            print(f"   ✓ Executive_Summary")

        # ── Costed curves ─────────────────────────────────────────────────
        if costed_ssr is not None and not costed_ssr.empty:
            costed_ssr.round(3).to_excel(writer, sheet_name="SSR_Curve_Costed", index=False)
            print(f"   ✓ SSR_Curve_Costed     ({len(costed_ssr)} points)")

        if costed_ps is not None and not costed_ps.empty:
            costed_ps.round(3).to_excel(writer, sheet_name="PeakShave_Curve_Costed", index=False)
            print(f"   ✓ PeakShave_Curve_Costed ({len(costed_ps)} points)")

        if costed_sub is not None and not costed_sub.empty:
            costed_sub.round(3).to_excel(writer, sheet_name="Sub_Curve_Costed", index=False)
            print(f"   ✓ Sub_Curve_Costed     ({len(costed_sub)} points)")

        # ── Co-optimisation surface ───────────────────────────────────────
        if cooptimised_surface is not None and not cooptimised_surface.empty:
            cooptimised_surface.round(3).to_excel(
                writer, sheet_name="CoOptimisation", index=False)
            print(f"   ✓ CoOptimisation       ({len(cooptimised_surface)} points)")

        # ── Optimal designs ───────────────────────────────────────────────
        opt_rows = []
        for label, opt in [
            ("Main · SSR Target",   optimal_ssr),
            ("Main · Peak Shaving", optimal_ps),
            ("Sub · No PV",         optimal_sub),
        ]:
            if opt is not None and not opt.empty:
                r = opt.to_dict()
                r["Scenario"] = label
                opt_rows.append(r)

        if opt_rows:
            pd.DataFrame(opt_rows).to_excel(
                writer, sheet_name="Optimal_Designs", index=False)
            print(f"   ✓ Optimal_Designs      ({len(opt_rows)} scenarios)")

        # ── Seasonal shifting ─────────────────────────────────────────────
        if seasonal_df is not None and not seasonal_df.empty:
            seasonal_df.to_excel(writer, sheet_name="Seasonal_Shifting", index=False)
            print(f"   ✓ Seasonal_Shifting    ({len(seasonal_df)} months)")

        # ── Grid services ─────────────────────────────────────────────────
        if grid_services_dict:
            gs_df = pd.DataFrame([grid_services_dict])
            gs_df.to_excel(writer, sheet_name="Grid_Services", index=False)
            print(f"   ✓ Grid_Services")

        # ── Site area check ───────────────────────────────────────────────
        if site_area_df is not None and not site_area_df.empty:
            site_area_df.round(3).to_excel(writer, sheet_name="Site_Area_Check", index=False)
            n_fit = site_area_df.get("Fits on Site", pd.Series([False])).sum()
            print(f"   ✓ Site_Area_Check      ({n_fit}/{len(site_area_df)} designs fit)")

    print(f"\n✅ Phase 2 report saved: {output_path.resolve()}")
    _print_executive_summary(optimal_ssr, optimal_ps, optimal_sub, grid_services_dict)


def _print_executive_summary(optimal_ssr, optimal_ps, optimal_sub, grid_services_dict):
    print("\n" + "=" * 65)
    print("  PHASE 2 — TECHNO-ECONOMIC OPTIMUM SUMMARY")
    print("=" * 65)

    for label, opt in [
        ("Main · SSR Target",   optimal_ssr),
        ("Main · Peak Shaving", optimal_ps),
        ("Sub · No PV",         optimal_sub),
    ]:
        if opt is None or opt.empty:
            continue
        print(f"\n  ── {label} ──")
        pv   = opt.get(CurveCols.PV_MW, 0) or 0
        bmw  = opt.get(CurveCols.BESS_MW, 0) or 0
        bmwh = opt.get(CurveCols.BESS_MWH, 0) or 0
        bh   = opt.get(CurveCols.BESS_DURATION_H, 0) or 0
        gc   = opt.get(CurveCols.PEAK_GC_MW, 0) or 0
        ssr  = opt.get(CurveCols.ACHIEVED_SSR_PCT, 0) or 0
        scr  = opt.get(CurveCols.ACHIEVED_SCR_PCT, 0) or 0
        cap  = opt.get("Total CAPEX (€M)", None)
        lcoe = opt.get("LCOE (€/MWh)", None)
        target_type = opt.get(CurveCols.TARGET_TYPE, "")
        target_val  = opt.get(CurveCols.TARGET_SSR_PCT) or opt.get(CurveCols.TARGET_GC_MW)

        print(f"  PV: {pv:.0f} MW  |  BESS: {bmw:.1f} MW / {bmwh:.1f} MWh ({bh:.1f}h)  |  Grid: {gc:.1f} MW")
        print(f"  SSR achieved: {ssr:.1f}%  |  SCR achieved: {scr:.1f}%")
        if cap is not None:
            print(f"  Total CAPEX: €{cap:.1f}M  |  LCOE: €{lcoe:.1f}/MWh" if lcoe else f"  Total CAPEX: €{cap:.1f}M")

    if grid_services_dict:
        rev = grid_services_dict.get("annual_net_revenue_€k", 0)
        note = grid_services_dict.get("note", "")
        print(f"\n  ── Grid Services ──")
        print(f"  {note}")
        print(f"  Annual net revenue: €{rev:.0f}k")

    print("\n" + "=" * 65)
