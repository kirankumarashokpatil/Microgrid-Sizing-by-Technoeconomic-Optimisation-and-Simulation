"""Hand-verifiable toy: a 12-timeslot profile to check the optimiser's battery
sizing across SSR 0->100% in 5% steps. Numbers are round so the result can be
checked by mental arithmetic:

  Demand     = 10 MW every hour            -> 120 MWh / 12 h
  Generation = 30 MW for 6 h, then 0 for 6 h -> 180 MWh   (gen/load ratio 1.5)

  No-battery SSR = 50%:
    day  (h0-5): gen 30, load 10 -> 10 served by gen, 20 curtailed, grid 0
    night(h6-11): gen 0,  load 10 -> 10 from grid = 60 MWh grid
    SSR = (120 - 60) / 120 = 50%

  Above 50% the battery stores day surplus and serves the night. To reach 100%
  the battery must deliver the whole 60 MWh night load, which (at 95% discharge
  efficiency) needs ~63 MWh of usable swing -> ~79 MWh nameplate at the 10-90%
  SOC band, ~11 MW power. The report lets you trace every hour.

Run from the repo root:
  python reports/toy_test/build.py
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import openpyxl  # noqa: E402
import pandas as pd  # noqa: E402

from reports.ssr_curve.build import build as build_ssr  # noqa: E402

HOURS  = 12
DEMAND = [10] * 12                                  # MW, flat
SOLAR  = [30, 30, 30, 30, 30, 30, 0, 0, 0, 0, 0, 0]  # MW, 6h on / 6h off
WIND   = [0] * 12


def make_input(path: Path) -> None:
    base = dt.datetime(2025, 1, 1)
    df = pd.DataFrame({
        "date_time": [(base + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S") for h in range(HOURS)],
        "solar_power_mw": SOLAR,
        "wind_power_mw": WIND,
        "Data center MW": DEMAND,
    })
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Energy Timeseries", index=False)


def run_case(inp: Path, here: Path, init_soc: float) -> pd.DataFrame:
    out = build_ssr(inp, here, dt_hours=1.0, fmt="bess", ssr_step=5.0,
                    ssr_start=0.0, init_soc=init_soc, solver_timeout=120)
    final = here / f"SSR_toy_12slot_init{int(init_soc)}.xlsx"
    Path(out).replace(final)
    # prepend a plain Inputs sheet for hand-checking
    wb = openpyxl.load_workbook(final)
    ws = wb.create_sheet("Inputs", 0)
    ws.append(["Hour", "Demand (MW)", "Generation (MW)", "Surplus/Deficit (MW)"])
    for h in range(HOURS):
        ws.append([h, DEMAND[h], SOLAR[h] + WIND[h], (SOLAR[h] + WIND[h]) - DEMAND[h]])
    ws.append([])
    ws.append(["Total demand (MWh)", sum(DEMAND)])
    ws.append(["Total generation (MWh)", sum(SOLAR) + sum(WIND)])
    ws.append(["No-battery SSR (%)", 50.0])
    ws.append(["Initial = terminal-floor SOC (%)", init_soc])
    ws.append(["Usable single-cycle swing (%)", round(90 - init_soc, 0)])
    wb.save(final)
    return pd.read_excel(final, "Summary")


def main():
    here = Path(__file__).resolve().parent
    inp = here / "toy_input_12slot.xlsx"
    make_input(inp)

    # Two passes: default 50% initial SOC (single-cycle pessimistic) and 10%
    # (full 10-90% band -> matches the intuitive hand calc).
    res = {soc: run_case(inp, here, soc) for soc in (50.0, 10.0)}

    cols = ["SSR target (%)", "BESS Power (MW)", "BESS Energy (MWh)",
            "Simulated SSR (%)", "Unmet (MWh)"]
    print("\n=== SSR sweep — initial SOC 50% (default) ===")
    print(res[50.0][cols].to_string(index=False))
    print("\n=== SSR sweep — initial SOC 10% (full band; matches hand calc) ===")
    print(res[10.0][cols].to_string(index=False))

    # hand-check formula:  B^e = (D/0.95) / (0.90 - init/100),  D = 120*tau - 60
    print("\n=== Hand-check at SSR=100%  (D = 60 MWh delivered at night) ===")
    for soc in (50.0, 10.0):
        be = (60 / 0.95) / (0.90 - soc / 100)
        print(f"  init {soc:.0f}%: formula B^e = (60/0.95)/(0.90-{soc/100:.2f}) = {be:.1f} MWh")


if __name__ == "__main__":
    main()
