"""
Schema Module
-------------
Column name constants for all DataFrames used in the pipeline.

Phase 1 adds CurveCols for sizing curve output.
All existing SimCols / ResultCols / KpiKeys are kept unchanged
for backward compatibility with the Phase 2 dispatch pipeline.
"""


# ──────────────────────────────────────────────────────────────────────────────
# Phase 2 — Dispatch simulation column names (unchanged)
# ──────────────────────────────────────────────────────────────────────────────

class SimCols:
    GRID_PRICE   = "grid_price"
    SOLAR_PRICE  = "solar_price"
    WIND_PRICE   = "wind_price"
    GRID_LIMIT   = "grid_limit"
    SOLAR_POWER  = "solar_power"
    WIND_POWER   = "wind_power"
    DEMAND       = "demand"


class ResultCols:
    SCENARIO        = "Scenario"
    TIME            = "Time"
    DEMAND          = "Demand (MW)"
    GRID_IMPORT     = "Grid Import (MW)"
    SOLAR_USED      = "Solar Used (MW)"
    WIND_USED       = "Wind Used (MW)"
    BESS_CHARGE     = "BESS Charge (MW)"
    BESS_DISCHARGE  = "BESS Discharge (MW)"
    GRID_EXPORT     = "Grid Export (MW)"
    UNMET_LOAD      = "Unmet Load (MW)"
    CURTAILED       = "Curtailed (MW)"
    SOC             = "SOC (MWh)"
    BTM_REGIME      = "BTM Regime"


class KpiKeys:
    SSR               = "SSR (%)"
    SCR               = "SCR (%)"
    OSR               = "OSR / Curtailment (%)"
    GCMIN_P95         = "GCmin P95 (MW)"
    GCMIN_PEAK        = "GCmin Peak (MW)"
    TOTAL_DEMAND      = "Total Demand (MWh)"
    TOTAL_GRID_IMPORT = "Total Grid Import (MWh)"
    TOTAL_GRID_EXPORT = "Total Grid Export (MWh)"
    TOTAL_UNMET_LOAD  = "Total Unmet Load (MWh)"
    SERVED_LOAD       = "Served Load (MWh)"
    RELIABILITY       = "Reliability (%)"


# ──────────────────────────────────────────────────────────────────────────────
# Phase 1 — Sizing curve column names
# ──────────────────────────────────────────────────────────────────────────────

class CurveCols:
    """Column names for the Phase 1 sizing curve DataFrames."""
    SCENARIO         = "Scenario"

    # Target that was requested for this LP solve
    TARGET_TYPE      = "Target Type"          # "ssr" | "peak_shaving"
    TARGET_SSR_PCT   = "Target SSR (%)"
    TARGET_GC_MW     = "Target Grid Connection (MW)"

    # Sized hardware
    PV_MW            = "PV Nameplate (MW)"
    BESS_MW          = "BESS Power (MW)"
    BESS_MWH         = "BESS Energy (MWh)"
    BESS_DURATION_H  = "BESS Duration (h)"

    # Results verified from LP solution
    PEAK_GC_MW       = "Achieved Peak Grid Import (MW)"
    PEAK_EXPORT_MW   = "Achieved Peak Grid Export (MW)"
    ACHIEVED_SSR_PCT = "Achieved SSR (%)"
    ACHIEVED_SCR_PCT = "Achieved SCR (%)"
    EXPORTED_MWH     = "Total Exported (MWh)"
    FEASIBLE         = "Feasible"

    # Phase 2 overlay (added later — not populated by Phase 1)
    CAPEX_M          = "CAPEX (€M)"
    OPEX_M_YR        = "Annual OPEX (€M/yr)"
    NPV_M            = "NPV (€M)"


# ──────────────────────────────────────────────────────────────────────────────
# Required column sets (used by require_columns() validation)
# ──────────────────────────────────────────────────────────────────────────────

SIM_REQUIRED_COLUMNS = (
    SimCols.GRID_PRICE,
    SimCols.SOLAR_PRICE,
    SimCols.WIND_PRICE,
    SimCols.GRID_LIMIT,
    SimCols.SOLAR_POWER,
    SimCols.WIND_POWER,
    SimCols.DEMAND,
)

RESULT_REQUIRED_COLUMNS = (
    ResultCols.TIME,
    ResultCols.DEMAND,
    ResultCols.GRID_IMPORT,
    ResultCols.SOLAR_USED,
    ResultCols.WIND_USED,
    ResultCols.BESS_CHARGE,
    ResultCols.BESS_DISCHARGE,
    ResultCols.UNMET_LOAD,
    ResultCols.CURTAILED,
    ResultCols.SOC,
    ResultCols.BTM_REGIME,
)

CURVE_REQUIRED_COLUMNS = (
    CurveCols.SCENARIO,
    CurveCols.TARGET_TYPE,
    CurveCols.PV_MW,
    CurveCols.BESS_MW,
    CurveCols.BESS_MWH,
    CurveCols.PEAK_GC_MW,
    CurveCols.ACHIEVED_SSR_PCT,
    CurveCols.FEASIBLE,
)


# ──────────────────────────────────────────────────────────────────────────────
# Validation helper
# ──────────────────────────────────────────────────────────────────────────────

def require_columns(df, required_columns, label):
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        missing_text = ", ".join(missing)
        raise ValueError(f"{label} is missing required columns: {missing_text}")
