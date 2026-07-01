// Step 3 — Resource & assumptions: the financial inputs that feed the Phase-2
// economics overlay. Grid connection size is deliberately NOT entered here — it
// is a residual output the engine computes.
import { Nav } from "./shared.jsx";
import { fmt } from "../lib/svg.js";

const FIELDS = [
  ["grid_cost_mwh", "Grid import tariff (€/MWh)"],
  ["grid_connection_cost_mw", "Grid connection cost (€/MW)"],
  ["nominal_discount_rate_pct", "Discount rate (%)"],
  ["cost_pv_mw", "PV capex (€/MW)"],
  ["cost_bess_mw", "BESS power capex (€/MW)"],
  ["cost_bess_mwh", "BESS energy capex (€/MWh)"],
];

export function Step3Resource({ cfg, patch, summary, step, go }) {
  const e = cfg.econ;
  const setE = (k, v) => patch({ econ: { ...e, [k]: +v } });
  return (
    <>
      <div className="pagehead"><h1>Step 4 — Assumptions</h1>
        <p>Financial assumptions that feed the Phase-2 economics overlay. Every IC-pack number traces back to these.</p></div>

      <div className="card">
        <h3>Model assumptions</h3>
        <div className="hint"><b style={{ color: "var(--teal-dark)" }}>Grid connection size is deliberately excluded</b> — it is computed as a residual output by the engine, not entered here.</div>
        <div className="row">
          {FIELDS.map(([k, lab]) => (
            <div key={k}><label className="fld">{lab}</label>
              <input type="number" value={e[k]} onChange={(ev) => setE(k, ev.target.value)} /></div>
          ))}
        </div>
      </div>

      <div className="card" style={{ borderColor: "var(--ok)", background: "var(--ok-light)" }}>
        <h3 style={{ color: "var(--ok)" }}>Feasibility Gate</h3>
        <div style={{ fontSize: 13, fontWeight: 600, color: "var(--ok)", padding: "8px 0" }}>
          {summary ? (() => {
            const maxGen = (cfg.parcel?.maxSolarMw || 0) + (cfg.tech?.wind ? (cfg.parcel?.maxWindMw || 0) : 0);
            const ok = maxGen >= summary.peak_load_mw;
            return <>
              {ok ? "✔" : "⚠"} Available generation from the parcel <b>{fmt.mw(maxGen)}</b>
              {" "}vs peak demand <b>{fmt.mw(summary.peak_load_mw)}</b> · annual load {fmt.gwh(summary.total_load_mwh)}.<br />
              {ok
                ? <>The engine will size BESS against this generation to reach the {cfg.ssrTarget}% SSR covenant and report the residual GCmin.</>
                : <span style={{ color: "var(--red)" }}>Parcel generation is below peak demand — enlarge the parcel in Step 2 or lower the SSR target.</span>}
            </>;
          })() : "Loading dataset summary…"}
        </div>
      </div>

      <Nav go={go} step={step} nextLabel="Continue → Optimise" />
    </>
  );
}
