"""
Report helpers — human-readable dispatch tables from a canonical FlowsFrame.
Ported from the per-scenario report builders so the web Excel export shows the
same per-slot merit-order detail: for every timestep, what served the load and
what the battery/grid did. Everything is derived from FlowCols in one place.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.schema import FlowCols


def route(flows: pd.DataFrame) -> dict:
    """Split each timestep into merit-order flows: PV serves load first, surplus
    charges the BESS (rest curtailed), the BESS covers residual load, the grid
    covers the rest."""
    load  = flows[FlowCols.LOAD_MW].to_numpy()
    pv_av = flows[FlowCols.PV_AVAIL_MW].to_numpy()
    charge = flows[FlowCols.CHARGE_MW].to_numpy()
    grid  = flows[FlowCols.GRID_IMP_MW].to_numpy()
    disch = flows[FlowCols.DISCHARGE_MW].to_numpy()
    unmet = flows[FlowCols.UNMET_MW].to_numpy()
    pv_to_load = np.minimum(pv_av, load)
    pv_surplus = pv_av - pv_to_load
    pv_charge  = np.minimum(pv_surplus, charge)
    grid_charge = charge - pv_charge
    return dict(load=load, pv_av=pv_av, pv_to_load=pv_to_load, pv_charge=pv_charge,
                grid_charge=grid_charge, grid_to_load=grid - grid_charge, bess_to_load=disch,
                unmet=unmet, curtail=pv_surplus - pv_charge, soc=flows[FlowCols.SOC_MWH].to_numpy())


def hourly_flows(flows: pd.DataFrame) -> pd.DataFrame:
    """Per-slot routing table: what served the load each timestep, with a CHECK
    column (= Load) and per-slot SSR/SCR."""
    r = route(flows)
    out = pd.DataFrame({
        "Timestamp": flows[FlowCols.TIMESTAMP].to_numpy(),
        "Load (MW)": r["load"], "PV available (MW)": r["pv_av"],
        "PV → Load (MW)": r["pv_to_load"], "PV → BESS (MW)": r["pv_charge"],
        "PV curtailed (MW)": r["curtail"], "BESS → Load (MW)": r["bess_to_load"],
        "Grid → Load (MW)": r["grid_to_load"], "Unmet (MW)": r["unmet"],
        "BESS SOC (MWh)": r["soc"],
    })
    out["CHECK served = Load"] = (out["PV → Load (MW)"] + out["BESS → Load (MW)"]
                                  + out["Grid → Load (MW)"] + out["Unmet (MW)"])
    with np.errstate(invalid="ignore", divide="ignore"):
        out["SSR this slot (%)"] = np.where(r["load"] > 1e-9,
            (r["pv_to_load"] + r["bess_to_load"]) / r["load"] * 100, np.nan)
        out["SCR this slot (%)"] = np.where(r["pv_av"] > 1e-9,
            (r["pv_to_load"] + r["pv_charge"]) / r["pv_av"] * 100, np.nan)
    return out.round(3)


def energy_split(flows: pd.DataFrame, dt: float) -> dict:
    """Annual MWh split — the four load sources plus PV usage and the residual."""
    r = route(flows)
    s = lambda a: round(float(a.sum() * dt), 1)
    return {
        "Load (MWh)": s(r["load"]), "PV → Load (MWh)": s(r["pv_to_load"]),
        "BESS → Load (MWh)": s(r["bess_to_load"]), "Grid → Load (MWh)": s(r["grid_to_load"]),
        "Unmet (MWh)": s(r["unmet"]), "PV available (MWh)": s(r["pv_av"]),
        "PV used (MWh)": s(r["pv_to_load"] + r["pv_charge"]), "PV curtailed (MWh)": s(r["curtail"]),
    }


def monthly(flows: pd.DataFrame, dt: float) -> pd.DataFrame:
    """12-row seasonal breakdown of the key energy flows."""
    f = flows.copy()
    f["Month"] = pd.to_datetime(f[FlowCols.TIMESTAMP]).dt.month
    g = f.groupby("Month").agg(
        Load_MWh=(FlowCols.LOAD_MW, lambda x: round(x.sum() * dt, 1)),
        PV_used_MWh=(FlowCols.PV_USED_MW, lambda x: round(x.sum() * dt, 1)),
        BESS_discharge_MWh=(FlowCols.DISCHARGE_MW, lambda x: round(x.sum() * dt, 1)),
        Grid_import_MWh=(FlowCols.GRID_IMP_MW, lambda x: round(x.sum() * dt, 1)),
        Unmet_MWh=(FlowCols.UNMET_MW, lambda x: round(x.sum() * dt, 1)),
    ).reset_index()
    return g
