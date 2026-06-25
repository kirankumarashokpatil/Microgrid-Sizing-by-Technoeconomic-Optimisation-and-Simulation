"""
Financials Module
-----------------
Architectural Purpose:
This module sits at the end of the pipeline to process the physical dispatch results into 
executive-level financial metrics. By isolating the financials, we can easily change 
tax assumptions, discount rates, or LCOE formulas without risking breaking the 
core dispatch logic.
"""
import pandas as pd
from archive.legacy_pipeline.legacy_params import ProjectParams
from optimizer.params import SizingResult
from optimizer.schema import KpiKeys, RESULT_REQUIRED_COLUMNS, ResultCols, require_columns

def calculate_financials(res_df: pd.DataFrame, dt: float, sizing_result: SizingResult, params: ProjectParams, kpi_dict: dict):
    print("\n--- Step 4: Extracting Final KPIs & Calculating Economics ---")
    require_columns(res_df, RESULT_REQUIRED_COLUMNS, "Dispatch results")
    
    total_grid_import_mwh = kpi_dict[KpiKeys.TOTAL_GRID_IMPORT]
    total_demand_mwh = kpi_dict[KpiKeys.TOTAL_DEMAND]
    total_unmet_mwh = kpi_dict.get(KpiKeys.TOTAL_UNMET_LOAD, 0.0)
    served_load_mwh = kpi_dict.get(KpiKeys.SERVED_LOAD, total_demand_mwh - total_unmet_mwh)
    peak_grid_mw = sizing_result.peak_grid_mw
    
    print(f"   - Achieved Self-Sufficiency Ratio (SSR): {kpi_dict[KpiKeys.SSR]}%")
    print(f"   - Achieved Self-Consumption Ratio (SCR): {kpi_dict[KpiKeys.SCR]}%")
    print(f"   - Over-Supply Ratio (OSR / Wasted Energy): {kpi_dict[KpiKeys.OSR]}%")
    print(f"   - Derived GCmin (95th Percentile Grid Import): {kpi_dict[KpiKeys.GCMIN_P95]} MW")
    print(f"   - Peak Grid Spike (Connection Sizing Basis): {kpi_dict[KpiKeys.GCMIN_PEAK]} MW")
    print(f"   - Reliability: {kpi_dict.get(KpiKeys.RELIABILITY, 100.0)}%")
    
    # --- Economics ---
    # CAPEX: Renewable + BESS cost is already calculated in Phase 1
    # Grid connection sized at actual peak MW (DSO requirement), not P95
    grid_capex_dollars = peak_grid_mw * params.grid_connection_cost_mw
    total_capex_dollars = (sizing_result.capex_m * 1_000_000.0) + grid_capex_dollars

    # OPEX: Variable Grid Cost
    annual_grid_cost = total_grid_import_mwh * params.grid_cost_mwh

    # OPEX: BESS Degradation
    total_bess_discharge_mwh = res_df[ResultCols.BESS_DISCHARGE].sum() * dt
    annual_deg_cost = total_bess_discharge_mwh * params.real_deg_cost

    # OPEX: Fixed O&M (insurance, monitoring, maintenance contracts)
    annual_fixed_om = sizing_result.bess_mwh * params.fixed_opex_per_mwh_year

    annual_opex = annual_grid_cost + annual_deg_cost + annual_fixed_om

    # Revenue — Internal off-take tariff charged to the consumer
    annual_revenue = max(0.0, served_load_mwh - total_grid_import_mwh) * params.off_take_tariff_mwh

    # DCF / LCOE using real (inflation-adjusted) discount rate
    total_pvc = total_capex_dollars + (annual_opex * params.pv_factor)
    total_pv_revenue = annual_revenue * params.pv_factor
    npv = total_pv_revenue - total_pvc
    
    discounted_demand = total_demand_mwh * params.pv_factor
    lcoe = total_pvc / discounted_demand if discounted_demand > 0 else 0.0
    
    print(f"\n   --- Business Case Summary ---")
    print(f"   - Total CAPEX: ${total_capex_dollars:,.2f} (Includes ${grid_capex_dollars:,.0f} Grid Connection @ Peak {peak_grid_mw:.2f} MW)")
    print(f"   - Annual OPEX: ${annual_opex:,.2f}")
    print(f"       Grid Energy: ${annual_grid_cost:,.0f}  |  Degradation: ${annual_deg_cost:,.0f}  |  Fixed O&M: ${annual_fixed_om:,.0f}")
    print(f"   - Annual Revenue: ${annual_revenue:,.2f} (@ ${params.off_take_tariff_mwh:.2f}/MWh off-take tariff)")
    print(f"   - Real Discount Rate (Fisher): {params.real_discount_rate:.2f}% (Nominal {params.nominal_discount_rate_pct:.1f}% - Inflation {params.inflation_rate_pct:.1f}%)")
    print(f"   - Present Value of Costs (PVC): ${total_pvc:,.2f}")
    print(f"   - Net Present Value (NPV): ${npv:,.2f}")
    print(f"   - Levelized Cost of Energy (LCOE): ${lcoe:.2f} / MWh")
    
    fin_df = pd.DataFrame({
        'Metric': [
            'Sized Solar Capacity (MW)',
            'Sized Wind Capacity (MW)',
            'Sized BESS Power (MW)',
            'Sized BESS Capacity (MWh)',
            'Achieved Self-Sufficiency Ratio (SSR) (%)',
            'Achieved Self-Consumption Ratio (SCR) (%)',
            'Over-Supply Ratio (%)',
            'Reliability (%)',
            'Total Unmet Load (MWh)',
            'Peak Grid Import - Connection Sizing (MW)',
            'Total CAPEX ($)',
            'Grid Connection CAPEX ($)',
            'Annual Grid Energy Cost ($)',
            'Annual Degradation Cost ($)',
            'Annual Fixed O&M Cost ($)',
            'Total Annual OPEX ($)',
            'Annual Revenue ($)',
            'Net Present Value (NPV) ($)',
            'Project Lifespan (Years)',
            'Nominal Discount Rate (%)',
            'Inflation Rate (%)',
            'Real Discount Rate - Fisher (%)',
            'DoD Fraction',
            'Real Degradation Cost ($/MWh discharged)',
            'Present Value of Costs ($)',
            'Levelized Cost of Energy ($/MWh)'
        ],
        'Value': [
            sizing_result.solar_mw, sizing_result.wind_mw, sizing_result.bess_mw, sizing_result.bess_mwh,
            kpi_dict[KpiKeys.SSR], kpi_dict[KpiKeys.SCR], kpi_dict[KpiKeys.OSR],
            kpi_dict.get(KpiKeys.RELIABILITY, 100.0), total_unmet_mwh, peak_grid_mw, 
            total_capex_dollars, grid_capex_dollars,
            annual_grid_cost, annual_deg_cost, annual_fixed_om, annual_opex, annual_revenue, npv,
            params.project_lifespan_years, params.nominal_discount_rate_pct, params.inflation_rate_pct, params.real_discount_rate,
            params.dod_fraction, params.real_deg_cost, total_pvc, lcoe
        ]
    })
    
    return fin_df
