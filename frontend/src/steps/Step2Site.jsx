// Step 1 — Energy System: the technologies, the land parcel that sets available
// generation, and the Closed-Loop Energy Flow Designer. Topology is DERIVED from
// the flow graph (its single source of truth) and flows to the Objective step.
import { Nav } from "./shared.jsx";
import SiteDesigner from "./SiteDesigner.jsx";
import FlowDesigner from "./FlowDesigner.jsx";

const TECH = [
  { k: "solar", ic: "☀️", t: "Solar PV", d: "Sized by the engine (nameplate from the parcel)" },
  { k: "wind",  ic: "🌬️", t: "Wind",     d: "Folded into generation (fixed nameplate from the parcel)" },
  { k: "bess",  ic: "🔋", t: "BESS",     d: "Battery storage — the sizing variable" },
];

export function Step2Site({ cfg, patch, step, go }) {
  const toggle = (k) => patch({ tech: { ...cfg.tech, [k]: !cfg.tech[k] } });
  const drawsParcel = cfg.tech.solar || cfg.tech.wind;

  // Data-centre demand is the anchor the whole design is sized to meet — captured
  // up-front here (per the Giga Park framing), then refined in the Consumer step.
  const loads = cfg.loads || [];
  const dcIdx = Math.max(0, loads.findIndex((l) => l.load_type === "data_centre"));
  const dc = loads[dcIdx];
  const setDc = (k, v) => patch({ loads: loads.map((l, i) => (i === dcIdx ? { ...l, [k]: +v } : l)) });

  // The FlowDesigner derives topology from the flow graph and hands it up here.
  function handleTopoChange(topo) {
    const sig = topo.signals || {};
    patch({
      flowTopology: topo,
      topology: sig.derived_topology || cfg.topology,
      topoSignals: sig,
      flowState: topo._raw,   // raw graph so the designer rehydrates on nav
    });
  }

  return (
    <>
      <div className="pagehead"><h1>Step 1 — Energy System</h1>
        <p>Start from the land and the demand: set the data-centre requirement, import or draw the parcel, choose the technologies, and design the energy flow. The topology is derived from your flow design and drives the objective.</p></div>

      {dc && (
        <div className="card" style={{ borderColor: "var(--teal)", background: "var(--teal-light)" }}>
          <h3 style={{ color: "var(--teal-dark)" }}>🖥️ Data-centre demand
            <span style={{ marginLeft: 8, fontSize: 10.5, fontWeight: 700, letterSpacing: ".04em",
              textTransform: "uppercase", color: "#15616d", background: "#fff",
              border: "1px solid #bcd6d9", borderRadius: 6, padding: "2px 7px", verticalAlign: "middle" }}>
              the anchor
            </span>
          </h3>
          <div className="hint" style={{ color: "var(--teal-dark)", opacity: 0.85 }}>
            The power requirement the whole design is sized to meet. Set it up-front here; add or refine other consumers
            (ports, EV hubs…) in the Consumer &amp; Load step.
          </div>
          <div className="row">
            <div><label className="fld">Peak demand (MW)</label>
              <input type="number" min="0" value={dc.peak_mw}
                     onChange={(e) => setDc("peak_mw", e.target.value)} /></div>
            <div><label className="fld">Baseline demand (MW)</label>
              <input type="number" min="0" value={dc.baseline_mw}
                     onChange={(e) => setDc("baseline_mw", e.target.value)} /></div>
          </div>
        </div>
      )}

      <div className="card">
        <h3>Technologies to consider</h3>
        <div className="hint">Toggling a technology adds or removes its node in the flow designer below. The engine sizes BESS against the available generation.</div>
        <div className="choices">
          {TECH.map((tch) => (
            <div key={tch.k} className={"choice" + (cfg.tech[tch.k] ? " sel" : "")} onClick={() => toggle(tch.k)}>
              <div className="ic">{tch.ic}</div><div className="t">{tch.t}</div><div className="d">{tch.d}</div>
            </div>
          ))}
        </div>
      </div>

      {drawsParcel && (
        <SiteDesigner cfg={cfg} patch={patch} />
      )}

      <FlowDesigner cfg={cfg} onTopoChange={handleTopoChange} />

      <Nav go={go} step={step} nextLabel="Continue → Consumer & Load" />
    </>
  );
}
