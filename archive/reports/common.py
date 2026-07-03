"""Shared helpers for the per-scenario report builders.

Everything here is computed from the canonical FlowsFrame (optimizer.schema.FlowCols)
so the per-hour routing, energy balance and KPIs are derived in exactly one place
and stay consistent across the SSR, peak-shaving and co-optimisation reports.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from core.schema import FlowCols, KpiKeys


# ──────────────────────────────────────────────────────────────────────────────
# Input loading — pick the file format
# ──────────────────────────────────────────────────────────────────────────────

def load_site(profiles_path, fmt: str, dt_hours: float):
    """Return (df, load_nameplate, generation_nameplate) for the chosen format.

    fmt="legacy" → hourly 8760 PV+Load file (load_profiles).
    fmt="bess"   → 15-min solar+wind+load file, generation = solar+wind
                   (load_bess_input).
    """
    from core.profile_loader import load_profiles, load_bess_input
    if fmt == "bess":
        return load_bess_input(profiles_path, dt_hours=dt_hours)
    return load_profiles(xlsx_path=profiles_path, dt_hours=dt_hours)


# ──────────────────────────────────────────────────────────────────────────────
# Sweep helpers
# ──────────────────────────────────────────────────────────────────────────────

def nice_step(span: float, n_points: int, options: list[float]) -> float:
    """Smallest 'nice' increment from `options` that fits ~n_points across `span`."""
    raw = span / max(1, n_points)
    for o in options:
        if o >= raw:
            return o
    return options[-1]


# ──────────────────────────────────────────────────────────────────────────────
# Per-hour merit-order routing (reconstructed from a FlowsFrame)
# ──────────────────────────────────────────────────────────────────────────────

def route(flows: pd.DataFrame) -> dict:
    """Split each hour into merit-order flows. PV serves load first; the surplus
    charges the BESS (rest curtailed); any remaining charge came from off-peak grid
    valley-fill; the BESS covers residual load; the grid covers the rest. Works for
    SSR runs (no grid charge -> grid_charge = 0) and peak-shaving runs alike."""
    load = flows[FlowCols.LOAD_MW].to_numpy()
    pv_av = flows[FlowCols.PV_AVAIL_MW].to_numpy()
    charge = flows[FlowCols.CHARGE_MW].to_numpy()
    grid = flows[FlowCols.GRID_IMP_MW].to_numpy()
    disch = flows[FlowCols.DISCHARGE_MW].to_numpy()
    unmet = flows[FlowCols.UNMET_MW].to_numpy()
    pv_to_load = np.minimum(pv_av, load)
    pv_surplus = pv_av - pv_to_load
    pv_charge = np.minimum(pv_surplus, charge)
    grid_charge = charge - pv_charge
    return dict(load=load, pv_av=pv_av, pv_to_load=pv_to_load, pv_charge=pv_charge,
                grid_charge=grid_charge, grid_to_load=grid - grid_charge, bess_to_load=disch,
                unmet=unmet, curtail=pv_surplus - pv_charge, soc=flows[FlowCols.SOC_MWH].to_numpy())


def hourly_flows(flows: pd.DataFrame, *, show_grid_to_bess: bool = False) -> pd.DataFrame:
    """Human-readable hourly routing table with a CHECK column (= Load every hour)
    and per-hour SSR/SCR (SCR blank when there is no PV). Set show_grid_to_bess for
    peak-shaving / co-opt runs that valley-fill from the grid."""
    r = route(flows)
    cols = {
        "Timestamp": flows[FlowCols.TIMESTAMP].to_numpy(),
        "Load (MW)": r["load"], "PV available (MW)": r["pv_av"],
        "PV -> Load (MW)": r["pv_to_load"], "PV -> BESS (MW)": r["pv_charge"],
    }
    if show_grid_to_bess:
        cols["Grid -> BESS (MW)"] = r["grid_charge"]
    cols.update({
        "PV curtailed (MW)": r["curtail"], "BESS -> Load (MW)": r["bess_to_load"],
        "Grid -> Load (MW)": r["grid_to_load"], "Unmet (MW)": r["unmet"], "BESS SOC (MWh)": r["soc"],
    })
    out = pd.DataFrame(cols)
    if show_grid_to_bess:
        out["Total grid import (MW)"] = out["Grid -> Load (MW)"] + out["Grid -> BESS (MW)"]
    out["CHECK served = Load"] = (out["PV -> Load (MW)"] + out["BESS -> Load (MW)"]
                                  + out["Grid -> Load (MW)"] + out["Unmet (MW)"])
    with np.errstate(invalid="ignore", divide="ignore"):
        out["SSR this hour (%)"] = np.where(r["load"] > 1e-9,
                                            (r["pv_to_load"] + r["bess_to_load"]) / r["load"] * 100, np.nan)
        out["SCR this hour (%)"] = np.where(r["pv_av"] > 1e-9,
                                            (r["pv_to_load"] + r["pv_charge"]) / r["pv_av"] * 100, np.nan)
    return out.round(2)


def energy_split(flows: pd.DataFrame, dt: float) -> dict:
    """Annual MWh split — the four load sources (PV/BESS/grid/unmet) plus PV usage
    and the balance residual (~0 = the accounting reconciles)."""
    r = route(flows)
    s = lambda a: float(a.sum() * dt)
    return dict(load=s(r["load"]), pv_to_load=s(r["pv_to_load"]), bess_to_load=s(r["bess_to_load"]),
                grid_to_load=s(r["grid_to_load"]), grid_charge=s(r["grid_charge"]), unmet=s(r["unmet"]),
                pv_av=s(r["pv_av"]), pv_used=s(r["pv_to_load"] + r["pv_charge"]), curt=s(r["curtail"]),
                residual=s(r["load"]) - (s(r["pv_to_load"]) + s(r["bess_to_load"]) + s(r["grid_to_load"]) + s(r["unmet"])))


def monthly(flows: pd.DataFrame, dt: float) -> pd.DataFrame:
    """12-row seasonal breakdown."""
    f = flows.copy()
    f["m"] = pd.to_datetime(f[FlowCols.TIMESTAMP]).dt.month
    g = f.groupby("m").agg(
        Load_MWh=(FlowCols.LOAD_MW, lambda x: x.sum() * dt),
        PV_used_MWh=(FlowCols.PV_USED_MW, lambda x: x.sum() * dt),
        BESS_discharge_MWh=(FlowCols.DISCHARGE_MW, lambda x: x.sum() * dt),
        Grid_import_MWh=(FlowCols.GRID_IMP_MW, lambda x: x.sum() * dt),
        Unmet_MWh=(FlowCols.UNMET_MW, lambda x: x.sum() * dt),
        Peak_grid_MW=(FlowCols.GRID_IMP_MW, "max"),
    ).reset_index()
    g["SSR_%"] = (g["Load_MWh"] - g["Grid_import_MWh"] - g["Unmet_MWh"]) / g["Load_MWh"] * 100
    nm = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
          7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
    g["Month"] = g["m"].map(nm)
    return g[["Month", "Load_MWh", "PV_used_MWh", "BESS_discharge_MWh",
              "Grid_import_MWh", "Unmet_MWh", "SSR_%", "Peak_grid_MW"]].round(1)


# ──────────────────────────────────────────────────────────────────────────────
# Excel layout helper
# ──────────────────────────────────────────────────────────────────────────────

def write_block(writer, sheet: str, title: str, dfb: pd.DataFrame, cur: int) -> int:
    """Write a titled table at 0-indexed row `cur`; return the next free row."""
    dfb.to_excel(writer, sheet_name=sheet, index=False, startrow=cur + 1)
    writer.sheets[sheet].cell(row=cur + 1, column=1, value=title)
    return cur + 1 + 1 + len(dfb) + 1
