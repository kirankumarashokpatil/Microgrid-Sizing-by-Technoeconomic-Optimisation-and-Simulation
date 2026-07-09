"""
DIP optimizer — behind-the-meter sizing + dispatch engine.

Module map (see optimizer/README.md for the full flow):

  Foundations (shared)
    schema.py          column-name + flows-table constants (single source)
    params.py          PhysicalParams, SizingResult
    solver.py          HiGHS solver factory
    profile_loader.py  load/validate the 8760 load+PV profiles

  Phase 1 — physical sizing
    sizing_engine.py   Model O: perfect-foresight LP (lower-bound size)
    rule_dispatch.py   Model R: causal BTM dispatch + KPIs + deliverable/EoL sizing
    curve_runner.py    sweep orchestration → sizing curves
    curve_reporter.py  write Phase-1 curves to Excel

  Output
    plotter.py         interactive HTML/PNG plots

The superseded original pipeline lives in archive/legacy_pipeline/ and is not
imported here.
"""
