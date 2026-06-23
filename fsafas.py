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
        max_iter = 1000
        
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
