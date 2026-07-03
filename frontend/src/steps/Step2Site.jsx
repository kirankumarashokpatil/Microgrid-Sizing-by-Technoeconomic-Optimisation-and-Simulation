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
        <p>Start from the land: import or draw the parcel, choose the technologies, and design the energy flow. The system topology is derived from your flow design and drives the objective. You define the consumers next.</p></div>

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
