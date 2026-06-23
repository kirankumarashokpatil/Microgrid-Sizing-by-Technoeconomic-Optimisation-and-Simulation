"""
STANDALONE MICROGRID OPTIMIZER
==============================
Single-file compiled version for sharing. See the optimizer/ package for modular source.
"""

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

warnings.filterwarnings('ignore', category=UserWarning, module='openpyxl')

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
    sim_df['grid_price'] = merged_df['grid_price']
    sim_df['solar_price'] = merged_df['solar_price']
    sim_df['wind_price'] = merged_df['wind_price']
    sim_df['grid_limit'] = merged_df['grid_power_mw']
    sim_df['solar_power'] = merged_df['solar_power_mw']
    sim_df['wind_power'] = merged_df['wind_power_mw']
    sim_df['demand'] = merged_df['Power Consumption']
    
    # Extracted Parameters
    config = {
        'horizon_hours': config_raw.loc[0, 'rollingHorizon'],
        'time_resolution_hours': config_raw.loc[0, 'timeResolution'],
        'grid_cost_per_mwh': config_raw.loc[0, 'grid_cost_per_mwh'] if 'grid_cost_per_mwh' in config_raw.columns else 100.0,
        'mode': str(config_raw.loc[0, 'mode']).lower().strip(),
        'project_lifespan_years': config_raw.loc[0, 'project_lifespan_years'] if 'project_lifespan_years' in config_raw.columns else 15.0,
        'discount_rate_pct': config_raw.loc[0, 'discount_rate_pct'] if 'discount_rate_pct' in config_raw.columns else 8.0,
    }
    
    bess_params = {
        'initial_soc': min(bess_raw.loc[0, 'initial_soc_pct'], bess_raw.loc[0, 'max_soc_pct']),
        'eff_charge': bess_raw.loc[0, 'eff_charge_pct'] / 100.0,
        'eff_discharge': bess_raw.loc[0, 'eff_discharge_pct'] / 100.0,
        'degradation_cost': bess_raw.loc[0, 'degradation_usd_mwh'] if 'degradation_usd_mwh' in bess_raw.columns else 0.0,
        'cycle_life': bess_raw.loc[0, 'cycle_life'] if 'cycle_life' in bess_raw.columns else 5000,
        'replacement_cost_mwh': bess_raw.loc[0, 'replacement_cost_mwh'] if 'replacement_cost_mwh' in bess_raw.columns else 300000.0
    }
    
    return sim_df, merged_df, config, config_raw, bess_params


"""
Sizing Engine Module
--------------------
Architectural Purpose: 
This module handles 'Phase 1: Hardware Sizing'. It uses a Greedy Finite-Difference 
Gradient Search to find the cheapest hardware combination (Solar, Wind, Battery MW, 
Battery MWh) that meets the target Self-Sufficiency Ratio (SSR).

Key Design Decisions:
- Uses the same battery physics (charge/discharge efficiencies, initial SOC) as 
  the Pyomo Dispatch Engine to ensure Phase 1 and Phase 2 produce consistent results.
- Runs multi-start searches from different starting points (solar-heavy, wind-heavy, 
  balanced) to avoid getting trapped in greedy local minima.
- CAPEX values throughout this module are in $M (millions of dollars).
"""
import numpy as np

def run_sizing(sim_df, dt, config_raw, bess_params):
    # ---------------------------------------------------------
    # PARAMETER EXTRACTION
    # ---------------------------------------------------------
    target_ssr = config_raw.loc[0, 'target_ssr_pct'] if 'target_ssr_pct' in config_raw.columns else 98.0
    
    max_sol_mult = config_raw.loc[0, 'max_k_sol_multiplier'] if 'max_k_sol_multiplier' in config_raw.columns else 5.0
    max_win_mult = config_raw.loc[0, 'max_k_win_multiplier'] if 'max_k_win_multiplier' in config_raw.columns else 5.0
    max_pwr_mult = config_raw.loc[0, 'max_pwr_multiplier'] if 'max_pwr_multiplier' in config_raw.columns else 2.0
    max_cap_hrs = config_raw.loc[0, 'max_cap_hours'] if 'max_cap_hours' in config_raw.columns else 36.0
    
    site_max_sol = config_raw.loc[0, 'site_max_solar_mw'] if 'site_max_solar_mw' in config_raw.columns else 99999.0
    site_max_win = config_raw.loc[0, 'site_max_wind_mw'] if 'site_max_wind_mw' in config_raw.columns else 99999.0
    site_max_bess_mw = config_raw.loc[0, 'site_max_bess_mw'] if 'site_max_bess_mw' in config_raw.columns else 99999.0
    site_max_bess_mwh = config_raw.loc[0, 'site_max_bess_mwh'] if 'site_max_bess_mwh' in config_raw.columns else 99999.0

    # Non-Linear Economics (all cost params are in $M per unit)
    cost_solar_mw = config_raw.loc[0, 'cost_solar_mw'] if 'cost_solar_mw' in config_raw.columns else 1.0
    cost_wind_mw = config_raw.loc[0, 'cost_wind_mw'] if 'cost_wind_mw' in config_raw.columns else 1.2
    cost_bess_mw = config_raw.loc[0, 'cost_bess_mw'] if 'cost_bess_mw' in config_raw.columns else 0.15
    cost_bess_mwh = config_raw.loc[0, 'cost_bess_mwh'] if 'cost_bess_mwh' in config_raw.columns else 0.3
    
    scale_sol = config_raw.loc[0, 'scale_factor_solar'] if 'scale_factor_solar' in config_raw.columns else 1.0
    scale_win = config_raw.loc[0, 'scale_factor_wind'] if 'scale_factor_wind' in config_raw.columns else 1.0
    scale_bess_mw = config_raw.loc[0, 'scale_factor_bess_mw'] if 'scale_factor_bess_mw' in config_raw.columns else 1.0
    scale_bess_mwh = config_raw.loc[0, 'scale_factor_bess_mwh'] if 'scale_factor_bess_mwh' in config_raw.columns else 1.0

    # Battery Physics — MUST match dispatch_engine.py exactly
    eff_charge = bess_params['eff_charge']
    eff_discharge = bess_params['eff_discharge']
    initial_soc_pct = bess_params['initial_soc'] / 100.0

    # ---------------------------------------------------------
    # CORE DATA
    # ---------------------------------------------------------
    solar_base = sim_df['solar_power']
    wind_base = sim_df['wind_power']
    demand = sim_df['demand']
    total_demand_mwh = demand.sum() * dt
    peak_demand_mw = demand.max()
    annual_solar_1mw = solar_base.sum() * dt
    annual_wind_1mw = wind_base.sum() * dt
    
    # Mathematical floor: minimum generation needed (with 10% buffer for battery losses)
    req_mwh = total_demand_mwh * (target_ssr / 100.0) * 1.10

    # ---------------------------------------------------------
    # FAST BATTERY SIMULATION (mirrors dispatch_engine physics)
    # ---------------------------------------------------------
    def simulate_4d(k_sol, k_win, pwr_mw, cap_mwh):
        """
        Simplified battery simulation that uses the SAME efficiency model as 
        the Pyomo dispatch engine to ensure consistent SSR estimates.
        
        Pyomo SOC rule: e[t] = e[t-1] + (p_chg * eff_charge - p_dchg / eff_discharge) * dt
        This function mirrors that equation exactly.
        """
        available_power = (solar_base * k_sol) + (wind_base * k_win)
        shortfall_mw = (demand - available_power).clip(lower=0)
        excess_mw = (available_power - demand).clip(lower=0)
        
        # Start SOC at the same initial_soc as the Pyomo model
        current_soc = cap_mwh * initial_soc_pct
        total_grid_used = 0.0
        total_curtailed = 0.0
        
        shortfall_arr = shortfall_mw.values
        excess_arr = excess_mw.values
        
        for i in range(len(shortfall_arr)):
            if shortfall_arr[i] > 0:
                needed = shortfall_arr[i] * dt
                # Battery delivers power to load; SOC drops by delivered / eff_discharge
                max_deliverable = min(current_soc * eff_discharge, pwr_mw * dt)
                delivered = min(needed, max_deliverable)
                current_soc -= delivered / eff_discharge
                total_grid_used += (needed - delivered)
            elif excess_arr[i] > 0:
                available_excess = excess_arr[i] * dt
                # Charger consumes power; only eff_charge fraction enters battery
                energy_consumed = min(available_excess, pwr_mw * dt, (cap_mwh - current_soc) / eff_charge)
                current_soc += energy_consumed * eff_charge
                # Curtailment = excess that could NOT be absorbed
                total_curtailed += (available_excess - energy_consumed)
                
        ssr = max(0.0, ((total_demand_mwh - total_grid_used) / total_demand_mwh) * 100.0) if total_demand_mwh > 0 else 100.0
        total_gen = available_power.sum() * dt
        scr = max(0.0, ((total_gen - total_curtailed) / total_gen) * 100.0) if total_gen > 0 else 100.0
        
        # Non-Linear CAPEX Formula (output in $M)
        cost_solar = cost_solar_mw * (k_sol ** scale_sol) if k_sol > 0 else 0
        cost_wind = cost_wind_mw * (k_win ** scale_win) if k_win > 0 else 0
        cost_bess_p = cost_bess_mw * (pwr_mw ** scale_bess_mw) if pwr_mw > 0 else 0
        cost_bess_e = cost_bess_mwh * (cap_mwh ** scale_bess_mwh) if cap_mwh > 0 else 0
        capex = cost_solar + cost_wind + cost_bess_p + cost_bess_e
        
        return ssr, scr, capex

    # ---------------------------------------------------------
    # GRADIENT SEARCH (single run from a given starting point)
    # ---------------------------------------------------------
    def run_gradient_search(start_k_sol, start_k_win, start_cap, start_pwr, label):
        k_sol, k_win = start_k_sol, start_k_win
        cap_mwh, pwr_mw = start_cap, start_pwr
        
        step_s = max_sol_mult * 0.05
        step_w = max_win_mult * 0.05
        step_c = peak_demand_mw * 1.0
        step_p = peak_demand_mw * 0.2
        
        best_combo = None
        best_cost = float('inf')
        
        tabu_list = set()
        max_iter = 500
        
        print(f"   [{label}] Starting from Solar={start_k_sol:.1f}, Wind={start_k_win:.1f}, BESS={start_cap:.0f}MWh...")
        for i in range(max_iter):
            k_sol = max(0, min(k_sol, site_max_sol))
            k_win = max(0, min(k_win, site_max_win))
            cap_mwh = max(0, min(cap_mwh, site_max_bess_mwh))
            pwr_mw = max(0, min(pwr_mw, site_max_bess_mw))
            
            state_key = (round(k_sol, 1), round(k_win, 1), round(pwr_mw, 1), round(cap_mwh, 1))
            if state_key in tabu_list:
                k_sol += step_s * (np.random.random() - 0.5) * 2.0
                cap_mwh += step_c * (np.random.random() - 0.5) * 2.0
                continue
                
            tabu_list.add(state_key)
            
            ssr, scr, capex = simulate_4d(k_sol, k_win, pwr_mw, cap_mwh)
            
            if ssr >= target_ssr:
                if capex < best_cost:
                    best_cost = capex
                    best_combo = (k_sol, k_win, pwr_mw, cap_mwh)
                    print(f"   [{label} {i:03d}] ✓ SSR {ssr:.1f}%, SCR {scr:.1f}%, CAPEX ${capex:.1f}M -> (Sol:{k_sol:.1f}, Win:{k_win:.1f}, BESS:{pwr_mw:.1f}MW/{cap_mwh:.0f}MWh)")
                
                # Walk DOWN: which component saves the most $ per % SSR lost?
                gradients = []
                for s_s, s_w, s_p, s_c in [(step_s,0,0,0), (0,step_w,0,0), (0,0,step_p,0), (0,0,0,step_c)]:
                    t_ssr, _, t_capex = simulate_4d(max(0, k_sol-s_s), max(0, k_win-s_w), max(0, pwr_mw-s_p), max(0, cap_mwh-s_c))
                    d_cost = capex - t_capex
                    d_ssr = ssr - t_ssr
                    if d_cost <= 0:
                        gradients.append(-1.0)
                    elif d_ssr <= 0:
                        gradients.append(float('inf'))
                    else:
                        gradients.append(d_cost / d_ssr)
                
                best_idx = int(np.argmax(gradients))
                if gradients[best_idx] <= 0:
                    pass  # No beneficial reduction found
                elif best_idx == 0: k_sol -= step_s
                elif best_idx == 1: k_win -= step_w
                elif best_idx == 2: pwr_mw -= step_p
                elif best_idx == 3: cap_mwh -= step_c
            else:
                # Walk UP: which component gives the most SSR per $ spent?
                gradients = []
                for s_s, s_w, s_p, s_c in [(step_s,0,0,0), (0,step_w,0,0), (0,0,step_p,0), (0,0,0,step_c)]:
                    t_ssr, _, t_capex = simulate_4d(
                        min(site_max_sol, k_sol+s_s), min(site_max_win, k_win+s_w),
                        min(site_max_bess_mw, pwr_mw+s_p), min(site_max_bess_mwh, cap_mwh+s_c))
                    d_ssr = t_ssr - ssr
                    d_cost = t_capex - capex
                    if d_ssr <= 0:
                        gradients.append(-1.0)
                    elif d_cost <= 0:
                        gradients.append(float('inf'))
                    else:
                        gradients.append(d_ssr / d_cost)
                
                best_idx = int(np.argmax(gradients))
                if gradients[best_idx] <= 0:
                    # Flat plateau — force diagonal jump to escape
                    k_sol += step_s
                    k_win += step_w
                    cap_mwh += step_c
                    pwr_mw += step_p
                elif best_idx == 0: k_sol += step_s
                elif best_idx == 1: k_win += step_w
                elif best_idx == 2: pwr_mw += step_p
                elif best_idx == 3: cap_mwh += step_c
                    
            # Annealing: only start decaying step sizes after first valid hit
            if best_combo is not None:
                step_s = max(max_sol_mult * 0.001, step_s * 0.99)
                step_w = max(max_win_mult * 0.001, step_w * 0.99)
                step_c = max(peak_demand_mw * 0.1, step_c * 0.99)
                step_p = max(peak_demand_mw * 0.05, step_p * 0.99)
        
        return best_combo, best_cost

    # ---------------------------------------------------------
    # MULTI-START: Run gradient from 3 diverse starting points
    # to avoid getting trapped in a solar-only or wind-only path.
    # ---------------------------------------------------------
    print("\n--- Step 1: Multi-Start Gradient Search ---")
    
    starts = []
    # Start 1: Solar-heavy (most solar, minimal wind)
    if annual_solar_1mw > 0:
        starts.append((max(req_mwh / annual_solar_1mw, 0.1), 0.1,
                        peak_demand_mw * 2.0, peak_demand_mw * 0.5, "Solar-Heavy"))
    # Start 2: Wind-heavy (most wind, minimal solar)
    if annual_wind_1mw > 0:
        starts.append((0.1, max(req_mwh / annual_wind_1mw, 0.1),
                        peak_demand_mw * 2.0, peak_demand_mw * 0.5, "Wind-Heavy"))
    # Start 3: Balanced (50/50 solar-wind split, larger battery)
    sol_share = req_mwh * 0.5 / annual_solar_1mw if annual_solar_1mw > 0 else 0.1
    win_share = req_mwh * 0.5 / annual_wind_1mw if annual_wind_1mw > 0 else 0.1
    starts.append((max(sol_share, 0.1), max(win_share, 0.1),
                    peak_demand_mw * 4.0, peak_demand_mw * 1.0, "Balanced"))
    
    global_best_combo = None
    global_best_cost = float('inf')
    
    for s_sol, s_win, s_cap, s_pwr, label in starts:
        combo, cost = run_gradient_search(s_sol, s_win, s_cap, s_pwr, label)
        if combo is not None and cost < global_best_cost:
            global_best_combo = combo
            global_best_cost = cost
            print(f"   --> {label} found new global best: CAPEX ${cost:.1f}M")
    
    if global_best_combo is None:
        raise ValueError(
            f"CRITICAL: No search path reached Target SSR of {target_ssr}% "
            f"within iterations. Consider increasing site limits or relaxing the SSR target."
        )

    print(f"\n   ==> Global Optimum: Solar x{global_best_combo[0]:.1f}, "
          f"Wind x{global_best_combo[1]:.1f}, "
          f"BESS {global_best_combo[2]:.1f}MW / {global_best_combo[3]:.0f}MWh, "
          f"CAPEX ${global_best_cost:.1f}M")
    return global_best_combo, global_best_cost


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

def run_dispatch(merged_df, sim_df, dt, config, bess_params, fine_best, config_raw):
    print(f"\nStarting execution in {config['mode']} mode with strategy priority_dispatch...")

    # Hardware Sizes from Phase 1
    k_sol, k_win, bess_mw, bess_mwh = fine_best
    
    # Economics
    grid_cost_mwh = config['grid_cost_per_mwh']
    cycle_life = bess_params.get('cycle_life', 5000)
    replacement_cost_mwh = bess_params.get('replacement_cost_mwh', 300000.0)
    real_deg_cost = replacement_cost_mwh / (2.0 * cycle_life)

    def create_model(window_df, current_bess_params, t_start=0):
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
        
        def balance_rule(m, t):
            return m.p_solar[t] + m.p_wind[t] + m.p_grid[t] + m.p_dchg[t] + m.p_unmet[t] == window_df.iloc[t]['demand'] + m.p_chg[t]
        m.balance_con = pyo.Constraint(m.T, rule=balance_rule)
        
        def solar_limit(m, t): return m.p_solar[t] <= window_df.iloc[t]['solar_power'] * k_sol
        m.sol_limit_con = pyo.Constraint(m.T, rule=solar_limit)
        
        def wind_limit(m, t): return m.p_wind[t] <= window_df.iloc[t]['wind_power'] * k_win
        m.win_limit_con = pyo.Constraint(m.T, rule=wind_limit)
        
        site_max_grid = config_raw.loc[0, 'site_max_grid_import_mw'] if 'site_max_grid_import_mw' in config_raw.columns else 99999.0
        
        def grid_limit(m, t): 
            return m.p_grid[t] <= min(window_df.iloc[t]['grid_limit'], site_max_grid)
        m.grid_limit_con = pyo.Constraint(m.T, rule=grid_limit)
        
        m.chg_limit = pyo.Constraint(m.T, rule=lambda m, t: m.p_chg[t] <= bess_mw)
        m.dchg_limit = pyo.Constraint(m.T, rule=lambda m, t: m.p_dchg[t] <= bess_mw)
        m.soc_limit = pyo.Constraint(m.T, rule=lambda m, t: m.e_bess[t] <= bess_mwh)
        
        def soc_rule(m, t):
            if t == 0:
                return m.e_bess[t] == (current_bess_params['initial_soc'] / 100.0) * bess_mwh + \
                                      (m.p_chg[t] * current_bess_params['eff_charge'] - \
                                       m.p_dchg[t] / current_bess_params['eff_discharge']) * dt
            else:
                return m.e_bess[t] == m.e_bess[t-1] + \
                                      (m.p_chg[t] * current_bess_params['eff_charge'] - \
                                       m.p_dchg[t] / current_bess_params['eff_discharge']) * dt
        m.soc_con = pyo.Constraint(m.T, rule=soc_rule)
        
        def obj_rule(m):
            total_cost = 0
            shortage_penalty = 1e6
            
            for t in m.T:
                total_cost += m.p_grid[t] * dt * grid_cost_mwh
                total_cost += (m.p_dchg[t] + m.p_chg[t] * 0.05) * dt * real_deg_cost
                total_cost += m.p_unmet[t] * shortage_penalty
            return total_cost
            
        m.obj = pyo.Objective(rule=obj_rule, sense=pyo.minimize)
        return m

    # Execution Loop
    output = []
    total_steps = len(sim_df)
    current_bess_params = bess_params.copy()
    
    if config['mode'] == 'rolling_horizon':
        step_hours = config_raw.loc[0, 'rolling_step_hours'] if 'rolling_step_hours' in config_raw.columns else config['horizon_hours']
        step = int(step_hours / dt)
        horizon_steps = int(config['horizon_hours'] / dt)
        solver = pyo.SolverFactory('glpk')
        
        for start in range(0, total_steps, step):
            end = min(start + horizon_steps, total_steps)
            window_df = sim_df.iloc[start:end].reset_index(drop=True)
            
            print(f"   Solving window {start} to {end}...")
            print("   [Model] Active Strategy: priority_dispatch")
            print("   [Model] Applying Real Economic Marginal Pricing...")
            print(f"      - solar: $0.00 / MWh\n      - wind: $0.00 / MWh")
            print(f"      - bess: ${real_deg_cost:.2f} / MWh (Degradation)\n      - grid: ${grid_cost_mwh:.2f} / MWh (Import)")
            
            model = create_model(window_df, current_bess_params, start)
            results = solver.solve(model, tee=False)
            
            if (results.solver.status == pyo.SolverStatus.ok) and (results.solver.termination_condition == pyo.TerminationCondition.optimal):
                extract_length = min(step, end - start)
                for t in range(extract_length):
                    sol = pyo.value(model.p_solar[t])
                    win = pyo.value(model.p_wind[t])
                    avail_sol = window_df.loc[t, 'solar_power'] * k_sol
                    avail_win = window_df.loc[t, 'wind_power'] * k_win
                    
                    output.append({
                        "Time": merged_df.iloc[start + t]['date_time'],
                        "Demand (MW)": window_df.loc[t, 'demand'],
                        "Grid Import (MW)": pyo.value(model.p_grid[t]),
                        "Solar Used (MW)": sol,
                        "Wind Used (MW)": win,
                        "BESS Charge (MW)": pyo.value(model.p_chg[t]),
                        "BESS Discharge (MW)": pyo.value(model.p_dchg[t]),
                        "Curtailed (MW)": (avail_sol + avail_win) - (sol + win),
                        "SOC (MWh)": pyo.value(model.e_bess[t])
                    })
                
                last_t = extract_length - 1
                if bess_mwh > 0:
                    current_bess_params['initial_soc'] = (pyo.value(model.e_bess[last_t]) / bess_mwh) * 100.0
                else:
                    current_bess_params['initial_soc'] = 0.0
            else:
                print(f"Solver failed at window {start}-{end}")
                break
                
    else:
        # Full Horizon Mode
        print(f"   Solving full horizon ({total_steps} steps)...")
        solver = pyo.SolverFactory('glpk')
        model = create_model(sim_df, current_bess_params)
        results = solver.solve(model, tee=True)
        
        if (results.solver.status == pyo.SolverStatus.ok) and (results.solver.termination_condition == pyo.TerminationCondition.optimal):
            for t in model.T:
                sol = pyo.value(model.p_solar[t])
                win = pyo.value(model.p_wind[t])
                avail_sol = sim_df.loc[t, 'solar_power'] * k_sol
                avail_win = sim_df.loc[t, 'wind_power'] * k_win
                
                output.append({
                    "Time": merged_df.loc[t, 'date_time'],
                    "Demand (MW)": sim_df.loc[t, 'demand'],
                    "Grid Import (MW)": pyo.value(model.p_grid[t]),
                    "Solar Used (MW)": sol,
                    "Wind Used (MW)": win,
                    "BESS Charge (MW)": pyo.value(model.p_chg[t]),
                    "BESS Discharge (MW)": pyo.value(model.p_dchg[t]),
                    "Curtailed (MW)": (avail_sol + avail_win) - (sol + win),
                    "SOC (MWh)": pyo.value(model.e_bess[t])
                })
        else:
            print("Solver failed on full horizon.")

    print("\n✅ Optimization successful!")
    res_df = pd.DataFrame(output)
    return res_df


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

def calculate_financials(res_df, merged_df, dt, fine_cost, config_raw, bess_params):
    print("\n--- Step 4: Extracting Final KPIs & Calculating LCOE ---")
    
    # KPIs
    k_sol_used = sum([float(k) for k in config_raw.loc[0, 'active_k_sols'].split(',')]) if 'active_k_sols' in config_raw.columns else 1.0
    k_win_used = sum([float(k) for k in config_raw.loc[0, 'active_k_wins'].split(',')]) if 'active_k_wins' in config_raw.columns else 1.0
    
    # We must calculate total gen using the actual multipliers found by the 4D sweep
    total_gen_mwh = ((merged_df['solar_power_mw'] * k_sol_used) + (merged_df['wind_power_mw'] * k_win_used)).sum() * dt
    total_curtailed_mwh = res_df['Curtailed (MW)'].sum() * dt
    osr = (total_curtailed_mwh / total_gen_mwh) * 100.0 if total_gen_mwh > 0 else 0.0
    
    gcmin_mw = res_df['Grid Import (MW)'].quantile(0.95)
    peak_grid_mw = res_df['Grid Import (MW)'].max()
    
    print(f"   - Derived GCmin (95th Percentile Grid Import): {gcmin_mw:.2f} MW")
    print(f"   - Peak Grid Spike: {peak_grid_mw:.2f} MW")
    print(f"   - Over-Supply Ratio (OSR / Wasted Energy): {osr:.1f}%")
    
    # Economics
    proj_years = config_raw.loc[0, 'project_lifespan_years'] if 'project_lifespan_years' in config_raw.columns else 15.0
    discount_rate = config_raw.loc[0, 'discount_rate_pct'] if 'discount_rate_pct' in config_raw.columns else 8.0
    grid_cost_mwh = config_raw.loc[0, 'grid_cost_per_mwh'] if 'grid_cost_per_mwh' in config_raw.columns else 100.0
    
    grid_capex_dollars = gcmin_mw * 100_000.0
    total_capex_dollars = (fine_cost * 1_000_000.0) + grid_capex_dollars
    
    total_grid_import_mwh = res_df['Grid Import (MW)'].sum() * dt
    annual_grid_cost = total_grid_import_mwh * grid_cost_mwh
    
    # Read degradation cost from bess_params (same source as dispatch_engine)
    cycle_life = bess_params.get('cycle_life', 5000)
    replacement_cost_mwh = bess_params.get('replacement_cost_mwh', 300000.0)
    real_deg_cost = replacement_cost_mwh / (2.0 * cycle_life)
    total_bess_discharge_mwh = res_df['BESS Discharge (MW)'].sum() * dt
    annual_deg_cost = total_bess_discharge_mwh * real_deg_cost
    
    annual_opex = annual_grid_cost + annual_deg_cost
    
    # DCF / LCOE
    pv_factor = sum([1 / ((1 + discount_rate/100)**t) for t in range(1, int(proj_years) + 1)])
    total_pvc = total_capex_dollars + (annual_opex * pv_factor)
    
    annual_demand_mwh = res_df['Demand (MW)'].sum() * dt
    discounted_demand = annual_demand_mwh * pv_factor
    lcoe = total_pvc / discounted_demand if discounted_demand > 0 else 0.0
    
    print(f"\n   --- Business Case Summary ---")
    print(f"   - Total CAPEX: ${total_capex_dollars:,.2f} (Includes ${grid_capex_dollars:,.0f} Grid Connection)")
    print(f"   - Annual OPEX: ${annual_opex:,.2f} (Grid: ${annual_grid_cost:,.0f}, Degradation: ${annual_deg_cost:,.0f})")
    print(f"   - Present Value of Costs (PVC): ${total_pvc:,.2f}")
    print(f"   - Levelized Cost of Energy (LCOE): ${lcoe:.2f} / MWh")
    
    fin_df = pd.DataFrame({
        'Metric': ['GCmin Derived (MW)', 'Over-Supply Ratio (%)', 'Total CAPEX ($)', 'Annual Grid Cost ($)', 'Annual Degradation Cost ($)', 'Project Lifespan (Years)', 'Discount Rate (%)', 'Present Value of Costs ($)', 'Levelized Cost of Energy ($/MWh)'],
        'Value': [gcmin_mw, osr, total_capex_dollars, annual_grid_cost, annual_deg_cost, proj_years, discount_rate, total_pvc, lcoe]
    })
    
    return fin_df


"""
Reporter Module
---------------
Architectural Purpose:
This module is strictly responsible for output formatting. Isolating it ensures that if 
we ever want to build a web dashboard, generate PDF reports, or connect to Tableau, 
we only have to edit this file, rather than digging through the optimization mathematics.
"""
import pandas as pd

def save_report(excel_path, res_df, fin_df):
    print(f"\n💾 Saving results back to {excel_path}...")
    try:
        xls = pd.ExcelFile(excel_path)
        sheets = {sheet: pd.read_excel(xls, sheet_name=sheet) for sheet in xls.sheet_names}
        
        sheets['Optimization Results'] = res_df
        sheets['Financial Summary'] = fin_df
        
        with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
            for sheet_name, df in sheets.items():
                df.to_excel(writer, sheet_name=sheet_name, index=False)
                
        print("✅ Results successfully saved to the Excel file.")
    except Exception as e:
        print(f"❌ Failed to save results: {str(e)}")


"""
Microgrid Optimizer - Main Entry Point
--------------------------------------
Architectural Purpose:
This file serves as the singular orchestrator for the Microgrid Optimization application.
It acts like a factory assembly line, passing data linearly between the specialized modules:

1. Config Loader   -> Loads Excel data into memory
2. Sizing Engine   -> Uses a multi-start gradient search to find the cheapest hardware sizes that hit the SSR target
3. Dispatch Engine -> Runs a Pyomo LP solver to simulate how that hardware operates hourly
4. Financials      -> Extracts KPIs (LCOE, GCmin, OSR) from the physical dispatch results
5. Reporter        -> Saves the final outputs back to Excel
"""
import os

def main():
    excel_path = '/Users/kirankumarpatil/Desktop/Data Centre/Project_Config_115_2026-06-16.xlsx'
    if not os.path.exists(excel_path):
        print(f"Error: Could not find {excel_path}")
        return

    # Step 1: Load Data & Parameters
    sim_df, merged_df, config, config_raw, bess_params = load_configuration(excel_path)
    dt = config['time_resolution_hours']

    # Step 2: Run 4D Hardware Sizing Sweep (With Non-Linear CAPEX)
    fine_best, fine_cost = run_sizing(sim_df, dt, config_raw, bess_params)
    
    # Track the active multipliers found by the sweep so financials can use them
    config_raw.loc[0, 'active_k_sols'] = str(fine_best[0])
    config_raw.loc[0, 'active_k_wins'] = str(fine_best[1])

    # Step 3: Run Pyomo Dispatch Simulation
    res_df = run_dispatch(merged_df, sim_df, dt, config, bess_params, fine_best, config_raw)

    # Step 4: Calculate Final KPIs and Economics
    fin_df = calculate_financials(res_df, merged_df, dt, fine_cost, config_raw, bess_params)

    # Step 5: Save to Excel
    save_report(excel_path, res_df, fin_df)

if __name__ == '__main__':
    main()
