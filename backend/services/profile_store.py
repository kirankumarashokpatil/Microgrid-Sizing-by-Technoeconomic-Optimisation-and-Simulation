"""
Profile store — the JSONB-envelope ⇄ DataFrame bridge.

A profile is a year of load + generation shapes. The engine wants it as a pandas
DataFrame (columns: timestamp, load_mw, pv_pu, wind_pu); Postgres stores it as a
single JSONB row (the three per-unit arrays). This module converts between the
two so ``core.scenarios.run_scenario`` never learns where the data came from.

Timestamps are NOT stored when the grid is regular (the common case): we keep
``start_ts`` + ``dt_hours`` + ``n_steps`` and rebuild ``start + i*dt_hours`` on
the way out. Irregular grids fall back to storing explicit timestamps.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

# The three series the engine consumes, in canonical order.
_SERIES_COLS = ("load_mw", "pv_pu", "wind_pu")


def build_envelope(
    df: pd.DataFrame,
    dt_hours: float,
    name: str,
    source: str | None = None,
    load_nameplate_mw: float | None = None,
    pv_nameplate_mw: float | None = None,
) -> dict:
    """Turn an engine profiles DataFrame into the kwargs for a ``Profile`` row.

    ``df`` must have ``load_mw`` and ``pv_pu``; ``wind_pu`` and ``timestamp`` are
    optional (wind defaults to zeros, timestamps are stored only if irregular).
    """
    n = int(len(df))
    if n == 0:
        raise ValueError("Cannot store an empty profile.")
    if "load_mw" not in df.columns or "pv_pu" not in df.columns:
        raise ValueError("Profile DataFrame must contain 'load_mw' and 'pv_pu' columns.")

    wind = df["wind_pu"] if "wind_pu" in df.columns else pd.Series([0.0] * n)
    series: dict = {
        "load_mw": _to_float_list(df["load_mw"]),
        "pv_pu": _to_float_list(df["pv_pu"]),
        "wind_pu": _to_float_list(wind),
    }

    start_ts: datetime | None = None
    if "timestamp" in df.columns and n > 0:
        ts = pd.to_datetime(df["timestamp"], errors="coerce")
        if ts.notna().any():
            start_ts = _as_utc(ts.iloc[0])
            if not _is_regular(ts, dt_hours):
                # Store explicit timestamps for an irregular grid.
                series["timestamps"] = [None if pd.isna(t) else str(t) for t in ts]

    return {
        "name": name,
        "source": source,
        "dt_hours": float(dt_hours),
        "n_steps": n,
        "start_ts": start_ts,
        "load_nameplate_mw": _opt_float(load_nameplate_mw),
        "pv_nameplate_mw": _opt_float(pv_nameplate_mw),
        "series": series,
        "meta": None,
    }


def df_from_profile(profile) -> tuple[pd.DataFrame, float]:
    """Rebuild the engine's profiles DataFrame from a stored ``Profile`` row.

    Returns ``(df, dt_hours)`` where df has timestamp, load_mw, pv_pu, wind_pu and
    an integer 0..N-1 index — exactly what ``_load_profile`` yields for an Excel.
    """
    series = profile.series or {}
    dt_hours = float(profile.dt_hours)
    n = int(profile.n_steps)

    load = _series_array(series.get("load_mw"), n)
    pv = _series_array(series.get("pv_pu"), n)
    wind = _series_array(series.get("wind_pu"), n)

    if series.get("timestamps"):
        ts = pd.to_datetime(pd.Series(series["timestamps"]), errors="coerce")
    else:
        start = profile.start_ts or datetime(1970, 1, 1, tzinfo=timezone.utc)
        freq = pd.to_timedelta(dt_hours, unit="h")
        ts = pd.date_range(start=pd.Timestamp(start), periods=n, freq=freq)

    df = pd.DataFrame(
        {"timestamp": ts, "load_mw": load, "pv_pu": pv, "wind_pu": wind}
    ).reset_index(drop=True)
    return df, dt_hours


# ── helpers ───────────────────────────────────────────────────────────────────

def _to_float_list(values) -> list[float]:
    """Coerce a series to a JSON-safe list of floats (NaN/inf → None)."""
    arr = np.asarray(values, dtype="float64")
    out: list = []
    for v in arr:
        out.append(None if (np.isnan(v) or np.isinf(v)) else float(v))
    return out


def _series_array(values, n: int) -> np.ndarray:
    """Rebuild a length-n float array, defaulting missing series to zeros."""
    if not values:
        return np.zeros(n, dtype="float64")
    arr = np.array([np.nan if v is None else float(v) for v in values], dtype="float64")
    if len(arr) != n:  # be lenient: pad/truncate to the declared length
        fixed = np.zeros(n, dtype="float64")
        fixed[: min(n, len(arr))] = arr[: min(n, len(arr))]
        arr = fixed
    return arr


def _opt_float(v) -> float | None:
    return None if v is None else float(v)


def _as_utc(ts) -> datetime:
    t = pd.Timestamp(ts)
    return (t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")).to_pydatetime()


def _is_regular(ts: pd.Series, dt_hours: float) -> bool:
    """True when successive gaps all equal dt_hours (within float tolerance)."""
    if len(ts) < 3:
        return True
    diffs = ts.diff().dropna().dt.total_seconds() / 3600.0
    return bool(np.allclose(diffs.to_numpy(), dt_hours, atol=1e-4))
