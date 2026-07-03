"""
Generation API client — turns a coordinate + area into per-unit generation shapes.

Wind (``/yield``) and solar (``/solar-profile``) share one deployable but return
DIFFERENT shapes:

    wind:  { "total_capacity_mw": 45.0,
             "series": { "times": [...], "mw": [...] }, ... }
    solar: { "dc_capacity_kw": 61200.0, "ac_capacity_kw": 51000.0,
             "series": { "times": [...], "mw": [...], "per_kwp": [...] }, ... }

The engine wants generation as a per-unit capacity-factor shape (0–1) it multiplies
by a nameplate. For wind we compute ``pu = mw / total_capacity_mw``; solar already
hands us ``per_kwp`` (normalised to DC capacity — it peaks below 1.0 because AC
clipping is real lost energy), so we use it directly with nameplate = DC MWp. The
area-fit run defaults wind_mw / pv_mw to these nameplates.

This module does the HTTP + normalise + resample; it never touches the DB. Solar
stays optional — a missing/unreachable solar endpoint yields zeros.
"""
from __future__ import annotations

from typing import Any, Callable

import httpx
import numpy as np

from db.config import get_settings


def _clamp01(values: list) -> list[float]:
    """Clamp a series into [0, 1] so the engine's pu<=1 validation never trips."""
    return [min(1.0, max(0.0, float(v))) for v in values]


def normalise_wind(resp: dict) -> dict:
    """Wind ``/yield`` response (absolute MW) → per-unit shape (0–1) + provenance."""
    series = resp.get("series") or {}
    mw = list(series.get("mw") or [])
    cap = float(resp.get("total_capacity_mw") or 0.0)
    pu = _clamp01([m / cap for m in mw]) if cap > 0 else [0.0 for _ in mw]
    return {
        "pu": pu,
        "nameplate_mw": cap,
        "times": list(series.get("times") or []),
        "meta": {
            "nameplate_mw": cap,
            "aep_mwh": resp.get("aep_mwh"),
            "capacity_factor_pct": resp.get("capacity_factor_pct"),
            "peak_mw": resp.get("peak_mw"),
            "layout": resp.get("layout"),
            "period": resp.get("period"),
        },
    }


def normalise_solar(resp: dict) -> dict:
    """Solar ``/solar-profile`` response → per-unit shape (0–1) + provenance.

    Solar already returns ``per_kwp`` (normalised to DC capacity), so we use it
    directly; the nameplate is DC MWp (``dc_capacity_kw`` / 1000) so that
    ``pu × nameplate == MW``. Falls back to ``mw / dc_mw`` if per_kwp is absent.
    """
    series = resp.get("series") or {}
    dc_kw = float(resp.get("dc_capacity_kw") or 0.0)
    nameplate_mw = dc_kw / 1000.0
    per_kwp = list(series.get("per_kwp") or [])
    if per_kwp:
        pu = _clamp01(per_kwp)
    else:
        mw = list(series.get("mw") or [])
        pu = _clamp01([m / nameplate_mw for m in mw]) if nameplate_mw > 0 else [0.0 for _ in mw]
    return {
        "pu": pu,
        "nameplate_mw": nameplate_mw,
        "times": list(series.get("times") or []),
        "meta": {
            "nameplate_mw": nameplate_mw,
            "dc_capacity_kw": dc_kw,
            "ac_capacity_kw": resp.get("ac_capacity_kw"),
            "aep_mwh": resp.get("aep_mwh"),
            "capacity_factor_pct": resp.get("capacity_factor_pct"),
            "tilt_deg": resp.get("tilt_deg"),
            "azimuth_deg": resp.get("azimuth_deg"),
            "orientation_auto": resp.get("orientation_auto"),
            "period": resp.get("period"),
        },
    }


def resample_to_length(values: list[float], target_len: int) -> list[float]:
    """Position-align a per-unit series to the target grid length by linear
    interpolation over the normalised index. A no-op when lengths already match."""
    n = len(values)
    if n == 0:
        return [0.0] * target_len
    if n == target_len:
        return [float(v) for v in values]
    src_idx = np.linspace(0.0, 1.0, n)
    dst_idx = np.linspace(0.0, 1.0, target_len)
    return [float(v) for v in np.interp(dst_idx, src_idx, np.asarray(values, dtype="float64"))]


async def _fetch(client: httpx.AsyncClient, url: str, lat: float, lon: float,
                 area_km2: float, extra: dict[str, Any] | None,
                 normaliser: Callable[[dict], dict]) -> dict:
    payload: dict[str, Any] = {"lat": lat, "lon": lon, "area_km2": area_km2,
                               "include_hourly": True}
    if extra:
        payload.update({k: v for k, v in extra.items() if v is not None})
    resp = await client.post(url, json=payload)
    resp.raise_for_status()
    return normaliser(resp.json())


async def fetch_generation(
    lat: float, lon: float, area_km2: float,
    target_len: int,
    wind_params: dict | None = None,
    solar_params: dict | None = None,
) -> dict:
    """Fetch wind (and solar when available) for a site and return per-unit shapes
    resampled to ``target_len``, plus each source's area-fit nameplate + provenance.

    Returns a dict:
        { "wind_pu": [...], "wind_nameplate_mw": float, "wind_meta": {...},
          "solar_pu": [...], "solar_nameplate_mw": float, "solar_meta": {...} }
    Solar is zero-filled (nameplate 0) when the solar endpoint is not configured
    or is unreachable and ``gen_solar_optional`` is set.
    """
    s = get_settings()
    out: dict[str, Any] = {
        "wind_pu": [0.0] * target_len, "wind_nameplate_mw": 0.0, "wind_meta": None,
        "solar_pu": [0.0] * target_len, "solar_nameplate_mw": 0.0, "solar_meta": None,
    }
    if not s.gen_enabled:
        return out

    async with httpx.AsyncClient(base_url=s.gen_api_base, timeout=s.gen_api_timeout) as client:
        # Wind — required. Let errors propagate so a bad build fails loudly.
        wind = await _fetch(client, s.gen_wind_path, lat, lon, area_km2,
                            wind_params, normalise_wind)
        out["wind_pu"] = resample_to_length(wind["pu"], target_len)
        out["wind_nameplate_mw"] = wind["nameplate_mw"]
        out["wind_meta"] = wind["meta"]

        # Solar — optional (endpoint may be absent in some deployments).
        if s.gen_solar_path:
            try:
                solar = await _fetch(client, s.gen_solar_path, lat, lon, area_km2,
                                    solar_params, normalise_solar)
                out["solar_pu"] = resample_to_length(solar["pu"], target_len)
                out["solar_nameplate_mw"] = solar["nameplate_mw"]
                out["solar_meta"] = solar["meta"]
            except Exception as exc:  # noqa: BLE001
                if not s.gen_solar_optional:
                    raise
                out["solar_meta"] = {"unavailable": str(exc)[:300]}
    return out
