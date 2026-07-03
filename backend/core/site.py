"""
Site / Land Capacity
--------------------
Turns a buildable land parcel into the AVAILABLE GENERATION it can host — the
maximum installable solar (MWp) and wind (MW), plus first-order annual yields.

This is the authoritative engineering calculation (it used to live in the
front-end). The UI only supplies the parcel area (and location); every capacity
and energy number is computed here so it is consistent and traceable.
"""

from __future__ import annotations

import math

# Utility-scale rules of thumb.
_GCR          = 0.45     # ground-coverage ratio (panel area / land area)
_PANEL_EFF    = 0.20     # module efficiency
_PR           = 0.75     # performance ratio (losses)
_KT           = 0.55     # clearness index (atmospheric transmittance)
_GSC          = 1367.0   # solar constant (W/m²)
_WIND_MW_PER_HA = 48.0 / 184.0   # ~0.26 MW/ha (calibrated to the reference site)
_WIND_CF      = 0.34     # wind capacity factor
# Solar nameplate density (MWp/ha) implied by GCR × module efficiency — the inverse
# of parcel_capacity's solar sizing. Location-independent (nameplate, not yield).
_SOLAR_MW_PER_HA = _GCR * _PANEL_EFF * 10_000.0 / 1000.0   # ≈ 0.9 MWp/ha


def _annual_irradiation_kwh_m2(lat_deg: float) -> float:
    """Clear-sky annual global horizontal irradiation (kWh/m²/yr) for a latitude,
    via a daily extraterrestrial-radiation integral scaled by the clearness index.
    Same model the mock UI used, ported to the backend."""
    phi = math.radians(lat_deg)
    total = 0.0
    for n in range(1, 366):
        delta = math.radians(23.45 * math.sin(math.radians(360 / 365 * (284 + n))))
        arg = -math.tan(phi) * math.tan(delta)
        if arg <= -1:
            ws = math.pi
        elif arg >= 1:
            ws = 0.0
        else:
            ws = math.acos(arg)
        ecc = 1 + 0.033 * math.cos(math.radians(360 * n / 365))
        ang = (math.cos(phi) * math.cos(delta) * math.sin(ws)
               + ws * math.sin(phi) * math.sin(delta))
        h0_joules = (24 * 3600 / math.pi) * _GSC * ecc * ang   # J/m²/day
        total += max(0.0, h0_joules) / 3.6e6                   # → kWh/m²/day
    return total * _KT


def parcel_capacity(area_ha: float, lat_deg: float = 51.96, lon_deg: float = 1.35) -> dict:
    """Available generation a parcel can host.

    Returns max installable solar (MWp) and wind (MW), the solar capacity factor,
    and first-order annual energy yields (GWh). Wind is location-agnostic here
    (a flat capacity factor); solar uses the latitude-dependent irradiation."""
    area_ha = max(0.0, float(area_ha))
    area_m2 = area_ha * 10_000.0

    # Solar
    solar_mwp = area_m2 * _GCR * _PANEL_EFF / 1000.0
    irr = _annual_irradiation_kwh_m2(lat_deg)                  # kWh/m²/yr
    solar_mwh = area_m2 * _GCR * _PANEL_EFF * irr * _PR / 1000.0
    solar_cf = (solar_mwh / (solar_mwp * 8760.0) * 100.0) if solar_mwp > 1e-9 else 0.0

    # Wind
    wind_mw = area_ha * _WIND_MW_PER_HA
    wind_mwh = wind_mw * 8760.0 * _WIND_CF

    return {
        "area_ha":      round(area_ha, 2),
        "lat":          lat_deg,
        "lon":          lon_deg,
        "max_solar_mw": round(solar_mwp, 1),
        "max_wind_mw":  round(wind_mw, 1),
        "solar_cf_pct": round(solar_cf, 1),
        "solar_gwh":    round(solar_mwh / 1000.0, 1),
        "wind_gwh":     round(wind_mwh / 1000.0, 1),
        "irradiation_kwh_m2": round(irr, 0),
    }


def area_for_capacity(mw: float, tech: str = "solar",
                      lat_deg: float = 51.96, lon_deg: float = 1.35) -> dict:
    """Inverse of parcel_capacity: the land a target nameplate needs.

    Given a desired solar/wind capacity (MW), return the required area (ha) at
    the tech's build density, plus the same yields parcel_capacity reports for
    that area — so the UI's "size by generation" is consistent with "size by
    area". Density is location-independent (nameplate); yields use the latitude."""
    mw = max(0.0, float(mw))
    density = _WIND_MW_PER_HA if tech == "wind" else _SOLAR_MW_PER_HA
    area_ha = mw / density if density > 1e-9 else 0.0
    out = parcel_capacity(area_ha, lat_deg, lon_deg)
    out["tech"] = tech
    out["target_mw"] = round(mw, 1)
    return out
