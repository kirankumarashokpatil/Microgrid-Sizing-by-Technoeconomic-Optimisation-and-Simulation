"""
core/sweep.py — parallel PV-sweep worker for the split optimiser.

Each PV point in /optimise-split is an INDEPENDENT full-year LP. The appsi/HiGHS
solver is in-process and NOT thread-safe (concurrent solves crash), so the sweep
fans points out across PROCESSES. This module is the picklable, FastAPI-free
worker each process runs: it reloads the profile (cached per process), sizes BESS
for the SSR target at one PV nameplate, and returns a small plain dict.

Kept deliberately free of any `api` import so a spawned worker only pays for the
engine, not the whole web app.
"""
from __future__ import annotations

from functools import lru_cache

from core.profile_loader import load_profiles, load_bess_input
from core.params import PhysicalParams
from core.sizing_engine import solve_sizing_point
from core.rule_dispatch import verify_sizing_with_rule
from core.schema import KpiKeys


@lru_cache(maxsize=4)
def _load(path: str):
    """(df, dt_hours) for a profile file — mirrors api.main._load_profile, cached
    once per worker process."""
    try:
        df, _load_np, _pv_np = load_profiles(xlsx_path=path)
    except Exception:
        df, _load_np, _pv_np = load_bess_input(xlsx_path=path)
    if len(df) >= 3:
        dt = round(df["timestamp"].diff().dropna().median().total_seconds() / 3600.0, 4)
    else:
        dt = 1.0
    return df, dt


def solve_ssr_point(path: str, load_peak_mw, pv_mw: float, wind_mw: float,
                    target_ssr_pct: float, topology: str, time_limit: int = 120) -> dict:
    """Size BESS for the SSR target at one solar nameplate. Returns a plain dict
    (picklable across the process boundary)."""
    df, dt = _load(path)
    df = df.copy()

    if load_peak_mw and float(df["load_mw"].max()) > 1e-9:
        df["load_mw"] = df["load_mw"] * (float(load_peak_mw) / float(df["load_mw"].max()))

    # Fold wind into the single generation series the engine consumes, mirroring
    # api.main.run(). eng_pv is the combined nameplate the LP sizes against; the
    # returned pv_mw stays the REAL solar nameplate (the swept variable) for costing.
    solar_mw = float(pv_mw or 0.0)
    eng_pv = solar_mw
    wind = float(wind_mw or 0.0)
    if wind > 0 and "wind_pu" in df.columns and float(df["wind_pu"].max()) > 1e-9:
        combined = df["pv_pu"].to_numpy() * solar_mw + df["wind_pu"].to_numpy() * wind
        peak = float(combined.max())
        if peak > 1e-9:
            df = df.copy()
            df["pv_pu"] = combined / peak
            eng_pv = peak

    params = PhysicalParams(dt_hours=dt, site_topology=topology)
    r = solve_sizing_point(df, params, eng_pv, "ssr", float(target_ssr_pct),
                           solver_time_limit=time_limit)
    if not r.feasible:
        return {"pv_mw": solar_mw, "feasible": False}

    # For a feasible BTM design unmet≈0, so grid import = (1 − SSR)·demand exactly.
    total_demand = float(df["load_mw"].sum()) * dt
    grid_import = max(0.0, (1.0 - r.achieved_ssr_pct / 100.0) * total_demand)
    # Curtailment from an energy balance on the generation bus, valid for ANY topology:
    #   generation = self_consumed + exported + curtailed
    # SCR = self_consumed / generation, so curtailed = generation·(1 − SCR) − exported.
    # (df["pv_pu"] × eng_pv is the combined PV+wind series; export=0 ⇒ curtailment = 100 − SCR.)
    total_generation = float(eng_pv) * float(df["pv_pu"].sum()) * dt
    if total_generation > 1e-9:
        self_consumed = (r.achieved_scr_pct / 100.0) * total_generation
        curtailed_mwh = max(0.0, total_generation - self_consumed - float(r.exported_mwh or 0.0))
        curtailment_pct = curtailed_mwh / total_generation * 100.0
    else:
        curtailment_pct = 0.0

    out = {
        # Model O — perfect-foresight LP: an optimistic lower bound.
        "pv_mw": solar_mw, "feasible": True,
        "bess_mw": r.bess_mw, "bess_mwh": r.bess_mwh,
        "grid_mw": r.peak_grid_mw, "ssr_pct": r.achieved_ssr_pct,
        "scr_pct": r.achieved_scr_pct, "curtailment_pct": curtailment_pct,
        "grid_import_mwh": grid_import,
    }

    # Model R — operate that exact battery under the causal contract rule (no
    # foresight). This is the deliverable truth the rest of the tool reports; the
    # LP size misses the target by the SSR gap. Only grid-connected BTM has causal
    # grid semantics the rule models, so other topologies keep LP-only numbers.
    if topology == "grid_connected_btm" and r.bess_mwh is not None:
        try:
            op_kpis, _ = verify_sizing_with_rule(
                df, params, pv_mw=eng_pv, bess_mw=r.bess_mw, bess_mwh=r.bess_mwh,
                target_type="ssr", grid_ceiling_mw=params.site_max_grid_mw,
            )
            out.update({
                "op_ssr_pct":        float(op_kpis[KpiKeys.SSR]),
                "op_scr_pct":        float(op_kpis[KpiKeys.SCR]),
                "op_curtailment_pct": float(op_kpis[KpiKeys.OSR]),
                "op_gc_mw":          float(op_kpis[KpiKeys.GCMIN_PEAK]),
                "op_unmet_mwh":      float(op_kpis[KpiKeys.TOTAL_UNMET_LOAD]),
            })
        except Exception:
            pass   # fall back to LP-only numbers if the rule pass fails
    return out


def solve_ssr_point_star(args: tuple) -> dict:
    """Unpack a tuple of args — ProcessPoolExecutor.map needs a single-arg callable."""
    return solve_ssr_point(*args)
