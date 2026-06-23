class SimCols:
    GRID_PRICE = "grid_price"
    SOLAR_PRICE = "solar_price"
    WIND_PRICE = "wind_price"
    GRID_LIMIT = "grid_limit"
    SOLAR_POWER = "solar_power"
    WIND_POWER = "wind_power"
    DEMAND = "demand"


class ResultCols:
    SCENARIO = "Scenario"
    TIME = "Time"
    DEMAND = "Demand (MW)"
    GRID_IMPORT = "Grid Import (MW)"
    SOLAR_USED = "Solar Used (MW)"
    WIND_USED = "Wind Used (MW)"
    BESS_CHARGE = "BESS Charge (MW)"
    BESS_DISCHARGE = "BESS Discharge (MW)"
    UNMET_LOAD = "Unmet Load (MW)"
    CURTAILED = "Curtailed (MW)"
    SOC = "SOC (MWh)"
    BTM_REGIME = "BTM Regime"


class KpiKeys:
    SSR = "SSR (%)"
    SCR = "SCR (%)"
    OSR = "OSR / Curtailment (%)"
    GCMIN_P95 = "GCmin P95 (MW)"
    GCMIN_PEAK = "GCmin Peak (MW)"
    TOTAL_DEMAND = "Total Demand (MWh)"
    TOTAL_GRID_IMPORT = "Total Grid Import (MWh)"
    TOTAL_UNMET_LOAD = "Total Unmet Load (MWh)"
    SERVED_LOAD = "Served Load (MWh)"
    RELIABILITY = "Reliability (%)"


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


def require_columns(df, required_columns, label):
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        missing_text = ", ".join(missing)
        raise ValueError(f"{label} is missing required columns: {missing_text}")
