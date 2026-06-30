// Routes a ScenarioResult to the right visuals — but framed as a guided,
// question-led narrative: every chart states the decision QUESTION it answers
// and a dynamically-computed ANSWER, and the seasonal view is a drill-down tree
// (click a month → see that month's typical day).
import { useState } from "react";
import {
  KpiCards, Gauges, Scorecard, DesignTable, KneeCurve, MarginalReturns,
  kneeRecommendation, OperationalOverlay, SurfaceHeatmap, DemandServedByScenario,
  EnergyBalanceBar, SsrScrBars, PvUtilisation, SocBand,
  EnergySankey, CarpetHeatmap, DispatchDay, GridDurationCurve,
  MonthlySeasonality, DayPanel,
} from "./Charts.jsx";
import { column } from "./api.js";
import { representativeDay } from "./energy.js";
import { ssrBand } from "./decision.js";
import {
  balanceInsight, pvInsight, monthlyInsight, socInsight, durationInsight, demandServedInsight,
} from "./insights.js";

function distinct(table, col) {
  if (!table || !table.columns.includes(col)) return 0;
  return new Set(column(table, col).filter((v) => v !== null && v !== undefined)).size;
}

// Every chart lives in a card that leads with its decision question and a
// computed answer — so the page reads as "here's what to decide, and here's
// what the data says", not a wall of plots.
function QuestionCard({ q, insight, children, full }) {
  return (
    <div className="chart-card" style={full ? { gridColumn: "1 / -1" } : {}}>
      <div style={{ padding: "10px 12px 0" }}>
        <div style={{ fontWeight: 600 }}>{q}</div>
        {insight && <div className="subtle" style={{ marginTop: 3 }}>{insight}</div>}
      </div>
      {children}
    </div>
  );
}

// Dynamic drill-down: the monthly chart is the parent; clicking a month grows a
// child node showing that month's representative day. Click again to collapse.
function SeasonalExplorer({ flows, dt }) {
  const [month, setMonth] = useState(null);
  const day = month != null ? representativeDay(flows, month) : null;
  return (
    <>
      <QuestionCard q="When in the year do I rely on the grid?" insight={monthlyInsight(flows, dt)}>
        <MonthlySeasonality flows={flows} dt={dt} selectedMonth={month}
          onMonthClick={(i) => setMonth((m) => (m === i ? null : i))} />
      </QuestionCard>
      {day ? (
        <QuestionCard q={`How does a typical day in ${day.label.split(" ")[0]} operate?`}
          insight="Drilled in from the month above — click that month again to collapse.">
          <DayPanel day={day} title={`Representative day — ${day.label}`} />
        </QuestionCard>
      ) : (
        <div className="subtle" style={{ margin: "2px 0 14px", paddingLeft: 4 }}>
          ↑ Tip: click any month bar to drill into a representative day for that month.
        </div>
      )}
    </>
  );
}

export default function Results({ result }) {
  if (!result) return null;
  const { table, flows } = result;
  const isSurface = table && distinct(table, "PV Nameplate (MW)") > 1
    && (distinct(table, "Target SSR (%)") > 1 || distinct(table, "Target Grid Connection (MW)") > 1);
  const isCurve = table && !isSurface && table.columns.includes("BESS Energy (MWh)");
  const dt = result.dt_hours || 1;

  return (
    <div>
      <div className="banner info" style={{ display: "flex", justifyContent: "space-between" }}>
        <span>
          <strong>{result.id}</strong> — {result.name}
          {!result.feasible && <span className="pill backlog" style={{ marginLeft: 8 }}>INFEASIBLE</span>}
        </span>
        <span className="subtle">{result.answer_mode}</span>
      </div>

      {/* Decision summary first */}
      {(result.design || result.kpis) && (
        <>
          <h2>What should I build? <span className="subtle" style={{ fontSize: 13 }}>— and is anything wrong?</span></h2>
          <Scorecard result={result} dt={dt} />
          {result.kpis && Object.keys(result.kpis).length > 0 && <Gauges kpis={result.kpis} />}
        </>
      )}

      {/* Curve — the sizing decision */}
      {isCurve && (
        <>
          <h2>How far is it worth pushing the target?</h2>
          {(() => {
            const band = ssrBand(table);
            return band ? (
              <div className="kpi-grid" style={{ marginBottom: 14 }}>
                <div className="kpi" style={{ borderLeft: "3px solid #8b98a5" }}>
                  <div className="label">Min SSR — PV only, no battery</div>
                  <div className="value">{band.floor.toFixed(0)}<span className="unit">%</span></div>
                  <div className="subtle">the floor you get for free</div>
                </div>
                <div className="kpi" style={{ borderLeft: "3px solid #f5a623" }}>
                  <div className="label">Max SSR — ceiling (SSR_max)</div>
                  <div className="value">{band.ceiling.toFixed(0)}<span className="unit">%</span></div>
                  <div className="subtle">most any battery can reach at this PV</div>
                </div>
                <div className="kpi" style={{ borderLeft: "3px solid #3fb6ff" }}>
                  <div className="label">Usable target band</div>
                  <div className="value" style={{ fontSize: 20 }}>{band.floor.toFixed(0)}–{band.ceiling.toFixed(0)}%</div>
                  <div className="subtle">choose your SSR target in here</div>
                </div>
              </div>
            ) : null;
          })()}
          {kneeRecommendation(table) && (
            <div className="banner info" style={{ borderLeft: "3px solid var(--accent)" }}>💡 {kneeRecommendation(table)}</div>
          )}
          <QuestionCard q="How much battery does each target need?"
            insight="★ marks the sweet spot — the elbow before costs accelerate.">
            <KneeCurve table={table} />
          </QuestionCard>
          <QuestionCard q="Where does extra battery stop paying off?"
            insight="Taller bars = each further step demands disproportionately more storage.">
            <MarginalReturns table={table} />
          </QuestionCard>
          <QuestionCard q="How does raising the target cut grid use?" insight={demandServedInsight(table)}>
            <DemandServedByScenario table={table} />
          </QuestionCard>
          <QuestionCard q="What does each design actually deliver?"
            insight="Operational SSR/SCR and peak grid under the causal rule, across the sweep.">
            <OperationalOverlay table={table} />
          </QuestionCard>
          <CurveTable table={table} />
        </>
      )}

      {/* Surface */}
      {isSurface && (
        <>
          <h2>How do PV and the target trade off?</h2>
          <QuestionCard q="What battery does each PV × target combination need?"
            insight="Darker = more battery. Read across to see how more PV lowers the storage needed.">
            <SurfaceHeatmap table={table} />
          </QuestionCard>
        </>
      )}

      {/* Energy balance */}
      {flows && (
        <>
          <h2>Where does the energy come from?</h2>
          <QuestionCard q="How is annual demand met — and how much still comes from the grid?"
            insight={balanceInsight(flows, dt)}>
            <EnergyBalanceBar flows={flows} dt={dt} />
          </QuestionCard>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
            <QuestionCard q="How self-sufficient is the site?"
              insight="SSR = on-site share of demand; SCR = share of PV actually used.">
              <SsrScrBars flows={flows} dt={dt} />
            </QuestionCard>
            <QuestionCard q="Am I wasting PV?" insight={pvInsight(flows, dt)}>
              <PvUtilisation flows={flows} dt={dt} />
            </QuestionCard>
          </div>
          <QuestionCard q="What's the full flow of energy through the site?"
            insight="Every MWh from source (PV/grid) to sink (load/battery/curtailed).">
            <EnergySankey flows={flows} dt={dt} />
          </QuestionCard>
        </>
      )}

      {/* Seasonality — interactive drill-down */}
      {flows && (
        <>
          <h2>How does it change through the year?</h2>
          <SeasonalExplorer flows={flows} dt={dt} />
        </>
      )}

      {/* Detailed dispatch — tucked away to keep the page crisp */}
      {flows && (
        <details style={{ marginTop: 10 }}>
          <summary style={{ cursor: "pointer", fontSize: 17, fontWeight: 600, margin: "12px 0" }}>
            Detailed operational dispatch ({flows.n_total.toLocaleString()} timesteps)
          </summary>
          <QuestionCard q="Which times of year and day stress the grid?"
            insight="Bright cells = high grid import — the seasonal/daily pattern at a glance.">
            <CarpetHeatmap flows={flows} />
          </QuestionCard>
          <QuestionCard q="How is the worst (peak-load) day covered?"
            insight="The day the site leans hardest on the grid — the design's stress test.">
            <DispatchDay flows={flows} />
          </QuestionCard>
          <QuestionCard q="Is the battery actually cycling?" insight={socInsight(flows)}>
            <SocBand flows={flows} designMwh={result.design?.bess_mwh} />
          </QuestionCard>
          <QuestionCard q="How peaky is grid import?" insight={durationInsight(flows)}>
            <GridDurationCurve flows={flows} dt={dt} />
          </QuestionCard>
        </details>
      )}

      {result.notes && <div className="banner info" style={{ marginTop: 12 }}>{result.notes}</div>}
    </div>
  );
}

// Compact scrollable table under a curve, so the raw numbers are still there.
function CurveTable({ table }) {
  const show = ["Target SSR (%)", "Target Grid Connection (MW)", "BESS Power (MW)",
    "BESS Energy (MWh)", "Deliverable BESS Energy (MWh · Model R)",
    "EoL-Sized BESS Energy (MWh)", "Operational SSR (%)", "Feasible"]
    .filter((c) => table.columns.includes(c));
  const idx = show.map((c) => table.columns.indexOf(c));
  return (
    <details className="card" style={{ marginTop: 4 }}>
      <summary className="subtle" style={{ cursor: "pointer" }}>Show curve data ({table.n_total} points)</summary>
      <div style={{ overflowX: "auto", marginTop: 10 }}>
        <table className="simple">
          <thead><tr>{show.map((c) => <td key={c} style={{ color: "var(--muted)" }}>{c.replace(/\s*\(.*?\)/, "")}</td>)}</tr></thead>
          <tbody>
            {table.rows.map((r, i) => (
              <tr key={i}>{idx.map((j, k) => (
                <td key={k}>{typeof r[j] === "number" ? r[j].toLocaleString(undefined, { maximumFractionDigits: 1 }) : String(r[j])}</td>
              ))}</tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
