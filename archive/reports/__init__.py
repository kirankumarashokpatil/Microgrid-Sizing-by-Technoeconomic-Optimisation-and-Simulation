"""Per-scenario report builders (Excel + interactive HTML) for the DIP optimiser.

Each scenario has its own folder with a runnable ``build.py``:
  reports/ssr_curve/        — SSR target -> min BESS curve, per-scenario simulation
  reports/peak_shaving/     — grid-connection target -> min BESS curve
  reports/co_optimisation/  — joint SSR + GC (one LP, both as constraints)
  reports/pv_bess_surface/  — PV x SSR -> min BESS 3D surface

Shared helpers live in reports.common. Run any builder from the repo root, e.g.
  python reports/ssr_curve/build.py
"""
