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
                      plotter.py  ── interactive HTML/PNG
```

## Three dispatch models (the core idea)

| | Model O — `sizing_engine.py` | Model W — `rolling_dispatch.py` | Model R — `rule_dispatch.py` |
|---|---|---|---|
| What | perfect-foresight LP | rolling-horizon LP (look ahead `horizon_h`, commit `commit_h`, roll) | causal merit-order rule (direct → charge → discharge → grid) |
| Foresight | whole year | limited (a forecast window) | none |
| Role | theoretical **lower bound** on BESS size | **realistic operation** — what a forecast-driven controller achieves | **contract truth** — what the site actually delivers with no foresight |
| Use  | fast first-pass curve | operate a FIXED design + show the "value of foresight" vs R | re-size to meet the target for real + verify + EoL gross-up |

Every Phase-1 curve point carries both the LP size and the deliverable
(Model R) size, plus the end-of-life-sized install (`attach_rule_sizing`).

**Model W** primarily operates an already-sized design. It emits the same canonical
`FlowsFrame`, so `compute_flow_kpis` / `validate_flows` reuse unchanged. It can also
size (`size_under_rolling`), but note the measured result: **W's deliverable size ≈
R's** (identical for SSR; marginally larger for peak-shaving, since R's greedy grid
valley-fill already holds a ceiling well). So foresight's value is *operational*
(efficiency, holding a lower connection at a given battery), **not** a smaller
recommended battery — the sizing routine is advisory/transparency, not a "buy less"
lever. Each window is a small SciPy/HiGHS LP; the per-window matrices are built
once per window length and reused across the year. It is where the battery can
**anticipate a peak and pre-charge** to hold a lower grid connection — the value R
(reactive/greedy) leaves on the table. Exposed via `POST /dispatch-rolling` and the
Excel export's "Dispatch model comparison" sheet. Grid-charging (pre-charge from the
grid) is gated by the same `allow_grid_charge` policy the UI exposes.

## Foundations (shared by both phases)

- `schema.py` — all column names + the canonical flows table (`FlowCols`). KPIs
  are defined once, in `rule_dispatch.compute_flow_kpis`.
- `params.py` — `PhysicalParams`, `SizingResult`.
- `solver.py` — HiGHS factory.

## Not here

The original Phase-2 "full" pipeline (config Excel → rolling-LP dispatch →
financials) is archived in `../archive/legacy_pipeline/` and is not imported.
