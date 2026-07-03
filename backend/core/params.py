"""
Parameters Module
-----------------
Defines all strongly-typed parameter containers used across the pipeline.

Phase 1 uses PhysicalParams only (no prices, no CAPEX).
Phase 2 additionally uses EconomicParams.

SizingResult is the output of one LP solve — used in both phases.
"""

from __future__ import annotations
from dataclasses import dataclass, field


# ──────────────────────────────────────────────────────────────────────────────
# PHASE 1 — Physical Parameters Only
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PhysicalParams:
    """
    All parameters needed to run the Phase 1 physical sizing LP.
    No prices. No CAPEX. No discount rates.
    """
    # ── Timestep ──────────────────────────────────────────────────────────
    dt_hours: float = 1.0               # 1.0 for hourly data, 0.25 for 15-min

    # ── BESS Physics ──────────────────────────────────────────────────────
    eff_charge: float    = 0.95         # one-way charging efficiency
    eff_discharge: float = 0.95         # one-way discharging efficiency
    min_soc_pct: float   = 10.0         # minimum allowed SOC (%)
    max_soc_pct: float   = 90.0         # maximum allowed SOC (%)
    initial_soc_pct: float = 50.0       # starting SOC (%) for first timestep

    # E/P ratio tie-breaker bounds (spec: 2h–8h)
    min_bess_duration_h: float = 2.0    # minimum BESS duration (MWh/MW)
    max_bess_duration_h: float = 8.0    # maximum BESS duration (MWh/MW)

    # ── Degradation (~20yr end of life) ──────────────────────────────────
    # Usable BESS energy retained at end of life as a fraction of day-one
    # nameplate (spec: size so the target still holds at EoL). e.g. 80 → a
    # day-one 100 MWh pack delivers 80 MWh of usable capacity at year ~20, so
    # the EoL-honest install is grossed up by 1 / 0.80.
    eol_capacity_retention_pct: float = 80.0

    # ── Site Hard Limits ─────────────────────────────────────────────────
    site_max_bess_mw: float  = 500.0    # physical site cap on BESS power (MW)
    site_max_bess_mwh: float = 4000.0   # physical site cap on BESS energy (MWh)
    site_max_grid_mw: float  = 200.0    # hard grid connection limit (MW)
    export_limit_mw: float   = 0.0      # maximum allowed export to grid (MW)

    # ── Universal Topologies ──
    # "grid_connected_btm" (default), "off_grid", "standalone_gen"
    site_topology: str = "grid_connected_btm"

    # ── Sweep Configuration ───────────────────────────────────────────────
    # SSR curve: step size in percentage points
    ssr_sweep_step_pct: float  = 5.0    # e.g. 5 → sweep 5%, 10%, 15%…
    # Peak-shaving curve: step size in MW
    gc_sweep_step_mw: float    = 5.0    # e.g. 5 → sweep GC in 5 MW decrements

    # ── Derived Properties ────────────────────────────────────────────────
    @property
    def dod_fraction(self) -> float:
        """Depth of discharge fraction (0–1)."""
        return (self.max_soc_pct - self.min_soc_pct) / 100.0

    @property
    def usable_soc_fraction(self) -> float:
        """Alias for dod_fraction — fraction of MWh that is usable."""
        return self.dod_fraction

    @property
    def eol_retention_fraction(self) -> float:
        """End-of-life usable-capacity retention as a fraction (0–1)."""
        return max(0.01, min(1.0, self.eol_capacity_retention_pct / 100.0))


# ──────────────────────────────────────────────────────────────────────────────
# PHASE 2 — Economic Parameters (not used by Phase 1 sizing LP)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class EconomicParams:
    """
    All financial assumptions needed for Phase 2 techno-economic overlay.
    These are read from the project config Excel workbook.
    """
    # CAPEX (€ per unit of capacity)
    cost_pv_mw: float              = 700_000.0    # €/MW
    cost_wind_mw: float            = 1_300_000.0  # €/MW (wind is opt-in; 0 nameplate ⇒ no cost)
    cost_bess_mw: float            = 150_000.0    # €/MW
    cost_bess_mwh: float           = 300_000.0    # €/MWh
    grid_connection_cost_mw: float = 250_000.0    # €/MW

    # OPEX
    grid_cost_mwh: float           = 150.0        # €/MWh import price
    fixed_opex_per_mwh_year: float = 8_000.0      # €/MWh/year BESS O&M

    # BESS degradation
    cycle_life: int                = 5000
    replacement_cost_mwh: float   = 300_000.0     # €/MWh replacement
    degradation_cost_mwh: float | None = None     # override if supplied directly

    # Project finance
    nominal_discount_rate_pct: float = 8.0
    inflation_rate_pct: float        = 2.5
    project_lifespan_years: float    = 20.0
    off_take_tariff_mwh: float       = 0.0

    # ── Derived Properties ────────────────────────────────────────────────
    @property
    def real_discount_rate(self) -> float:
        return (
            (1 + self.nominal_discount_rate_pct / 100.0)
            / (1 + self.inflation_rate_pct / 100.0) - 1
        ) * 100.0

    @property
    def pv_factor(self) -> float:
        r = self.real_discount_rate / 100.0
        if r <= 0:
            return self.project_lifespan_years
        return sum(1 / (1 + r) ** t for t in range(1, int(self.project_lifespan_years) + 1))

    @property
    def real_deg_cost(self) -> float:
        if self.degradation_cost_mwh is not None:
            return self.degradation_cost_mwh
        dod = 0.8   # assume 80% DoD for degradation calculation
        if self.cycle_life <= 0 or dod <= 0:
            return 0.0
        return self.replacement_cost_mwh / (self.cycle_life * dod)


# ──────────────────────────────────────────────────────────────────────────────
# Sizing Result — output of ONE LP solve (Phase 1 or Phase 2)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SizingResult:
    """
    Strongly-typed output of a single LP sizing solve.
    Used by both Phase 1 (curve generation) and Phase 2 (dispatch verification).
    """
    scenario_name: str

    # Sized hardware
    pv_mw: float           # PV nameplate (MW) — fixed input in BESS-only sweep,
                           # or solved variable in PV+BESS co-opt
    bess_mw: float         # BESS power (MW)
    bess_mwh: float        # BESS energy capacity (MWh)
    peak_grid_mw: float    # peak grid import implied by the LP solution (MW)

    # Target that was optimised against
    target_type: str       # "ssr" | "peak_shaving" | "pv_bess_surface" | "co_opt"
    target_value: float    # e.g. 60.0 for SSR=60%, or 50.0 for GC=50 MW
    target_gc_mw: float | None = None  # Secondary target for co_opt

    # Achieved physical KPIs (read back from LP after solve)
    achieved_ssr_pct: float  = 0.0
    achieved_scr_pct: float  = 0.0   # self-consumption ratio: PV used / PV available
    bess_duration_h: float   = 0.0   # = bess_mwh / bess_mw (or 0 if bess_mw=0)
    exported_mwh: float      = 0.0
    peak_export_mw: float    = 0.0
    feasible: bool           = True

    # Phase 2 only — CAPEX (€M), populated after economic overlay
    capex_m: float = 0.0

    # ── Legacy fields kept for backward compat with dispatch_engine / financials ──
    @property
    def solar_mw(self) -> float:
        """Alias for pv_mw — legacy dispatch engine uses this name."""
        return self.pv_mw

    @property
    def wind_mw(self) -> float:
        """No wind in Phase 1 — always 0."""
        return 0.0
