import pandas as pd
from archive.legacy_pipeline.legacy_params import ProjectParams
from optimizer.schema import KpiKeys, RESULT_REQUIRED_COLUMNS, ResultCols, require_columns

def compute_kpis(res_df: pd.DataFrame, dt: float, params: ProjectParams) -> dict:
    """
    Primary outputs for investment committee and contract reporting.
    Every value is directly traceable to res_df columns.
    """
    require_columns(res_df, RESULT_REQUIRED_COLUMNS, "Dispatch results")

    total_demand = res_df[ResultCols.DEMAND].sum() * dt
    total_grid = res_df[ResultCols.GRID_IMPORT].sum() * dt
    total_unmet = res_df[ResultCols.UNMET_LOAD].sum() * dt
    total_solar_used = res_df[ResultCols.SOLAR_USED].sum() * dt
    total_wind_used = res_df[ResultCols.WIND_USED].sum() * dt
    total_curtailed = res_df[ResultCols.CURTAILED].sum() * dt
    
    total_gen_available = total_solar_used + total_wind_used + total_curtailed

    # SSR: verified from actual dispatch (not just the sizing constraint)
    actual_ssr = ((total_demand - total_grid - total_unmet) / total_demand * 100.0) if total_demand > 0 else 0.0
    served_load = total_demand - total_unmet
    reliability = (served_load / total_demand * 100.0) if total_demand > 0 else 0.0

    # SCR: what fraction of available generation was actually consumed (directly or via BESS)
    actual_scr = ((total_gen_available - total_curtailed) / total_gen_available * 100.0) if total_gen_available > 0 else 0.0

    # GCmin: what grid connection do we actually need
    gcmin_p95 = res_df[ResultCols.GRID_IMPORT].quantile(0.95)  # reference
    gcmin_peak = res_df[ResultCols.GRID_IMPORT].max()          # DSO sizing basis

    # OSR: wasted generation
    osr = (total_curtailed / total_gen_available * 100.0) if total_gen_available > 0 else 0.0

    return {
        KpiKeys.SSR: round(actual_ssr, 2),
        KpiKeys.SCR: round(actual_scr, 2),
        KpiKeys.OSR: round(osr, 2),
        KpiKeys.GCMIN_P95: round(gcmin_p95, 2),
        KpiKeys.GCMIN_PEAK: round(gcmin_peak, 2),
        KpiKeys.TOTAL_DEMAND: round(total_demand, 1),
        KpiKeys.TOTAL_GRID_IMPORT: round(total_grid, 1),
        KpiKeys.TOTAL_UNMET_LOAD: round(total_unmet, 1),
        KpiKeys.SERVED_LOAD: round(served_load, 1),
        KpiKeys.RELIABILITY: round(reliability, 2),
    }
