# optimizer/ — module map

Behind-the-meter sizing + dispatch engine for the DIP Italy data-centre project.
Two phases, driven from the repo-root `main.py`.

## Data flow

```
                 data/8760_PV&Load Profiles.xlsx
                            │
                   profile_loader.py        ── load + validate profiles
                            │
        ┌───────────────────┴────────────────────┐
        │                PHASE 1                  │   (physics only, no €)
        │                                         │
        │   curve_runner.py  ── sweeps targets    │
        │        │                                │
        │        ├─ sizing_engine.py  (Model O)   │   perfect-foresight LP
        │        │     = smallest BESS that COULD  │   → lower-bound size
        │        │       hit the target            │
        │        │                                │
        │        └─ rule_dispatch.py  (Model R)   │   causal BTM controller
        │              = smallest BESS that        │   (no foresight)
        │                ACTUALLY hits it, + EoL   │   → deliverable size
        │              gross-up for ~20yr fade     │
        │        │                                │
        │   curve_reporter.py ── Phase1_Sizing_Curves.xlsx
        └───────────────────┬────────────────────┘
                            │
        ┌───────────────────┴────────────────────┐
        │                PHASE 2                  │   (overlay economics)
        │   economic_overlay.py ── CAPEX/OPEX/LCOE │
        │   phase2_reporter.py  ── Phase2_TechnoEconomic.xlsx
        └─────────────────────────────────────────┘
                            │
                      plotter.py  ── interactive HTML/PNG
```

## Two dispatch models (the core idea)

| | Model O — `sizing_engine.py` | Model R — `rule_dispatch.py` |
|---|---|---|
| What | perfect-foresight LP | causal merit-order rule (direct → charge → discharge → grid) |
| Role | theoretical **lower bound** on BESS size | **contract truth** — what the site actually delivers |
| Use  | fast first-pass curve | re-size to meet the target for real + verify + EoL gross-up |

Every Phase-1 curve point carries both: the LP size and the deliverable
(Model R) size, plus the end-of-life-sized install (`attach_rule_sizing`).

## Foundations (shared by both phases)

- `schema.py` — all column names + the canonical flows table (`FlowCols`). KPIs
  are defined once, in `rule_dispatch.compute_flow_kpis`.
- `params.py` — `PhysicalParams` (Phase 1), `EconomicParams` (Phase 2),
  `SizingResult`.
- `solver.py` — HiGHS factory.

## Not here

The original Phase-2 "full" pipeline (config Excel → rolling-LP dispatch →
financials) is archived in `../archive/legacy_pipeline/` and is not imported.
