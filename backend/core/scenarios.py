"""
Scenario Registry & Runner
--------------------------
ONE place that knows every scenario the optimiser can answer, and ONE way to
run any of them and get outputs in a consistent shape.

Design (why this exists)
========================
The full scenario library (S00…S123) is not ~50 different optimisers — it is a
handful of engine capabilities reused many ways, each a choice of:

    topology         grid_connected_btm | bess_load_only | off_grid | standalone_gen
    fixed inputs     what the development manager supplies (load, PV size, …)
    solve variables  what the optimiser returns (BESS MW/MWh, PV MW, GCmin, …)
    target           the physical goal (SSR %, GC ceiling MW, firmness %, curtailment %)
    answer mode      point | curve | surface | frontier | forward_eval | bound
    policy           dispatch model, grid-charge rule, export rule, lifecycle

So a scenario is DATA (a ScenarioSpec row), not a bespoke function. `run_scenario`
looks the row up and dispatches to the right existing solver, then returns a
uniform `ScenarioResult` (design + KPIs + optional table + optional flows). Adding
a scenario is adding a row; the output contract never changes.

Status of each row is explicit and honest:
    READY                — runs on the engine today
    NEEDS_PV_VARIABLE    — needs PV promoted to an LP decision variable (the one
                           real engine change; unlocks the co-design family)
    NEEDS_CONSUMER_LAYER — needs the load preprocessor (port schedule→profile,
                           multi-consumer aggregation); no engine change
    NEEDS_MINOR          — small add-on (utilisation filter, frontier sweep,
                           symmetric surface) over an existing solver

`coverage()` reports the counts so "does this cover everything?" is answerable at
a glance, and the un-READY rows are the precise backlog to work through one by one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd

from core.params import PhysicalParams, SizingResult
from core.schema import CurveCols, KpiKeys, FlowCols


# ──────────────────────────────────────────────────────────────────────────────
# Status
# ──────────────────────────────────────────────────────────────────────────────

class Status:
    READY                = "ready"
    NEEDS_PV_VARIABLE    = "needs_pv_variable"
    NEEDS_CONSUMER_LAYER = "needs_consumer_layer"
    NEEDS_MINOR          = "needs_minor"


# ──────────────────────────────────────────────────────────────────────────────
# Spec, context, result — the three data shapes
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ScenarioSpec:
    """One row of the registry: what the scenario is and how to run it."""
    id: str
    name: str
    stage: str
    topology: str
    family: str
    fixed_inputs: str
    solve_vars: str
    target_metric: str
    answer_mode: str           # point | curve | surface | frontier | forward_eval | bound
    handler: str               # key into _HANDLERS (or "" when not yet runnable)
    status: str
    needs: str = ""            # one-line note on what's missing for non-READY rows


@dataclass
class ScenarioContext:
    """
    Everything a handler might need to actually run. Callers fill in what their
    chosen scenario requires; sensible defaults cover the rest. One context can
    drive many scenarios.
    """
    profiles_df: pd.DataFrame
    params: PhysicalParams
    pv_mw: float = 0.0                      # fixed PV nameplate (when PV is an input)
    target_ssr_pct: float = 60.0
    target_gc_mw: Optional[float] = None
    target_firmness_pct: float = 99.0
    target_curtailment_pct: float = 5.0
    # for forward-eval / fixed-design scenarios:
    bess_mw: float = 0.0
    bess_mwh: float = 0.0
    grid_ceiling_mw: Optional[float] = None
    # S42 deliverable sizing: which target to hit ("ssr" or "gc"). Explicit so the
    # result never depends on whether an unrelated context field is populated.
    deliverable_target: str = "ssr"
    # for sweeps:
    pv_sweep_mw: tuple = ()
    ssr_targets_pct: tuple = ()
    bess_sweep_mw: tuple = ()
    solver_time_limit: int = 120


@dataclass
class ScenarioResult:
    """Uniform output of any scenario run."""
    id: str
    name: str
    answer_mode: str
    status: str
    feasible: bool = True
    design: dict = field(default_factory=dict)   # pv_mw, bess_mw, bess_mwh, gc_mw, …
    kpis: dict = field(default_factory=dict)      # SSR, SCR, GCmin, unmet, …
    table: Optional[pd.DataFrame] = None          # curve / surface / frontier
    flows: Optional[pd.DataFrame] = None          # timestep FlowsFrame (forward eval)
    notes: str = ""

    def summary(self) -> str:
        head = f"[{self.id}] {self.name}  ({self.answer_mode}"
        head += "" if self.feasible else ", INFEASIBLE"
        head += ")"
        lines = [head]
        if self.status != Status.READY:
            return head + f"\n   ⏳ not yet runnable — {self.notes}"
        if self.design:
            d = self.design
            bits = []
            if d.get("pv_mw"):   bits.append(f"PV {d['pv_mw']:.0f} MW")
            if d.get("bess_mw") is not None:
                bits.append(f"BESS {d.get('bess_mw',0):.1f} MW / {d.get('bess_mwh',0):.1f} MWh")
            if d.get("bess_mwh_eol"):
                bits.append(f"(EoL {d['bess_mwh_eol']:.1f} MWh)")
            if d.get("gc_mw") is not None:
                bits.append(f"GC {d['gc_mw']:.1f} MW")
            if bits:
                lines.append("   design: " + " | ".join(bits))
        if self.kpis:
            k = self.kpis
            kbits = []
            for label, key in (("SSR", KpiKeys.SSR), ("SCR", KpiKeys.SCR),
                               ("peak", KpiKeys.GCMIN_PEAK), ("unmet", KpiKeys.TOTAL_UNMET_LOAD)):
                if key in k:
                    unit = "%" if "%" in key else (" MWh" if "MWh" in key else " MW")
                    kbits.append(f"{label} {k[key]:.1f}{unit}")
            if kbits:
                lines.append("   KPIs: " + " | ".join(kbits))
        if self.table is not None:
            lines.append(f"   table: {len(self.table)} rows")
        if self.notes:
            lines.append(f"   note: {self.notes}")
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Handlers — small adapters from ScenarioContext to ScenarioResult, each reusing
# an existing solver. Registered by name in _HANDLERS.
# ──────────────────────────────────────────────────────────────────────────────

def _point_from_sizing(spec: ScenarioSpec, res: SizingResult,
                        ctx: ScenarioContext) -> ScenarioResult:
    """Build a point ScenarioResult from an LP SizingResult, plus the honest
    operational (Model R) KPIs for grid-connected BTM designs."""
    design = {
        "pv_mw":   res.pv_mw,
        "bess_mw": res.bess_mw,
        "bess_mwh": res.bess_mwh,
        "gc_mw":   res.peak_grid_mw,
        "duration_h": res.bess_duration_h,
    }
    kpis = {
        KpiKeys.SSR: res.achieved_ssr_pct,
        KpiKeys.SCR: res.achieved_scr_pct,
        KpiKeys.GCMIN_PEAK: res.peak_grid_mw,
    }
    notes = ""
    flows = None
    # Honest operational check under the causal rule (cheap, on-brief). We keep
    # the flows it produces so a point design can be SEEN operating over the year
    # (energy provenance, dispatch on the peak day, when the grid is used) — not
    # just reported as scalars.
    if res.feasible and ctx.params.site_topology == "grid_connected_btm":
        from core.rule_dispatch import verify_sizing_with_rule
        ttype = "peak_shaving" if res.target_type == "peak_shaving" else "ssr"
        ceiling = (res.target_value if res.target_type == "peak_shaving"
                   else ctx.params.site_max_grid_mw)
        try:
            opk, flows = verify_sizing_with_rule(
                ctx.profiles_df, ctx.params,
                pv_mw=res.pv_mw, bess_mw=res.bess_mw, bess_mwh=res.bess_mwh,
                target_type=ttype, grid_ceiling_mw=ceiling,
            )
            kpis[KpiKeys.SSR] = opk[KpiKeys.SSR]           # Model R is the truth
            kpis[KpiKeys.SCR] = opk[KpiKeys.SCR]
            kpis[KpiKeys.GCMIN_PEAK] = opk[KpiKeys.GCMIN_PEAK]
            kpis[KpiKeys.TOTAL_UNMET_LOAD] = opk[KpiKeys.TOTAL_UNMET_LOAD]
            notes = "KPIs are operational (Model R); LP is the lower bound."
        except Exception:
            flows = None
            notes = "LP KPIs only (operational verify unavailable)."
    return ScenarioResult(spec.id, spec.name, "point", spec.status,
                          feasible=res.feasible, design=design, kpis=kpis,
                          flows=flows, notes=notes)


def _h_point_ssr(spec, ctx):
    from core.sizing_engine import solve_sizing_point
    res = solve_sizing_point(ctx.profiles_df, ctx.params, ctx.pv_mw,
                             "ssr", ctx.target_ssr_pct,
                             scenario_label=spec.id, solver_time_limit=ctx.solver_time_limit)
    return _point_from_sizing(spec, res, ctx)


def _h_point_gc(spec, ctx):
    from core.sizing_engine import solve_sizing_point
    gc = ctx.target_gc_mw if ctx.target_gc_mw is not None else float(ctx.profiles_df["load_mw"].max())
    res = solve_sizing_point(ctx.profiles_df, ctx.params, ctx.pv_mw,
                             "peak_shaving", gc,
                             scenario_label=spec.id, solver_time_limit=ctx.solver_time_limit)
    return _point_from_sizing(spec, res, ctx)


def _h_point_firmness(spec, ctx):
    import dataclasses
    from core.sizing_engine import solve_sizing_point
    # Firmness is sized "subject to the grid cap": thread the GC cap into the LP's
    # grid ceiling (and the operational verify) when supplied, else the site max.
    eff = ctx
    if ctx.grid_ceiling_mw is not None:
        eff = dataclasses.replace(
            ctx, params=dataclasses.replace(ctx.params, site_max_grid_mw=ctx.grid_ceiling_mw))
    res = solve_sizing_point(eff.profiles_df, eff.params, eff.pv_mw,
                             "firmness", eff.target_firmness_pct,
                             scenario_label=spec.id, solver_time_limit=eff.solver_time_limit)
    return _point_from_sizing(spec, res, eff)


def _curve_result(spec, ctx, curve_df, *, with_overlays=True):
    """Attach Model R overlays (operational + deliverable + EoL) to a curve and
    wrap it in a ScenarioResult."""
    if with_overlays and ctx.params.site_topology == "grid_connected_btm" \
            and curve_df is not None and not curve_df.empty:
        from core.rule_dispatch import attach_operational_kpis, attach_rule_sizing
        curve_df = attach_operational_kpis(curve_df, ctx.profiles_df, ctx.params)
        curve_df = attach_rule_sizing(curve_df, ctx.profiles_df, ctx.params)
    feasible = bool(curve_df is not None and not curve_df.empty
                    and curve_df.get(CurveCols.FEASIBLE, pd.Series([False])).any())
    return ScenarioResult(spec.id, spec.name, spec.answer_mode, spec.status,
                          feasible=feasible, table=curve_df)


def _h_curve_ssr(spec, ctx):
    from core.curve_runner import run_ssr_curve
    df = run_ssr_curve(ctx.profiles_df, ctx.params, ctx.pv_mw,
                       scenario_label=spec.name, solver_time_limit=ctx.solver_time_limit)
    return _curve_result(spec, ctx, df)


def _h_curve_gc(spec, ctx):
    from core.curve_runner import run_peak_shaving_curve
    df = run_peak_shaving_curve(ctx.profiles_df, ctx.params, ctx.pv_mw,
                                scenario_label=spec.name, solver_time_limit=ctx.solver_time_limit)
    return _curve_result(spec, ctx, df)


def _h_sub_curve_gc(spec, ctx):
    from core.curve_runner import run_sub_peak_shaving_curve
    df = run_sub_peak_shaving_curve(ctx.profiles_df, ctx.params,
                                    scenario_label=spec.name, solver_time_limit=ctx.solver_time_limit)
    return _curve_result(spec, ctx, df)


def _h_surface_ssr(spec, ctx):
    from core.curve_runner import run_pv_bess_surface
    pv_pts = ctx.pv_sweep_mw or (50.0, 100.0, 150.0, 200.0, 250.0)
    ssr_tgts = ctx.ssr_targets_pct or tuple(range(10, 90, 10))
    df = run_pv_bess_surface(ctx.profiles_df, ctx.params, pv_pts, ssr_tgts,
                             scenario_label=spec.name, solver_time_limit=ctx.solver_time_limit)
    return _curve_result(spec, ctx, df)


def _h_co_opt(spec, ctx):
    from core.curve_runner import run_co_opt_scenario
    gc = ctx.target_gc_mw if ctx.target_gc_mw is not None else round(float(ctx.profiles_df["load_mw"].max()) * 0.8, 0)
    ssr_tgts = ctx.ssr_targets_pct or tuple(range(10, 90, 10))
    df = run_co_opt_scenario(ctx.profiles_df, ctx.params, ctx.pv_mw, ssr_tgts, gc,
                             scenario_label=spec.name, solver_time_limit=ctx.solver_time_limit)
    return _curve_result(spec, ctx, df)


def _h_off_grid(spec, ctx):
    from core.curve_runner import run_off_grid_curve
    import copy
    p = copy.copy(ctx.params); p.site_topology = "off_grid"
    pv_sweep = ctx.pv_sweep_mw or (150.0, 200.0, 250.0, 300.0, 400.0)
    df = run_off_grid_curve(ctx.profiles_df, p, ctx.target_firmness_pct, pv_sweep,
                            scenario_label=spec.name, solver_time_limit=ctx.solver_time_limit)
    return _curve_result(spec, ctx, df, with_overlays=False)


def _h_standalone(spec, ctx):
    from core.curve_runner import run_standalone_export_curve
    import copy
    p = copy.copy(ctx.params); p.site_topology = "standalone_gen"
    if p.export_limit_mw <= 0:
        p.export_limit_mw = 50.0
    curt = ctx.ssr_targets_pct or (0.0, 5.0, 10.0, 15.0, 20.0)
    df = run_standalone_export_curve(ctx.profiles_df, p, ctx.pv_mw, curt,
                                     scenario_label=spec.name, solver_time_limit=ctx.solver_time_limit)
    return _curve_result(spec, ctx, df, with_overlays=False)


def _h_forward_eval(spec, ctx):
    """Fixed design → operational KPIs + flows under the causal rule (Model R).

    Dispatch policy is decided by the design itself, never by an incidental
    context field: no generation ⇒ peak-shaving with off-peak grid-charge (the
    only way a BESS-only site does anything); otherwise self-sufficiency ("grid
    as last resort"), which also yields the true GCmin for a fixed design.
    """
    from core.rule_dispatch import run_rule_dispatch, compute_flow_kpis, validate_flows
    pv0 = ctx.pv_mw
    no_pv = pv0 <= 1e-9
    ceiling = ctx.grid_ceiling_mw if ctx.grid_ceiling_mw is not None else ctx.params.site_max_grid_mw
    mode = "peak_shaving" if no_pv else "self_sufficiency"
    # Frontend dispatch-policy overrides (params); fall back to the design-derived
    # default (grid as last resort, grid-charge only for a BESS-only site).
    priority = tuple(getattr(ctx.params, "dispatch_priority", ()) or ())
    gc_override = getattr(ctx.params, "allow_grid_charge", None)
    grid_charge = gc_override if gc_override is not None else no_pv
    flows = run_rule_dispatch(ctx.profiles_df, ctx.params,
                              pv_mw=pv0,
                              bess_mw=ctx.bess_mw, bess_mwh=ctx.bess_mwh,
                              grid_ceiling_mw=ceiling, mode=mode,
                              allow_grid_charge=grid_charge, priority=priority)
    validate_flows(flows, ctx.params, bess_mwh=ctx.bess_mwh, export_limit_mw=ctx.params.export_limit_mw)
    kpis = compute_flow_kpis(flows, ctx.params.dt_hours)
    design = {"pv_mw": pv0 if not no_pv else 0.0, "bess_mw": ctx.bess_mw,
              "bess_mwh": ctx.bess_mwh, "gc_mw": kpis[KpiKeys.GCMIN_PEAK]}
    return ScenarioResult(spec.id, spec.name, "forward_eval", spec.status,
                          feasible=True, design=design, kpis=kpis, flows=flows)


def _h_bound_gcmin(spec, ctx):
    from core.sizing_engine import find_gc_min
    gc = find_gc_min(ctx.profiles_df, ctx.params, ctx.pv_mw, ctx.solver_time_limit)
    return ScenarioResult(spec.id, spec.name, "bound", spec.status,
                          feasible=True, design={"pv_mw": ctx.pv_mw, "gc_mw": gc},
                          notes="GCmin = minimum feasible grid connection (LP, BESS free).")


def _h_bound_ssrmax(spec, ctx):
    from core.sizing_engine import find_ssr_max
    ssr = find_ssr_max(ctx.profiles_df, ctx.params, ctx.pv_mw, ctx.solver_time_limit)
    return ScenarioResult(spec.id, spec.name, "bound", spec.status,
                          feasible=True, design={"pv_mw": ctx.pv_mw},
                          kpis={KpiKeys.SSR: ssr},
                          notes="SSRmax = ceiling SSR reachable with this PV (LP, BESS free).")


def _h_deliverable(spec, ctx):
    """LP-sized point → deliverable BESS under the causal rule + EoL gross-up."""
    from core.rule_dispatch import size_by_bisection
    ttype = "peak_shaving" if ctx.deliverable_target == "gc" else "ssr"
    if ttype == "peak_shaving":
        tval = ctx.target_gc_mw if ctx.target_gc_mw is not None else float(ctx.profiles_df["load_mw"].max())
    else:
        tval = ctx.target_ssr_pct
    res = size_by_bisection(ctx.profiles_df, ctx.params, pv_mw=ctx.pv_mw,
                            target_type=ttype, target_value=tval)
    ret = ctx.params.eol_retention_fraction
    design = {"pv_mw": ctx.pv_mw, "bess_mw": res.bess_mw, "bess_mwh": res.bess_mwh,
              "bess_mwh_eol": round(res.bess_mwh / ret, 2)}
    return ScenarioResult(spec.id, spec.name, "point", spec.status,
                          feasible=res.feasible, design=design, kpis=res.kpis,
                          notes="Deliverable size meets the target under the causal rule.")


_HANDLERS: dict[str, Callable[[ScenarioSpec, ScenarioContext], ScenarioResult]] = {
    "point_ssr":     _h_point_ssr,
    "point_gc":      _h_point_gc,
    "point_firmness":_h_point_firmness,
    "curve_ssr":     _h_curve_ssr,
    "curve_gc":      _h_curve_gc,
    "sub_curve_gc":  _h_sub_curve_gc,
    "surface_ssr":   _h_surface_ssr,
    "co_opt":        _h_co_opt,
    "off_grid":      _h_off_grid,
    "standalone":    _h_standalone,
    "forward_eval":  _h_forward_eval,
    "bound_gcmin":   _h_bound_gcmin,
    "bound_ssrmax":  _h_bound_ssrmax,
    "deliverable":   _h_deliverable,
}


# ──────────────────────────────────────────────────────────────────────────────
# THE REGISTRY — every scenario, one row each (single source of truth)
# ──────────────────────────────────────────────────────────────────────────────

def _row(id, name, stage, topo, family, fixed, solve, target, mode, handler, status, needs=""):
    return ScenarioSpec(id, name, stage, topo, family, fixed, solve, target, mode, handler, status, needs)

_BTM = "grid_connected_btm"
_SUB = "bess_load_only"
_OFF = "off_grid"
_STD = "standalone_gen"

REGISTRY: list[ScenarioSpec] = [
    # ── Stage 0 — foundation ────────────────────────────────────────────────
    _row("S00_FIXED_DESIGN_EVAL", "Fixed Design KPI Evaluation", "0", _BTM, "A",
         "load, pv_mw, bess_mw, bess_mwh", "—", "none", "forward_eval", "forward_eval", Status.READY),
    # ── Stage 1 — core point solves ─────────────────────────────────────────
    _row("S11_BTM_SSR_TARGET_BESS", "Min BESS for Target SSR", "1", _BTM, "A",
         "load, pv_mw", "bess_mw, bess_mwh", "ssr_pct", "point", "point_ssr", Status.READY),
    _row("S12_BTM_GC_TARGET_BESS", "Min BESS for Target Grid Connection", "1", _BTM, "A",
         "load, pv_mw", "bess_mw, bess_mwh", "grid_ceiling_mw", "point", "point_gc", Status.READY),
    # ── Stage 2 — core curves ───────────────────────────────────────────────
    _row("S21_BTM_SSR_CURVE", "BESS Curve vs Self-Sufficiency", "2", _BTM, "A",
         "load, pv_mw", "bess per ssr point", "ssr_pct_range", "curve", "curve_ssr", Status.READY),
    _row("S22_BTM_GC_CURVE", "BESS Curve vs Grid Connection", "2", _BTM, "A",
         "load, pv_mw", "bess per gc point", "gc_range_mw", "curve", "curve_gc", Status.READY),
    # ── Stage 3 — BESS-only sub-scenarios ───────────────────────────────────
    _row("S31_SUB_GC_TARGET_BESS", "Min BESS for Backup/Peak (no gen)", "3", _SUB, "E",
         "load (PV=0)", "bess_mw, bess_mwh", "grid_ceiling_mw", "point", "point_gc", Status.READY),
    _row("S32_SUB_GC_CURVE", "Backup BESS Curve vs Grid Connection", "3", _SUB, "E",
         "load (PV=0)", "bess per gc point", "gc_range_mw", "curve", "sub_curve_gc", Status.READY),
    _row("S33_SUB_FIXED_BESS_EVAL", "Fixed BESS Backup Evaluation", "3", _SUB, "E",
         "load, bess_mw, bess_mwh", "residual peak, unmet", "none", "forward_eval", "forward_eval", Status.READY),
    _row("S34_SUB_FIRMNESS_TARGET_BESS", "Min BESS for Backup Firmness", "3", _SUB, "E",
         "load, gc_cap (PV=0)", "bess_mw, bess_mwh", "firmness_pct", "point", "point_firmness", Status.READY),
    _row("S35_SUB_UTILISATION_BESS_CURVE", "Utilisation-Constrained Backup Curve", "3", _SUB, "E",
         "load, min_utilisation", "bess curve vs gc", "gc_range under util floor", "curve", "", Status.NEEDS_MINOR,
         "sub GC curve + equivalent-full-cycle / active-hours filter"),
    # ── Stage 4 — operational truth ─────────────────────────────────────────
    _row("S41_OPERATIONAL_VERIFY", "Operational Verification (causal)", "4", _BTM, "A",
         "any fixed design", "operational SSR/SCR/peak", "none", "forward_eval", "forward_eval", Status.READY),
    _row("S42_DELIVERABLE_BESS_SIZE", "Deliverable BESS Under Real Operation", "4", _BTM, "A",
         "target, ~duration", "deliverable bess (+EoL)", "ssr_pct | gc_mw", "point", "deliverable", Status.READY),
    # ── Stage 5 — grid minimisation ─────────────────────────────────────────
    _row("S51_BTM_GCMIN_BESSOPT", "Min Grid Connection, Optimised BESS", "5", _BTM, "C",
         "load, pv_mw", "bess, gcmin", "min_gc", "bound", "bound_gcmin", Status.READY),
    _row("S52_BTM_GCMIN_FIXED_DESIGN", "Min Grid Connection, Fixed Design", "5", _BTM, "C",
         "load, pv_mw, bess", "gcmin", "min_peak_grid", "forward_eval", "forward_eval", Status.READY),
    _row("S53_BTM_FULL_CLOSED_LOOP_SSR", "Full Closed-Loop Design for SSR", "5", _BTM, "C",
         "load, pv_unit_profile", "pv_mw, bess, gc", "ssr_pct", "point", "", Status.NEEDS_PV_VARIABLE,
         "PV as LP decision variable + GC objective"),
    _row("S54_BTM_SSR_UNDER_GC_CAP", "Grid-Limited SSR Design", "5", _BTM, "C",
         "load, gc_cap", "pv_mw, bess", "ssr under gc cap", "point", "", Status.NEEDS_PV_VARIABLE,
         "PV variable under fixed GC cap"),
    _row("S55_BTM_GCMIN_FRONTIER", "GC Frontier Under BESS Sweep", "5", _BTM, "C",
         "load, pv context", "gcmin per bess size", "gc frontier", "frontier", "", Status.NEEDS_MINOR,
         "outer BESS sweep → find_gc_min per size"),
    # ── Stage 6 — generation solving ────────────────────────────────────────
    _row("S61_BTM_PVBESS_SSR_TARGET", "Min PV+BESS for SSR", "6", _BTM, "B",
         "load, pv_unit_profile", "pv_mw, bess", "ssr_pct", "point", "", Status.NEEDS_PV_VARIABLE,
         "PV as LP decision variable"),
    _row("S62_BTM_PVBESS_SSR_SURFACE", "PV–BESS Surface vs SSR", "6", _BTM, "B",
         "load, pv_sweep, ssr_sweep", "bess per (pv,ssr)", "ssr+pv range", "surface", "surface_ssr", Status.READY,
         "approximates co-optimum by sweeping PV (exact once PV is a variable)"),
    _row("S63_BTM_PVBESS_GC_TARGET", "Min PV+BESS for Grid Connection", "6", _BTM, "B",
         "load, pv_unit_profile", "pv_mw, bess", "grid_ceiling_mw", "point", "", Status.NEEDS_PV_VARIABLE,
         "PV variable + GC constraint"),
    _row("S64_BTM_PVBESS_GC_SURFACE", "PV–BESS Surface vs Grid Connection", "6", _BTM, "B",
         "load, pv_sweep, gc_sweep", "bess per (pv,gc)", "gc+pv range", "surface", "", Status.NEEDS_MINOR,
         "symmetric sweep to run_pv_bess_surface but over GC"),
    # ── Stage 7 — joint targets ─────────────────────────────────────────────
    _row("S71_BTM_SSR_PLUS_GC_BESS", "Min BESS for SSR within Grid Limit", "7", _BTM, "A",
         "load, pv_mw, gc_cap", "bess", "ssr & gc", "point", "co_opt", Status.READY),
    _row("S72_BTM_PVBESS_SSR_PLUS_GC", "Min PV+BESS for SSR within Grid Limit", "7", _BTM, "B",
         "load, gc_cap", "pv_mw, bess", "ssr & gc", "point", "", Status.NEEDS_PV_VARIABLE,
         "PV variable + SSR target + GC cap"),
    # ── Stage 8 — consumer-specific ─────────────────────────────────────────
    _row("S81_DC_SELF_SUFFICIENT_DESIGN", "Data Centre Self-Sufficient Design", "8", _BTM, "F",
         "dc_load, pv_unit_profile", "pv_mw, bess, gc", "ssr_pct", "point", "", Status.NEEDS_PV_VARIABLE,
         "DC load + PV variable co-design (= S53 with DC load)"),
    _row("S82_DC_FIRMNESS_DESIGN", "Data Centre Firmness Design", "8", _BTM, "F",
         "dc_load, pv_unit_profile", "pv_mw, bess, gc", "firmness_pct", "point", "", Status.NEEDS_PV_VARIABLE,
         "PV variable + firmness target"),
    _row("S83_PORT_EVENT_BESS_DESIGN", "Port BESS for Vessel Events", "8", _BTM, "F",
         "vessel_schedule, gc_cap", "bess", "event coverage", "point", "", Status.NEEDS_CONSUMER_LAYER,
         "schedule→load builder, then point_gc"),
    _row("S84_PORT_PRECHARGE_DESIGN", "Port Pre-Charged BESS under GC", "8", _BTM, "F",
         "vessel_schedule, gc_limit", "bess", "peak coverage in cap", "point", "", Status.NEEDS_CONSUMER_LAYER,
         "schedule→load + schedule-aware pre-charge policy"),
    _row("S85_PORT_REINFORCEMENT_AVOIDANCE", "Port Reinforcement Avoidance", "8", _BTM, "F",
         "vessel_schedule, gc_cap", "pv?, bess", "avoid reinforcement", "point", "", Status.NEEDS_CONSUMER_LAYER,
         "schedule→load, then co_opt / reliability under cap"),
    _row("S86_MULTI_CONSUMER_DESIGN", "Multi-Consumer Shared BESS", "8", _BTM, "F",
         "many loads, pv_unit_profile", "pv_mw, bess, gc", "ssr_pct", "point", "", Status.NEEDS_CONSUMER_LAYER,
         "aggregate loads → then PV-variable co-design"),
    _row("S87_DC_PORT_COLOCATION", "DC + Port Colocation Design", "8", _BTM, "F",
         "dc_load + port_schedule", "shared bess, pv?, gc", "ssr/gc/firmness", "point", "", Status.NEEDS_CONSUMER_LAYER,
         "aggregate DC + port load → co-design"),
    # ── Stage 9 — off-grid & standalone ─────────────────────────────────────
    _row("S91_OFFGRID_FIRMNESS_PVBESS", "Off-Grid Firmness Design", "9", _OFF, "G",
         "load, pv_sweep", "pv?, bess", "firmness_pct", "curve", "off_grid", Status.READY),
    _row("S92_OFFGRID_GENMIN", "Off-Grid Minimum Generation", "9", _OFF, "G",
         "load, pv_unit_profile", "pv_mw, bess", "firmness_pct", "point", "", Status.NEEDS_PV_VARIABLE,
         "generation as decision variable (off-grid)"),
    _row("S93_STANDALONE_EXPORTLIMIT_BESS", "Standalone Gen w/ Export Limit", "9", _STD, "G",
         "pv_profile, pv_mw, export_limit", "bess", "curtailment_pct", "curve", "standalone", Status.READY),
    _row("S94_STANDALONE_GENSIZE_EXPORTLIMIT", "Standalone Generation Sizing", "9", _STD, "G",
         "pv_unit_profile, export_limit", "pv_mw, bess?", "usable export", "point", "", Status.NEEDS_PV_VARIABLE,
         "generation size as decision variable"),
    # ── Stage 10 — constraint overlays ──────────────────────────────────────
    _row("S101_SITE_AREA_CONSTRAINED", "Site-Area-Constrained Design", "10", _BTM, "overlay",
         "any base + site_area", "feasible subset", "fits on site", "overlay", "", Status.NEEDS_MINOR,
         "Phase-2 check_site_area exists; wire as overlay on a base scenario"),
    _row("S102_BESS_CAP_CONSTRAINED", "BESS Capacity-Constrained Design", "10", _BTM, "overlay",
         "any base + bess cap", "best feasible", "feasibility under cap", "overlay", "", Status.NEEDS_MINOR,
         "apply site_max_bess_* then run base (param overlay)"),
    _row("S103_PV_CAP_CONSTRAINED", "PV Capacity-Constrained Design", "10", _BTM, "overlay",
         "any gen-solve + pv cap", "constrained design", "binding cap?", "overlay", "", Status.NEEDS_PV_VARIABLE,
         "meaningful only once PV is a variable"),
    _row("S104_IMPORT_ONLY_NO_EXPORT", "Strict Import-Only BTM", "10", _BTM, "overlay",
         "any BTM base", "import-only result", "no export", "overlay", "", Status.NEEDS_MINOR,
         "default already; expose as explicit policy flag"),
    _row("S105_EXPORT_LIMITED", "Export-Limited Hybrid BTM", "10", _BTM, "overlay",
         "any base + export cap", "hybrid result", "export ≤ cap", "overlay", "", Status.NEEDS_MINOR,
         "export_limit_mw supported in LP; wire as policy overlay"),
    # ── Stage 11 — lifecycle & operating ────────────────────────────────────
    _row("S111_DAYONE_SIZING", "Day-One Sizing", "11", _BTM, "overlay",
         "any base", "day-one design", "year-1 only", "overlay", "", Status.NEEDS_MINOR,
         "base run with EoL retention = 100% (lens on existing curves)"),
    _row("S112_EOL_HONEST_SIZING", "End-of-Life-Honest Sizing", "11", _BTM, "overlay",
         "any base + retention", "day-one + EoL size", "target holds at EoL", "overlay", "deliverable", Status.READY,
         "EoL gross-up built into attach_rule_sizing / deliverable handler"),
    _row("S113_SEASONAL_GRID_SHIFT", "Seasonal Grid Time-Shifting", "11", _BTM, "overlay",
         "design + grid headroom", "shifting value", "off-peak charge benefit", "overlay", "", Status.NEEDS_MINOR,
         "Phase-2 analyse_seasonal_shifting exists; wire as overlay"),
    _row("S114_GRID_SERVICES_SOC_RESERVE", "Grid Services with Reserved SOC", "11", _BTM, "overlay",
         "design + reserved soc", "dual-use trade-off", "service value vs BTM", "overlay", "", Status.NEEDS_MINOR,
         "Phase-2 analyse_grid_services exists; wire as overlay"),
    # ── Stage 12 — financial overlays ───────────────────────────────────────
    _row("S121_ECONOMIC_CURVE_RANKING", "Techno-Economic Ranking", "12", _BTM, "overlay",
         "a feasible curve + costs", "commercial optimum", "knee/LCOE/NPV", "overlay", "", Status.NEEDS_MINOR,
         "Phase-2 evaluate_costs + find_optimal_point exist; wire as overlay"),
    _row("S122_FINANCING_SSR_COVENANT", "Financing / Covenant SSR Design", "12", _BTM, "overlay",
         "ssr covenant + costs", "min-capex compliant design", "covenant SSR", "overlay", "", Status.NEEDS_PV_VARIABLE,
         "feasible PV–BESS set (PV variable) + economic ranking"),
    _row("S123_GRID_REINFORCEMENT_VS_BESS", "Reinforcement vs BESS Trade-Off", "12", _BTM, "overlay",
         "GC frontier + costs", "preferred trade-off", "value across frontier", "overlay", "", Status.NEEDS_MINOR,
         "GC frontier (S55) + reinforcement-cost overlay"),
]

_BY_ID = {s.id: s for s in REGISTRY}


# ──────────────────────────────────────────────────────────────────────────────
# Authoritative spec — proper name, Inputs, Outputs, How the optimiser works.
# Transcribed from the DIP Phase-1 docx / strategic-brief PDF scenario tables.
# This is the contract each scenario must honour; the consistency check below
# fails loud if a registry row ever lacks a spec (or vice versa), so the two
# can never silently drift apart.
# ──────────────────────────────────────────────────────────────────────────────

# id -> (proper_name, inputs, outputs, how)
SPEC_DOC: dict[str, tuple[str, str, str, str]] = {
    "S00_FIXED_DESIGN_EVAL": (
        "Fixed Design KPI Evaluation",
        "Fixed load profile, fixed PV profile, fixed PV MW, fixed BESS MW, fixed BESS MWh, grid ceiling, dispatch policy.",
        "SCR, SSR, peak grid, grid import, curtailment, unmet load, flow time series.",
        "Run one full-year dispatch simulation with the fixed design, then compute KPIs from flows. Best done with causal rule dispatch for operational truth."),
    "S11_BTM_SSR_TARGET_BESS": (
        "Minimum BESS for Target Self-Sufficiency",
        "Fixed load profile, fixed PV profile, fixed PV MW, SSR target %, BESS bounds and efficiencies.",
        "Minimum BESS MW, minimum BESS MWh, achieved SSR, achieved SCR, peak grid, feasibility.",
        "Build one annual LP, keep PV fixed, make BESS MW and MWh decision variables, enforce SSR target, minimise BESS MWh with MW as tie-breaker."),
    "S12_BTM_GC_TARGET_BESS": (
        "Minimum BESS for Target Grid Connection",
        "Fixed load profile, fixed PV profile, fixed PV MW, target GC MW, BESS bounds and efficiencies.",
        "Minimum BESS MW, minimum BESS MWh, achieved peak grid, achieved SSR, feasibility.",
        "Build one annual LP, enforce timestep grid ceiling, minimise BESS MW with MWh as tie-breaker."),
    "S21_BTM_SSR_CURVE": (
        "BESS Curve vs Self-Sufficiency",
        "Fixed load profile, fixed PV profile, fixed PV MW, SSR sweep step.",
        "Curve of SSR target vs minimum BESS MW/MWh, plus SCR, peak grid, feasibility at each point.",
        "First solve SSR_max, then run many SSR point solves from sweep step up to the feasible maximum."),
    "S22_BTM_GC_CURVE": (
        "BESS Curve vs Grid Connection",
        "Fixed load profile, fixed PV profile, fixed PV MW, GC sweep step.",
        "Curve of GC target vs minimum BESS MW/MWh, plus SSR, SCR, feasibility at each point.",
        "First solve GC_min, then sweep GC targets from peak demand down to GCmin and run one peak-shaving LP per point."),
    "S31_SUB_GC_TARGET_BESS": (
        "Minimum BESS for Backup / Peak Shaving Without Generation",
        "Fixed load profile, PV fixed at zero, target GC MW, BESS bounds, grid-charge policy.",
        "Minimum BESS MW, minimum BESS MWh, achieved residual peak grid, feasibility.",
        "Same optimisation as GC-target BTM sizing, but with PV set to zero and BESS acting as a grid-charged buffer by policy."),
    "S32_SUB_GC_CURVE": (
        "Backup BESS Curve vs Grid Connection",
        "Fixed load profile, PV fixed at zero, GC sweep step, grid-charge policy.",
        "Curve of GC target vs minimum BESS MW/MWh, plus feasibility.",
        "Sweep GC targets and solve repeated BESS-only peak-shaving point problems."),
    "S33_SUB_FIXED_BESS_EVAL": (
        "Fixed BESS Backup Evaluation",
        "Fixed load profile, PV fixed at zero, fixed BESS MW/MWh, grid ceiling.",
        "Residual peak grid, grid import, unmet load, operational KPIs.",
        "Run causal peak-shaving dispatch with allowed grid charging and compute achieved backup performance."),
    "S34_SUB_FIRMNESS_TARGET_BESS": (
        "Minimum BESS for Backup Firmness",
        "Fixed load profile, PV fixed at zero, GC cap, firmness target.",
        "Minimum BESS MW/MWh, achieved firmness, unmet energy, feasibility.",
        "Solve one annual LP with firmness target type, using BESS to cover load deficits subject to grid cap."),
    "S35_SUB_UTILISATION_BESS_CURVE": (
        "Utilisation-Constrained Backup BESS Curve",
        "Fixed load profile, PV fixed at zero, GC sweep range, minimum utilisation threshold.",
        "GC-vs-BESS curve filtered to batteries that are sufficiently used.",
        "First generate the normal BESS-only GC curve, then apply a utilisation constraint or post-filter based on cycles or active hours."),
    "S41_OPERATIONAL_VERIFY": (
        "Operational Verification Under Causal Dispatch",
        "Any fixed design from a previous scenario: load, PV MW, BESS MW/MWh, grid ceiling, dispatch mode.",
        "Operational SCR, SSR, peak grid, unmet load, KPI gap versus LP sizing.",
        "Take an LP-designed point, run it again under run_rule_dispatch(), and compare operational KPIs against optimistic LP KPIs."),
    "S42_DELIVERABLE_BESS_SIZE": (
        "Deliverable BESS Size Under Real Operation",
        "A target designed under LP, plus E/P duration assumption, dispatch rule, and site bounds.",
        "Deliverable BESS MW/MWh under causal rule, optional end-of-life gross-up, pass/fail.",
        "Use rule-based bisection to increase battery size until the causal controller actually meets the target."),
    "S51_BTM_GCMIN_BESSOPT": (
        "Minimum Grid Connection with Fixed PV and Optimised BESS",
        "Fixed load profile, fixed PV profile, fixed PV MW, BESS bounds, reliability rule.",
        "GCmin, optimal BESS MW, optimal BESS MWh, achieved SSR/SCR, feasibility.",
        "Build one annual LP with free BESS sizing and a peak-grid tracker, then minimise peak grid subject to physics and reliability."),
    "S52_BTM_GCMIN_FIXED_DESIGN": (
        "Minimum Grid Connection with Fixed PV and Fixed BESS",
        "Fixed load profile, fixed PV MW, fixed BESS MW/MWh, dispatch mode.",
        "GCmin for the fixed design, peak residual grid, SCR/SSR.",
        "Run forward simulation and take the maximum residual import as the minimum required grid connection."),
    "S53_BTM_FULL_CLOSED_LOOP_SSR": (
        "Full Closed-Loop Design for Target SSR",
        "Fixed load profile, fixed PV unit profile, SSR target, design bounds for PV, BESS, and GC.",
        "Solved PV MW, BESS MW, BESS MWh, GCmin or chosen GC, achieved SSR/SCR.",
        "Use a multi-variable optimisation where PV, BESS, and GC are all decision variables; enforce target SSR and minimise chosen design objective."),
    "S54_BTM_SSR_UNDER_GC_CAP": (
        "Grid-Limited SSR Design",
        "Fixed load profile, fixed PV unit profile, GC cap, SSR target, design bounds.",
        "PV MW, BESS MW, BESS MWh, feasibility, achieved SSR.",
        "Solve a constrained co-design problem: enforce both target SSR and hard GC ceiling, then minimise design size."),
    "S55_BTM_GCMIN_FRONTIER": (
        "GC Frontier Under BESS Sweep",
        "Fixed load profile, fixed or capped PV, sweep range of BESS sizes.",
        "Frontier of BESS size vs GCmin.",
        "Fix BESS at each candidate size, run GCmin evaluation or bound solve, and assemble the frontier."),
    "S61_BTM_PVBESS_SSR_TARGET": (
        "Minimum PV and BESS for Target Self-Sufficiency",
        "Fixed load profile, fixed PV unit profile, SSR target, PV cap, BESS bounds.",
        "PV MW, BESS MW, BESS MWh, achieved SSR/SCR, feasibility.",
        "Make PV size and BESS size decision variables, enforce target SSR, and add a tie-breaker because many PV/BESS pairs may be feasible."),
    "S62_BTM_PVBESS_SSR_SURFACE": (
        "PV–BESS Surface vs Self-Sufficiency",
        "Fixed load profile, PV sweep range, SSR sweep range.",
        "Surface table of PV MW, SSR target, minimum BESS MW/MWh, feasibility.",
        "For each PV size, solve SSR_max, skip infeasible targets, and run repeated SSR point solves over the PV × SSR grid."),
    "S63_BTM_PVBESS_GC_TARGET": (
        "Minimum PV and BESS for Target Grid Connection",
        "Fixed load profile, PV unit profile, GC target MW, design bounds.",
        "PV MW, BESS MW, BESS MWh, achieved peak grid, feasibility.",
        "Make PV and BESS variables, enforce GC ceiling, minimise chosen design objective."),
    "S64_BTM_PVBESS_GC_SURFACE": (
        "PV–BESS Surface vs Grid Connection",
        "Fixed load profile, PV sweep range, GC sweep range.",
        "Surface of PV MW, GC target, minimum BESS MW/MWh, feasibility.",
        "Sweep PV values and GC targets, then solve repeated GC-target point problems."),
    "S71_BTM_SSR_PLUS_GC_BESS": (
        "Minimum BESS for Target SSR Within a Grid Limit",
        "Fixed load profile, fixed PV MW, SSR target, GC cap.",
        "Minimum BESS MW, BESS MWh, feasibility, achieved KPIs.",
        "Use the co_opt target type: enforce both annual SSR and timestep GC limit, then minimise BESS size."),
    "S72_BTM_PVBESS_SSR_PLUS_GC": (
        "Minimum PV and BESS for Target SSR Within a Grid Limit",
        "Fixed load profile, PV unit profile, SSR target, GC cap, design bounds.",
        "PV MW, BESS MW, BESS MWh, achieved SSR, feasibility.",
        "Same as the fixed-PV joint target, but with PV also free to vary. Solve a constrained co-design problem."),
    "S81_DC_SELF_SUFFICIENT_DESIGN": (
        "Data Centre Self-Sufficient Supply Design",
        "Fixed DC load profile, generation unit profile, SSR or reliability target, design bounds.",
        "PV MW, BESS MW, BESS MWh, GC, SCR/SSR, feasibility.",
        "Reuse the full closed-loop design engine, but with the DC load profile as the fixed demand baseline."),
    "S82_DC_FIRMNESS_DESIGN": (
        "Data Centre Firmness Design",
        "Fixed DC load profile, firmness target, generation unit profile, design bounds.",
        "PV MW, BESS MW, BESS MWh, GC, achieved firmness, unmet load.",
        "Solve a reliability-first co-design problem where firmness replaces plain SSR as the main target."),
    "S83_PORT_EVENT_BESS_DESIGN": (
        "Port BESS for Vessel Demand Events",
        "Vessel schedule or derived port load profile, optional PV profile, GC constraint, BESS bounds.",
        "BESS MW, BESS MWh, event coverage, residual peak grid.",
        "Convert vessel schedule into load events, then size BESS against the resulting peak/event pattern."),
    "S84_PORT_PRECHARGE_DESIGN": (
        "Port Pre-Charged BESS Under Grid Constraint",
        "Vessel schedule, grid cap, optional PV profile, pre-charge policy.",
        "BESS MW, BESS MWh, pre-charge schedule, event coverage, residual GC.",
        "Use known vessel arrivals to pre-charge battery ahead of peaks, then discharge during berthing events."),
    "S85_PORT_REINFORCEMENT_AVOIDANCE": (
        "Port Reinforcement Avoidance",
        "Constrained port GC, vessel schedule, optional PV, reliability target.",
        "Required BESS, optional PV, evidence that reinforcement is avoided, peak residual grid.",
        "Solve BESS-plus-optional-PV sizing under a hard port grid constraint to prove peak events can be buffered."),
    "S86_MULTI_CONSUMER_DESIGN": (
        "Multi-Consumer Shared BESS Design",
        "Multiple fixed consumer load profiles, generation unit profile, target metric, design bounds.",
        "Shared PV MW, shared BESS MW/MWh, GC, aggregate KPIs.",
        "Aggregate consumer profiles into one site curve, then run the same shared closed-loop optimisation."),
    "S87_DC_PORT_COLOCATION": (
        "DC + Port Colocation Design",
        "Combined DC baseline load and port event schedule, generation unit profile, design bounds.",
        "Shared PV MW, shared BESS MW/MWh, GC, combined KPIs.",
        "Build a combined demand curve with continuous and pulsed loads, then solve shared BESS/PV/GC design."),
    "S91_OFFGRID_FIRMNESS_PVBESS": (
        "Off-Grid Firmness Design",
        "Fixed load profile, PV profile, firmness target, PV sweep or PV bounds, BESS bounds.",
        "PV MW, BESS MW, BESS MWh, achieved firmness, unmet load, feasibility.",
        "Set off-grid topology, forbid import/export, and solve BESS sizing against firmness for each PV point or directly."),
    "S92_OFFGRID_GENMIN": (
        "Off-Grid Minimum Generation Design",
        "Fixed load profile, PV unit profile, firmness target, BESS bounds.",
        "Minimum PV MW, BESS MW, BESS MWh, achieved firmness.",
        "Make generation size a variable, enforce off-grid reliability, and minimise generation or total design size."),
    "S93_STANDALONE_EXPORTLIMIT_BESS": (
        "Standalone Generation with Export Limit and BESS",
        "Fixed generation profile, fixed PV MW, export limit, curtailment target, BESS bounds.",
        "Minimum BESS MW/MWh, achieved curtailment, exported energy, feasibility.",
        "Set standalone topology, enforce export cap and curtailment target, then minimise BESS size."),
    "S94_STANDALONE_GENSIZE_EXPORTLIMIT": (
        "Standalone Generation Sizing Under Export Constraint",
        "PV unit profile, export limit, optional BESS cap, curtailment or export objective.",
        "Optimal generation MW, optional BESS MW/MWh, usable export, curtailed energy.",
        "Solve generation size, optionally with capped BESS, to maximise useful export or minimise curtailment under the export limit."),
    "S101_SITE_AREA_CONSTRAINED": (
        "Site-Area-Constrained Design",
        "Base scenario inputs plus area cap and technology density assumptions.",
        "Best feasible PV/BESS design under physical area limit, or infeasible flag.",
        "Translate area into PV/BESS upper bounds, then rerun the base optimisation under those constraints."),
    "S102_BESS_CAP_CONSTRAINED": (
        "BESS Capacity-Constrained Design",
        "Base scenario inputs plus hard BESS MW/MWh caps.",
        "Best feasible design under BESS cap, or infeasible flag.",
        "Apply BESS bounds in the LP and solve the same base problem."),
    "S103_PV_CAP_CONSTRAINED": (
        "PV Capacity-Constrained Design",
        "Base scenario inputs plus PV upper bound.",
        "Best feasible design under PV cap, or infeasible flag.",
        "Apply PV bound in any generation-solving scenario and resolve."),
    "S104_IMPORT_ONLY_NO_EXPORT": (
        "Strict Import-Only No-Export BTM",
        "Base BTM inputs plus export forced to zero.",
        "Import-only feasible design and KPIs.",
        "Set export to zero and curtail any surplus beyond load plus charging headroom."),
    "S105_EXPORT_LIMITED": (
        "Export-Limited Hybrid BTM",
        "Base inputs plus finite export cap.",
        "Design/KPIs with export, curtailment, and residual grid.",
        "Allow export variable up to a set cap, then solve the base optimisation with that extra outlet for surplus."),
    "S111_DAYONE_SIZING": (
        "Day-One Sizing",
        "Any base scenario inputs plus day-one lifecycle mode.",
        "Design that meets the target at initial commissioning only.",
        "Run the base optimisation on the representative year without degradation gross-up."),
    "S112_EOL_HONEST_SIZING": (
        "End-of-Life-Honest Sizing",
        "Base scenario inputs plus degradation / retention assumptions over asset life.",
        "Day-one installed size, end-of-life effective size, feasibility at EOL.",
        "Either gross up day-one capacity or directly optimise against end-of-life effective capacity, then confirm target is still met."),
    "S113_SEASONAL_GRID_SHIFT": (
        "Seasonal Grid Time-Shifting",
        "Base scenario inputs plus allowed grid-charge windows or seasonal charging policy.",
        "Revised KPIs and/or revised design with seasonal charging benefit.",
        "Permit controlled grid charging during non-binding periods and rerun dispatch or optimisation."),
    "S114_GRID_SERVICES_SOC_RESERVE": (
        "Grid Services with Reserved SOC",
        "Base scenario inputs plus reserved SOC fraction for grid services.",
        "BTM KPIs under reduced usable storage, optional service capacity.",
        "Reduce the battery energy available for BTM duty, then rerun the base scenario to see the trade-off."),
    "S121_ECONOMIC_CURVE_RANKING": (
        "Techno-Economic Ranking Along a Sizing Curve",
        "A feasible physical curve/frontier from Stage 1–10, plus CAPEX, tariffs, grid costs, and value assumptions.",
        "Ranked points, IRR, NPV, economic optimum.",
        "Take already-feasible physical points, attach commercial assumptions, and rank the points economically."),
    "S122_FINANCING_SSR_COVENANT": (
        "Financing / Covenant SSR Design",
        "Required lender SSR covenant, feasible PV/BESS/GC set, CAPEX assumptions.",
        "Minimum-capex covenant-compliant design, lender-ready KPIs.",
        "Generate or filter feasible points that meet the covenant, then select the cheapest or best-value compliant configuration."),
    "S123_GRID_REINFORCEMENT_VS_BESS": (
        "Reinforcement vs BESS Trade-Off",
        "GC frontier or constrained feasible set, grid reinforcement cost assumptions, BESS costs.",
        "Economic trade-off curve and preferred solution.",
        "Compare the cost of buying more grid connection against the cost of installing more BESS across the feasible frontier."),
}


def _check_registry_doc_consistency() -> None:
    """Fail loud if the registry and the authoritative spec drift apart — a row
    without a spec (or a spec without a row) is a bug, not a silent gap."""
    reg_ids = set(_BY_ID)
    doc_ids = set(SPEC_DOC)
    missing_doc = reg_ids - doc_ids
    missing_row = doc_ids - reg_ids
    problems = []
    if missing_doc:
        problems.append(f"registry rows with no SPEC_DOC: {sorted(missing_doc)}")
    if missing_row:
        problems.append(f"SPEC_DOC entries with no registry row: {sorted(missing_row)}")
    if problems:
        raise AssertionError("scenarios.py registry/spec mismatch:\n  - " + "\n  - ".join(problems))


_check_registry_doc_consistency()


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def get_scenario(scenario_id: str) -> ScenarioSpec:
    if scenario_id not in _BY_ID:
        raise KeyError(f"unknown scenario '{scenario_id}'. Known: {', '.join(_BY_ID)}")
    return _BY_ID[scenario_id]


def describe(scenario_id: str) -> dict:
    """Full contract for one scenario: proper name + authoritative Inputs /
    Outputs / How, alongside the run status and handler."""
    spec = get_scenario(scenario_id)
    proper, inputs, outputs, how = SPEC_DOC[scenario_id]
    return {
        "id": spec.id,
        "name": proper,
        "stage": spec.stage,
        "topology": spec.topology,
        "status": spec.status,
        "answer_mode": spec.answer_mode,
        "inputs": inputs,
        "outputs": outputs,
        "how": how,
        "needs": spec.needs,
    }


def run_scenario(scenario_id: str, ctx: ScenarioContext) -> ScenarioResult:
    """Run any scenario by id and return a uniform ScenarioResult.

    The scenario's declared `topology` is authoritative: BESS-only sub-scenarios
    are forced to PV=0 regardless of what the (often shared) context carries, so a
    sub-scenario can never silently include generation. One context can therefore
    drive many scenarios without cross-contamination.
    """
    import dataclasses
    spec = get_scenario(scenario_id)
    if spec.status != Status.READY or not spec.handler:
        return ScenarioResult(spec.id, spec.name, spec.answer_mode, spec.status,
                              feasible=False, notes=spec.needs or "not yet runnable")
    eff = ctx
    if spec.topology == _SUB and ctx.pv_mw != 0.0:
        eff = dataclasses.replace(ctx, pv_mw=0.0)
    return _HANDLERS[spec.handler](spec, eff)


def list_scenarios(status: Optional[str] = None) -> list[ScenarioSpec]:
    return [s for s in REGISTRY if status is None or s.status == status]


def coverage() -> dict[str, int]:
    """Counts by status — answers 'does this cover everything yet?' at a glance."""
    out: dict[str, int] = {}
    for s in REGISTRY:
        out[s.status] = out.get(s.status, 0) + 1
    out["total"] = len(REGISTRY)
    return out
