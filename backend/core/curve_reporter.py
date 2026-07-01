"""
Curve Reporter Module (Phase 1)
--------------------------------
Writes Phase 1 sizing curve results to a clean Excel workbook.

Output file: Phase1_Sizing_Curves.xlsx
Sheets:
  - Summary          : one-line per scenario, key stats
  - Main_SSR_Curve   : Scenario A results (SSR target → BESS)
  - Main_PeakShave   : Scenario B results (GC target → BESS)
  - PV_BESS_Surface  : Scenario C results (PV × SSR → BESS)
  - Sub_PeakShave    : Scenario D results (no PV, GC target → BESS)
  - Input_Profiles   : the raw load + PV profiles used
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from core.schema import CurveCols


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def save_phase1_report(
    output_path: str | Path,
    profiles_df: pd.DataFrame,
    curve_a: pd.DataFrame | None = None,   # SSR curve
    curve_b: pd.DataFrame | None = None,   # Main peak shaving
    curve_c: pd.DataFrame | None = None,   # PV+BESS surface
    curve_d: pd.DataFrame | None = None,   # Sub-scenario peak shaving
    curve_e: pd.DataFrame | None = None,   # Co-optimization
    curve_f: pd.DataFrame | None = None,   # Off-grid
    curve_g: pd.DataFrame | None = None,   # Standalone Gen
) -> None:
    """
    Write all Phase 1 sizing curves to an Excel workbook.

    Parameters
    ----------
    output_path : str | Path
        Path for the output Excel file (e.g. 'Phase1_Sizing_Curves.xlsx').
    profiles_df : pd.DataFrame
        The input profiles (timestamp, load_mw, pv_pu).
    curve_a : pd.DataFrame | None
        Scenario A — Main · SSR Target sizing curve.
    curve_b : pd.DataFrame | None
        Scenario B — Main · Peak Shaving sizing curve.
    curve_c : pd.DataFrame | None
        Scenario C — PV+BESS Co-Sizing surface.
    curve_d : pd.DataFrame | None
        Scenario D — Sub-Scenario (no PV) peak shaving curve.
    curve_e : pd.DataFrame | None
        Scenario E — Co-Optimization results.
    curve_f : pd.DataFrame | None
        Scenario F — Off-Grid results.
    curve_g : pd.DataFrame | None
        Scenario G — Standalone Generation results.
    """
    output_path = Path(output_path)
    print(f"\n💾 Saving Phase 1 results → {output_path.name} …")

    summary_rows = []

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:

        # ── Input profiles ────────────────────────────────────────────────
        profiles_out = profiles_df.copy().round(4)
        profiles_out.to_excel(writer, sheet_name="Input_Profiles", index=False)
        print(f"   ✓ Input_Profiles   ({len(profiles_out)} timesteps)")

        # ── Scenario E — Co-Optimization ──────────────────────────────────
        if curve_e is not None and not curve_e.empty:
            _write_curve_sheet(writer, curve_e, "Co_Opt_SSR_GC")
            summary_rows.append(_summarise_curve(curve_e, "Scenario E — Co-Opt"))
            print(f"   ✓ Co_Opt_SSR_GC    ({len(curve_e)} points, "
                  f"{curve_e[CurveCols.FEASIBLE].sum()} feasible)")

        # ── Scenario F — Off-Grid ─────────────────────────────────────────
        if curve_f is not None and not curve_f.empty:
            _write_curve_sheet(writer, curve_f, "Off_Grid")
            summary_rows.append(_summarise_curve(curve_f, "Scenario F — Off-Grid"))
            print(f"   ✓ Off_Grid         ({len(curve_f)} points, "
                  f"{curve_f[CurveCols.FEASIBLE].sum()} feasible)")

        # ── Scenario G — Standalone Gen ───────────────────────────────────
        if curve_g is not None and not curve_g.empty:
            _write_curve_sheet(writer, curve_g, "Standalone_Gen")
            summary_rows.append(_summarise_curve(curve_g, "Scenario G — Standalone Gen"))
            print(f"   ✓ Standalone_Gen   ({len(curve_g)} points, "
                  f"{curve_g[CurveCols.FEASIBLE].sum()} feasible)")

        # ── Scenario A — SSR Curve ────────────────────────────────────────
        if curve_a is not None and not curve_a.empty:
            _write_curve_sheet(writer, curve_a, "Main_SSR_Curve")
            summary_rows.append(_summarise_curve(curve_a, "Scenario A — SSR Curve"))
            print(f"   ✓ Main_SSR_Curve   ({len(curve_a)} points, "
                  f"{curve_a[CurveCols.FEASIBLE].sum()} feasible)")

        # ── Scenario B — Peak Shaving Curve ──────────────────────────────
        if curve_b is not None and not curve_b.empty:
            _write_curve_sheet(writer, curve_b, "Main_PeakShave")
            summary_rows.append(_summarise_curve(curve_b, "Scenario B — Peak Shaving"))
            print(f"   ✓ Main_PeakShave   ({len(curve_b)} points, "
                  f"{curve_b[CurveCols.FEASIBLE].sum()} feasible)")

        # ── Scenario C — PV+BESS Surface ─────────────────────────────────
        if curve_c is not None and not curve_c.empty:
            _write_curve_sheet(writer, curve_c, "PV_BESS_Surface")
            summary_rows.append(_summarise_curve(curve_c, "Scenario C — PV+BESS Surface"))
            print(f"   ✓ PV_BESS_Surface  ({len(curve_c)} points, "
                  f"{curve_c[CurveCols.FEASIBLE].sum()} feasible)")

        # ── Scenario D — Sub-Scenario ─────────────────────────────────────
        if curve_d is not None and not curve_d.empty:
            _write_curve_sheet(writer, curve_d, "Sub_PeakShave")
            summary_rows.append(_summarise_curve(curve_d, "Scenario D — Sub (no PV)"))
            print(f"   ✓ Sub_PeakShave    ({len(curve_d)} points, "
                  f"{curve_d[CurveCols.FEASIBLE].sum()} feasible)")

        # ── Summary sheet ─────────────────────────────────────────────────
        if summary_rows:
            summary_df = pd.DataFrame(summary_rows)
            summary_df.to_excel(writer, sheet_name="Summary", index=False)
            print(f"   ✓ Summary          ({len(summary_rows)} scenarios)")

    print(f"\n✅ Phase 1 report saved: {output_path.resolve()}")
    _print_console_summary(curve_a, curve_b, curve_c, curve_d, curve_e, curve_f, curve_g)


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _write_curve_sheet(
    writer: pd.ExcelWriter,
    curve_df: pd.DataFrame,
    sheet_name: str,
) -> None:
    """Write a curve DataFrame to one Excel sheet, rounded for readability."""
    out = curve_df.copy()
    # Round all numeric columns to 2 decimal places
    numeric_cols = out.select_dtypes(include="number").columns
    out[numeric_cols] = out[numeric_cols].round(2)
    out.to_excel(writer, sheet_name=sheet_name, index=False)


def _summarise_curve(curve_df: pd.DataFrame, label: str) -> dict:
    """Build one summary row for the Summary sheet."""
    feasible = curve_df[curve_df[CurveCols.FEASIBLE] == True]
    row = {
        "Scenario":           label,
        "Total Points":       len(curve_df),
        "Feasible Points":    int(feasible.shape[0]),
    }
    if not feasible.empty:
        row["Min BESS MW"]  = feasible[CurveCols.BESS_MW].min()
        row["Max BESS MW"]  = feasible[CurveCols.BESS_MW].max()
        row["Min BESS MWh"] = feasible[CurveCols.BESS_MWH].min()
        row["Max BESS MWh"] = feasible[CurveCols.BESS_MWH].max()
        if CurveCols.TARGET_SSR_PCT in feasible.columns:
            ssr_col = feasible[CurveCols.TARGET_SSR_PCT].dropna()
            if not ssr_col.empty:
                row["SSR Range"] = f"{ssr_col.min():.0f}% – {ssr_col.max():.0f}%"
        if CurveCols.TARGET_GC_MW in feasible.columns:
            gc_col = feasible[CurveCols.TARGET_GC_MW].dropna()
            if not gc_col.empty:
                row["GC Range (MW)"] = f"{gc_col.min():.0f} – {gc_col.max():.0f}"
    return row


def _print_console_summary(
    curve_a, curve_b, curve_c, curve_d, curve_e=None, curve_f=None, curve_g=None
) -> None:
    """Print a quick human-readable summary table to stdout."""
    print("\n" + "=" * 65)
    print("  PHASE 1 SIZING CURVES — SUMMARY")
    print("=" * 65)

    def _print_curve(label, curve_df, target_col, target_label):
        if curve_df is None or curve_df.empty:
            print(f"  {label}: no results")
            return
        feasible = curve_df[curve_df[CurveCols.FEASIBLE] == True]
        if feasible.empty:
            print(f"  {label}: 0 feasible points")
            return
        has_r = CurveCols.R_BESS_MWH in feasible.columns
        print(f"\n  {label} ({len(feasible)} feasible points)")
        if has_r:
            # LP = optimistic lower bound; R = deliverable (meets target under rule);
            # EoL = R grossed up for ~20yr fade — the number to install day-one.
            print(f"  {'Target':>12}  {'LP MWh':>9}  {'Deliv MWh':>10}  "
                  f"{'EoL MWh':>9}  {'EoL MW':>8}  {'Dur':>5}")
            print(f"  {'-'*12}  {'-'*9}  {'-'*10}  {'-'*9}  {'-'*8}  {'-'*5}")
        else:
            print(f"  {'Target':>12}  {'BESS MW':>9}  {'BESS MWh':>10}  {'Duration':>9}  {'Peak GC':>9}")
            print(f"  {'-'*12}  {'-'*9}  {'-'*10}  {'-'*9}  {'-'*9}")
        for _, row in feasible.iterrows():
            t_val = row.get(target_col)
            t_str = f"{t_val:.1f}" if t_val is not None and pd.notna(t_val) else "—"
            bess_mw  = row.get(CurveCols.BESS_MW,  0) or 0
            bess_mwh = row.get(CurveCols.BESS_MWH, 0) or 0
            dur      = row.get(CurveCols.BESS_DURATION_H, 0) or 0
            gc       = row.get(CurveCols.PEAK_GC_MW, 0) or 0
            if has_r:
                r_mwh  = row.get(CurveCols.R_BESS_MWH, None)
                e_mwh  = row.get(CurveCols.EOL_BESS_MWH, None)
                e_mw   = row.get(CurveCols.EOL_BESS_MW, None)
                r_str  = f"{r_mwh:10.1f}" if r_mwh is not None and pd.notna(r_mwh) else f"{'—':>10}"
                em_str = f"{e_mwh:9.1f}"  if e_mwh is not None and pd.notna(e_mwh) else f"{'—':>9}"
                ew_str = f"{e_mw:8.1f}"   if e_mw  is not None and pd.notna(e_mw)  else f"{'—':>8}"
                flag   = " ⚠cap" if bool(row.get(CurveCols.EOL_CAPPED, False)) else ""
                # Duration of the battery actually being reported (EoL install),
                # not the LP point's — peak-shaving sizes MW and MWh independently
                # so the deliverable E/P differs from the LP tie-break.
                deliv_dur = (e_mwh / e_mw) if (e_mw and pd.notna(e_mw) and e_mw > 1e-9
                                               and e_mwh is not None and pd.notna(e_mwh)) else dur
                print(f"  {t_str:>11}{target_label}  {bess_mwh:9.1f}  {r_str}  "
                      f"{em_str}  {ew_str}  {deliv_dur:4.1f}h{flag}")
            else:
                print(f"  {t_str:>11}{target_label}  {bess_mw:9.1f}  {bess_mwh:10.1f}  {dur:8.1f}h  {gc:9.1f}")

    _print_curve("Scenario A — SSR Curve",
                 curve_a, CurveCols.TARGET_SSR_PCT, "%")
    _print_curve("Scenario B — Peak Shaving",
                 curve_b, CurveCols.TARGET_GC_MW, " MW")
    _print_curve("Scenario D — Sub (no PV)",
                 curve_d, CurveCols.TARGET_GC_MW, " MW")
    if curve_e is not None:
        _print_curve("Scenario E — Co-Opt",
                     curve_e, CurveCols.TARGET_SSR_PCT, "%")
    if curve_f is not None:
        _print_curve("Scenario F — Off-Grid",
                     curve_f, CurveCols.TARGET_SSR_PCT, "%")
    if curve_g is not None:
        _print_curve("Scenario G — Standalone Gen",
                     curve_g, CurveCols.TARGET_SSR_PCT, "%")

    if curve_c is not None and not curve_c.empty:
        feasible_c = curve_c[curve_c[CurveCols.FEASIBLE] == True]
        print(f"\n  Scenario C — PV+BESS Surface: {len(feasible_c)}/{len(curve_c)} feasible points")
        if not feasible_c.empty:
            pv_vals  = sorted(feasible_c[CurveCols.PV_MW].dropna().unique())
            ssr_vals = sorted(feasible_c[CurveCols.TARGET_SSR_PCT].dropna().unique())
            print(f"  PV range: {pv_vals[0]:.0f} – {pv_vals[-1]:.0f} MW")
            print(f"  SSR range: {ssr_vals[0]:.0f}% – {ssr_vals[-1]:.0f}%")

    print("\n" + "=" * 65)
