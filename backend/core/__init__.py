"""
DIP optimizer — behind-the-meter sizing + dispatch engine.

Module map (see optimizer/README.md for the full flow):

  Foundations (shared)
    schema.py          column-name + flows-table constants (single source)
    params.py          PhysicalParams, EconomicParams, SizingResult
    solver.py          HiGHS solver factory
    profile_loader.py  load/validate the 8760 load+PV profiles

  Phase 1 — physical sizing (no economics)
    sizing_engine.py   Model O: perfect-foresight LP (lower-bound size)
    rule_dispatch.py   Model R: causal BTM dispatch + KPIs + deliverable/EoL sizing
    curve_runner.py    sweep orchestration → sizing curves
    curve_reporter.py  write Phase-1 curves to Excel

  Phase 2 — techno-economic overlay
    economic_overlay.py  CAPEX/OPEX/LCOE on the Phase-1 curves
    phase2_reporter.py   write Phase-2 results to Excel

  Output
    plotter.py         interactive HTML/PNG plots

The superseded original pipeline lives in archive/legacy_pipeline/ and is not
imported here.
"""
