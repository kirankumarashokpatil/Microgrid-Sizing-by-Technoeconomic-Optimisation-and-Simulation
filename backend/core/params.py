"""
Parameters Module
-----------------
Defines all strongly-typed parameter containers used across the pipeline.

Physical sizing uses PhysicalParams only (no prices, no CAPEX).

SizingResult is the output of one LP solve.
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

    # ── Dispatch policy (Model R) — frontend-overridable ──────────────────
    # Custom causal merit order. Empty ⇒ use the mode's built-in default order
    # (see rule_dispatch.DEFAULT_PRIORITY). Entries must be dispatch actions
    # from rule_dispatch.DISPATCH_ACTIONS, applied in the given sequence.
    dispatch_priority: tuple = ()
    # Tri-state grid-charging override. None ⇒ infer from mode (peak-shaving on,
    # self-sufficiency off). True/False ⇒ force the frontend's choice.
    allow_grid_charge: object = None    # Optional[bool]

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
    achieved_osr_pct: float  = 0.0   # over-supply ratio / curtailment %
    total_curtailed_mwh: float = 0.0
    bess_duration_h: float   = 0.0   # = bess_mwh / bess_mw (or 0 if bess_mw=0)
    exported_mwh: float      = 0.0
    peak_export_mw: float    = 0.0
    feasible: bool           = True

    # ── Legacy fields kept for backward compat with dispatch_engine / financials ──
    @property
    def solar_mw(self) -> float:
        """Alias for pv_mw — legacy dispatch engine uses this name."""
        return self.pv_mw

    @property
    def wind_mw(self) -> float:
        """No wind in Phase 1 — always 0."""
        return 0.0
