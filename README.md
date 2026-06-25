# DIP Italy Microgrid Optimizer

A behind-the-meter sizing and dispatch optimization engine specifically designed for data centres with on-site PV and BESS. The tool uses Pyomo and the HiGHS solver to perform linear programming (LP) based optimization.

It complies with the two-phase approach specified in the DIP specification:
- **Phase 1**: Sizes the BESS purely on physical constraints (no economics) to generate objective feasibility curves.
- **Phase 2**: Overlays CAPEX, OPEX, degradation, and alternative revenue streams to identify the techno-economic optimal point on those curves.

## Requirements

The tool requires Python 3.10+ and the packages listed in `requirements.txt`:

```bash
pip install -r requirements.txt
```

*Note: The tool relies on the `highspy` package to provide the open-source HiGHS linear programming solver.*

## Usage

### Phase 1: Physical Sizing Curves

Generates the physical feasibility curves. This phase sweeps across different targets (SSR and Grid Limits) and calculates the minimum BESS capacity required.

```bash
python main.py --phase1 --profiles "8760_PV&Load Profiles.xlsx"
```

**Key Arguments:**
- `--profiles`: Path to the Excel file containing 8760 hourly load and PV data.
- `--resample-15min`: Automatically interpolates the hourly data into 15-minute resolution before solving (recommended for accurate peak shaving).
- `--scenarios A B C D`: Choose which scenarios to run.
  - A: Main SSR curve
  - B: Main Peak Shaving curve
  - C: PV+BESS co-sizing surface
  - D: Sub-scenario Peak Shaving (no PV)

**Outputs:**
- `Phase1_Sizing_Curves.xlsx` (Contains all points and configurations)
- `plot_ssr_curve.html` (Interactive plot)
- `plot_peakshaving_curve.html` (Interactive plot)
- `plot_surface.html` (Interactive 3D surface plot)

---

### Phase 2: Techno-Economic Overlay

Reads the Phase 1 curves and identifies the "knee-of-curve" optimal point by overlaying CAPEX and OPEX assumptions. It also performs grid services and seasonal shifting analyses.

```bash
python main.py --phase2 --curves "Phase1_Sizing_Curves.xlsx" --verify-dispatch
```

**Key Arguments:**
- `--curves`: Path to the Phase 1 output file.
- `--verify-dispatch`: Takes the chosen optimal design and runs it through a 48-hour rolling horizon dispatch simulation to prove it operates correctly with real-world forecasting constraints.
- `--site-area`: Optional limit on physical land area (in m²) to constrain large solar/BESS combinations.
- `--cost-pv-mw`, `--cost-bess-mwh`, `--grid-price`, etc: Customise all unit economics via the CLI. Run `python main.py -h` for the full list.

**Outputs:**
- `Phase2_TechnoEconomic.xlsx` (Full financial breakdown, seasonal shifting, site constraints)
- `Phase2_Dispatch_Verification.xlsx` (If `--verify-dispatch` is passed)
