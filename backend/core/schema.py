"""
Schema Module
-------------
Column name constants for all DataFrames used in the pipeline.

Phase 1 adds CurveCols for sizing curve output.
All existing SimCols / ResultCols / KpiKeys are kept unchanged
for backward compatibility with the Phase 2 dispatch pipeline.
"""


# ──────────────────────────────────────────────────────────────────────────────
# LEGACY column names — used ONLY by archive/legacy_pipeline (the superseded
# Phase-2 full pipeline). The live engine uses CurveCols + FlowCols + KpiKeys
# below. Kept here so the archived modules still import; safe to delete once the
# archive is dropped.
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
    WIND_MW          = "Wind Nameplate (MW)"
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

    # Operational verification under the causal rule (Model R). The columns above
    # are the perfect-foresight LP (Model O, lower bound); these are what the
    # auditable controller actually achieves, plus the gap between them.
    OP_SSR_PCT       = "Operational SSR (%)"
    OP_SCR_PCT       = "Operational SCR (%)"
    OP_PEAK_GC_MW    = "Operational Peak Grid (MW)"
    OP_GRID_MWH      = "Operational Grid Import (MWh)"
    OP_UNMET_MWH     = "Operational Unmet (MWh)"
    OP_SSR_GAP_PP    = "SSR Gap O−R (pp)"

    # Deliverable size (Model R). The BESS_MW/MWh columns above are the LP lower
    # bound (Model O) — the smallest battery that COULD hit the target with perfect
    # foresight. These are the smallest battery that ACTUALLY hits the target under
    # the causal contract dispatch, at the same E/P duration. This is the number to
    # buy. R_FEASIBLE is False when the target is unreachable under the rule even at
    # the site's maximum BESS.
    R_BESS_MW        = "Deliverable BESS Power (MW · Model R)"
    R_BESS_MWH       = "Deliverable BESS Energy (MWh · Model R)"
    R_FEASIBLE       = "Target Met Under Rule"

    # End-of-life sizing. The deliverable size grossed up so the target still holds
    # at ~20yr after capacity fade (spec: "oversize day-one so target met at EoL").
    # This is the day-one nameplate to install. EOL_CAPPED flags when the gross-up
    # exceeds the site's physical BESS limit.
    EOL_BESS_MW      = "EoL-Sized BESS Power (MW)"
    EOL_BESS_MWH     = "EoL-Sized BESS Energy (MWh)"
    EOL_CAPPED       = "EoL Size Capped by Site Limit"

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
# Canonical FlowsFrame — the SINGLE energy-flows table every dispatch path emits
# ──────────────────────────────────────────────────────────────────────────────
#
# Both Model O (LP read-back) and Model R (causal rule) must produce a DataFrame
# with exactly these columns. All KPIs (SSR/SCR/GCmin/OSR) are computed from this
# table and nowhere else — see optimizer/rule_dispatch.compute_flow_kpis().
# Every column is power in MW at each timestep, except SOC which is energy in MWh.

class FlowCols:
    TIMESTAMP   = "timestamp"
    LOAD_MW     = "load_mw"          # consumer demand
    PV_AVAIL_MW = "pv_avail_mw"      # generation available (nameplate × p.u.)
    PV_USED_MW  = "pv_used_mw"       # generation used on-site (to load or BESS)
    GRID_IMP_MW = "grid_import_mw"   # import from grid
    CHARGE_MW   = "bess_charge_mw"   # power into the BESS
    DISCHARGE_MW= "bess_discharge_mw"# power out of the BESS
    SOC_MWH     = "bess_soc_mwh"     # state of charge (energy)
    CURTAIL_MW  = "curtailed_mw"     # generation spilled
    UNMET_MW    = "unmet_mw"         # load shed (not served)
    EXPORT_MW   = "export_mw"        # export to grid (0 for BTM import-only)


FLOW_REQUIRED_COLUMNS = (
    FlowCols.TIMESTAMP,
    FlowCols.LOAD_MW,
    FlowCols.PV_AVAIL_MW,
    FlowCols.PV_USED_MW,
    FlowCols.GRID_IMP_MW,
    FlowCols.CHARGE_MW,
    FlowCols.DISCHARGE_MW,
    FlowCols.SOC_MWH,
    FlowCols.CURTAIL_MW,
    FlowCols.UNMET_MW,
    FlowCols.EXPORT_MW,
)


# ──────────────────────────────────────────────────────────────────────────────
# Validation helper
# ──────────────────────────────────────────────────────────────────────────────

def require_columns(df, required_columns, label):
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        missing_text = ", ".join(missing)
        raise ValueError(f"{label} is missing required columns: {missing_text}")
