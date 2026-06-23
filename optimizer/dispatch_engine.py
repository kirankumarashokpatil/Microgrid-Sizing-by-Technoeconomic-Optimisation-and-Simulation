"""
Dispatch Engine Module
----------------------
Architectural Purpose:
This module handles 'Phase 2: Operational Dispatch'. Once the Sizing Engine has found 
the perfect hardware capacities, this module uses a Pyomo Linear Programming (LP) solver
to simulate exactly how that hardware will operate hour-by-hour over the lifespan of the project.
It uses 'Real Economic Marginal Pricing' (e.g. Solar=$0/MWh, BESS=$Degradation/MWh) to force
the solver to naturally prioritize cheap renewables over expensive grid imports.
"""
import pyomo.environ as pyo
import pandas as pd
from optimizer.params import ProjectParams, SizingResult
from optimizer.schema import RESULT_REQUIRED_COLUMNS, SIM_REQUIRED_COLUMNS, ResultCols, SimCols, require_columns
from optimizer.solver import create_highs_solver

def run_dispatch(merged_df, sim_df, dt, params: ProjectParams, sizing_result: SizingResult):
    print(f"\nStarting execution in {params.mode} mode with strategy priority_dispatch...")
    require_columns(sim_df, SIM_REQUIRED_COLUMNS, "Simulation data")

    def _classify_regime(grid_mw, dchg_mw, chg_mw, curtailed_mw, threshold=0.01):
        """Returns a human-readable label for the active dispatch regime at each timestep.
        This allows an investment committee to audit exactly why each decision was made."""
        if grid_mw > threshold:
            return 'Grid Supplement'
        elif dchg_mw > threshold:
            return 'BESS Discharge'
        elif chg_mw > threshold:
            return 'Surplus -> BESS'
        elif curtailed_mw > threshold:
            return 'Surplus -> Curtailed'
        else:
            return 'Renewables Direct'

    def create_model(window_df, current_initial_soc_pct, t_start=0):
        m = pyo.ConcreteModel()
        horizon = len(window_df)
        m.T = pyo.RangeSet(0, horizon - 1)
        
        m.p_solar = pyo.Var(m.T, within=pyo.NonNegativeReals)
        m.p_wind = pyo.Var(m.T, within=pyo.NonNegativeReals)
        m.p_grid = pyo.Var(m.T, within=pyo.NonNegativeReals)
        m.p_chg = pyo.Var(m.T, within=pyo.NonNegativeReals)
        m.p_dchg = pyo.Var(m.T, within=pyo.NonNegativeReals)
        m.p_unmet = pyo.Var(m.T, within=pyo.NonNegativeReals)
        m.e_bess = pyo.Var(m.T, within=pyo.NonNegativeReals)
        
        def unmet_limit(m, t):
            return m.p_unmet[t] <= window_df.iloc[t][SimCols.DEMAND]
        m.unmet_limit_con = pyo.Constraint(m.T, rule=unmet_limit)

        def balance_rule(m, t):
            return m.p_solar[t] + m.p_wind[t] + m.p_grid[t] + m.p_dchg[t] + m.p_unmet[t] == window_df.iloc[t][SimCols.DEMAND] + m.p_chg[t]
        m.balance_con = pyo.Constraint(m.T, rule=balance_rule)
        
        def solar_limit(m, t): return m.p_solar[t] <= window_df.iloc[t][SimCols.SOLAR_POWER] * sizing_result.solar_mw
        m.sol_limit_con = pyo.Constraint(m.T, rule=solar_limit)
        
        def wind_limit(m, t): return m.p_wind[t] <= window_df.iloc[t][SimCols.WIND_POWER] * sizing_result.wind_mw
        m.win_limit_con = pyo.Constraint(m.T, rule=wind_limit)
        
        def grid_limit(m, t): 
            return m.p_grid[t] <= min(window_df.iloc[t][SimCols.GRID_LIMIT], params.site_max_grid_mw, sizing_result.peak_grid_mw)
        m.grid_limit_con = pyo.Constraint(m.T, rule=grid_limit)
        
        m.chg_limit = pyo.Constraint(m.T, rule=lambda m, t: m.p_chg[t] <= sizing_result.bess_mw)
        m.dchg_limit = pyo.Constraint(m.T, rule=lambda m, t: m.p_dchg[t] <= sizing_result.bess_mw)
        m.max_soc_limit = pyo.Constraint(m.T, rule=lambda m, t: m.e_bess[t] <= sizing_result.bess_mwh * (params.max_soc_pct / 100.0))
        m.min_soc_limit = pyo.Constraint(m.T, rule=lambda m, t: m.e_bess[t] >= sizing_result.bess_mwh * (params.min_soc_pct / 100.0))
        
        def soc_rule(m, t):
            if t == 0:
                return m.e_bess[t] == (current_initial_soc_pct / 100.0) * sizing_result.bess_mwh + \
                                      (m.p_chg[t] * params.eff_charge - \
                                       m.p_dchg[t] / params.eff_discharge) * dt
            else:
                return m.e_bess[t] == m.e_bess[t-1] + \
                                      (m.p_chg[t] * params.eff_charge - \
                                       m.p_dchg[t] / params.eff_discharge) * dt
        m.soc_con = pyo.Constraint(m.T, rule=soc_rule)
        
        def obj_rule(m):
            total_cost = 0
            shortage_penalty = 1e6
            
            for t in m.T:
                total_cost += m.p_grid[t] * dt * params.grid_cost_mwh
                total_cost += m.p_dchg[t] * dt * params.real_deg_cost
                total_cost += m.p_unmet[t] * dt * shortage_penalty
                
                # Tie-breaker to prevent procrastination: Tiny reward for holding energy (e_bess).
                # This forces the solver to charge AS EARLY AS POSSIBLE and hold it, 
                # instead of pushing all charging to the end of the rolling horizon.
                total_cost -= m.e_bess[t] * 1e-4
                
            # Terminal Value: Stored energy at the end of the window is worth $ (Grid - Degradation)
            # Discounted by 5% to break LP indifference (so it prefers discharging now over hoarding)
            terminal_value_per_mwh = max(0, params.grid_cost_mwh - params.real_deg_cost) * 0.95
            last_t = m.T.last()
            total_cost -= m.e_bess[last_t] * terminal_value_per_mwh
            
            return total_cost
            
        m.obj = pyo.Objective(rule=obj_rule, sense=pyo.minimize)
        return m

    # Execution Loop
    output = []
    total_steps = len(sim_df)
    current_initial_soc_pct = params.initial_soc_pct
    
    solver = create_highs_solver()
    
    if params.mode == 'rolling_horizon':
        step = int(params.rolling_step_hours / dt)
        horizon_steps = int(params.horizon_hours / dt)
        
        for start in range(0, total_steps, step):
            end = min(start + horizon_steps, total_steps)
            window_df = sim_df.iloc[start:end].reset_index(drop=True)
            
            print(f"   Solving window {start} to {end}...")
            if start == 0:
                print("   [Model] Active Strategy: priority_dispatch")
                print("   [Model] Applying Real Economic Marginal Pricing...")
                print(f"      - solar: $0.00 / MWh\n      - wind: $0.00 / MWh")
                print(f"      - bess: ${params.real_deg_cost:.2f} / MWh (Degradation)\n      - grid: ${params.grid_cost_mwh:.2f} / MWh (Import)")
            
            model = create_model(window_df, current_initial_soc_pct, start)
            results = solver.solve(model, tee=False)
            
            if (results.solver.status == pyo.SolverStatus.ok) and (results.solver.termination_condition == pyo.TerminationCondition.optimal):
                extract_length = min(step, end - start)
                for t in range(extract_length):
                    sol = pyo.value(model.p_solar[t])
                    win = pyo.value(model.p_wind[t])
                    avail_sol = window_df.loc[t, SimCols.SOLAR_POWER] * sizing_result.solar_mw
                    avail_win = window_df.loc[t, SimCols.WIND_POWER] * sizing_result.wind_mw
                    grid_val = pyo.value(model.p_grid[t])
                    dchg_val = pyo.value(model.p_dchg[t])
                    chg_val = pyo.value(model.p_chg[t])
                    unmet_val = pyo.value(model.p_unmet[t])
                    curtailed_val = max(0.0, (avail_sol + avail_win) - (sol + win))
                    
                    output.append({
                        ResultCols.TIME: merged_df.iloc[start + t]['date_time'],
                        ResultCols.DEMAND: window_df.loc[t, SimCols.DEMAND],
                        ResultCols.GRID_IMPORT: grid_val,
                        ResultCols.SOLAR_USED: sol,
                        ResultCols.WIND_USED: win,
                        ResultCols.BESS_CHARGE: chg_val,
                        ResultCols.BESS_DISCHARGE: dchg_val,
                        ResultCols.UNMET_LOAD: unmet_val,
                        ResultCols.CURTAILED: curtailed_val,
                        ResultCols.SOC: pyo.value(model.e_bess[t]),
                        ResultCols.BTM_REGIME: _classify_regime(grid_val, dchg_val, chg_val, curtailed_val)
                    })
                
                last_t = extract_length - 1
                if sizing_result.bess_mwh > 0:
                    current_initial_soc_pct = (pyo.value(model.e_bess[last_t]) / sizing_result.bess_mwh) * 100.0
                else:
                    current_initial_soc_pct = 0.0
            else:
                raise RuntimeError(f"Dispatch solver failed at window {start}-{end}: {results.solver.termination_condition}")
                
    else:
        # Full Horizon Mode
        print(f"   Solving full horizon ({total_steps} steps) with HiGHS...")
        model = create_model(sim_df, current_initial_soc_pct)
        results = solver.solve(model, tee=False)
        
        if (results.solver.status == pyo.SolverStatus.ok) and (results.solver.termination_condition == pyo.TerminationCondition.optimal):
            for t in model.T:
                sol = pyo.value(model.p_solar[t])
                win = pyo.value(model.p_wind[t])
                avail_sol = sim_df.loc[t, SimCols.SOLAR_POWER] * sizing_result.solar_mw
                avail_win = sim_df.loc[t, SimCols.WIND_POWER] * sizing_result.wind_mw
                grid_val = pyo.value(model.p_grid[t])
                dchg_val = pyo.value(model.p_dchg[t])
                chg_val = pyo.value(model.p_chg[t])
                unmet_val = pyo.value(model.p_unmet[t])
                curtailed_val = max(0.0, (avail_sol + avail_win) - (sol + win))
                
                output.append({
                    ResultCols.TIME: merged_df.loc[t, 'date_time'],
                    ResultCols.DEMAND: sim_df.loc[t, SimCols.DEMAND],
                    ResultCols.GRID_IMPORT: grid_val,
                    ResultCols.SOLAR_USED: sol,
                    ResultCols.WIND_USED: win,
                    ResultCols.BESS_CHARGE: chg_val,
                    ResultCols.BESS_DISCHARGE: dchg_val,
                    ResultCols.UNMET_LOAD: unmet_val,
                    ResultCols.CURTAILED: curtailed_val,
                    ResultCols.SOC: pyo.value(model.e_bess[t]),
                    ResultCols.BTM_REGIME: _classify_regime(grid_val, dchg_val, chg_val, curtailed_val)
                })
        else:
            raise RuntimeError(f"Dispatch solver failed on full horizon: {results.solver.termination_condition}")

    res_df = pd.DataFrame(output)
    if res_df.empty:
        raise RuntimeError("Dispatch produced no output rows.")
    require_columns(res_df, RESULT_REQUIRED_COLUMNS, "Dispatch results")

    print("\n✅ Optimization successful!")
    return res_df
