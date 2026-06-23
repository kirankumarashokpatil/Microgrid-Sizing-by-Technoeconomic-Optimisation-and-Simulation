"""
Sizing Engine Module
--------------------
Architectural Purpose: 
This module handles 'Phase 1: Hardware Sizing'. It uses a Pyomo Linear Programming (LP)
formulation to find the cheapest hardware capacities (Solar, Wind, Battery MW, Battery MWh)
that meet the target Self-Sufficiency Ratio (SSR) over the simulation horizon.
"""
import pyomo.environ as pyo
from optimizer.params import ProjectParams, SizingResult
from optimizer.schema import SIM_REQUIRED_COLUMNS, SimCols, require_columns
from optimizer.solver import create_highs_solver

def run_sizing(sim_df, dt, params: ProjectParams):
    print("\n--- Step 1: Mathematical Hardware Sizing (Pyomo) ---")
    require_columns(sim_df, SIM_REQUIRED_COLUMNS, "Simulation data")
    
    total_demand_mwh = sim_df[SimCols.DEMAND].sum() * dt

    print(f"   Building Pyomo LP model over {len(sim_df)} timesteps...")
    
    m = pyo.ConcreteModel()
    horizon = len(sim_df)
    m.T = pyo.RangeSet(0, horizon - 1)
    
    # Decision Variables (Hardware Capacities)
    m.k_sol = pyo.Var(within=pyo.NonNegativeReals, bounds=(0, params.site_max_sol))
    m.k_win = pyo.Var(within=pyo.NonNegativeReals, bounds=(0, params.site_max_win))
    m.bess_mw = pyo.Var(within=pyo.NonNegativeReals, bounds=(0, params.site_max_bess_mw))
    m.bess_mwh = pyo.Var(within=pyo.NonNegativeReals, bounds=(0, params.site_max_bess_mwh))
    
    # Decision Variables (Time-Series Dispatch)
    m.p_solar = pyo.Var(m.T, within=pyo.NonNegativeReals)
    m.p_wind = pyo.Var(m.T, within=pyo.NonNegativeReals)
    m.p_grid = pyo.Var(m.T, within=pyo.NonNegativeReals)
    m.p_chg = pyo.Var(m.T, within=pyo.NonNegativeReals)
    m.p_dchg = pyo.Var(m.T, within=pyo.NonNegativeReals)
    m.p_unmet = pyo.Var(m.T, within=pyo.NonNegativeReals)
    m.peak_grid_mw = pyo.Var(within=pyo.NonNegativeReals)
    m.e_bess = pyo.Var(m.T, within=pyo.NonNegativeReals)
    
    # Bound unmet load by demand
    def unmet_limit(m, t):
        return m.p_unmet[t] <= sim_df.iloc[t][SimCols.DEMAND]
    m.unmet_limit_con = pyo.Constraint(m.T, rule=unmet_limit)

    # 1. Power Balance
    def balance_rule(m, t):
        return m.p_solar[t] + m.p_wind[t] + m.p_grid[t] + m.p_dchg[t] + m.p_unmet[t] == sim_df.iloc[t][SimCols.DEMAND] + m.p_chg[t]
    m.balance_con = pyo.Constraint(m.T, rule=balance_rule)
    
    # 2. Renewables Availability Limits
    def solar_limit(m, t): 
        return m.p_solar[t] <= sim_df.iloc[t][SimCols.SOLAR_POWER] * m.k_sol
    m.sol_limit_con = pyo.Constraint(m.T, rule=solar_limit)
    
    def wind_limit(m, t): 
        return m.p_wind[t] <= sim_df.iloc[t][SimCols.WIND_POWER] * m.k_win
    m.win_limit_con = pyo.Constraint(m.T, rule=wind_limit)
    
    # 3. Grid Limit
    def grid_limit(m, t): 
        return m.p_grid[t] <= min(sim_df.iloc[t][SimCols.GRID_LIMIT], params.site_max_grid_mw)
    m.grid_limit_con = pyo.Constraint(m.T, rule=grid_limit)
    
    def peak_grid_rule(m, t):
        return m.p_grid[t] <= m.peak_grid_mw
    m.peak_grid_con = pyo.Constraint(m.T, rule=peak_grid_rule)
    
    # 4. BESS Power & SOC Limits
    m.chg_limit = pyo.Constraint(m.T, rule=lambda m, t: m.p_chg[t] <= m.bess_mw)
    m.dchg_limit = pyo.Constraint(m.T, rule=lambda m, t: m.p_dchg[t] <= m.bess_mw)
    m.min_duration_limit = pyo.Constraint(rule=lambda m: m.bess_mwh >= m.bess_mw * params.min_bess_duration_hours)
    m.max_duration_limit = pyo.Constraint(rule=lambda m: m.bess_mwh <= m.bess_mw * params.max_bess_duration_hours)
    m.max_soc_limit = pyo.Constraint(m.T, rule=lambda m, t: m.e_bess[t] <= m.bess_mwh * (params.max_soc_pct / 100.0))
    m.min_soc_limit = pyo.Constraint(m.T, rule=lambda m, t: m.e_bess[t] >= m.bess_mwh * (params.min_soc_pct / 100.0))
    
    # 5. BESS State of Charge Physics
    def soc_rule(m, t):
        if t == 0:
            return m.e_bess[t] == (params.initial_soc_pct / 100.0 * m.bess_mwh) + \
                                  (m.p_chg[t] * params.eff_charge - m.p_dchg[t] / params.eff_discharge) * dt
        else:
            return m.e_bess[t] == m.e_bess[t-1] + \
                                  (m.p_chg[t] * params.eff_charge - m.p_dchg[t] / params.eff_discharge) * dt
    m.soc_con = pyo.Constraint(m.T, rule=soc_rule)
    last_t = m.T.last()
    m.terminal_soc_con = pyo.Constraint(
        expr=m.e_bess[last_t] >= params.initial_soc_pct / 100.0 * m.bess_mwh
    )
    
    # 6. Target SSR Constraint
    allowed_grid_mwh = total_demand_mwh * (1.0 - (params.target_ssr_pct / 100.0))
    def ssr_rule(m):
        return sum((m.p_grid[t] + m.p_unmet[t]) * dt for t in m.T) <= allowed_grid_mwh
    m.ssr_con = pyo.Constraint(rule=ssr_rule)
    
    # ---------------------------------------------------------
    # PHASE 1A: Global Optimum (Base Case)
    # ---------------------------------------------------------
    def linear_obj_rule(m):
        capex = (params.cost_solar_mw * m.k_sol) + \
                (params.cost_wind_mw * m.k_win) + \
                (params.cost_bess_mw * m.bess_mw) + \
                (params.cost_bess_mwh * m.bess_mwh)
        
        annual_grid_cost = sum(m.p_grid[t] * dt * params.grid_cost_mwh for t in m.T)
        annual_deg_cost = sum(m.p_dchg[t] * dt * params.real_deg_cost for t in m.T)
        annual_fixed_om = m.bess_mwh * params.fixed_opex_per_mwh_year

        grid_connection_cost = (m.peak_grid_mw * params.grid_connection_cost_mw)
        
        pv_opex = (annual_grid_cost + annual_deg_cost + annual_fixed_om) * params.pv_factor / 1_000_000.0
        pv_grid_connection = grid_connection_cost / 1_000_000.0
        
        # Dispatch Tie-breaker: Tiny penalty for unmet demand (blackouts).
        # Tiny reward for holding energy (e_bess) to force BESS to soak up free renewables early instead of curtailing.
        dispatch_penalty = sum(m.p_dchg[t] * 1e-6 - m.e_bess[t] * 1e-6 + (m.p_unmet[t] * 1e6) for t in m.T)
        
        return capex + pv_opex + pv_grid_connection + dispatch_penalty
        
    m.lin_obj = pyo.Objective(rule=linear_obj_rule, sense=pyo.minimize)
    
    print("   [Phase 1A] Solving Global Optimum with HiGHS...")
    solver_highs = create_highs_solver()
    res_highs = solver_highs.solve(m, tee=False)
    
    if (res_highs.solver.status != pyo.SolverStatus.ok) or (res_highs.solver.termination_condition != pyo.TerminationCondition.optimal):
        raise ValueError(f"CRITICAL: HiGHS Linear solve failed. Condition: {res_highs.solver.termination_condition}")
        
    optimal_obj_val = pyo.value(m.lin_obj)

    def extract_sizes(scenario_name):
        sol = max(0.0, pyo.value(m.k_sol))
        win = max(0.0, pyo.value(m.k_win))
        bess_mw = max(0.0, pyo.value(m.bess_mw))
        bess_mwh = max(0.0, pyo.value(m.bess_mwh))
        peak_grid_mw = max(0.0, pyo.value(m.peak_grid_mw))
        
        fine_cost = (params.cost_solar_mw * (sol ** params.scale_sol) if sol > 0 else 0) + \
                    (params.cost_wind_mw * (win ** params.scale_win) if win > 0 else 0) + \
                    (params.cost_bess_mw * (bess_mw ** params.scale_bess_mw) if bess_mw > 0 else 0) + \
                    (params.cost_bess_mwh * (bess_mwh ** params.scale_bess_mwh) if bess_mwh > 0 else 0)
        
        return SizingResult(scenario_name, sol, win, bess_mw, bess_mwh, peak_grid_mw, fine_cost)

    candidates = []
    candidates.append(extract_sizes("Lowest Cost"))
    print(f"      ✓ Base Optimum: Sol {candidates[0].solar_mw:.1f} MW, Win {candidates[0].wind_mw:.1f} MW, BESS {candidates[0].bess_mw:.1f} MW / {candidates[0].bess_mwh:.1f} MWh")

    # ---------------------------------------------------------
    # PHASE 1B: Modeling to Generate Alternatives (MGA)
    # ---------------------------------------------------------
    print("   [Phase 1B] Generating Alternative Scenarios (+10% Budget)...")
    
    m.lin_obj.deactivate()
    m.mga_budget_con = pyo.Constraint(rule=lambda m: linear_obj_rule(m) <= optimal_obj_val * 1.10)
    
    mga_scenarios = {}
    
    # Only generate Wind scenarios if wind is allowed and there is wind data
    if params.site_max_win > 0 and sim_df[SimCols.WIND_POWER].max() > 0:
        mga_scenarios["Wind-Led"] = (m.k_win, pyo.maximize)
        
    # Only generate Solar scenarios if solar is allowed and there is solar data
    if params.site_max_sol > 0 and sim_df[SimCols.SOLAR_POWER].max() > 0:
        mga_scenarios["Solar-Led"] = (m.k_sol, pyo.maximize)
        
    # Only generate Battery scenarios if batteries are allowed
    if params.site_max_bess_mwh > 0:
        mga_scenarios["Storage-Led"] = (m.bess_mwh, pyo.maximize)
    
    m.mga_obj = pyo.Objective(expr=0, sense=pyo.maximize)
    for name, (expr, sense) in mga_scenarios.items():
        m.mga_obj.expr = expr
        m.mga_obj.sense = sense
        try:
            res_mga = solver_highs.solve(m, tee=False)
        except RuntimeError as exc:
            print(f"      ✗ {name}: Solver failed or infeasible. {exc}")
            continue
        
        term_cond = str(res_mga.solver.termination_condition)
        valid_conditions = ['optimal', 'maxTimeLimit', 'maxIterations', 'TerminationCondition.optimal', 'TerminationCondition.maxTimeLimit', 'TerminationCondition.maxIterations']
        
        if any(vc in term_cond for vc in valid_conditions) or (len(res_mga.solution) > 0):
            res = extract_sizes(name)
            candidates.append(res)
            print(f"      ✓ {name}: Sol {res.solar_mw:.1f} MW, Win {res.wind_mw:.1f} MW, BESS {res.bess_mw:.1f} MW / {res.bess_mwh:.1f} MWh")
        else:
            print(f"      ✗ {name}: Solver failed or infeasible. Condition: {term_cond}")
            
    print("\n✅ Hardware Sizing MGA complete!")
    return candidates
