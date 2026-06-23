"""
Config Loader Module
--------------------
Architectural Purpose: 
This module strictly handles I/O operations with the Excel file. By isolating data 
extraction from the mathematical engines, we ensure that the optimizer can be easily 
adapted to pull from a SQL database or a REST API in the future without changing the core math.
"""
import pandas as pd
import warnings
from optimizer.params import ProjectParams
from optimizer.schema import SIM_REQUIRED_COLUMNS, SimCols, require_columns

warnings.filterwarnings('ignore', category=UserWarning, module='openpyxl')

def validate(params: ProjectParams, sim_df: pd.DataFrame):
    require_columns(sim_df, SIM_REQUIRED_COLUMNS, "Simulation data")

    if params.dod_fraction <= 0:
        raise ValueError("DoD fraction must be positive")
    if params.grid_cost_mwh < 0:
        raise ValueError("Grid cost must be non-negative")
    if (sim_df[SimCols.DEMAND] < 0).any():
        raise ValueError("Negative demand values found")
    if sim_df.isnull().any().any():
        raise ValueError("NaN values in simulation data")
    if sim_df[SimCols.SOLAR_POWER].max() > 1.01:
        raise ValueError("Solar data appears to be in MW not per-unit - check normalisation")
    
    time_diffs = sim_df.index.to_series().diff().dropna()
    if time_diffs.nunique() > 1:
        raise ValueError("Irregular timesteps detected")

def load_configuration(excel_path):
    print(f"Loading data from {excel_path}...")
    
    # 1. Load Data
    dc_raw = pd.read_excel(excel_path, sheet_name='Data Center time series')
    dc_df = pd.DataFrame()
    dc_df['date_time'] = pd.to_datetime(dc_raw.iloc[:, 0])
    dc_df['Power Consumption'] = pd.to_numeric(dc_raw.iloc[:, 1], errors='coerce').fillna(0)
    
    try:
        gen_df = pd.read_excel(excel_path, sheet_name='Energy Timeseries')
    except Exception:
        gen_df = pd.DataFrame({'Time': dc_df['date_time'], 'solar_power_mw': 0.0, 'wind_power_mw': 0.0})
        
    if 'date_time' in gen_df.columns:
        gen_df['date_time'] = pd.to_datetime(gen_df['date_time'])
    elif 'Time' in gen_df.columns:
        gen_df['date_time'] = pd.to_datetime(gen_df['Time'])
    gen_df['solar_power_mw'] = pd.to_numeric(gen_df['solar_power_mw'], errors='coerce').fillna(0)
    gen_df['wind_power_mw'] = pd.to_numeric(gen_df['wind_power_mw'], errors='coerce').fillna(0)
    
    # Optional Prices
    gen_df['grid_price'] = pd.to_numeric(gen_df.get('grid_price', 0), errors='coerce').fillna(0)
    gen_df['solar_price'] = pd.to_numeric(gen_df.get('solar_price', 0), errors='coerce').fillna(0)
    gen_df['wind_price'] = pd.to_numeric(gen_df.get('wind_price', 0), errors='coerce').fillna(0)
    gen_df['grid_power_mw'] = pd.to_numeric(gen_df.get('grid_power_mw', 99999.0), errors='coerce').fillna(99999.0)
    
    config_raw = pd.read_excel(excel_path, sheet_name='Optimization Config')
    bess_raw = pd.read_excel(excel_path, sheet_name='BESS Config')
    
    # Merge frames
    merged_df = pd.merge(dc_df, gen_df, on="date_time", how="inner").fillna(0)
    
    sim_df = pd.DataFrame()
    # Set the index explicitly for validation time_diffs
    sim_df.index = merged_df['date_time']
    sim_df[SimCols.GRID_PRICE] = merged_df['grid_price'].values
    sim_df[SimCols.SOLAR_PRICE] = merged_df['solar_price'].values
    sim_df[SimCols.WIND_PRICE] = merged_df['wind_price'].values
    sim_df[SimCols.GRID_LIMIT] = merged_df['grid_power_mw'].values
    sim_df[SimCols.SOLAR_POWER] = merged_df['solar_power_mw'].values
    sim_df[SimCols.WIND_POWER] = merged_df['wind_power_mw'].values
    sim_df[SimCols.DEMAND] = merged_df['Power Consumption'].values
    
    # Extracted Parameters into Single Source of Truth
    rolling_step_hours = (
        config_raw.loc[0, 'rolling_step_hours']
        if 'rolling_step_hours' in config_raw.columns
        else config_raw.loc[0, 'rollingStep']
        if 'rollingStep' in config_raw.columns
        else config_raw.loc[0, 'rollingHorizon'] / 2
    )

    degradation_cost_mwh = (
        float(bess_raw.loc[0, 'degradation_usd_mwh'])
        if 'degradation_usd_mwh' in bess_raw.columns and pd.notna(bess_raw.loc[0, 'degradation_usd_mwh'])
        else None
    )

    params = ProjectParams(
        horizon_hours=int(config_raw.loc[0, 'rollingHorizon']),
        time_resolution_hours=float(config_raw.loc[0, 'timeResolution']),
        mode=str(config_raw.loc[0, 'mode']).lower().strip(),
        rolling_step_hours=int(rolling_step_hours),
        
        eff_charge=float(bess_raw.loc[0, 'eff_charge_pct']) / 100.0,
        eff_discharge=float(bess_raw.loc[0, 'eff_discharge_pct']) / 100.0,
        initial_soc_pct=float(min(bess_raw.loc[0, 'initial_soc_pct'], bess_raw.loc[0, 'max_soc_pct'])),
        min_soc_pct=float(bess_raw.loc[0, 'min_soc_pct']) if 'min_soc_pct' in bess_raw.columns else 10.0,
        max_soc_pct=float(bess_raw.loc[0, 'max_soc_pct']),
        cycle_life=int(bess_raw.loc[0, 'cycle_life']) if 'cycle_life' in bess_raw.columns else 5000,
        replacement_cost_mwh=float(bess_raw.loc[0, 'replacement_cost_mwh']) if 'replacement_cost_mwh' in bess_raw.columns else 300000.0,
        
        grid_cost_mwh=float(config_raw.loc[0, 'grid_cost_per_mwh']) if 'grid_cost_per_mwh' in config_raw.columns else 150.0,
        grid_connection_cost_mw=float(config_raw.loc[0, 'grid_connection_cost_per_mw']) if 'grid_connection_cost_per_mw' in config_raw.columns else 250000.0,
        nominal_discount_rate_pct=float(config_raw.loc[0, 'discount_rate_pct']) if 'discount_rate_pct' in config_raw.columns else 8.0,
        inflation_rate_pct=float(config_raw.loc[0, 'inflation_rate_pct']) if 'inflation_rate_pct' in config_raw.columns else 2.5,
        fixed_opex_per_mwh_year=float(bess_raw.loc[0, 'fixed_opex_per_mwh_year']) if 'fixed_opex_per_mwh_year' in bess_raw.columns else 8000.0,
        project_lifespan_years=float(config_raw.loc[0, 'project_lifespan_years']) if 'project_lifespan_years' in config_raw.columns else 15.0,
        off_take_tariff_mwh=float(config_raw.loc[0, 'off_take_tariff_mwh']) if 'off_take_tariff_mwh' in config_raw.columns else 0.0,
        degradation_cost_mwh=degradation_cost_mwh,
        
        cost_solar_mw=float(config_raw.loc[0, 'cost_solar_mw']) if 'cost_solar_mw' in config_raw.columns else 1.0,
        cost_wind_mw=float(config_raw.loc[0, 'cost_wind_mw']) if 'cost_wind_mw' in config_raw.columns else 1.2,
        cost_bess_mw=float(config_raw.loc[0, 'cost_bess_mw']) if 'cost_bess_mw' in config_raw.columns else 0.15,
        cost_bess_mwh=float(config_raw.loc[0, 'cost_bess_mwh']) if 'cost_bess_mwh' in config_raw.columns else 0.3,
        scale_sol=float(config_raw.loc[0, 'scale_factor_solar']) if 'scale_factor_solar' in config_raw.columns else 1.0,
        scale_win=float(config_raw.loc[0, 'scale_factor_wind']) if 'scale_factor_wind' in config_raw.columns else 1.0,
        scale_bess_mw=float(config_raw.loc[0, 'scale_factor_bess_mw']) if 'scale_factor_bess_mw' in config_raw.columns else 1.0,
        scale_bess_mwh=float(config_raw.loc[0, 'scale_factor_bess_mwh']) if 'scale_factor_bess_mwh' in config_raw.columns else 1.0,
        
        target_ssr_pct=float(config_raw.loc[0, 'target_ssr_pct']) if 'target_ssr_pct' in config_raw.columns else 98.0,
        site_max_sol=float(config_raw.loc[0, 'site_max_solar_mw']) if 'site_max_solar_mw' in config_raw.columns else 99999.0,
        site_max_win=float(config_raw.loc[0, 'site_max_wind_mw']) if 'site_max_wind_mw' in config_raw.columns else 99999.0,
        site_max_bess_mw=float(config_raw.loc[0, 'site_max_bess_mw']) if 'site_max_bess_mw' in config_raw.columns else 99999.0,
        site_max_bess_mwh=float(config_raw.loc[0, 'site_max_bess_mwh']) if 'site_max_bess_mwh' in config_raw.columns else 99999.0,
        site_max_grid_mw=float(config_raw.loc[0, 'site_max_grid_import_mw']) if 'site_max_grid_import_mw' in config_raw.columns else 99999.0,
        min_bess_duration_hours=float(config_raw.loc[0, 'min_cap_hours']) if 'min_cap_hours' in config_raw.columns else 0.0,
        max_bess_duration_hours=float(config_raw.loc[0, 'max_cap_hours']) if 'max_cap_hours' in config_raw.columns else 99999.0
    )
    
    validate(params, sim_df)
    sim_df = sim_df.reset_index(drop=True)
    
    return sim_df, merged_df, params
