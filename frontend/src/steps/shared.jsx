// Small pieces shared across step screens: the footer navigation, and the guard
// shown on Steps 5–6 before the engine has produced a result.

export function Nav({ go, step, nextLabel = "Continue →", onNext, nextDisabled }) {
  return (
    <div className="navbtns">
      <button className="btn" disabled={step === 0} onClick={() => go(step - 1)}>Back</button>
      <button className="btn primary" disabled={nextDisabled}
              onClick={() => (onNext ? onNext() : go(step + 1))}>{nextLabel}</button>
    </div>
  );
}

// ── Dispatch policy (Model R) — shared metadata + read-only summary ─────────
// Mirrors backend rule_dispatch.DISPATCH_ACTIONS / DEFAULT_PRIORITY.
export const DISPATCH_LABELS = {
  gen_to_load:  "Generation → Load",
  gen_to_bess:  "Generation → Battery",
  bess_to_load: "Battery → Load",
  grid_to_load: "Grid → Load",
  grid_to_bess: "Grid → Battery",
};
export const DISPATCH_DEFAULT_ORDER = {
  self_sufficiency: ["gen_to_load", "gen_to_bess", "bess_to_load", "grid_to_load"],
  peak_shaving:     ["gen_to_load", "gen_to_bess", "grid_to_load", "bess_to_load", "grid_to_bess"],
};

// The effective (possibly-defaulted) dispatch policy for a given config.
export function dispatchPolicy(cfg) {
  const d = cfg?.dispatch || { priority: [], allowGridCharge: null };
  const mode = (cfg?.topology === "btm" && cfg?.btmObjective === "gc") ? "peak_shaving" : "self_sufficiency";
  const custom = (d.priority || []).length > 0;
  return {
    custom,
    mode,
    order: custom ? d.priority : DISPATCH_DEFAULT_ORDER[mode],
    allowGridCharge: d.allowGridCharge,   // null | true | false
  };
}

// Read-only chip strip describing the active merit order + grid-charging state.
export function DispatchPolicyBadge({ cfg }) {
  const { custom, order, allowGridCharge } = dispatchPolicy(cfg);
  const gc = allowGridCharge == null ? "auto" : (allowGridCharge ? "on" : "off");
  return (
    <div className="card" style={{ marginTop: 0 }}>
      <h3>Dispatch policy <span style={{ fontWeight: 400, color: "#8a949b", fontSize: 13 }}>
        — {custom ? "custom merit order" : "automatic (grid as last resort)"} · grid-charging {gc}</span></h3>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6, marginTop: 6 }}>
        {order.map((a, i) => (
          <span key={a} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span style={{ background: "#eef2f3", borderRadius: 5, padding: "3px 9px", fontSize: 12, fontWeight: 600 }}>
              <b style={{ color: "#8a949b", marginRight: 5 }}>{i + 1}</b>{DISPATCH_LABELS[a]}</span>
            {i < order.length - 1 && <span style={{ color: "#b6c0c5" }}>→</span>}
          </span>
        ))}
      </div>
    </div>
  );
}

export function NeedRun({ go, ranInfeasible }) {
  return (
    <div className="card">
      <div className={"banner " + (ranInfeasible ? "err" : "warn")}>
        {ranInfeasible
          ? <>The last run found <b>no feasible design</b>. Go back to Size and relax the target, enlarge the parcel, or add a grid connection.</>
          : <>No solved result yet. Go to the <b>Size</b> step and run the sizing first.</>}
      </div>
      <button className="btn primary" style={{ marginTop: 14 }} onClick={() => go(3)}>← Back to Size</button>
    </div>
  );
}
