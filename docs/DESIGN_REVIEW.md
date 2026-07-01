# DIP Optimizer — Engine Design Review

**Status:** proposal for team sign-off
**Scope:** the Phase-1 sizing / Phase-2 economic engine (`optimizer/`)
**Grounding docs:** NatPower CEO Strategic Brief v3 (Jun 2026); DIP Italy Phase-1 spec
**Author:** engine review, 2026-06

---

## 1. The one decision that fixes the most

The codebase quietly answers a foundational question two different ways, and that
contradiction is the root of most of the risk.

**Is dispatch an optimization or a rule?**

- The **CEO brief** is unambiguous: dispatch is a *hardcoded, auditable priority
  order* — **direct → charge BESS → discharge BESS → grid** — "correct at every
  time step," and the SCR/SSR it produces "go into contracts."
- The **Italy spec** is equally clear that the *sizing* runs as a perfect-foresight
  LP, and that "the resulting BESS size is a best-case theoretical lower bound, not
  a number a real-time controller is guaranteed to achieve."

Both are correct — they describe **two different dispatch models**, and the tool
must hold both, explicitly:

| Model | What it is | Foresight | Role |
|-------|-----------|-----------|------|
| **O** — optimal | the sizing LP (`sizing_engine`) | perfect (full year) | theoretical **lower bound** on BESS; never the contract number |
| **R** — rule | causal BTM merit-order controller | none (causal) | the **auditable** controller; the **only** honest source of SCR/SSR/GCmin |

Today the "verification" dispatch (`run_dispatch_simulation`) is a *third* thing —
a 48 h rolling LP with penalty weights (`p_grid*100`, `p_dchg*10`, `−e_bess*0.01`)
— which is neither a clean lower bound nor the auditable rule.
`_classify_regime()` then paints priority-order labels ("BESS Discharge", "Grid
Supplement") onto that LP's output *after the fact*. That looks auditable but is
not: the LP can grid-charge the battery while the label reads "Grid Supplement,"
silently violating "grid as last resort."

**The fix that closes the loop:**

1. **Size** with Model O (fast LP screen / lower bound).
2. **Verify** with Model R, the real causal rule.
3. If the rule misses the target, **tighten BESS by bisection against Model R**
   until it holds. Report both numbers and the gap.

For the simpler scenarios (A, B, D) the LP is not even needed to *size*: a
vectorized causal simulation of the 4-step merit order over 35,040 steps runs in
milliseconds with no solver, and `BESS size → SSR` is monotone, so capacity can be
**bisected directly against the rule** (~20 rule-sims per target). That yields
operationally-honest sizing as the *primary* number, with the LP kept purely as
the optimistic bound and cross-check. Use the LP where optimization is genuinely
needed (co-sizing PV+BESS — scenarios C/E).

**Decision (this review):** the causal rule is **strict "grid as last resort" —
it never charges the BESS from the grid.** The battery charges only from surplus
generation. (Revisited for seasonal shifting in Phase 2, behind an explicit gate.)

---

## 2. First-principles decomposition

Strip the seven scenarios down and they are **one problem** viewed along four
orthogonal axes:

1. **Consumer load** — a fixed time series (DC baseline, port pulses, or their
   sum). Always an input.
2. **Generation** — PV profile × nameplate; nameplate is fixed (A, B, D) or a free
   variable (C, E).
3. **Storage + dispatch policy** — the BESS plus the rule that operates it. *The
   only thing being sized.*
4. **Grid boundary** — import-only, with a ceiling that is either fixed
   (peak-shaving target) or an output (SSR sizing).

Every scenario is the same core with a different `(free variables, target
constraint, objective)` triple:

| Scenario | Free var(s) | Target constraint | Objective / tie-break |
|----------|-------------|-------------------|-----------------------|
| A — SSR sizing | BESS MW, MWh | SSR ≥ target | min MWh, tie-break min MW |
| B — peak shaving | BESS MW, MWh | peak grid ≤ target | min MW (within 2–8h E/P) |
| C — PV+BESS surface | PV, BESS | SSR ≥ target | min cost / min size |
| D — sub (no PV) | BESS MW, MWh | peak grid ≤ target | min MW + **min-utilization floor** |
| E — co-opt | BESS | SSR ≥ target **and** GC ≤ limit | min size |
| F — off-grid | BESS (PV swept) | firmness ≥ target | min MWh |
| G — standalone gen | BESS | curtailment ≤ cap | min size |

`_build_lp` already has this shape via `target_type`, so the abstraction is right.
It should become an explicit, registered **strategy object** per scenario
(declaring free vars / fixed inputs / target / tie-breaker) rather than a chain of
`elif` branches. Then "add a hydrogen/EV consumer" or "add a new objective" is a
config addition, not engine surgery — exactly the extensibility the spec demands.

---

## 3. Concrete errors, ranked by cost to the IC-pack numbers

All verified against the current code.

1. **Sizing↔dispatch foresight gap is never reconciled (highest risk).**
   Perfect-foresight LP sizes the battery; a rolling LP "verifies" it; the SSR you
   would put in a contract is the *operational* one, which is systematically lower
   and is not what gets reported as the design target. → §1.

2. **SSR/SCR computed in ≥3 places with different formulas.** `sizing_engine`,
   `dispatch_engine`, and `kpi_engine` each recompute them; `EconomicParams
   .real_deg_cost` hardcodes DoD = 0.8 while `ProjectParams.real_deg_cost` uses
   `dod_fraction`. For a tool whose credibility rests on SSR/SCR, these must be
   computed **once** from a single canonical flows table.

3. **15-minute resolution is faked by interpolation.** `profile_loader` upsamples
   hourly→15min with `resample('15min').interpolate('time')`, which *smooths away*
   the sub-hourly peaks that 15-min resolution exists to capture for Italian
   contracted-power peak-shaving. Interpolated 15-min is worse than honest hourly
   because it looks right. Require native 15-min; if only hourly exists, run hourly
   and label it — do not fabricate.

4. **Silent magic-number fallbacks.** `find_ssr_max` returns `80.0` and
   `find_gc_min` returns `10.0` on solver failure. These set the sweep bounds, so
   one solver hiccup silently corrupts an entire curve that flows into an IC pack.
   Fail loud; never default.

5. **Broken exception path in `solve_sizing_point`.** If `solver.solve()` raises,
   `results` is never bound, and the unconditional re-check
   (`ok = results.solver.status…`) throws `NameError`, masking the original error.

6. **`anti_proc = −Σe_bess·1e-6` rewards a *fuller* battery.** In a price-free,
   perfect-foresight LP with free grid charging, the solver can grid-charge purely
   to sit at high SOC and collect the reward, distorting dispatch away from the BTM
   merit order. Legitimate in a rolling window (avoids end-of-window dumping),
   harmful in the full-year sizing LP. Tie-break on *throughput* (Σ charge) or grid
   energy instead.

7. **Sub-scenario (D) has no minimum-utilization constraint.** The spec explicitly
   calls for one ("minimum equivalent full cycles or operating hours per year") to
   avoid an enormous, barely-cycled battery at the tight-GC tail. Currently absent,
   so that tail is physically silly. Add the constraint *or* carry the flag into
   Phase 2 and let cost rule it out — pick one and document it.

8. **Two parallel parameter and schema universes.** `PhysicalParams`/`Economic
   Params` vs legacy `ProjectParams`; `load_mw`/`pv_pu` vs `SimCols` solar/wind;
   the Phase-1 dispatch emits `grid_import_mw` while `kpi_engine` expects
   `ResultCols.GRID_IMPORT`. **`kpi_engine` literally cannot consume the Phase-1
   dispatch output** — a silent schema fork.

9. **SSR sweep excludes its own ceiling.** `np.arange(step, ssr_max, step)` never
   sizes the max-reachable SSR point — often the most decision-relevant one.

---

## 4. The change-tolerant architecture

Five moves, ordered so each is independently shippable:

1. **One source of truth for parameters.** Collapse to a single layered config: a
   physical core, with an optional economic overlay attached only in Phase 2.
   Quarantine legacy `ProjectParams` behind one clearly-deprecated adapter, or
   delete it. No third copy of any constant.

2. **One canonical `FlowsFrame` schema.** Every dispatch path — LP read-back,
   causal rule, economic LP — emits the *same* columns (load, pv_used, grid_import,
   charge, discharge, soc, curtailed, unmet, export). Defined once in `schema.py`,
   validated with `require_columns` at every boundary.

3. **One `dispatch()` interface, two implementations.**
   `dispatch(profiles, hardware, params) -> FlowsFrame`, with `LPForesightDispatch`
   and `RuleCausalDispatch` behind it. KPIs are computed **only** from a
   `FlowsFrame`, so sizing, verification, and economics share identical downstream
   code. This single change removes errors #2, #8, and the #5 class at once.

4. **Fail-loud, with invariants as tests.** Replace magic defaults with explicit
   `Infeasible` results that propagate. Add cheap property checks that must hold on
   *every* `FlowsFrame`: energy balance to 1e-6, SOC within [min, max], grid ≥ 0,
   export = 0 when `export_limit = 0`, round-trip energy conserved, terminal SOC ≥
   initial. These automatically catch simultaneous charge/discharge and the
   grid-charge-to-hoard pathology (#6).

5. **A scenario registry.** Each scenario becomes a small declarative object (free
   vars, fixed inputs, target constraint, tie-breaker). Adding a consumer type =
   add a load profile; adding an objective = register a strategy. The engine never
   changes.

---

## 5. Build order (six-month MVP)

The CEO's test is: *"a development manager can answer — what grid connection do I
need for this site and consumer."* Shortest honest path:

**Tranche 1 (this work) — auditable truth, no solver in the critical path:**
- `FlowsFrame` schema + validator (move 2 + 4)
- unified `kpi_engine` as the **sole** KPI site (move 3, KPI half)
- `RuleCausalDispatch` — strict no-grid-charge merit order (move 3, rule half)
- bisection sizing for A/B/D against the rule

This alone produces auditable GCmin/SSR/SCR with no solver dependency.

**Tranche 2 — optimization where it earns its keep:**
- keep the LP as Model O lower bound + cross-check
- PV+BESS surface (C/E) co-sizing via LP
- scenario registry refactor; parameter unification

**Tranche 3 — economics & extensions:** CAPEX/price overlay on rule-verified
curves; degradation; seasonal grid shifting (gated grid-charge); grid services;
multi-consumer aggregation.

---

## 6. Open ambiguities needing a named owner

The spec flags most of these; they are business/modelling decisions, not code:

1. **Target SSR / firmness by consumer type.** "Reliable" for a DC vs a port vs
   grid backup is a different number, and it drives BESS size more than anything.
2. **Degradation mode.** Day-one oversize vs size-for-day-one-and-flag. Changes
   every MWh figure; pick the default explicitly.
3. **PV↔BESS linking constraint** for co-sizing (C/E). The surface has no single
   optimum without it; spec leaves this "to explore."
4. **Grid-charging policy.** *Decided:* the causal rule never grid-charges for the
   **main** scenarios (strict "grid as last resort", PV present). **Empirical
   finding (rule self-test):** with strict no-grid-charge and no PV, the
   **sub-scenario (D) is inert** — the battery can never charge, does zero
   peak-shaving, and sheds load (PV=0, GC=70 MW → SSR≈0%, 5099 MWh unmet). The
   Italy spec explicitly defines the sub-scenario as *"the battery charges from the
   grid"*. **Resolution needed:** allow a gated grid-charge mode **only** for the
   no-PV sub-scenario (charge under GC headroom, bounded by a min-utilization
   floor — open item #7), keeping the main scenarios strict. Same gate later powers
   Phase-2 seasonal shifting.
