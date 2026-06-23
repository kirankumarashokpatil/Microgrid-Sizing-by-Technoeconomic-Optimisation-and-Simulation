"""
Microgrid Optimizer - Main Entry Point
--------------------------------------
Architectural Purpose:
This file serves as the singular orchestrator for the Microgrid Optimization application.
It acts like a factory assembly line, passing data linearly between the specialized modules:

1. Config Loader   -> Loads Excel data into memory and instantiates ProjectParams
2. Sizing Engine   -> Uses HiGHS to find the cheapest hardware sizes that hit the SSR target
3. Dispatch Engine -> Runs HiGHS LP solver to simulate how that hardware operates hourly
4. KPI Engine      -> Extracts physical metrics (SCR, SSR, GCmin) from the dispatch results
5. Financials      -> Combines physical KPIs with economics to produce LCOE, NPV, etc.
6. Reporter        -> Saves the final outputs back to Excel
"""
import argparse
from pathlib import Path
import pandas as pd
from optimizer.config_loader import load_configuration
from optimizer.sizing_engine import run_sizing
from optimizer.dispatch_engine import run_dispatch
from optimizer.kpi_engine import compute_kpis
from optimizer.financials import calculate_financials
from optimizer.reporter import save_report
from optimizer.schema import ResultCols

DEFAULT_EXCEL_PATH = Path(__file__).with_name("Project_Config_115_2026-06-16.xlsx")

def parse_args():
    parser = argparse.ArgumentParser(description="Run the microgrid optimizer.")
    parser.add_argument(
        "excel_path",
        nargs="?",
        default=str(DEFAULT_EXCEL_PATH),
        help="Path to the Excel workbook. Defaults to Project_Config_115_2026-06-16.xlsx next to main.py.",
    )
    return parser.parse_args()

def main(excel_path=None):
    excel_path = Path(excel_path or parse_args().excel_path).expanduser()
    if not excel_path.exists():
        print(f"Error: Could not find {excel_path}")
        return

    # Step 1: Load Data & Parameters
    sim_df, merged_df, params = load_configuration(str(excel_path))
    dt = params.time_resolution_hours

    # Step 2: Run MGA Hardware Sizing Sweep
    candidates = run_sizing(sim_df, dt, params)
    
    all_res_df = []
    all_fin_df = []
    
    for sizing_result in candidates:
        scenario_name = sizing_result.scenario_name
        print(f"\n========================================================")
        print(f"--- Running Dispatch & Financials: {scenario_name} ---")
        print(f"========================================================")
        
        # Step 3: Run Pyomo Dispatch Simulation
        res_df = run_dispatch(merged_df, sim_df, dt, params, sizing_result)
        res_df.insert(0, ResultCols.SCENARIO, scenario_name)

        # Step 4: Calculate KPIs
        kpi_dict = compute_kpis(res_df, dt, params)

        # Step 5: Calculate Economics
        fin_df = calculate_financials(res_df, dt, sizing_result, params, kpi_dict)
        fin_df.insert(0, ResultCols.SCENARIO, scenario_name)
        
        # Sizing / Dispatch Cross-Check
        dispatch_peak = res_df[ResultCols.GRID_IMPORT].max()
        if abs(sizing_result.peak_grid_mw - dispatch_peak) >= 1.0:
            raise ValueError(
                f"Sizing and dispatch peak grid mismatch: "
                f"{sizing_result.peak_grid_mw:.1f} vs {dispatch_peak:.1f} MW"
            )
        
        all_res_df.append(res_df)
        all_fin_df.append(fin_df)

    final_res_df = pd.concat(all_res_df, ignore_index=True)
    final_fin_df = pd.concat(all_fin_df, ignore_index=True)

    # Step 6: Save to Excel
    save_report(str(excel_path), final_res_df, final_fin_df)

if __name__ == '__main__':
    main()
