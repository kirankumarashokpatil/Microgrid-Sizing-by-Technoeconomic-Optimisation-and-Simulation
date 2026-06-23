# How We Solve the Microgrid Sizing Problem From First Principles

This document explains the reasoning behind the optimizer from first principles. It is written for someone who is new to energy modelling, BESS sizing, and behind-the-meter project design.

The aim is not just to explain what the code does. The aim is to explain why this is the right type of method for the problem.

The short version:

We are designing a local energy system for a real consumer. Energy must be available at the exact time the consumer needs it. Solar, wind, battery, and grid are not independent decisions; they interact hour by hour. Therefore we solve the problem as a time-series optimisation problem, not as a static spreadsheet calculation.

## 1. The First Principles

Before thinking about software, finance, or scenarios, the problem has a few basic physical truths.

### Principle 1: Demand must be served at each moment

A data centre or port does not consume "annual average energy". It consumes power now.

At every timestep, demand must be met by some combination of:

- solar generation used directly
- wind generation used directly
- battery discharge
- grid import
- unmet load, which should normally be zero

So the core physical equation is:

```text
demand = solar used + wind used + battery discharge + grid import + unmet load - battery charge
```

Battery charge appears on the demand side because charging the battery consumes energy at that moment.

This is the first reason we need timestep modelling. Annual totals are not enough.

### Principle 2: Renewable generation is available only when nature provides it

Solar and wind are not dispatchable in the same way as a diesel generator or grid supply.

At each timestep, the model can use only the solar and wind that are available at that time.

```text
solar used <= available solar
wind used <= available wind
```

If generation is available but cannot be consumed or stored, it is curtailed.

This means adding more solar or wind is not always useful. It helps only if the generation arrives when demand or battery capacity can absorb it.

### Principle 3: A battery shifts energy through time, but does not create energy

A battery is not a source of energy. It is a time-shifting device.

It can charge during surplus periods and discharge during deficit periods, but:

- it has a maximum charging/discharging power, measured in MW
- it has a storage capacity, measured in MWh
- it loses some energy through inefficiency
- it cannot go below minimum state of charge
- it cannot exceed maximum state of charge
- it should not finish the simulation artificially depleted

So the battery state evolves like this:

```text
new battery energy =
previous battery energy
+ charging energy after efficiency losses
- discharging energy adjusted for efficiency losses
```

This is the second reason we need timestep modelling. The value of storage depends on the sequence of events, not just annual totals.

### Principle 4: The grid should cover only the residual

In this platform, the grid is not the primary design object. The consumer is.

The behind-the-meter system tries to serve the consumer with local generation and battery first. The grid is used only for what remains.

```text
residual grid import =
demand
- direct renewable supply
- battery discharge
+ battery charge
```

The maximum residual import tells us the grid connection size we actually need.

This is very different from starting with a guessed grid connection size. Here, the grid size is an output of the physics.

### Principle 5: Reliability and self-sufficiency must be measured, not assumed

It is easy to say "this site is 90% renewable" using annual energy totals. But that can be misleading.

Self-sufficiency must be checked after dispatch, timestep by timestep.

In this model, SSR means:

```text
SSR = demand served by local generation and battery / total demand
```

Equivalently, if grid import and unmet load are the parts not served locally:

```text
SSR = 1 - (grid import + unmet load) / total demand
```

Including unmet load is important. Otherwise a model could look good by simply failing to serve demand. We do not allow that trick.

### Principle 6: Cost is a system result, not an asset-by-asset result

The cheapest solar plant is not necessarily the cheapest energy system.

The cheapest battery is not necessarily the right battery.

The cheapest grid connection is not necessarily reliable.

The cost only makes sense after we account for how the assets interact:

- more solar may reduce grid import but increase curtailment
- more wind may match night demand better than solar
- more battery may reduce grid dependency but increase capex
- more grid capacity may reduce storage need but increase connection cost

Therefore the design problem is a system optimisation problem.

## 2. The Problem Statement

From those principles, the actual problem becomes:

> Find the combination of solar capacity, wind capacity, BESS power, BESS energy capacity, and grid import capacity that serves the consumer reliably, reaches the target SSR, and minimises total cost.

That is why we solve for all major capacities together.

We do not first pick solar, then separately pick battery, then separately check grid. That would miss the interactions.

The decision variables are:

- solar MW
- wind MW
- BESS MW
- BESS MWh
- peak grid import MW

The constraints are:

- power balance at every timestep
- solar and wind availability limits
- battery charge and discharge power limits
- battery state-of-charge limits
- battery duration limits
- terminal battery state-of-charge condition
- grid import limits
- target SSR
- site maximum limits

The objective is:

> minimise the total cost of the system while meeting the physical constraints and target self-sufficiency.

This is why the core engine is an optimisation model rather than a normal calculation sheet.

## 3. Why We Solve It This Way

There are three possible ways someone might try to solve this problem.

### Option A: Annual average spreadsheet

This is simple but weak.

It might compare annual demand with annual solar and wind generation.

The problem is that energy timing disappears. A site may generate enough energy annually but still need a large grid connection because generation arrives at the wrong time.

So this approach cannot correctly size BESS or grid import.

### Option B: Manual scenario testing

This is better.

Someone can choose a solar size, wind size, and battery size, then simulate the year.

The problem is that there are thousands of possible combinations. Manual testing can show whether one design works, but it does not reliably find the best design.

### Option C: Time-series optimisation plus dispatch verification

This is our method.

It does two things:

1. Optimisation searches the design space and finds a cost-effective system that meets the target.
2. Dispatch simulation verifies how that system actually operates timestep by timestep.

This is the natural method because the problem has both:

- continuous decisions, such as MW and MWh sizes
- time-dependent physics, such as battery state of charge

That is why the method is stronger than a static spreadsheet and more systematic than manual trial and error.

## 4. The Business Problem

A modern project is not a single asset. It is a system made of:

- renewable generation, such as solar and wind
- a BESS, meaning battery energy storage system
- a consumer load, such as a data centre or port
- a grid connection
- commercial assumptions, such as capex, opex, grid cost, and discount rate

The hard question is:

> What combination of generation, battery, and grid connection gives the consumer reliable energy at the lowest sensible cost?

This cannot be answered well with a static spreadsheet because the answer depends on time.

For example:

- solar output changes every hour
- wind output changes every hour
- consumer demand changes every hour
- the battery state of charge changes every hour
- the grid is needed only when local generation plus battery cannot cover demand

So the problem is not just "how many MW of solar do we need?" or "how many MWh of battery do we need?"

The real problem is:

> Across every timestep in the year, how does energy flow between generation, battery, consumer, and grid?

## 5. The Behind-the-Meter Principle

The optimiser follows a behind-the-meter, or BTM, design principle.

In this context, "behind the meter" means the consumer, generation, and battery are treated as a local closed loop. The public grid sits outside that loop.

The priority order is:

1. Use renewable generation directly to serve the consumer.
2. If there is surplus generation, charge the battery.
3. If generation is not enough, discharge the battery.
4. Use the grid only for the remaining residual demand.

This matters because the grid connection should be sized from the residual requirement, not from a rough top-down assumption.

Traditional thinking often starts with the grid connection. Our method starts with the consumer load.

## 6. What We Are Optimising

The optimizer chooses these hardware sizes:

- solar capacity, in MW
- wind capacity, in MW
- BESS power, in MW
- BESS energy capacity, in MWh
- peak grid import requirement, in MW

It is trying to meet a target self-sufficiency ratio, or SSR, at minimum cost.

SSR means:

> What percentage of consumer demand is covered by local generation and battery rather than grid import?

For example, if the target SSR is 98%, the system is allowed to use the grid for only about 2% of annual demand.

The optimiser also tracks other important outputs:

- SCR: self-consumption ratio, meaning how much available renewable generation is actually used on site
- curtailment: renewable energy that could not be used or stored
- GCmin: the minimum grid connection requirement implied by the simulated residual demand
- reliability: how much load is actually served
- financial outputs such as capex, opex, NPV, and LCOE

## 7. Why Time-Series Simulation Is Necessary

Averages are dangerous in this problem.

Suppose annual renewable generation is equal to annual demand. That sounds good, but it does not mean the project is self-sufficient. Generation and demand may happen at different times.

The battery exists because timing matters.

The optimizer therefore runs through the data timestep by timestep. In the current workbook, the timestep is 0.25 hours, meaning 15 minutes.

At every timestep, the model asks:

- how much demand exists?
- how much solar is available?
- how much wind is available?
- how full is the battery?
- can surplus generation charge the battery?
- can the battery discharge to meet demand?
- how much grid import is still required?
- how much generation must be curtailed?

This is why the method is better than a simple spreadsheet. It does not assume the battery helps. It proves when and how the battery helps.

## 8. The Two-Stage Method

The code uses a two-stage workflow.

### Stage 1: Hardware sizing (Reverse Engineering)

The first stage decides the asset sizes using a mathematical optimisation model (Pyomo/HiGHS).

In traditional modelling, you guess the hardware sizes and see what Self-Sufficiency Ratio (SSR) you get. Stage 1 works in reverse: the **Target SSR** is the strict mathematical anchor. If the target SSR is 90%, the optimizer is given a strict "grid budget" of 10% of the annual demand. It works backward from this scarcity constraint to calculate the exact, cheapest physical assets needed to fill the gap.

It calculates the 4 critical variables by evaluating exact €/MWh trade-offs across real-world profiles:

- **Solar MW & Wind MW (Generation):** For a 100 MW Data Center needing 95% firmness (*Case: DC-01*), the optimizer tests massive solar capacities to cover summer daytime peaks. However, to bridge the 0.0 MW solar gaps at midnight, it calculates the exact €/MWh cross-over point between building Wind turbines versus buying massive batteries to shift the noon solar.
- **BESS MW (Power):** This is decided by **power spikes**. If a port with a 30 MW grid connection is hit with an 80 MW vessel spike (*Case: PORT-02*), the local grid cannot handle it. To prevent a blackout and avoid grid reinforcement, the optimizer is mathematically forced to increase `BESS MW` to exactly 50 MW to instantly cover the power gap.
- **BESS MWh (Energy):** This is decided by **time duration**. If that 50 MW vessel spike lasts for exactly 10 hours (*Case: PORT-01*), the battery needs 500 MWh of sustained energy. The optimizer tracks the State of Charge and forces `BESS MWh` up to 500 MWh (automatically pre-charging it before the vessel arrives).

*Note on Curtailment:* The optimizer does not explicitly penalize wasted (curtailed) energy. In BTM design, zero curtailment is a trap—it is often cheaper to heavily overbuild solar to survive winter (wasting summer solar) than to buy a massively expensive seasonal battery. The optimizer balances the mix to reduce waste via capital efficiency.

Subject to constraints:
- demand must be balanced at each timestep
- battery state of charge is cyclical (wrap-around) allowing the model to choose its optimal starting state
- grid import plus unmet load must stay within the allowed SSR limit
- site maximums for solar, wind, BESS, and grid must be respected

The key point:

The model does not size solar, wind, battery, and grid separately. It sizes them together as one system.

### Stage 2: Dispatch simulation

Once a hardware size is selected, the second stage simulates how that system operates over time.

Dispatch means deciding how the available energy flows at each timestep.

The dispatch model uses the selected asset sizes and calculates:

- solar used
- wind used
- BESS charge
- BESS discharge
- grid import
- unmet load, if any
- curtailment
- battery state of charge
- operating regime, such as "BESS Discharge" or "Grid Supplement"

This second stage is important because it verifies that the sizing result actually works operationally.

## 9. Why This Is a Good Method

The method is strong because it combines optimisation and simulation.

A pure spreadsheet is easy to understand but can miss the real physics of storage.

A pure simulation can test one design, but it does not automatically find the best design.

Our method does both:

1. Optimisation finds a good hardware configuration.
2. Dispatch simulation proves how that configuration behaves hour by hour.
3. KPIs translate the technical results into business metrics.
4. Financial calculations translate the physical system into investment outputs.

This gives a development manager a defensible answer to questions like:

- What grid connection do we actually need?
- How much battery improves SSR?
- Is it cheaper to add storage or increase grid capacity?
- Is this site viable with the available solar and wind profile?
- What is the lowest-cost design that meets the SSR target?
- What changes if we prefer solar, wind, or storage?

## 10. The Four Scenario Types (Modeling to Generate Alternatives)

The four scenarios are born at the end of Stage 1 optimization. Engineers know the absolute "cheapest" mathematical design might face real-world friction (e.g., land permitting limits). To give development managers practical options, the optimizer uses a technique called Modeling to Generate Alternatives (MGA).

### 1. Lowest Cost
The optimizer finds the absolute mathematical minimum cost to hit the SSR target. This is the main reference case.

### 2. Solar-Led
The optimizer turns off the "Minimize Cost" objective and adds a new hard budget constraint: *The total cost must not exceed the Base Case + 10%.* Under this new budget, it maximizes the Solar capacity variable (`k_sol`).

### 3. Wind-Led
Using the exact same +10% budget constraint, it resets and maximizes the Wind capacity variable (`k_win`).

### 4. Storage-Led
Using the same +10% budget constraint, it resets and maximizes the Battery Energy capacity (`bess_mwh`), which usually results in a much smaller grid connection size.

These scenarios give four decision-ready options that are technically valid, meet the SSR target, and are within a strict commercial tolerance of the absolute best price. That is easier for a colleague, investor, or development manager to understand.

## 11. What Comes Out of the Model

The final report has two major output sheets.

### Optimisation Results

This is the detailed timestep-by-timestep dispatch result.

It shows, for each scenario and timestep:

- demand
- grid import
- solar used
- wind used
- BESS charge
- BESS discharge
- unmet load
- curtailed generation
- battery state of charge
- operating regime

This sheet is useful for technical audit.

### Financial Summary

This is the scenario comparison table.

It includes:

- solar capacity
- wind capacity
- BESS power
- BESS energy capacity
- SSR
- SCR
- curtailment
- reliability
- peak grid import
- capex
- annual opex
- revenue
- NPV
- LCOE

This sheet is useful for investment discussion.

## 12. Important Terms

### MW

MW means power. It is the rate of energy flow at a moment in time.

Example: a 10 MW load needs 10 MW right now.

### MWh

MWh means energy. It is power over time.

Example: a 10 MW load running for 2 hours consumes 20 MWh.

### BESS MW

BESS MW is how fast the battery can charge or discharge.

### BESS MWh

BESS MWh is how much energy the battery can store.

### Battery duration

Battery duration is:

```text
BESS MWh / BESS MW
```

For example, a 40 MWh battery with 10 MW power has 4 hours of duration.

### SSR

Self-sufficiency ratio.

It measures how much consumer demand is served by local generation plus battery rather than the grid.

### SCR

Self-consumption ratio.

It measures how much available renewable generation is actually consumed on site instead of being curtailed.

### Curtailment

Renewable generation that is available but cannot be used or stored.

### GCmin

Minimum grid connection size needed to cover the residual demand after local generation and battery have done their work.

## 13. Why the Grid Is an Output, Not the Starting Point

This is one of the most important ideas.

In many traditional models, someone assumes a grid connection size first, then checks whether the project works.

Here, we do the opposite.

We simulate the closed loop first:

```text
consumer demand
minus renewable generation used directly
minus battery discharge
= residual grid import
```

The maximum residual import tells us the grid connection requirement.

That means the grid connection is derived from the system behaviour.

This is a better development method because it can show that a smaller grid connection may be enough if the generation and BESS are sized properly.

## 14. How the Current Code Maps to the Method

The code is organised as a pipeline:

```text
main.py
  -> load_configuration()
  -> run_sizing()
  -> run_dispatch()
  -> compute_kpis()
  -> calculate_financials()
  -> save_report()
```

### Configuration loader

Reads the Excel workbook and creates clean simulation data plus a single `ProjectParams` object.

This keeps assumptions consistent across the model.

### Sizing engine

Finds the hardware sizes that satisfy the target SSR at minimum cost.

This is where solar MW, wind MW, BESS MW, BESS MWh, and peak grid MW are selected.

### Dispatch engine

Uses the selected hardware size and simulates operation over time.

This proves the design works operationally.

### KPI engine

Calculates SSR, SCR, curtailment, GCmin, unmet load, and reliability from the dispatch result.

### Financials

Converts the physical design into investment metrics.

### Reporter

Writes the results back into the Excel workbook.

## 15. What Is Already Good About the Method

The current method has several strong features:

- It uses time-series data rather than annual averages.
- It treats the consumer load as the central design constraint.
- It sizes generation, battery, and grid together.
- It models BESS state of charge continuously.
- It checks SSR from actual dispatch, not just from assumptions.
- It includes unmet load, so the model cannot hide reliability problems.
- It gives a small number of clear scenarios rather than overwhelming the user.
- It produces both technical and financial outputs.

This is aligned with the platform principle:

> Behind the meter first. Grid as last resort.

## 16. Current Caveat

There is one modelling caveat to understand.

The optimisation currently solves with a linear cost objective. The final report can apply nonlinear scale factors to capex.

This means the "Lowest Cost" scenario is the lowest-cost solution under the linear optimisation model. It is a strong and practical result, but not a mathematically guaranteed global optimum under every nonlinear cost curve.

There are two ways to handle this later:

- keep scale factors at `1.0` when we want a fully linear, exact LP result
- add a piecewise or nonlinear optimisation method if nonlinear capex curves become essential

For the current platform MVP, the linear method is a sensible choice because it is transparent, fast, auditable, and explainable.

## 17. How to Explain This in One Minute

Here is the simple explanation:

We are designing a behind-the-meter energy system for a real consumer. Instead of guessing the grid connection size or using annual averages, we simulate every timestep of the year. The model decides how much solar, wind, battery power, battery energy, and grid connection are needed to hit a target self-sufficiency level at minimum cost. It then runs an operational dispatch simulation to prove the design works and calculates SSR, SCR, curtailment, grid requirement, reliability, and financial metrics. The result is a small set of decision-ready scenarios: lowest cost, solar-led, wind-led, and storage-led.

## 18. The Main Takeaway

The value of the tool is not just that it produces charts or tables.

The value is that it creates an auditable calculation chain:

```text
input assumptions
-> physical sizing
-> operational dispatch
-> technical KPIs
-> financial comparison
-> investment decision
```

That is why the method is stronger than a manual spreadsheet. It connects engineering reality to commercial decision-making.


