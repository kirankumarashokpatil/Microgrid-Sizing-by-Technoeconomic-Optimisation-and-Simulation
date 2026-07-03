# Giga Park — Change Analysis

**What we're building:** reframing the DIP app from a *BESS optimiser* (generation known,
size the battery) into a **land-first design tool** (developer knows the land + the
data-centre demand; the tool proposes the whole build). Reconciles the 3 Jul 2026
"Giga Park Design" meeting with the existing engine.

Spec: the shared one-pager (Giga Park land-first design tool). This file is the
engineering companion — *what actually has to change, where, and in what order.*

---

## TL;DR — the one central change

The engine today sizes **only the battery**; solar/wind nameplates are **fixed inputs**
(`core/params.py` — `pv_mw … a number ⇒ PV fixed`; `SizingResult.pv_mw` = "fixed input in
BESS-only sweep"). `core/scenarios.py` names the gap outright:

> `NEEDS_PV_VARIABLE` — *"needs PV promoted to an LP decision variable (the one big missing capability)."*

**Everything else is plumbing around that one capability.** Promote PV (and wind) to LP
decision variables under a **hard land-budget constraint**, and ~11 registry scenarios
(family B, S81, S101, …) light up and the land-first tool becomes possible.

---

## Current state (what exists)

| Layer | File(s) | Status |
|---|---|---|
| LP / sizing | `core/sizing_engine.py`, `core/solver.py` | BESS-only; PV & wind fixed |
| Land → capacity | `core/site.py` | area → max solar/wind MW (one-way) |
| Boundary import | `core/boundary.py` | ✅ GeoJSON/KMZ/KML → rings (added this session) |
| Timeseries | `core/profile_loader.py` | loads bundled 8760 PV+Load; no DC-shape synthesis, no location weather |
| Scenario registry | `core/scenarios.py` | 46 scenarios; ~11 `needs_pv_variable`, several `needs_minor` |
| Routing | `core/resolver.py` | routes topology (btm/backup/off-grid/standalone) + pv_mode from inputs |
| Economics | `core/economic_overlay.py`, `core/params.py` | CAPEX overlay; no sunk-cost / grid-infra split |
| API | `api/main.py` | `/run /resolve /ssr-range /economics /parcel-capacity /parse-boundary /profile-*` |
| Frontend | `frontend/src/App.jsx`, `steps/*`, `lib/*` | ✅ land-first order, SiteDesigner, Available-Land + Load parcels, land accounting (this session) |

---

## Changes by layer

### 1. Engine — PV/wind as variables + land budget  ⭐ **[L, the keystone]**
`core/sizing_engine.py`, `core/solver.py`

- Add decision variables `P_s`, `P_w` (currently fixed parameters).
- Generation term: `g(t) = Σ built P·pu(t) + P_s·s(t) + P_w·w(t)` (existing plant + new).
- **Hard land budget:** `P_s/ρ_s + P_w/ρ_w + B_e/ρ_b ≤ A_avail` (densities from `site.py`).
- Optional per-tech ceilings when land is pre-allotted (`P_s ≤ ρ_s·a_s_fixed`, or equality).
- Keep it a **single LP** (all terms linear) — no MILP needed for capacities.
- Wire the new mode through `curve_runner.py` / answer modes (point, curve, surface).

> This is the change that clears every `NEEDS_PV_VARIABLE` scenario. Do it once, centrally.

### 2. Land ↔ generation coupling  **[S–M]**
`core/site.py`, `api/main.py`

- Add **MW → area** (inverse of `parcel_capacity`) so the UI radio `[Area | MW]` works both ways.
- Expose densities `ρ_s, ρ_w, ρ_b` to the engine for the land-budget constraint.
- Compute **`A_avail` = A_total − DC footprint(+safety buffer) − reserved(substation/no-build)**;
  pass it as the site-area cap (this is scenario **S101**).

### 3. Timeseries spine  **[M]**
`core/profile_loader.py` (+ a new `core/demand.py`, `core/weather.py`)

- **Demand `D(t)`:** support (a) uploaded DC profile, (b) synthesise 8760 from **peak MW × a
  data-centre load shape** (near-flat, mild diurnal). Pluggable source behind one series.
- **Weather `s(t)`/`w(t)`:** support (a) bundled 8760 profiles, (b) **location-driven** from
  parcel lat/lon (reuse the irradiation model already in `site.py`; add a wind per-unit series).
- Contract: the LP only ever sees arrays — origin is abstracted.

### 4. Scenario routing  **[S–M]**
`core/resolver.py`, `core/scenarios.py`

- Extend `_signals`/`_decide` to route on **land-first inputs**: what's given
  (built / allotted-area / MW-target / unknown) → pick family A vs B vs C vs overlay.
- Flip the `needs_pv_variable` scenarios to `ready` once change #1 lands:
  **S53, S54, S61, S63, S72, S81, S82, S92, S94, S103, S122**.
- Clear the `needs_minor` items where they only await wiring: **S55, S64, S101, S102, S104,
  S105, S111, S113, S114, S121, S123**.

### 5. Scenario comparison (with / without BESS)  **[S]**
`api/main.py`

- Run the solve under a small canonical set and return a **list**, not one result:
  **Grid-only** (no gen, no BESS) · **Generation, no BESS** (`B=0` forced) · **Generation + BESS**
  (`B` free). Same LP, different configs. (Ties to the resolved "zero-battery optimum".)
- New/extended endpoint: `/design` (or extend `/run`) taking the land-first payload
  (A_avail, blocks, D-source, weather-source) → scenario array.

### 6. Existing plant, surplus land, economics  **[M]**
`core/economic_overlay.py`, `core/params.py`

- **Existing plant:** `status=built` block adds fixed generation, consumes land, **CapEx sunk**
  (excluded from the objective).
- **Surplus land (Stage 2):** after Stage 1 meets demand at min cost, if land remains, a second
  pass maximises value of the surplus (more solar for export vs land cost) against `price(t)`.
- **Grid economics:** separate **connection/infrastructure CapEx** (per MW) from energy price so
  grid correctly "comes last" (Fabrizio's doc) — a bigger connection carries a bigger fixed charge.

### 7. Frontend  **[M–L, coordinate with Alice]**
`App.jsx`, `steps/*`, `SiteDesigner.jsx`, `lib/api.js`, `lib/loads.js`

- **Data-centre demand as a first-class, up-front input** (MW), not buried in step 2.
- **Per-tech radio `[ Area | Generation MW ]` + one slider**; call backend for the reciprocal.
- **Scenario-comparison output** (grid-only / no-BESS / +BESS) + surplus-land suggestion.
- DC parcel already reserves land — add the **safety-buffer** offset.
- (Already done: land-first order, Available-Land + Load parcel types, land-for-generation KPI.)

---

## Cross-cutting concerns

- **Slider latency.** A full-year solve is **2–4 min** — cannot run on every drag. Build a
  **fast analytic SSR surrogate** for live feedback; full LP only on **Confirm**. *(design decision — do early)*
- **Area ↔ MW basis.** Density depends on panel/turbine model (rotor spacing, GCR). Default models
  now; **Asad's model library** as a dropdown later. Label outputs as ballpark until then.
- **Wind in v1?** Italy is solar-dominant — consider **solar-only first**; wind folds in as another block.

---

## Registry backlog (what "cover all scenarios" means concretely)

| Group | Scenarios | Blocked on |
|---|---|---|
| **PV variable** (family B, DC, off-grid, standalone) | S53 S54 S61 S63 S72 S81 S82 S92 S94 S103 S122 | change **#1** |
| **Wiring only** (`needs_minor`) | S55 S64 S101 S102 S104 S105 S111 S113 S114 S121 S123 | changes #2/#4/#6 |
| **Consumer layer** (port / multi-consumer) | S83 S84 S85 S86 S87 | consumer modelling (later) |
| **Ready today** (18) | S00 S11 S12 S21 S22 S41 S42 S71 S51 S52 S62 S91 S93 S31–S34 S112 | — build the flow on these first |

**v1 anchors:** `S81` (DC self-sufficient), `S101` (site-area-constrained), **family B** (split
optimiser). **Build-first easy case:** `S11 / S00` with generation fixed from the parcels.

---

## Sequenced roadmap

| Phase | Deliverable | Touches | Size |
|---|---|---|---|
| **00** | Land capture ✅ | `SiteDesigner`, `boundary.py` | done |
| **01** | **Evaluation core** — land → existing solve × (with/without BESS) → scenario array. Backend `POST /design` ✅ + `runDesign()` client ✅ (branch `feat/giga-park-land-first`); frontend comparison view pending | #5, #2 (A_avail) | **in progress** |
| **02** | Reciprocal `[Area\|MW]` control ✅ (backend `capacity-from-mw` + `area_for_capacity`; SiteDesigner `GenSizer` reshapes polygon); **DC demand up-front pending** | #2, #3, #7 | in progress |
| **03** | **PV/wind variable + land budget** → split optimiser; finish `needs_pv_variable` | **#1**, #4 | L |
| **04** | Existing plant, surplus (Stage 2), grid/economics realism | #6 | M |
| **05** | Decision pack — proposed layout on map, KPIs, sensitivity, export | #7 | M |

Phase 01 is deliberately small and reuses today's engine — it proves the whole
land → solve → scenarios spine before the keystone LP change in Phase 03.

---

## Open decisions (pin before building)

1. **Stage-1 objective** — min cost to hit SSR target *(recommended)* vs min land.
2. **Demand shape** — synthesise from peak (default DC shape) vs require upload.
3. **Weather** — location-driven vs bundled as the default per project.
4. **Wind in v1** — include or solar-only first.
5. **Slider** — surrogate now vs full-solve-on-confirm only.
6. **UI ownership** — backend + spec drive Alice's UI; agree screen flow before functional React.
