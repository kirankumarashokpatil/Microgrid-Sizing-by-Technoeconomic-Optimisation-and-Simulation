// Step 3 — Network Design: Closed-Loop Energy Flow Designer
// The user places generation / storage / load / grid nodes on a canvas and
// connects them to define the physical energy topology.  The derived topology
// signal (btm | off_grid | standalone | backup) flows up to App and is used
// by Steps 4–6.
import FlowDesigner from "./FlowDesigner.jsx";

export function StepNetworkDesign({ cfg, patch, step, go }) {
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
    <FlowDesigner
      cfg={cfg}
      onTopoChange={handleTopoChange}
      onLoadsChange={(loads) => patch({ loads })}  // load edits flow back to Step 2
      step={step}
      go={go}
    />
  );
}
