"""SSR sizing curve (S21): fixed load + fixed PV, sweep SSR targets, find the
minimum BESS (MW & MWh) per target; report peak grid (GCmin readable per design).
Each point is validated under the causal simulation (Model R).

Run from the repo root:
  python reports/ssr_curve/build.py
  python reports/ssr_curve/build.py --profiles "8760_PV&Load Profiles.xlsx" --out reports/ssr_curve
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import pandas as pd  # noqa: E402

from optimizer.params import PhysicalParams  # noqa: E402
from optimizer.curve_runner import run_ssr_curve  # noqa: E402
from optimizer.rule_dispatch import verify_sizing_with_rule, validate_flows, size_by_bisection  # noqa: E402
from optimizer.schema import CurveCols, KpiKeys  # noqa: E402
from reports.common import load_site, hourly_flows, energy_split, monthly, write_block  # noqa: E402


def build(profiles_path: Path, out_dir: Path, *, dt_hours: float = 1.0,
          fmt: str = "legacy", ssr_step: float = 5.0, ssr_start: float | None = None,
          n_points: int | None = None, min_dur: float = 2.0, max_dur: float = 8.0,
          init_soc: float = 50.0, solver_timeout: int = 120) -> Path:
    df, _, pv = load_site(profiles_path, fmt, dt_hours)
    p = PhysicalParams(dt_hours=dt_hours, ssr_sweep_step_pct=ssr_step,
                       min_bess_duration_h=min_dur, max_bess_duration_h=max_dur,
                       initial_soc_pct=init_soc)
    dt = p.dt_hours
    # Data-driven sweep start: skip the trivial region below the no-battery baseline
    # SSR (where BESS = 0). With n_points, the same number of targets is then placed
    # evenly across [baseline, SSR_max] so every scenario is sampled consistently.
    if ssr_start is None:
        bk, _ = verify_sizing_with_rule(df, p, pv_mw=pv, bess_mw=0.0, bess_mwh=0.0,
                                        target_type="ssr", grid_ceiling_mw=p.site_max_grid_mw)
        ssr_start = float(int(bk[KpiKeys.SSR]))   # floor of baseline SSR
    curve = run_ssr_curve(df, p, pv_mw_fixed=pv, scenario_label="Main SSR sizing",
                          solver_time_limit=solver_timeout, ssr_start_pct=ssr_start,
                          n_points=n_points)
    feas = curve[curve[CurveCols.FEASIBLE] == True]

    def sim(mw, mwh):
        k, flows = verify_sizing_with_rule(df, p, pv_mw=pv, bess_mw=mw, bess_mwh=mwh,
                                           target_type="ssr", grid_ceiling_mw=p.site_max_grid_mw)
        ok = True
        try:
            validate_flows(flows, p, bess_mwh=mwh, export_limit_mw=p.export_limit_mw)
        except Exception:
            ok = False
        return k, flows, ok

    suffix = "_BESS15min" if fmt == "bess" else ""
    out = out_dir / f"SSR_Scenarios_Report{suffix}.xlsx"
    summary, made = [], {}
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        pd.DataFrame({"_": []}).to_excel(w, sheet_name="Summary", index=False)
        for _, r in feas.iterrows():
            tgt = float(r[CurveCols.TARGET_SSR_PCT]); mw = float(r[CurveCols.BESS_MW]); mwh = float(r[CurveCols.BESS_MWH])
            sheet = "Sim_NoBESS" if mwh < 1e-6 else f"SSR_{int(tgt)}pct"
            k, flows, ok = sim(mw, mwh)
            es = energy_split(flows, dt)
            dur = mwh / mw if mw > 1e-6 else 0.0
            deliv = size_by_bisection(df, p, pv_mw=pv, target_type="ssr", target_value=tgt, duration_h=dur or 4.0)
            summary.append({
                "SSR target (%)": tgt, "BESS Power (MW)": round(mw, 1), "BESS Energy (MWh)": round(mwh, 1),
                "Duration (h)": round(dur, 1), "Peak grid (MW)": round(r[CurveCols.PEAK_GC_MW], 1),
                "Simulated SSR (%)": round(k[KpiKeys.SSR], 1), "Simulated SCR (%)": round(k[KpiKeys.SCR], 1),
                "Unmet (MWh)": round(es["unmet"], 1), "Deliverable BESS (MWh)": round(deliv.bess_mwh, 1),
                "Energy-balance residual (MWh)": round(es["residual"], 3),
                "Physics valid": "ok" if ok else "FAIL",
                "Viable in simulation": "YES" if k[KpiKeys.SSR] >= tgt - 0.5 else "undersized",
                "Detail sheet": sheet,
            })
            if sheet in made:
                continue
            made[sheet] = True
            soc0 = mwh * p.initial_soc_pct / 100.0
            design = pd.DataFrame({
                "Item": ["Scenario", "Fixed PV (MW)", "Chosen SSR target (%)", "Identified BESS power (MW)",
                         "Identified BESS energy (MWh)", "BESS duration (h)", "Initial SOC (%)", "Initial SOC (MWh)",
                         "Usable SOC band (%)", "Min SOC / floor (MWh)", "Max SOC (MWh)", "Charge / discharge efficiency"],
                "Value": [f"Main · SSR target {tgt:.0f}% · BESS sizing", round(pv, 1), tgt, round(mw, 1),
                          round(mwh, 1), round(dur, 1), p.initial_soc_pct, round(soc0, 1),
                          f"{p.min_soc_pct:.0f}-{p.max_soc_pct:.0f}", round(mwh * p.min_soc_pct / 100, 1),
                          round(mwh * p.max_soc_pct / 100, 1), f"{p.eff_charge:.2f} / {p.eff_discharge:.2f}"]})
            bal = pd.DataFrame({
                "Energy flow (MWh/yr)": ["Total demand (load)", "  served by PV directly", "  served by BESS discharge",
                                         "  imported from grid", "  unmet (shed)", "CHECK: four sources = demand?",
                                         "Balance residual (~0 = verified)", "PV available", "PV used on-site",
                                         "PV curtailed"],
                "MWh/yr": [round(es["load"], 1), round(es["pv_to_load"], 1), round(es["bess_to_load"], 1),
                           round(es["grid_to_load"], 1), round(es["unmet"], 1),
                           round(es["pv_to_load"] + es["bess_to_load"] + es["grid_to_load"] + es["unmet"], 1),
                           round(es["residual"], 3), round(es["pv_av"], 1), round(es["pv_used"], 1), round(es["curt"], 1)]})
            kpi = pd.DataFrame({
                "KPI": ["SSR = served on-site / demand", "    served on-site (MWh)", "    demand (MWh)",
                        "SCR = PV used / PV available", "    PV used (MWh)", "    PV available (MWh)",
                        "Peak grid import (MW)", "Unmet load (MWh)", "Physics valid"],
                "Value": [f"{k[KpiKeys.SSR]:.1f}%", round(es["pv_to_load"] + es["bess_to_load"], 1), round(es["load"], 1),
                          f"{k[KpiKeys.SCR]:.1f}%", round(es["pv_used"], 1), round(es["pv_av"], 1),
                          round(k[KpiKeys.GCMIN_PEAK], 1), round(es["unmet"], 1), "ok" if ok else "FAIL"]})
            cur = 0
            cur = write_block(w, sheet, "SCENARIO DESIGN", design, cur)
            cur = write_block(w, sheet, "ANNUAL ENERGY BALANCE  (four sources must sum to demand)", bal, cur)
            cur = write_block(w, sheet, "RESULTING SSR & SCR  (final, from annual totals)", kpi, cur)
            cur = write_block(w, sheet, "MONTHLY BREAKDOWN", monthly(flows, dt), cur)
            cur = write_block(w, sheet, "HOURLY ENERGY FLOWS  (merit order; CHECK = Load)", hourly_flows(flows), cur)

        sdf = pd.DataFrame(summary)
        w.book.remove(w.book["Summary"])
        sdf.to_excel(w, sheet_name="Summary", index=False)
        w.book.move_sheet("Summary", -(len(w.book.sheetnames) - 1))
    print(f"Saved {out}")
    return out


def main():
    ap = argparse.ArgumentParser(description="SSR sizing-curve report")
    ap.add_argument("--input", choices=["legacy", "bess"], default="legacy",
                    help="legacy=hourly PV+Load 8760; bess=15-min solar+wind+load")
    ap.add_argument("--profiles", default=None)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--dt", type=float, default=None, help="timestep (h); default 1.0 legacy / 0.25 bess")
    ap.add_argument("--ssr-step", type=float, default=5.0)
    ap.add_argument("--n-points", type=int, default=None,
                    help="number of SSR targets evenly across [baseline, SSR_max] (overrides step)")
    ap.add_argument("--ssr-start", type=float, default=None, help="sweep start %% (default = no-battery baseline SSR)")
    ap.add_argument("--min-duration", type=float, default=2.0, help="min BESS E/P (h)")
    ap.add_argument("--max-duration", type=float, default=8.0, help="max BESS E/P (h); raise for long-lull sites")
    ap.add_argument("--init-soc", type=float, default=50.0, help="initial = terminal-floor SOC (%%)")
    ap.add_argument("--solver-timeout", type=int, default=None, help="HiGHS limit/solve (s); default 120 legacy / 600 bess")
    a = ap.parse_args()
    dt = a.dt if a.dt else (0.25 if a.input == "bess" else 1.0)
    timeout = a.solver_timeout if a.solver_timeout else (600 if a.input == "bess" else 120)
    profiles = a.profiles or str(REPO / ("BESS_Input.xlsx" if a.input == "bess" else "8760_PV&Load Profiles.xlsx"))
    build(Path(profiles), Path(a.out), dt_hours=dt, fmt=a.input, ssr_step=a.ssr_step,
          ssr_start=a.ssr_start, n_points=a.n_points, min_dur=a.min_duration,
          max_dur=a.max_duration, init_soc=a.init_soc, solver_timeout=timeout)


if __name__ == "__main__":
    main()
