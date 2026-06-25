"""Joint SSR + GC co-optimisation (Option A): one LP, BOTH targets as hard
constraints, minimise the BESS. The (SSR target x GC cap) grid is DERIVED from the
data — SSR from the no-battery baseline up to find_ssr_max, GC from peak demand
down to find_gc_min — not hardcoded. Each cell is validated under the causal
simulation (does one battery hold the GC ceiling AND reach the SSR target?).
Outputs an Excel (Bounds + grid + per-design hourly) and a 3D surface HTML.

Run from the repo root:
  python reports/co_optimisation/build.py
  python reports/co_optimisation/build.py --n-ssr 6 --n-gc 8
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from optimizer.params import PhysicalParams  # noqa: E402
from optimizer.sizing_engine import solve_sizing_point, find_ssr_max, find_gc_min  # noqa: E402
from optimizer.rule_dispatch import verify_sizing_with_rule, validate_flows  # noqa: E402
from optimizer.schema import KpiKeys  # noqa: E402
from reports.common import load_site, nice_step, hourly_flows, write_block  # noqa: E402


def derive_bounds(df, p, pv, n_ssr, n_gc, solver_timeout=120):
    """SSR axis: no-BESS baseline -> find_ssr_max. GC axis: peak demand -> find_gc_min.
    Step chosen from the span and the point budget, rounded to a nice increment."""
    peak_demand = float(df["load_mw"].max())
    base_k, _ = verify_sizing_with_rule(df, p, pv_mw=pv, bess_mw=0.0, bess_mwh=0.0,
                                        target_type="ssr", grid_ceiling_mw=p.site_max_grid_mw)
    ssr_base = base_k[KpiKeys.SSR]
    ssr_max = find_ssr_max(df, p, pv, solver_timeout)
    gc_min = find_gc_min(df, p, pv, solver_timeout)

    ssr_lo, ssr_hi = int(np.ceil(ssr_base)), int(np.floor(ssr_max))
    ssr_step = nice_step(ssr_hi - ssr_lo, n_ssr - 1, [1, 2, 5, 10])
    ssr_tgts = sorted(set(range(ssr_lo, ssr_hi + 1, ssr_step)) | {ssr_hi})

    gc_hi, gc_lo = int(np.floor(peak_demand)), int(np.ceil(gc_min))
    gc_step = nice_step(gc_hi - gc_lo, n_gc - 1, [1, 2, 5, 10])
    gc_caps = sorted(set(range(gc_hi, gc_lo - 1, -gc_step)) | {gc_lo}, reverse=True)
    return dict(ssr_base=ssr_base, ssr_max=ssr_max, ssr_step=ssr_step, ssr_tgts=ssr_tgts,
                peak_demand=peak_demand, gc_min=gc_min, gc_step=gc_step, gc_caps=gc_caps)


def build(profiles_path: Path, out_dir: Path, n_ssr: int = 5, n_gc: int = 6,
          *, dt_hours: float = 1.0, fmt: str = "legacy", solver_timeout: int = 120) -> tuple[Path, Path]:
    import plotly.graph_objects as go
    df, _, pv = load_site(profiles_path, fmt, dt_hours)
    p = PhysicalParams(dt_hours=dt_hours)
    dt = p.dt_hours
    b = derive_bounds(df, p, pv, n_ssr, n_gc, solver_timeout)
    SSR_TGTS, GC_CAPS = b["ssr_tgts"], b["gc_caps"]
    print(f"SSR {b['ssr_base']:.1f}->{b['ssr_max']:.1f}% step {b['ssr_step']} => {SSR_TGTS}")
    print(f"GC {b['peak_demand']:.1f}->{b['gc_min']:.1f}MW step {b['gc_step']} => {GC_CAPS}")

    rows = []
    Z = np.full((len(SSR_TGTS), len(GC_CAPS)), np.nan)
    designs = {}
    for j, gc in enumerate(GC_CAPS):
        for i, ssr in enumerate(SSR_TGTS):
            r = solve_sizing_point(df, p, pv, "co_opt", float(ssr), target_gc_mw=float(gc),
                                   solver_time_limit=solver_timeout)
            if not r.feasible:
                rows.append(dict(SSR_target=ssr, GC_cap=gc, LP_feasible="NO", BESS_MW=None, BESS_MWh=None,
                                 Sim_SSR=None, Sim_peak=None, Sim_unmet=None, Meets_both="infeasible (LP)"))
                continue
            k, _ = verify_sizing_with_rule(df, p, pv_mw=pv, bess_mw=r.bess_mw, bess_mwh=r.bess_mwh,
                                           target_type="peak_shaving", grid_ceiling_mw=float(gc))
            holds = (k[KpiKeys.GCMIN_PEAK] <= gc + 0.5) and (k[KpiKeys.TOTAL_UNMET_LOAD] <= 1.0)
            meets_ssr = k[KpiKeys.SSR] >= ssr - 1.0
            verdict = "YES" if (holds and meets_ssr) else ("GC ok, SSR short" if holds else f"sheds {k[KpiKeys.TOTAL_UNMET_LOAD]:.0f} MWh")
            Z[i, j] = round(r.bess_mwh, 1)
            if verdict == "YES":
                designs[(ssr, gc)] = (r.bess_mw, r.bess_mwh)
            rows.append(dict(SSR_target=ssr, GC_cap=gc, LP_feasible="YES",
                             BESS_MW=round(r.bess_mw, 1), BESS_MWh=round(r.bess_mwh, 1),
                             Sim_SSR=round(k[KpiKeys.SSR], 1), Sim_peak=round(k[KpiKeys.GCMIN_PEAK], 1),
                             Sim_unmet=round(k[KpiKeys.TOTAL_UNMET_LOAD], 1), Meets_both=verdict))

    flat = pd.DataFrame(rows)
    grid = pd.DataFrame(Z, index=[f"SSR {s}%" for s in SSR_TGTS], columns=[f"GC<={g}MW" for g in GC_CAPS])

    suffix = "_BESS15min" if fmt == "bess" else ""
    html = out_dir / f"CoOpt_SSR_GC_surface_3d{suffix}.html"
    fig = go.Figure(go.Surface(x=GC_CAPS, y=SSR_TGTS, z=Z, colorscale="Viridis",
                               colorbar=dict(title="BESS MWh"),
                               hovertemplate="GC<=%{x} MW<br>SSR %{y}%<br>BESS %{z} MWh<extra></extra>"))
    fig.update_layout(title="Joint SSR + GC co-optimisation — minimum BESS energy (MWh)",
                      scene=dict(xaxis_title="GC cap (MW)  [tighter ->]", yaxis_title="SSR target (%)",
                                 zaxis_title="Min BESS energy (MWh)"),
                      height=750, margin=dict(l=0, r=0, t=40, b=0))
    fig.write_html(html, include_plotlyjs="cdn")

    out = out_dir / f"CoOpt_SSR_GC_Report{suffix}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        pd.DataFrame({
            "Bound (derived from the data, not hardcoded)": [
                "SSR baseline — no BESS (%)", "SSR max — find_ssr_max (%)", "SSR sweep step (%)",
                "Peak demand (MW)", "GC min — find_gc_min (MW)", "GC sweep step (MW)"],
            "Value": [round(b["ssr_base"], 1), round(b["ssr_max"], 1), b["ssr_step"],
                      round(b["peak_demand"], 1), round(b["gc_min"], 1), b["gc_step"]],
        }).to_excel(w, sheet_name="Bounds", index=False)
        flat.to_excel(w, sheet_name="All_combinations", index=False)
        grid.to_excel(w, sheet_name="BESS_MWh_grid")
        for (ssr, gc), (mw, mwh) in sorted(designs.items(), key=lambda kv: (kv[0][1], -kv[0][0]))[:3]:
            sheet = f"SSR{ssr}_GC{gc}"
            k, flows = verify_sizing_with_rule(df, p, pv_mw=pv, bess_mw=mw, bess_mwh=mwh,
                                               target_type="peak_shaving", grid_ceiling_mw=float(gc))
            ok = True
            try:
                validate_flows(flows, p, bess_mwh=mwh, export_limit_mw=p.export_limit_mw)
            except Exception:
                ok = False
            design = pd.DataFrame({"Item": ["Scenario", "Fixed PV (MW)", "SSR target (%)", "GC cap (MW)",
                                            "Min BESS power (MW)", "Min BESS energy (MWh)", "BESS duration (h)"],
                                   "Value": [f"Joint: SSR>={ssr}% AND grid<={gc}MW", round(pv, 1), ssr, gc,
                                             round(mw, 1), round(mwh, 1), round(mwh / mw, 1) if mw > 0 else 0]})
            res = pd.DataFrame({"Check": [f"Simulated SSR (%) (target {ssr})", f"Simulated peak grid (MW) (cap {gc})",
                                          "Peak <= cap?", "Unmet load (MWh)", "Serves all load?", "MEETS BOTH?", "Physics valid"],
                                "Value": [round(k[KpiKeys.SSR], 1), round(k[KpiKeys.GCMIN_PEAK], 1),
                                          "YES" if k[KpiKeys.GCMIN_PEAK] <= gc + 0.5 else "NO", round(k[KpiKeys.TOTAL_UNMET_LOAD], 1),
                                          "YES" if k[KpiKeys.TOTAL_UNMET_LOAD] <= 1 else "NO",
                                          "YES" if (k[KpiKeys.GCMIN_PEAK] <= gc + 0.5 and k[KpiKeys.TOTAL_UNMET_LOAD] <= 1 and k[KpiKeys.SSR] >= ssr - 1) else "NO",
                                          "ok" if ok else "FAIL"]})
            cur = 0
            cur = write_block(w, sheet, "SCENARIO  (joint SSR + GC, minimum BESS)", design, cur)
            cur = write_block(w, sheet, "VALIDATION UNDER SIMULATION  (does one battery meet BOTH?)", res, cur)
            cur = write_block(w, sheet, "HOURLY ENERGY FLOWS  (merit order; Total grid import <= cap)",
                              hourly_flows(flows, show_grid_to_bess=True), cur)
    print(f"Saved {out} and {html.name}")
    return out, html


def main():
    ap = argparse.ArgumentParser(description="Joint SSR + GC co-optimisation report")
    ap.add_argument("--input", choices=["legacy", "bess"], default="legacy",
                    help="legacy=hourly PV+Load 8760; bess=15-min solar+wind+load")
    ap.add_argument("--profiles", default=None)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--dt", type=float, default=None, help="timestep (h); default 1.0 legacy / 0.25 bess")
    ap.add_argument("--n-ssr", type=int, default=5)
    ap.add_argument("--n-gc", type=int, default=6)
    ap.add_argument("--solver-timeout", type=int, default=None, help="HiGHS limit/solve (s); default 120 legacy / 600 bess")
    a = ap.parse_args()
    dt = a.dt if a.dt else (0.25 if a.input == "bess" else 1.0)
    timeout = a.solver_timeout if a.solver_timeout else (600 if a.input == "bess" else 120)
    profiles = a.profiles or str(REPO / ("BESS_Input.xlsx" if a.input == "bess" else "8760_PV&Load Profiles.xlsx"))
    build(Path(profiles), Path(a.out), a.n_ssr, a.n_gc, dt_hours=dt, fmt=a.input, solver_timeout=timeout)


if __name__ == "__main__":
    main()
