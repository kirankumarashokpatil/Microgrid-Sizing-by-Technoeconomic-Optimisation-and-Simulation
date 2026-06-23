from dataclasses import dataclass

@dataclass
class ProjectParams:
    """
    Single Source of Truth for all physical and economic assumptions.
    Prevents different modules from using different defaults.
    """
    # General Config
    horizon_hours: int
    time_resolution_hours: float
    mode: str
    rolling_step_hours: int
    
    # BESS Physics
    eff_charge: float
    eff_discharge: float
    initial_soc_pct: float
    min_soc_pct: float
    max_soc_pct: float
    cycle_life: int
    replacement_cost_mwh: float

    # Economics
    grid_cost_mwh: float
    grid_connection_cost_mw: float
    nominal_discount_rate_pct: float
    inflation_rate_pct: float
    fixed_opex_per_mwh_year: float
    project_lifespan_years: float
    off_take_tariff_mwh: float
    degradation_cost_mwh: float | None
    
    # Hardware Costs
    cost_solar_mw: float
    cost_wind_mw: float
    cost_bess_mw: float
    cost_bess_mwh: float
    scale_sol: float
    scale_win: float
    scale_bess_mw: float
    scale_bess_mwh: float
    
    # Site Constraints
    target_ssr_pct: float
    site_max_sol: float
    site_max_win: float
    site_max_bess_mw: float
    site_max_bess_mwh: float
    site_max_grid_mw: float
    min_bess_duration_hours: float
    max_bess_duration_hours: float

    # Derived Properties
    @property
    def dod_fraction(self) -> float:
        return (self.max_soc_pct - self.min_soc_pct) / 100.0

    @property
    def real_discount_rate(self) -> float:
        return ((1 + self.nominal_discount_rate_pct / 100.0) /
                (1 + self.inflation_rate_pct / 100.0) - 1) * 100.0

    @property
    def real_deg_cost(self) -> float:
        if self.degradation_cost_mwh is not None:
            return self.degradation_cost_mwh
        if self.cycle_life <= 0 or self.dod_fraction <= 0:
            return 0.0
        return self.replacement_cost_mwh / (self.cycle_life * self.dod_fraction)

    @property
    def pv_factor(self) -> float:
        r = self.real_discount_rate / 100.0
        if r <= 0:
            return self.project_lifespan_years
        return sum(1 / (1 + r)**t for t in range(1, int(self.project_lifespan_years) + 1))


@dataclass
class SizingResult:
    """
    Strongly typed container for sizing results to be passed between modules.
    """
    scenario_name: str
    solar_mw: float
    wind_mw: float
    bess_mw: float
    bess_mwh: float
    peak_grid_mw: float
    capex_m: float
