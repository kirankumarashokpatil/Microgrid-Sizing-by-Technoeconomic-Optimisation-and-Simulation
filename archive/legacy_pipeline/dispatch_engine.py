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
from optimizer.params import SizingResult, PhysicalParams
from optimizer.schema import RESULT_REQUIRED_COLUMNS, SIM_REQUIRED_COLUMNS, ResultCols, SimCols, require_columns
from optimizer.solver import create_highs_solver

def run_dispatch_simulation(
    profiles_df: pd.DataFrame,
    sizing_result: SizingResult,
    phys_params: PhysicalParams,
    horizon_hours: int = 48,
    overlap_hours: int = 24,
) -> pd.DataFrame:
    """
    Phase 2 Operational Verification:
    Runs the rolling-horizon dispatch using the sized capacities.
    """
    print(f"\n   [dispatch] Simulating 8760h with {horizon_hours}h horizon (rolling every {horizon_hours-overlap_hours}h)...")
    
    dt = phys_params.dt_hours
    total_steps = len(profiles_df)
    step_size = int((horizon_hours - overlap_hours) / dt)
    horizon_steps = int(horizon_hours / dt)
    
    solver = create_highs_solver()
    output = []
    
    # We use a purely penalised objective (no real prices needed just for physical verification)
    # Goal: minimise unmet load and grid over-limit, then minimise general grid use, and battery cycling
    
    current_soc_mwh = sizing_result.bess_mwh * (phys_params.initial_soc_pct / 100.0)
    
    def create_model(window_df, init_soc):
        m = pyo.ConcreteModel()
        m.T = pyo.RangeSet(0, len(window_df) - 1)
        
        m.p_pv = pyo.Var(m.T, within=pyo.NonNegativeReals)
        m.p_grid = pyo.Var(m.T, within=pyo.NonNegativeReals)
        m.p_chg = pyo.Var(m.T, within=pyo.NonNegativeReals)
        m.p_dchg = pyo.Var(m.T, within=pyo.NonNegativeReals)
        m.p_curt = pyo.Var(m.T, within=pyo.NonNegativeReals)
        m.p_unmet = pyo.Var(m.T, within=pyo.NonNegativeReals)
        m.p_export = pyo.Var(m.T, within=pyo.NonNegativeReals)
        m.e_bess = pyo.Var(m.T, within=pyo.NonNegativeReals)

        # Power balance — MUST match the sizing LP (optimizer/sizing_engine._build_lp):
        #   pv_used + grid + discharge + unmet == load + charge + export
        # Putting charge on the demand side (not export) lets surplus PV charge the
        # battery, which the previous "no_export" form silently forbade.
        def balance_rule(m, t):
            return (m.p_pv[t] + m.p_grid[t] + m.p_dchg[t] + m.p_unmet[t] ==
                    window_df['load_mw'].iloc[t] + m.p_chg[t] + m.p_export[t])
        m.balance = pyo.Constraint(m.T, rule=balance_rule)

        # PV availability + explicit curtailment accounting (matches sizing LP):
        # everything generated is either used or curtailed.
        def pv_avail_rule(m, t):
            return m.p_pv[t] + m.p_curt[t] == window_df['pv_pu'].iloc[t] * sizing_result.pv_mw
        m.pv_avail = pyo.Constraint(m.T, rule=pv_avail_rule)

        # BTM, import-only: no export to the grid (export ceiling from params).
        m.export_lim = pyo.Constraint(m.T, rule=lambda m, t: m.p_export[t] <= max(0.0, phys_params.export_limit_mw))
        m.grid_lim = pyo.Constraint(m.T, rule=lambda m, t: m.p_grid[t] <= sizing_result.peak_grid_mw)

        if sizing_result.bess_mw > 0.001:
            m.chg_lim = pyo.Constraint(m.T, rule=lambda m, t: m.p_chg[t] <= sizing_result.bess_mw)
            m.dchg_lim = pyo.Constraint(m.T, rule=lambda m, t: m.p_dchg[t] <= sizing_result.bess_mw)
            m.soc_max = pyo.Constraint(m.T, rule=lambda m, t: m.e_bess[t] <= sizing_result.bess_mwh * (phys_params.max_soc_pct / 100.0))
            m.soc_min = pyo.Constraint(m.T, rule=lambda m, t: m.e_bess[t] >= sizing_result.bess_mwh * (phys_params.min_soc_pct / 100.0))
            
            def soc_rule(m, t):
                last_e = init_soc if t == 0 else m.e_bess[t-1]
                return m.e_bess[t] == last_e + (m.p_chg[t] * phys_params.eff_charge - m.p_dchg[t] / phys_params.eff_discharge) * dt
            m.soc_update = pyo.Constraint(m.T, rule=soc_rule)
        else:
            m.chg_lim = pyo.Constraint(m.T, rule=lambda m, t: m.p_chg[t] == 0)
            m.dchg_lim = pyo.Constraint(m.T, rule=lambda m, t: m.p_dchg[t] == 0)
            m.soc_zero = pyo.Constraint(m.T, rule=lambda m, t: m.e_bess[t] == 0)

        def obj_rule(m):
            # Penalty weights
            return sum(
                m.p_unmet[t] * 1e6 +       # 1. Never drop load
                m.p_grid[t] * 100 +        # 2. Minimise grid
                m.p_dchg[t] * 10 -         # 3. Minimise battery cycling (use PV first)
                m.e_bess[t] * 0.01         # 4. Anti-procrastination (charge early)
                for t in m.T
            ) - m.e_bess[m.T.last()] * 50  # 5. Terminal SOC value
        m.obj = pyo.Objective(rule=obj_rule, sense=pyo.minimize)
        return m

    for start in range(0, total_steps, step_size):
        end = min(start + horizon_steps, total_steps)
        window = profiles_df.iloc[start:end].reset_index(drop=True)
        
        m = create_model(window, current_soc_mwh)
        res = solver.solve(m, tee=False)
        
        if res.solver.status != pyo.SolverStatus.ok or res.solver.termination_condition != pyo.TerminationCondition.optimal:
            print(f"   [dispatch] WARNING: Solver failed at step {start}. Filling with empty.")
            break
            
        # Extract only the non-overlapping portion
        extract_len = min(step_size, end - start)
        for t in range(extract_len):
            row = {
                "timestamp": window["timestamp"].iloc[t],
                "load_mw": window["load_mw"].iloc[t],
                "pv_avail_mw": window["pv_pu"].iloc[t] * sizing_result.pv_mw,
                "pv_used_mw": pyo.value(m.p_pv[t]),
                "grid_import_mw": pyo.value(m.p_grid[t]),
                "bess_charge_mw": pyo.value(m.p_chg[t]),
                "bess_discharge_mw": pyo.value(m.p_dchg[t]),
                "bess_soc_mwh": pyo.value(m.e_bess[t]),
                "curtailed_mw": pyo.value(m.p_curt[t]),
                "unmet_load_mw": pyo.value(m.p_unmet[t]),
            }
            output.append(row)
            
        last_t = extract_len - 1
        current_soc_mwh = pyo.value(m.e_bess[last_t])

    df_out = pd.DataFrame(output)

    # ── Verification KPIs from the realistic rolling-horizon dispatch ─────────
    # The sizing LP runs under perfect foresight, so dispatch is a best-effort
    # operational check, not an exact reproduction. We report SSR and SCR (both
    # first-class per the CEO brief) and verify the objective that actually drove
    # the sizing: SSR for an SSR design, grid-limit compliance for peak shaving.
    if not df_out.empty:
        tot_load = df_out["load_mw"].sum() * dt
        tot_grid = df_out["grid_import_mw"].sum() * dt
        tot_unmet = df_out["unmet_load_mw"].sum() * dt
        tot_pv_avail = df_out["pv_avail_mw"].sum() * dt
        tot_pv_used = df_out["pv_used_mw"].sum() * dt
        peak_grid = df_out["grid_import_mw"].max()

        achieved_ssr = (1 - (tot_grid + tot_unmet) / tot_load) * 100 if tot_load > 0 else 0.0
        achieved_scr = (tot_pv_used / tot_pv_avail) * 100 if tot_pv_avail > 1e-9 else 0.0

        print(f"   [dispatch] Verification complete (rolling horizon, no foresight):")
        print(f"      SSR = {achieved_ssr:5.1f}%   |   SCR = {achieved_scr:5.1f}%   |   "
              f"peak grid = {peak_grid:.1f} MW   |   unmet = {tot_unmet:.1f} MWh")

        if sizing_result.target_type == "peak_shaving":
            limit = sizing_result.peak_grid_mw
            ok = peak_grid <= limit + 1e-3
            verdict = "✓ holds" if ok else "✗ BREACHED"
            print(f"      Peak-shaving check: peak grid {peak_grid:.1f} MW vs limit "
                  f"{limit:.1f} MW  →  {verdict}")
        else:
            tgt = sizing_result.target_value if sizing_result.target_type == "ssr" else sizing_result.achieved_ssr_pct
            ok = achieved_ssr >= tgt - 1.0
            verdict = "✓ meets target" if ok else "✗ below target"
            print(f"      SSR check: achieved {achieved_ssr:.1f}% vs target {tgt:.1f}%  →  {verdict}")

    return df_out


def run_dispatch(merged_df, sim_df, dt, params, sizing_result):

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
