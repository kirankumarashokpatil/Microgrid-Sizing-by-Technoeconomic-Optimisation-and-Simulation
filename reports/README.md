# reports/ — per-scenario result builders

Each scenario has its own folder with a runnable `build.py` that drives the
optimiser, validates every design under the causal simulation (Model R), and
writes its Excel / interactive-HTML deliverables **into that same folder**.

Shared logic (per-hour merit-order routing, energy balance, monthly breakdown,
Excel layout, dynamic sweep steps) lives in [`common.py`](common.py) so the KPIs
are derived in exactly one place.

## Scenarios

| Folder | Question | Sweeps | Outputs |
|---|---|---|---|
| [`ssr_curve/`](ssr_curve/) | Min BESS to reach an SSR target (PV fixed) | SSR target | `SSR_Scenarios_Report.xlsx` |
| [`peak_shaving/`](peak_shaving/) | Min BESS to hold a grid-connection ceiling | GC ceiling | `GC_PeakShaving_Report.xlsx` |
| [`co_optimisation/`](co_optimisation/) | Min BESS to satisfy **SSR and GC together** (one LP) | SSR × GC grid (data-driven) | `CoOpt_SSR_GC_Report.xlsx`, `CoOpt_SSR_GC_surface_3d.html` |
| [`pv_bess_surface/`](pv_bess_surface/) | How PV size trades off against BESS for a target | PV × SSR grid | `PV_SSR_BESS_surface_3d.html`, `PV_SSR_BESS_grid.csv` |

## Running

From the repo root (no extra setup — each `build.py` adds the repo to the path):

```bash
python reports/ssr_curve/build.py
python reports/peak_shaving/build.py
python reports/co_optimisation/build.py            # bounds derived from the data
python reports/pv_bess_surface/build.py
```

Useful flags:

```bash
python reports/ssr_curve/build.py   --ssr-step 5
python reports/peak_shaving/build.py --gc-step 10
python reports/co_optimisation/build.py --n-ssr 6 --n-gc 8
python reports/pv_bess_surface/build.py --pv 100 150 200 250 --ssr 30 40 50 60
python reports/ssr_curve/build.py   --profiles "8760_PV&Load Profiles.xlsx" --out /some/dir
```

## What every Excel report contains

- **Summary** — one row per design: identified BESS MW/MWh, peak grid, simulated
  SSR/SCR, unmet, deliverable size, energy-balance residual, and a
  validated/meets-target verdict.
- **Per-design sheets** — scenario design (incl. initial SOC and band), a
  reconciling annual energy balance, the KPI derivation, a monthly breakdown, and
  the full 8760-hour merit-order flow trace with per-hour SSR/SCR.

The **co-optimisation** report additionally carries a `Bounds` sheet recording the
data-derived SSR/GC envelope (`find_ssr_max`, `find_gc_min`, baseline SSR, peak
demand) and the chosen sweep steps, so the grid is reproducible on any dataset.

> The numbers are not just LP-optimal — every design is re-run under the causal
> dispatch and the reports flag where the perfect-foresight LP would shed load
> (notably tight peak-shaving ceilings).
