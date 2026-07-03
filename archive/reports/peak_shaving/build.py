"""Peak-shaving sizing curve (S22): fixed load + fixed PV, sweep the grid-connection
ceiling, find the minimum BESS that holds each ceiling. Validated under the causal
simulation (the LP-sized battery may shed load below ~61 MW — flagged honestly).

Run from the repo root:
  python reports/peak_shaving/build.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import pandas as pd  # noqa: E402

from core.params import PhysicalParams  # noqa: E402
from core.curve_runner import run_peak_shaving_curve  # noqa: E402
from core.rule_dispatch import verify_sizing_with_rule, validate_flows, size_by_bisection  # noqa: E402
from core.schema import CurveCols, KpiKeys  # noqa: E402
from reports.common import load_site, hourly_flows, energy_split, monthly, write_block  # noqa: E402


def build(profiles_path: Path, out_dir: Path, *, dt_hours: float = 1.0,
          fmt: str = "legacy", gc_step: float = 10.0, min_dur: float = 2.0,
          max_dur: float = 8.0, solver_timeout: int = 120) -> Path:
    df, _, pv = load_site(profiles_path, fmt, dt_hours)
    p = PhysicalParams(dt_hours=dt_hours, gc_sweep_step_mw=gc_step,
                       min_bess_duration_h=min_dur, max_bess_duration_h=max_dur)
    dt = p.dt_hours
    curve = run_peak_shaving_curve(df, p, pv_mw_fixed=pv, scenario_label="Main peak shaving",
                                   solver_time_limit=solver_timeout)
    feas = curve[curve[CurveCols.FEASIBLE] == True]

    def sim(mw, mwh, gc):
        k, flows = verify_sizing_with_rule(df, p, pv_mw=pv, bess_mw=mw, bess_mwh=mwh,
                                           target_type="peak_shaving", grid_ceiling_mw=gc)
        ok = True
        try:
            validate_flows(flows, p, bess_mwh=mwh, export_limit_mw=p.export_limit_mw)
        except Exception:
            ok = False
        return k, flows, ok

    suffix = "_BESS15min" if fmt == "bess" else ""
    out = out_dir / f"GC_PeakShaving_Report{suffix}.xlsx"
    summary, made = [], {}
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        pd.DataFrame({"_": []}).to_excel(w, sheet_name="Summary", index=False)
        for _, r in feas.iterrows():
            gc = float(r[CurveCols.TARGET_GC_MW]); mw = float(r[CurveCols.BESS_MW]); mwh = float(r[CurveCols.BESS_MWH])
            sheet = "NoBESS" if mwh < 1e-6 else f"GC_{int(round(gc))}MW"
            k, flows, ok = sim(mw, mwh, gc)
            es = energy_split(flows, dt)
            peak = k[KpiKeys.GCMIN_PEAK]; unmet = k[KpiKeys.TOTAL_UNMET_LOAD]
            dur = mwh / mw if mw > 1e-6 else 0.0
            deliv = size_by_bisection(df, p, pv_mw=pv, target_type="peak_shaving", target_value=gc)
            verdict = "YES" if (unmet <= 1.0 and peak <= gc + 0.5) else f"NO - sheds {unmet:.0f} MWh/yr"
            deliv_note = round(deliv.bess_mwh, 1) if deliv.feasible else f"{deliv.bess_mwh:.0f} (site cap, infeasible)"
            summary.append({
                "GC ceiling (MW)": gc, "BESS Power (MW)": round(mw, 1), "BESS Energy (MWh)": round(mwh, 1),
                "Duration (h)": round(dur, 1), "Achieved peak grid (MW)": round(peak, 1),
                "Simulated SSR (%)": round(k[KpiKeys.SSR], 1), "Simulated SCR (%)": round(k[KpiKeys.SCR], 1),
                "Unmet / shed (MWh)": round(unmet, 1), "Deliverable BESS (MWh, Model R)": deliv_note,
                "Energy-balance residual (MWh)": round(es["residual"], 3),
                "Physics valid": "ok" if ok else "FAIL", "Meets target in simulation": verdict, "Detail sheet": sheet,
            })
            if sheet in made:
                continue
            made[sheet] = True
            soc0 = mwh * p.initial_soc_pct / 100.0
            design = pd.DataFrame({
                "Item": ["Scenario", "Fixed PV (MW)", "Chosen GC ceiling (MW)", "Identified BESS power (MW)",
                         "Identified BESS energy (MWh)", "BESS duration (h)", "Initial SOC (%)", "Initial SOC (MWh)",
                         "Usable SOC band (%)", "Min SOC / floor (MWh)", "Max SOC (MWh)", "Charge / discharge efficiency"],
                "Value": [f"Main · GC ceiling {gc:.0f} MW · BESS sizing", round(pv, 1), gc, round(mw, 1),
                          round(mwh, 1), round(dur, 1), p.initial_soc_pct, round(soc0, 1),
                          f"{p.min_soc_pct:.0f}-{p.max_soc_pct:.0f}", round(mwh * p.min_soc_pct / 100, 1),
                          round(mwh * p.max_soc_pct / 100, 1), f"{p.eff_charge:.2f} / {p.eff_discharge:.2f}"]})
            bal = pd.DataFrame({
                "Energy flow (MWh/yr)": ["Total demand (load)", "  served by PV directly", "  served by BESS discharge",
                                         "  served by grid (-> load)", "  unmet (shed)", "CHECK: four sources = demand?",
                                         "Balance residual (~0 = verified)", "Grid -> BESS (off-peak valley-fill)",
                                         "PV available", "PV used on-site", "PV curtailed"],
                "MWh/yr": [round(es["load"], 1), round(es["pv_to_load"], 1), round(es["bess_to_load"], 1),
                           round(es["grid_to_load"], 1), round(es["unmet"], 1),
                           round(es["pv_to_load"] + es["bess_to_load"] + es["grid_to_load"] + es["unmet"], 1),
                           round(es["residual"], 3), round(es["grid_charge"], 1), round(es["pv_av"], 1),
                           round(es["pv_used"], 1), round(es["curt"], 1)]})
            kpi = pd.DataFrame({
                "KPI": ["Grid ceiling target (MW)", "Achieved peak grid (MW)", "Peak <= ceiling?",
                        "Serves all load (no shedding)?", "Load shed (MWh/yr)",
                        "MEETS TARGET (ceiling held + no shedding)?", "SSR (%)", "SCR (%)", "Physics valid"],
                "Value": [gc, round(peak, 1), "YES" if peak <= gc + 0.5 else "NO",
                          "YES" if unmet <= 1.0 else "NO", round(unmet, 1),
                          "YES" if (unmet <= 1.0 and peak <= gc + 0.5) else "NO",
                          round(k[KpiKeys.SSR], 1), round(k[KpiKeys.SCR], 1), "ok" if ok else "FAIL"]})
            cur = 0
            cur = write_block(w, sheet, "SCENARIO DESIGN", design, cur)
            cur = write_block(w, sheet, "ANNUAL ENERGY BALANCE  (four sources must sum to demand)", bal, cur)
            cur = write_block(w, sheet, "RESULT  (does the BESS hold the grid ceiling?)", kpi, cur)
            cur = write_block(w, sheet, "MONTHLY BREAKDOWN", monthly(flows, dt), cur)
            cur = write_block(w, sheet, "HOURLY ENERGY FLOWS  (merit order; Total grid import <= ceiling)",
                              hourly_flows(flows, show_grid_to_bess=True), cur)

        sdf = pd.DataFrame(summary)
        w.book.remove(w.book["Summary"])
        sdf.to_excel(w, sheet_name="Summary", index=False)
        w.book.move_sheet("Summary", -(len(w.book.sheetnames) - 1))
    print(f"Saved {out}")
    return out


def main():
    ap = argparse.ArgumentParser(description="Peak-shaving sizing-curve report")
    ap.add_argument("--input", choices=["legacy", "bess"], default="legacy",
                    help="legacy=hourly PV+Load 8760; bess=15-min solar+wind+load")
    ap.add_argument("--profiles", default=None)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--dt", type=float, default=None, help="timestep (h); default 1.0 legacy / 0.25 bess")
    ap.add_argument("--gc-step", type=float, default=10.0)
    ap.add_argument("--min-duration", type=float, default=2.0, help="min BESS E/P (h)")
    ap.add_argument("--max-duration", type=float, default=8.0, help="max BESS E/P (h); raise for long-lull sites")
    ap.add_argument("--solver-timeout", type=int, default=None, help="HiGHS limit/solve (s); default 120 legacy / 600 bess")
    a = ap.parse_args()
    dt = a.dt if a.dt else (0.25 if a.input == "bess" else 1.0)
    timeout = a.solver_timeout if a.solver_timeout else (600 if a.input == "bess" else 120)
    profiles = a.profiles or str(REPO / "data" / ("BESS_Input.xlsx" if a.input == "bess" else "8760_PV&Load Profiles.xlsx"))
    build(Path(profiles), Path(a.out), dt_hours=dt, fmt=a.input, gc_step=a.gc_step,
          min_dur=a.min_duration, max_dur=a.max_duration, solver_timeout=timeout)


if __name__ == "__main__":
    main()
