# Legacy pipeline (archived — not used by the live optimizer)

These modules were the **original Phase-2 "full" pipeline**. They are superseded
by the current two-phase engine in `optimizer/` and are kept here only for
reference. **Nothing in `main.py` or `optimizer/` imports them.**

| File | Was | Replaced by |
|------|-----|-------------|
| `config_loader.py`   | read project-config Excel → `ProjectParams` | `optimizer/profile_loader.py` + CLI args |
| `dispatch_engine.py` | rolling-horizon LP dispatch | `optimizer/rule_dispatch.py` (Model R causal dispatch) |
| `financials.py`      | NPV / LCOE on a single sized design | `optimizer/economic_overlay.py` |
| `kpi_engine.py`      | SSR/SCR KPIs from a sim frame | `optimizer/rule_dispatch.compute_flow_kpis()` (single source of truth) |
| `reporter.py`        | plain-text result dump | `optimizer/curve_reporter.py` + `phase2_reporter.py` |
| `legacy_params.py`   | `ProjectParams` combined param object | `optimizer/params.py` (`PhysicalParams` + `EconomicParams`) |

This is a frozen snapshot: it is not guaranteed to run. If you need any of this
behaviour, port it into the live `optimizer/` package rather than reviving it here.
