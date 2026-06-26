"""Scaling study: how the optimiser responds when generation and demand are
scaled (inversely) on the same shape.

1. Downsample the 15-min BESS_Input to HOURLY by averaging power over each hour
   (a legitimate aggregation — preserves energy; the loader forbids the reverse,
   upsampling, because that would fabricate peaks).
2. Write 3-4 scaled BESS-format Excel inputs (generation × g, demand × l).
3. Run the SSR sizing optimiser on each and print a comparison.

Run from the repo root:
  python reports/scaling_study/build.py
  python reports/scaling_study/build.py --ssr-step 10
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import pandas as pd  # noqa: E402

from reports.ssr_curve.build import build as build_ssr  # noqa: E402

# (name, generation scale, demand scale) — inverse pairs sweep the gen:load ratio
VARIANTS = [
    ("base",       1.00, 1.00),   # ratio ~0.74
    ("gen_rich",   1.50, 0.80),   # lots of generation, smaller load -> high SSR_max
    ("load_heavy", 0.70, 1.30),   # scarce generation, bigger load  -> grid-dependent
    ("balanced",   1.25, 0.90),   # moderate uplift
]


def to_hourly(src: Path) -> pd.DataFrame:
    """15-min -> hourly average power (energy-preserving)."""
    raw = pd.read_excel(src, sheet_name="Energy Timeseries")
    raw["ts"] = pd.to_datetime(raw["date_time"])
    h = (raw.set_index("ts")[["solar_power_mw", "wind_power_mw", "Data center MW"]]
            .resample("1h").mean().dropna())
    return h


def write_input(hourly: pd.DataFrame, gen_scale: float, load_scale: float, path: Path) -> None:
    out = pd.DataFrame({
        "date_time": hourly.index.strftime("%Y-%m-%dT%H:%M:%S"),
        "solar_power_mw": (hourly["solar_power_mw"] * gen_scale).round(4).values,
        "wind_power_mw":  (hourly["wind_power_mw"] * gen_scale).round(4).values,
        "Data center MW": (hourly["Data center MW"] * load_scale).round(4).values,
    })
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        out.to_excel(w, sheet_name="Energy Timeseries", index=False)


def main():
    ap = argparse.ArgumentParser(description="PV/demand scaling study (hourly)")
    ap.add_argument("--src", default=str(REPO / "BESS_Input.xlsx"))
    ap.add_argument("--n-points", type=int, default=6,
                    help="SSR targets per variant, evenly across each [baseline, SSR_max] band")
    a = ap.parse_args()

    base_dir = Path(__file__).resolve().parent
    inputs = base_dir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)

    hourly = to_hourly(Path(a.src))
    print(f"Downsampled to {len(hourly)} hourly steps "
          f"(load mean {hourly['Data center MW'].mean():.1f} MW, "
          f"gen mean {(hourly['solar_power_mw'] + hourly['wind_power_mw']).mean():.1f} MW)")

    rows = []
    for name, g, l in VARIANTS:
        inp = inputs / f"input_{name}.xlsx"
        write_input(hourly, g, l, inp)
        outdir = base_dir / name
        outdir.mkdir(parents=True, exist_ok=True)
        print(f"\n{'='*60}\n  {name}:  generation x{g}   demand x{l}\n{'='*60}")
        xlsx = build_ssr(inp, outdir, dt_hours=1.0, fmt="bess",
                         n_points=a.n_points, solver_timeout=120)
        # rename the generic output to a variant-clear name
        nice = outdir / f"SSR_{name}.xlsx"
        Path(xlsx).replace(nice)

        s = pd.read_excel(nice, "Summary")
        if s.empty:
            rows.append({"variant": name, "gen_x": g, "load_x": l, "note": "no feasible SSR points"})
            continue
        gen_mwh = (hourly["solar_power_mw"] + hourly["wind_power_mw"]).sum() * g
        load_mwh = hourly["Data center MW"].sum() * l
        top = s.iloc[-1]
        rows.append({
            "variant": name, "gen_x": g, "load_x": l,
            "gen/load ratio": round(gen_mwh / load_mwh, 2),
            "baseline SSR %": s["Simulated SSR (%)"].iloc[0],
            "top SSR target %": top["SSR target (%)"],
            "BESS MW @ top": top["BESS Power (MW)"],
            "BESS MWh @ top": top["BESS Energy (MWh)"],
            "peak grid MW": s["Peak grid (MW)"].iloc[0],
        })

    comp = pd.DataFrame(rows)
    comp.to_csv(base_dir / "comparison.csv", index=False)
    print(f"\n{'='*60}\n  COMPARISON across scaled inputs\n{'='*60}")
    print(comp.to_string(index=False))
    print(f"\nSaved inputs -> {inputs}/  |  per-variant reports -> reports/scaling_study/<variant>/")
    print("Comparison -> reports/scaling_study/comparison.csv")


if __name__ == "__main__":
    main()
