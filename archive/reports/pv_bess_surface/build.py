"""PV x SSR -> minimum BESS energy: the co-sizing surface (PV is a second input).
Each grid point is one optimiser solve; cells are blank where an SSR target is
unreachable for that PV size. Shows the PV-vs-BESS trade-off. Outputs a 3D HTML.

Run from the repo root:
  python reports/pv_bess_surface/build.py
  python reports/pv_bess_surface/build.py --pv 100 150 200 250 300 --ssr 20 30 40 50 60 70
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core.profile_loader import load_profiles  # noqa: E402
from core.params import PhysicalParams  # noqa: E402
from core.sizing_engine import solve_sizing_point, find_ssr_max  # noqa: E402


def build(profiles_path: Path, out_dir: Path, pv_sweep, ssr_sweep) -> Path:
    import plotly.graph_objects as go
    df, _, _ = load_profiles(xlsx_path=profiles_path, dt_hours=1.0)
    p = PhysicalParams(dt_hours=1.0)
    Z = np.full((len(ssr_sweep), len(pv_sweep)), np.nan)
    for j, pv in enumerate(pv_sweep):
        ssr_max = find_ssr_max(df, p, float(pv))
        print(f"PV {pv} MW -> SSR_max {ssr_max:.1f}%")
        for i, ssr in enumerate(ssr_sweep):
            if ssr > ssr_max:
                continue
            r = solve_sizing_point(df, p, float(pv), "ssr", float(ssr))
            if r.feasible:
                Z[i, j] = round(r.bess_mwh, 1)

    out = out_dir / "PV_SSR_BESS_surface_3d.html"
    fig = go.Figure(go.Surface(x=list(pv_sweep), y=list(ssr_sweep), z=Z, colorscale="Viridis",
                               colorbar=dict(title="BESS MWh"),
                               hovertemplate="PV %{x} MW<br>SSR %{y}%<br>BESS %{z} MWh<extra></extra>"))
    fig.update_layout(title="Minimum BESS energy vs PV size and SSR target (optimiser surface)",
                      scene=dict(xaxis_title="PV nameplate (MW)", yaxis_title="SSR target (%)",
                                 zaxis_title="Min BESS energy (MWh)"),
                      height=750, margin=dict(l=0, r=0, t=40, b=0))
    fig.write_html(out, include_plotlyjs="cdn")
    grid = pd.DataFrame(Z, index=[f"SSR {s}%" for s in ssr_sweep], columns=[f"PV {v}MW" for v in pv_sweep])
    grid.to_csv(out_dir / "PV_SSR_BESS_grid.csv")
    print(f"Saved {out.name} and PV_SSR_BESS_grid.csv")
    print(grid.to_string())
    return out


def main():
    ap = argparse.ArgumentParser(description="PV x SSR -> BESS 3D surface")
    ap.add_argument("--profiles", default=str(REPO / "data" / "8760_PV&Load Profiles.xlsx"))
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--pv", nargs="+", type=float, default=[100, 150, 200, 250, 300])
    ap.add_argument("--ssr", nargs="+", type=float, default=[20, 30, 40, 50, 60, 70])
    a = ap.parse_args()
    build(Path(a.profiles), Path(a.out), a.pv, a.ssr)


if __name__ == "__main__":
    main()
