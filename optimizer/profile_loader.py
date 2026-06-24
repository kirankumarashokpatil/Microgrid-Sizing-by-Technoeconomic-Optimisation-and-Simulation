"""
Profile Loader Module (Phase 1)
--------------------------------
Reads the '8760_PV&Load Profiles.xlsx' file which contains one year of
hourly load and PV profiles for the Italian data-centre site.

This loader is intentionally independent of the project config Excel workbook.
Phase 1 only needs physical profiles — no prices, no CAPEX, no financial columns.

Sheet layout (datacenter_load_8760):
  Row 0  : metadata / labels (skipped)
  Row 1  : column headers
  Col 0  : timestamp (hourly, 2025-01-01 … 2025-12-31)
  Col 4  : Datacenter [MW]        — absolute load in MW
  Col 7  : PV Profile MW/MWp      — normalised PV (0–1 p.u.)
  Col 14 : Datacenter nameplate   = 100 MW  (read for reference)
  Col 18 : PV nameplate           = 150 MW  (read for reference)
"""

from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

# ──────────────────────────────────────────────────────────────────────────────
# Constants — column positions in the raw sheet (0-indexed)
# ──────────────────────────────────────────────────────────────────────────────
_COL_TIMESTAMP   = 0
_COL_LOAD_MW     = 4   # "Datacenter [MW]"
_COL_PV_PU       = 7   # "PV Profile\nMW/MWp"  (normalised, 0–1)
_COL_LOAD_NAMEP  = 15  # 100.0  — lives in row index 1 of the raw sheet
_COL_PV_NAMEP    = 18  # 150.0  — lives in row index 1 of the raw sheet
_SHEET_NAME      = "datacenter_load_8760"
_NAMEPLATE_ROW   = 1   # row index (0-based) that contains nameplate values + headers
_DATA_START_ROW  = 2   # first actual data row (0-based); skiprows=2 in read_excel


def load_profiles(
    xlsx_path: str | Path,
    load_mw_nameplate: float | None = None,
    pv_mw_nameplate: float | None = None,
    dt_hours: float = 1.0,
) -> tuple[pd.DataFrame, float, float]:
    """
    Read the 8760 hourly profile file and return a clean DataFrame.

    Parameters
    ----------
    xlsx_path : str | Path
        Path to '8760_PV&Load Profiles.xlsx'.
    load_mw_nameplate : float | None
        Override the nameplate load size (MW).  If None, reads from the file.
    pv_mw_nameplate : float | None
        Override the nameplate PV size (MW).  If None, reads from the file.
    dt_hours : float
        Timestep size in hours (1.0 for hourly, 0.25 for 15-min if resampled).

    Returns
    -------
    df : pd.DataFrame
        Columns: timestamp, load_mw, pv_pu
        Index  : integer 0 … N-1
    load_mw_nameplate : float
        Confirmed nameplate load (MW).
    pv_mw_nameplate : float
        Confirmed nameplate PV (MW).
    """
    xlsx_path = Path(xlsx_path)
    if not xlsx_path.exists():
        raise FileNotFoundError(f"Profile file not found: {xlsx_path}")

    # ── Read raw (no header) to inspect all rows ──────────────────────────
    raw = pd.read_excel(xlsx_path, sheet_name=_SHEET_NAME, header=None)

    # ── Extract nameplates from row 1 (0-indexed), which contains both
    #    column headers and the nameplate values in the side columns ─────
    nameplate_row = raw.iloc[_NAMEPLATE_ROW]

    if load_mw_nameplate is None:
        try:
            load_mw_nameplate = float(nameplate_row.iloc[_COL_LOAD_NAMEP])
        except (ValueError, TypeError):
            load_mw_nameplate = 100.0
            print(f"   [profile_loader] Could not read load nameplate — defaulting to {load_mw_nameplate} MW")

    if pv_mw_nameplate is None:
        try:
            pv_mw_nameplate = float(nameplate_row.iloc[_COL_PV_NAMEP])
        except (ValueError, TypeError):
            pv_mw_nameplate = 150.0
            print(f"   [profile_loader] Could not read PV nameplate — defaulting to {pv_mw_nameplate} MW")

    # ── Read data rows — skip row 0 (blank) + row 1 (headers/nameplates) ─
    data = pd.read_excel(xlsx_path, sheet_name=_SHEET_NAME, skiprows=_DATA_START_ROW, header=None)

    df = pd.DataFrame()
    df["timestamp"] = pd.to_datetime(data.iloc[:, _COL_TIMESTAMP], errors="coerce")
    df["load_mw"]   = pd.to_numeric(data.iloc[:, _COL_LOAD_MW], errors="coerce")
    df["pv_pu"]     = pd.to_numeric(data.iloc[:, _COL_PV_PU],  errors="coerce")

    # Drop rows where timestamp is missing (footer / blank rows)
    df = df.dropna(subset=["timestamp"]).reset_index(drop=True)
    df = df.sort_values('timestamp').reset_index(drop=True)

    # 4. Resolution check — NEVER fabricate finer resolution than the data has.
    #    The spec wants 15-min because an hourly engine under-sizes for peak
    #    shaving; but interpolating hourly → 15-min SMOOTHS AWAY the sub-hourly
    #    peaks it is meant to capture, so it is worse than honest hourly. We
    #    therefore require native data at the requested resolution and fail loud
    #    rather than silently invent peaks (DESIGN_REVIEW.md §3).
    if len(df) >= 3:
        native_dt_h = (
            df["timestamp"].diff().dropna().median().total_seconds() / 3600.0
        )
    else:
        native_dt_h = dt_hours

    if dt_hours < native_dt_h - 1e-6:
        raise ValueError(
            f"Requested timestep {dt_hours:g} h is finer than the data's native "
            f"resolution {native_dt_h:g} h. Refusing to fabricate sub-resolution "
            f"detail by interpolation (it would smooth away the very peaks 15-min "
            f"resolution exists to capture). Supply native {dt_hours:g} h profiles, "
            f"or run at dt_hours={native_dt_h:g}."
        )
    if abs(dt_hours - native_dt_h) > 1e-6:
        print(f"   [profile_loader] NOTE: native resolution is {native_dt_h:g} h; "
              f"running at requested {dt_hours:g} h.")

    # Fill any stray NaN in profiles with zero
    df["load_mw"] = df["load_mw"].fillna(0.0)
    df["pv_pu"]   = df["pv_pu"].fillna(0.0)

    # ── Validate ──────────────────────────────────────────────────────────
    _validate_profiles(df, load_mw_nameplate, pv_mw_nameplate)

    n = len(df)
    print(f"   [profile_loader] Loaded {n} timesteps  |  dt = {dt_hours} h  "
          f"|  Load {load_mw_nameplate:.0f} MW nameplate  |  PV {pv_mw_nameplate:.0f} MW nameplate")
    print(f"   [profile_loader] Load   → min {df['load_mw'].min():.2f} MW, "
          f"max {df['load_mw'].max():.2f} MW, "
          f"mean {df['load_mw'].mean():.2f} MW")
    print(f"   [profile_loader] PV p.u.→ max {df['pv_pu'].max():.4f}  "
          f"(≡ {df['pv_pu'].max() * pv_mw_nameplate:.1f} MW at nameplate)")

    return df, load_mw_nameplate, pv_mw_nameplate


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _validate_profiles(df: pd.DataFrame, load_nameplate: float, pv_nameplate: float) -> None:
    """Raise on obvious data quality problems."""
    if (df["load_mw"] < 0).any():
        raise ValueError("Negative load values found in profile data.")
    if (df["pv_pu"] < 0).any() or (df["pv_pu"] > 1.05).any():
        raise ValueError(
            f"PV profile values out of expected 0–1 p.u. range "
            f"(max = {df['pv_pu'].max():.4f}). Check normalisation."
        )
    if df["load_mw"].max() > load_nameplate * 1.10:
        raise ValueError(
            f"Peak load {df['load_mw'].max():.1f} MW exceeds nameplate "
            f"{load_nameplate:.0f} MW by >10%. Check data."
        )
    if df.isnull().any().any():
        raise ValueError("NaN values remain in profile DataFrame after cleaning.")


def compute_annual_energy(df: pd.DataFrame, dt_hours: float = 1.0) -> dict:
    """
    Convenience helper — returns key annual energy totals from the profiles.
    Useful for quick sanity checks before running the LP.
    """
    total_load_mwh   = df["load_mw"].sum() * dt_hours
    # Note: pv_pu is unitless; caller must multiply by pv_mw_nameplate for MWh
    return {
        "total_load_mwh":   round(total_load_mwh, 1),
        "peak_load_mw":     round(df["load_mw"].max(), 2),
        "min_load_mw":      round(df["load_mw"].min(), 2),
        "mean_load_mw":     round(df["load_mw"].mean(), 2),
        "pv_capacity_factor": round(df["pv_pu"].mean(), 4),
        "n_timesteps":      len(df),
    }
