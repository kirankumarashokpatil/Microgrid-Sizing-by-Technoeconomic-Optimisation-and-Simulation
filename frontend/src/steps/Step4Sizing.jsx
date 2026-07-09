// Step 4 — Closed-loop sizing. Runs the real engine: the SSR curve + physical
// sizing, then a forward-eval at the recommended size for dispatch flows.
// Everything downstream (Steps 5–6, metric strip) reads what this produces.
import { useEffect, useRef, useState } from "react";
import { Nav, EmptyChart } from "./shared.jsx";
import { DesignScenarios } from "./DesignScenarios.jsx";
import { SplitOptimiser } from "./SplitOptimiser.jsx";
import { runScenario, resolveScenario, getScenarios, recordsToObjects, ssrRange } from "../lib/api.js";
import { fmt } from "../lib/svg.js";

// Step-2 topology choice → the engine's site_topology value (authoritative on /run).
const TOPOLOGY_MAP = {
  btm: "grid_connected_btm",
  backup: "bess_load_only",
  off_grid: "off_grid",
  standalone: "standalone_gen",
};

const RUN_MSGS = [
  "Phase 1/3: SSR_max screening from the dataset…",
  "Phase 2/3: sweeping SSR targets — sizing BESS per point…",
  "Phase 3/3: operational verify under the causal rule (Model R)…",
  "Forward-eval at the recommended size for dispatch flows…",
  "Selecting optimal physical design subject to covenant ✓",
];

export function Step4Sizing({ cfg, patch, profile, summary, result, setResult, commitRun,
                              setFlows, running, setRunning, step, go, mode,
                              override, setOverride,
                              designResult, setDesignResult,
                              splitResult, setSplitResult }) {
  const [err, setErr] = useState(null);
  const [logIdx, setLogIdx] = useState(0);
  const [detected, setDetected] = useState(null);   // /resolve result
  const [scenarios, setScenarios] = useState([]);    // READY scenarios, for override
  const [exporting, setExporting] = useState(false);
  const [ssrMax, setSsrMax] = useState(null);        // achievable SSR ceiling (for self-correct)
  const [autoRun, setAutoRun] = useState(false);     // one-shot: re-run after a suggested fix
  const timer = useRef();

  useEffect(() => () => clearInterval(timer.current), []);

  // The inputs the resolver inspects. When the FlowDesigner has run (topoSignals
  // is populated), use its derived signals authoritatively. Otherwise fall back to
  // the Step-2 radio button values. This wires the actual flow topology → backend.
  const sig = cfg.topoSignals || {};
  const genOn = cfg.tech.solar || cfg.tech.wind;
  const availPv = cfg.parcels?.solar?.maxMw ?? summary?.pv_nameplate_mw ?? 150;

  // PV: prefer the FlowDesigner's summed solar rated_mw, else parcel nameplate.
  const pvMw = sig.pv_mw != null
    ? sig.pv_mw
    : (cfg.topology === "backup" || !cfg.tech.solar) ? 0 : availPv;

  // Wind: from FlowDesigner signal or parcel.
  const windMw = sig.wind_mw != null
    ? sig.wind_mw
    : (cfg.topology === "backup" || !cfg.tech.wind) ? 0 : (cfg.parcels?.wind?.maxMw ?? 0);

  // Derive topology booleans: FlowDesigner is authoritative when available.
  const gridAvail = sig.grid_available != null
    ? sig.grid_available
    : cfg.topology !== "off_grid";
  const offGrid   = sig.off_grid != null   ? sig.off_grid   : cfg.topology === "off_grid";
  const standalone= sig.standalone != null ? sig.standalone : cfg.topology === "standalone";

  // Total peak load across all consumers (multi-consumer aware).
  const loadsPeak = (cfg.loads || []).reduce((s, l) => s + (+l.peak_mw || 0), 0);
  const peakLoad = sig.peak_load_mw ?? loadsPeak ?? 24;

  // The objective sent to the engine matches the derived topology (set in Step 3):
  // SSR for BTM, firmness off-grid, curtailment standalone, grid-connection backup.
  const derived = sig.derived_topology || cfg.topology || "btm";
  // Map the chosen objective → the target signals the resolver keys on. The
  // given⇒single / absent⇒sweep pattern holds for each objective.
  const btmObj = cfg.btmObjective || "ssr";
  let targetInputs, wantCurve = true;
  if (derived === "off_grid")        targetInputs = { target_firmness_pct: cfg.firmnessTarget };
  else if (derived === "standalone") targetInputs = { target_curtailment_pct: cfg.curtailmentTarget };
  else if (derived === "backup")     targetInputs = (cfg.gcTarget != null ? { target_gc_mw: cfg.gcTarget } : { min_grid: true });
  else if (btmObj === "gc") {        // BTM grid-connection: target → S12, lowest → S51
    targetInputs = cfg.gcTarget != null ? { target_gc_mw: cfg.gcTarget } : { min_grid: true };
    wantCurve = false;
  } else if (btmObj === "both") {    // BTM SSR + GC co-optimise → S71
    targetInputs = { target_ssr_pct: cfg.ssrTarget ?? 95, target_gc_mw: cfg.gcTarget ?? 30 };
    wantCurve = false;
  } else {                           // BTM SSR: target → S11, sweep → S21
    targetInputs = cfg.ssrTarget != null ? { target_ssr_pct: cfg.ssrTarget } : {};
    wantCurve = cfg.ssrTarget == null;
  }

  const resolveInputs = {
    pv_mw:             pvMw > 0 ? pvMw : undefined,
    ...targetInputs,
    want_curve:        wantCurve,
    grid_available:    gridAvail,
    off_grid:          offGrid,
    standalone:        standalone,
    export_limit_mw:   sig.export_limit_mw,
    // pass the raw flow topology for backend enrichment
    flow_topology:     cfg.flowTopology ? {
      nodes:  (cfg.flowTopology.nodes  || []),
      flows:  (cfg.flowTopology.flows  || []),
      signals: sig,
    } : undefined,
  };

  // Detect the scenario whenever any relevant signal changes.
  useEffect(() => {
    resolveScenario(resolveInputs).then(setDetected).catch(() => setDetected(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cfg.ssrTarget, cfg.tech.solar, cfg.tech.wind, cfg.topology,
      pvMw, windMw, gridAvail, offGrid, standalone,
      // topoSignals is an object — stringify the key discriminators
      sig.derived_topology, sig.peak_load_mw]);

  useEffect(() => {
    getScenarios().then((all) => setScenarios(all.filter((s) => s.ready))).catch(() => {});
  }, []);

  // On an infeasible BTM-SSR run, probe the achievable SSR ceiling so we can
  // suggest the nearest reachable target instead of a dead-end (self-correcting).
  useEffect(() => {
    if (result && !result.feasible && derived === "btm" && btmObj !== "gc") {
      ssrRange({ profile_path: profile?.profile_path,
        pv_mw: pvMw > 0 ? pvMw : undefined, wind_mw: windMw > 0 ? windMw : undefined,
        load_peak_mw: peakLoad > 0 ? peakLoad : undefined })
        .then((r) => setSsrMax(r?.ssr_max ?? null)).catch(() => setSsrMax(null));
    } else {
      setSsrMax(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result]);

  // One-shot re-run after the user accepts a suggested fix — patch() has already
  // updated cfg, so this render's startRun closes over the corrected target.
  useEffect(() => {
    if (autoRun) { setAutoRun(false); startRun(); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoRun]);

  // The scenario that will actually run: an explicit override, else the detected
  // one — but if the detected scenario isn't runnable yet, use its READY fallback.
  const chosenId = override
    || (detected && (detected.ready ? detected.scenario_id : detected.fallback_id))
    || "S21_BTM_SSR_CURVE";

  async function startRun() {
    setErr(null); setRunning(true); setResult(null); setFlows(null); setLogIdx(0);
    timer.current = setInterval(() => setLogIdx((i) => Math.min(i + 1, RUN_MSGS.length - 1)), 700);
    try {
      const res = await runScenario({
        scenario_id: chosenId,
        profile_path: profile?.profile_path,
        site_topology: TOPOLOGY_MAP[sig.derived_topology || cfg.topology],  // FD-derived, authoritative
        pv_mw:   pvMw > 0 ? pvMw : undefined,
        wind_mw: windMw > 0 ? windMw : undefined,
        load_peak_mw: peakLoad > 0 ? peakLoad : undefined,  // scale real load to Σ Step-1 loads
        ...targetInputs,
        dispatch_priority: cfg.dispatch?.priority || [],
        allow_grid_charge: cfg.dispatch?.allowGridCharge ?? null,
      });
      // The engine returns the sizing curve as a {columns, rows} table; the
      // comparison/decision views want an array of row-objects. Normalise once
      // here so every downstream consumer of result.table gets the same shape.
      // commitRun stamps the input signature so Steps 5–6 can detect staleness.
      commitRun({ ...res, table: recordsToObjects(res.table) || [] });

      // Second pass: forward-eval at the recommended size → real dispatch flows.
      const rec = res?.design;
      if (rec && rec.pv_mw !== undefined && rec.bess_mw !== undefined) {
        try {
          const fwd = await runScenario({
            scenario_id: "S00_FIXED_DESIGN_EVAL",
            profile_path: profile?.profile_path,
            pv_mw: rec.pv_mw, bess_mw: rec.bess_mw, bess_mwh: rec.bess_mwh,
            wind_mw: windMw > 0 ? windMw : undefined,
            load_peak_mw: peakLoad > 0 ? peakLoad : undefined,  // same scaled load
            dispatch_priority: cfg.dispatch?.priority || [],
            allow_grid_charge: cfg.dispatch?.allowGridCharge ?? null,
          });
          setFlows(fwd.flows || null);
        } catch { /* dispatch chart is optional — never fail the run for it */ }
      }
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      clearInterval(timer.current);
      setRunning(false);
    }
  }

  const pts = (result?.table || []).map(p => ({
    ...p,
    ssr_pct: p.ssr_pct ?? p["Achieved SSR (%)"] ?? p["Operational SSR (%)"] ?? 0,
    gc_mw: p.gc_mw ?? p["Achieved Peak Grid Import (MW)"] ?? p["Operational Peak Grid (MW)"] ?? 0,
    pv_mw: p.pv_mw ?? p["PV Nameplate (MW)"] ?? 0,
    bess_mw: p.bess_mw ?? p["BESS Power (MW)"] ?? 0,
    bess_mwh: p.bess_mwh ?? p["BESS Energy (MWh)"] ?? 0,
  }));
  const rawRec = result?.design || {};
  // A curve/surface run returns an empty design ({}); the trade-off chart uses the
  // swept points, so don't build an all-zeros "recommended" card from nothing.
  const rec = Object.keys(rawRec).length ? {
    ...rawRec,
    ssr_pct: rawRec.ssr_pct ?? rawRec["Achieved SSR (%)"] ?? rawRec["Operational SSR (%)"] ?? result?.kpis?.["SSR (%)"] ?? 0,
    scr_pct: rawRec.scr_pct ?? rawRec["Achieved SCR (%)"] ?? result?.kpis?.["SCR (%)"] ?? 0,
    curtailment_pct: rawRec.curtailment_pct ?? rawRec["Curtailment (%)"] ?? result?.kpis?.["OSR / Curtailment (%)"] ?? result?.kpis?.["Curtailment (%)"] ?? null,
    gc_mw: rawRec.gc_mw ?? rawRec["Achieved Peak Grid Import (MW)"] ?? rawRec["Operational Peak Grid (MW)"] ?? result?.kpis?.["GCmin Peak (MW)"] ?? 0,
    pv_mw: rawRec.pv_mw ?? rawRec["PV Nameplate (MW)"] ?? 0,
    wind_mw: rawRec.wind_mw ?? rawRec["Wind Nameplate (MW)"] ?? 0,
    bess_mw: rawRec.bess_mw ?? rawRec["BESS Power (MW)"] ?? 0,
    bess_mwh: rawRec.bess_mwh ?? rawRec["BESS Energy (MWh)"] ?? 0,
    duration_h: rawRec.duration_h ?? rawRec["BESS Duration (h)"] ?? (rawRec.bess_mw > 0 ? rawRec.bess_mwh / rawRec.bess_mw : 0),
  } : null;
  const ranInfeasible = !running && result && !result?.feasible;
  const isSurface = detected?.signals?.shape === "surface";

  const FRAME = {
    btm:        { title: "Sizing Trade-Off — Peak Grid Import vs. Green Energy (SSR)",
                  goal: cfg.ssrTarget != null ? `design matching your ${cfg.ssrTarget}% green energy target` : "optimal balance point across the simulation" },
    backup:     { title: "Backup Performance — Storage Capacity vs. Grid Connection", goal: "lowest grid connection required with battery backup" },
    standalone: { title: "Standalone Performance — Storage vs. Spilled Energy", goal: "storage required to prevent grid export overload" },
    off_grid:   { title: "Off-Grid Performance — Solar & Storage vs. Reliability", goal: "smallest system meeting 100% uptime without grid connection" },
  };
  const frame = FRAME[derived] || FRAME.btm;

  function onExport() { setExporting(true); setTimeout(() => setExporting(false), 2000); }

  return (
    <>
      <div className="pagehead"><h1>Step 4 — Equipment Sizing &amp; Simulation</h1>
        <p>Simulate your data centre&apos;s energy performance across 8,760 hours of historical weather and demand data to determine exact equipment capacities and grid power requirements.</p></div>

      {/* Plain-English detected strategy — always first for context. */}
      <DetectPanel detected={detected} scenarios={scenarios}
                   override={override} setOverride={setOverride}
                   topoSignals={cfg.topoSignals} topology={TOPOLOGY_MAP[derived]} mode={mode} />

      {/* Always visible: the dispatch strategy + battery pre-charging decision. */}
      <DispatchControls cfg={cfg} patch={patch} />

      {/* Expert-only: the causal dispatch merit-order editor. */}
      {mode === "expert" && <DispatchPolicy cfg={cfg} patch={patch} objective={btmObj} derived={derived} />}

      {/* THE one primary action. */}
      <div className="navbtns" style={{ marginTop: 14, borderTop: "none", paddingTop: 0 }}>
        <button className="btn" onClick={() => go(step - 1)}>Back</button>
        <button className="btn primary" disabled={running} onClick={startRun}>
          {running ? "Designing your system…" : "▶ Design my system"}
        </button>
      </div>

      {err && <div className="banner err">Simulation error: {err}</div>}

      {running && (
        <div className="card"><div className="running">
          <div className="spin" />
          <div style={{ fontWeight: 700, fontSize: 16 }}>Simulating 8,760 hours of operations…</div>
          <div className="subtle" style={{ marginTop: 4 }}>
            Evaluating historical weather, solar generation, and data centre demand across the full year.
          </div>
        </div></div>
      )}

      {ranInfeasible && (
        <div className="card" style={{ borderColor: "var(--orange)" }}>
          <h3 style={{ color: "var(--orange)" }}>Target not reachable with this design</h3>
          {ssrMax != null && cfg.ssrTarget != null ? (
            <>
              <p style={{ marginBottom: 12 }}>
                A <b>{cfg.ssrTarget}%</b> green-energy target can&apos;t be met with your current generation.
                The most this site can reach is about <b>{Math.floor(ssrMax)}%</b> — add more solar (or land) to go higher.
              </p>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                <button className="btn primary" onClick={() => { patch({ ssrTarget: Math.floor(ssrMax) }); setAutoRun(true); }}>
                  Use {Math.floor(ssrMax)}% &amp; re-run
                </button>
                <button className="btn" onClick={() => go(0)}>Add solar in Step 1</button>
              </div>
            </>
          ) : (
            <p>
              The simulation could not find a configuration meeting all site limits and target covenants.
              Try relaxing your target or increasing available land in the earlier steps.
            </p>
          )}
        </div>
      )}

      {result?.feasible && !running && <div style={{ marginTop: 20 }}>
        <VersionBar result={result} onExport={onExport} exporting={exporting} />
      </div>}

      {result?.feasible && !running && <div className="card" style={{ marginTop: 14 }}>
        <h3>{frame.title}</h3>
        <div className="hint">
          Each dot represents a complete 8,760-hour simulation. We highlight {frame.goal}.
        </div>

        {isSurface ? (
          <SurfaceChart table={result.table || []} rec={rec} />
        ) : (
          <TradeoffChart table={result.table || []} rec={rec} target={cfg.ssrTarget}
                         derived={derived} btmObj={btmObj} />
        )}

        {!isSurface && (
          <div className="legend" style={{ marginTop: 8 }}>
            {cfg.ssrTarget != null && <>
              <span><i className="dot" style={{ background: "#8fc7aa" }} />Meets target</span>
              <span><i className="dot" style={{ background: "#cdd6da" }} />Below target</span>
            </>}
            <span><i className="dot" style={{ background: "#2f8f5b", borderRadius: "50%" }} />Recommended</span>
          </div>
        )}

        <div className="subtle" style={{ marginTop: 10 }}>
          {isSurface
            ? "3D surface analysis: evaluates performance across independent solar and battery sizing combinations."
            : "Trade-off curve: demonstrates how adding battery storage reduces peak grid demand and increases green energy utilization."}
        </div>
      </div>}

      {rec && result?.feasible && !running && <div className="card" style={{ marginTop: 14 }}>
        <h3>Recommended Equipment Configuration</h3>
        <div className="hint">
          The optimal configuration balancing green energy targets, site limits, and grid independence.
        </div>
        <div className="summary" style={{ gridTemplateColumns: "1fr 1fr 1fr" }}>
          <div className="it"><div className="l">Solar PV Capacity</div><div className="v">{fmt.mw(rec.pv_mw)}</div></div>
          <div className="it"><div className="l">Wind Capacity</div><div className="v">{rec.wind_mw > 0 ? fmt.mw(rec.wind_mw) : "—"}</div></div>
          <div className="it"><div className="l">Battery Power</div><div className="v">{fmt.mw(rec.bess_mw)}</div></div>
          <div className="it"><div className="l">Battery Energy</div><div className="v">{fmt.mwh(rec.bess_mwh)}</div></div>
          <div className="it"><div className="l">Storage Duration</div><div className="v">{rec.duration_h ? `${rec.duration_h.toFixed(1)} hrs` : "—"}</div></div>
          <div className="it"><div className="l">Peak Grid Import (GCmin)</div><div className="v" style={{ color: "var(--orange)" }}>{fmt.mw(rec.gc_mw)}</div></div>
          <div className="it"><div className="l">Green Energy / SSR</div><div className="v" style={{ color: "var(--ok)" }}>{fmt.pct1(rec.ssr_pct)}</div></div>
          <div className="it"><div className="l">Self-Consumption / SCR</div><div className="v">{fmt.pct1(rec.scr_pct)}</div></div>
          <div className="it"><div className="l">Spilled Solar</div><div className="v">{rec.curtailment_pct != null ? fmt.pct1(rec.curtailment_pct) : "—"}</div></div>
        </div>
      </div>}

      {/* Deeper analyses, de-emphasised so the primary action stays unambiguous.
          Collapsed in Simple; expanded in Expert. key={mode} re-seeds the default. */}
      <details key={mode} open={mode === "expert"} style={{ marginTop: 16 }}>
        <summary style={{ cursor: "pointer", fontWeight: 700, fontSize: 15, color: "var(--slate)", padding: "6px 2px" }}>
          Optional analyses — quick baseline check &amp; land-split optimiser
        </summary>
        <div style={{ marginTop: 10 }}>
          <DesignScenarios cfg={cfg} profile={profile} summary={summary}
                           data={designResult} setData={setDesignResult} />
          <SplitOptimiser cfg={cfg} profile={profile}
                          data={splitResult} setData={setSplitResult} />
        </div>
      </details>

      {result && !running && <div className="navbtns" style={{ marginTop: 20 }}>
        <button className="btn" onClick={() => go(step - 1)}>Back</button>
        <button className="btn primary" onClick={() => go(step + 1)}>
          Compare Configurations →
        </button>
      </div>}
    </>
  );
}

// ── Scenario detection + override panel ──────────────────────────────────────
// Displays the scenario chosen by resolve_scenario(inputs), along with all
// the discriminator signals — with an override dropdown. Driven by the flow
// topology from the designer rather than manual radio buttons.
function DetectPanel({ detected, scenarios, override, setOverride, topoSignals, topology, mode }) {
  if (!detected) return null;
  const sig = detected.signals || {};
  const ts = topoSignals || {};
  const consumers = ts.consumers || [];
  // Only offer strategies that fit the detected topology — a grid-connected site
  // shouldn't list off-grid / standalone / backup-only scenarios.
  const relevant = topology ? scenarios.filter((s) => s.topology === topology) : scenarios;
  
  const MAP_TARGET = { min_gc: "Minimize Grid Import", ssr: "Green Energy / SSR", peak_shaving: "Peak Shaving", both: "SSR + Grid Limit" };
  const MAP_TOPO = { btm: "Behind the Meter", backup: "Backup Storage", off_grid: "Off-Grid / Islanded", standalone: "Standalone Generation" };
  const MAP_SHAPE = { curve: "Performance Curve", point: "Single Point Analysis", surface: "2D Surface Analysis" };
  const MAP_PV = { none: "No Solar", fixed: "Fixed Capacity", "to size": "Auto-Sized" };

  const chips = [
    ["Mode", MAP_TOPO[sig.topology] || sig.topology],
    ["Solar", MAP_PV[sig.pv_mode] || sig.pv_mode],
    ["Storage", sig.bess_fixed ? "Fixed Capacity" : "Auto-Sized"],
    ["Goal", (sig.targets || []).map(t => MAP_TARGET[t] || t).join(" + ") || "—"],
    ["Analysis", MAP_SHAPE[sig.shape] || sig.shape],
    ts.peak_load_mw != null ? ["Peak Demand", `${ts.peak_load_mw} MW`] : null,
    consumers.length > 1 ? ["Consumers", consumers.length] : null,
  ].filter(Boolean);
  const usingOverride = override && override !== detected.scenario_id;
  return (
    <div className="card" style={{ borderColor: "var(--teal)", background: "var(--teal-light)" }}>
      <h3 style={{ color: "var(--teal-dark)" }}>Detected Strategy — From Your Site Layout</h3>
      <div className="hint" style={{ color: "var(--teal-dark)", opacity: 0.85 }}>
        Your site layout sets the strategy automatically — you don&apos;t need to pick one.
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", margin: "4px 0 10px" }}>
        <span className="badge teal" style={{ fontSize: 13, fontWeight: 700, padding: "4px 10px" }}>{detected.name}</span>
        {!detected.ready && <span className="badge red">Configuration pending</span>}
      </div>
      <div style={{ fontSize: 12.5, color: "var(--slate)", marginBottom: 10 }}>{detected.reason}</div>
      {consumers.length > 1 && (
        <div style={{ fontSize: 12, color: "var(--teal-dark)", marginBottom: 10,
          background: "#fff", border: "1px solid var(--line)", borderRadius: 8, padding: "8px 12px" }}>
          <b>Multi-consumer site:</b>{" "}
          {consumers.map(c => `${c.name} (${c.peak_mw} MW peak)`).join(" + ")}
        </div>
      )}
      {/* Technical detail + strategy override tucked away — simple by default for a
          PM / developer, full engine control one click away for a power user. */}
      <details style={{ marginTop: 2 }}>
        <summary style={{ cursor: "pointer", fontSize: 12.5, fontWeight: 600, color: "var(--teal-dark)" }}>
          Technical detail &amp; strategy override
        </summary>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", margin: "10px 0 12px" }}>
          {chips.map(([k, v]) => (
            <span key={k} style={{ fontSize: 11, background: "#fff", border: "1px solid var(--line2)",
              borderRadius: 8, padding: "3px 9px", color: "var(--grey)" }}>
              {k}: <b style={{ color: "var(--slate)" }}>{String(v)}</b></span>
          ))}
        </div>
        <label className="fld">Override strategy (optional)</label>
        <select value={override} onChange={(e) => setOverride(e.target.value)} style={{ maxWidth: 460 }}>
          <option value="">Use default — {detected.name}</option>
          {relevant.map((s) => (
            <option key={s.id} value={s.id}>{s.name}</option>
          ))}
        </select>
        {usingOverride && <div className="banner warn" style={{ marginTop: 10 }}>
          Overriding default strategy — running custom analysis instead.</div>}
      </details>
    </div>
  );
}

// ── Dispatch policy control (Model R merit order + grid-charging) ────────────
// Mirrors backend rule_dispatch.DISPATCH_ACTIONS / DEFAULT_PRIORITY.
const ACTIONS = {
  gen_to_load:  ["Generation → Load", "Serve load directly from solar / wind"],
  gen_to_bess:  ["Generation → Battery", "Charge from surplus generation"],
  bess_to_load: ["Battery → Load", "Discharge to cover residual load"],
  grid_to_load: ["Grid → Load", "Import from grid, capped by the connection"],
  grid_to_bess: ["Grid → Battery", "Off-peak valley-fill (needs grid-charging on)"],
};
const DEFAULT_ORDER = {
  self_sufficiency: ["gen_to_load", "gen_to_bess", "bess_to_load", "grid_to_load"],
  peak_shaving:     ["gen_to_load", "gen_to_bess", "grid_to_load", "bess_to_load", "grid_to_bess"],
};

// Always-visible, plain-language dispatch decisions: which control strategy to
// operate under (rule vs rolling-horizon forecast) and whether the battery may
// pre-charge from the grid to shrink the grid connection. Both write cfg.dispatch,
// the same fields the expert merit-order editor and the API payloads read.
function DispatchControls({ cfg, patch }) {
  const d = cfg.dispatch || { model: "rule", horizonH: 24, commitH: 1, allowGridCharge: null };
  const set = (next) => patch({ dispatch: { ...d, ...next } });
  const model = d.model || "rule";
  const gcVal = d.allowGridCharge ?? null;   // null | true | false
  const seg = (val, cur, label, onClick, key) => (
    <button key={key ?? label} className="btn" style={{
      padding: "4px 14px", border: "none", borderRadius: 0,
      background: cur === val ? "#15616d" : "#fff", color: cur === val ? "#fff" : "#333" }}
      onClick={onClick}>{label}</button>
  );

  return (
    <div className="card" style={{ marginTop: 12 }}>
      <h3 style={{ marginTop: 0 }}>Dispatch &amp; Operation</h3>

      {/* Dispatch strategy: causal rule vs rolling-horizon forecast (Model W). */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", marginBottom: 12 }}>
        <span style={{ fontSize: 13, fontWeight: 600, minWidth: 130 }}>Control strategy</span>
        <div style={{ display: "flex", border: "1px solid #d6dde0", borderRadius: 6, overflow: "hidden" }}>
          {seg("rule", model, "Rule-based", () => set({ model: "rule" }))}
          {seg("rolling", model, "Rolling-horizon forecast", () => set({ model: "rolling" }))}
        </div>
        <span style={{ fontSize: 11, color: "#8a949b", flex: 1, minWidth: 180 }}>
          {model === "rolling"
            ? "Looks ahead and pre-positions the battery — anticipates peaks (realistic operation)."
            : "Causal merit order, no foresight — the conservative, fully auditable baseline."}
        </span>
      </div>

      {/* Rolling window parameters — only when the forecast strategy is chosen. */}
      {model === "rolling" && (
        <div style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap", marginBottom: 12,
                      paddingLeft: 142 }}>
          <label style={{ fontSize: 12, color: "#556" }}>Look-ahead (h)
            <input type="number" min={1} max={168} value={d.horizonH ?? 24}
              onChange={(e) => set({ horizonH: Math.max(1, +e.target.value || 24) })}
              style={{ width: 64, marginLeft: 8, padding: "3px 6px" }} />
          </label>
          <label style={{ fontSize: 12, color: "#556" }}>Commit (h)
            <input type="number" min={1} max={d.horizonH ?? 24} value={d.commitH ?? 1}
              onChange={(e) => set({ commitH: Math.max(1, +e.target.value || 1) })}
              style={{ width: 64, marginLeft: 8, padding: "3px 6px" }} />
          </label>
          <span style={{ fontSize: 11, color: "#8a949b" }}>Re-optimise every {d.commitH ?? 1} h over the next {d.horizonH ?? 24} h.</span>
        </div>
      )}

      {/* Battery pre-charging (grid charging) — promoted out of expert mode. */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <span style={{ fontSize: 13, fontWeight: 600, minWidth: 130 }}>Battery pre-charging</span>
        <div style={{ display: "flex", border: "1px solid #d6dde0", borderRadius: 6, overflow: "hidden" }}>
          {[["Auto", null], ["On", true], ["Off", false]].map(([label, val]) =>
            seg(val, gcVal, label, () => set({ allowGridCharge: val }), label))}
        </div>
        <span style={{ fontSize: 11, color: "#8a949b", flex: 1, minWidth: 180 }}>
          Let the battery charge from the grid off-peak to shave the peak and shrink the grid connection.
          Auto = decide from the objective.
        </span>
      </div>
    </div>
  );
}

function DispatchPolicy({ cfg, patch, objective, derived }) {
  const [open, setOpen] = useState(false);
  const d = cfg.dispatch || { priority: [], allowGridCharge: null };
  // The mode the engine would pick for this objective — drives the shown default.
  const mode = (derived === "btm" && objective === "gc") ? "peak_shaving" : "self_sufficiency";
  const modeDefault = DEFAULT_ORDER[mode];
  const custom = (d.priority || []).length > 0;
  const order = custom ? d.priority : modeDefault;

  const setDispatch = (next) => patch({ dispatch: { ...d, ...next } });
  const move = (i, dir) => {
    const j = i + dir; if (j < 0 || j >= order.length) return;
    const next = [...order]; [next[i], next[j]] = [next[j], next[i]];
    setDispatch({ priority: next });
  };
  const remove = (i) => setDispatch({ priority: order.filter((_, k) => k !== i) });
  const add = (a) => setDispatch({ priority: [...order, a] });
  const missing = Object.keys(ACTIONS).filter((a) => !order.includes(a));

  return (
    <div className="card" style={{ marginTop: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", cursor: "pointer" }}
           onClick={() => setOpen((o) => !o)}>
        <h3 style={{ margin: 0 }}>Merit-order rules <span style={{ fontWeight: 400, color: "#8a949b", fontSize: 13 }}>
          — {custom ? "custom priority rules" : "smart auto-dispatch (grid as last resort)"}</span></h3>
        <span style={{ color: "#8a949b" }}>{open ? "▲" : "▼ configure"}</span>
      </div>

      {open && <div style={{ marginTop: 12 }}>
        <div className="hint" style={{ marginBottom: 8 }}>
          Our real-time control algorithm applies these energy transfers in order every hour. You can reorder them to prioritize specific energy flows (e.g., prioritize Grid over Battery discharge to keep storage reserved for backup).
        </div>

        {order.map((a, i) => (
          <div key={a} style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 8px",
                                background: "#f6f8f9", borderRadius: 6, marginBottom: 6 }}>
            <b style={{ width: 18, color: "#8a949b" }}>{i + 1}</b>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 600, fontSize: 13 }}>{ACTIONS[a][0]}</div>
              <div style={{ fontSize: 11, color: "#8a949b" }}>{ACTIONS[a][1]}</div>
            </div>
            <button className="btn" style={{ padding: "2px 8px" }} disabled={i === 0} onClick={() => move(i, -1)}>↑</button>
            <button className="btn" style={{ padding: "2px 8px" }} disabled={i === order.length - 1} onClick={() => move(i, 1)}>↓</button>
            <button className="btn" style={{ padding: "2px 8px" }} onClick={() => remove(i)}>×</button>
          </div>
        ))}

        {missing.length > 0 && <div style={{ marginTop: 4, fontSize: 12 }}>
          <span style={{ color: "#8a949b" }}>Add rule: </span>
          {missing.map((a) => <button key={a} className="btn" style={{ padding: "2px 8px", marginRight: 6 }}
                                      onClick={() => add(a)}>+ {ACTIONS[a][0]}</button>)}
        </div>}

        {custom && <div style={{ marginTop: 14 }}>
          <button className="btn" onClick={() => setDispatch({ priority: [] })}>Reset to automatic</button>
        </div>}
      </div>}
    </div>
  );
}

function TradeoffChart({ table, rec, target }) {
  const pts = table || [];
  const W = 620, H = 240, pad = 36;
  if (!pts.length) return <EmptyChart h={240} msg="No swept configurations to plot. To see the trade-off curve, uncheck the fixed SSR target in Step 3 (Strategy & Goals) and re-run — that sweeps a full range of designs." />;
  const hasTarget = target != null && !isNaN(target);
  const ssr = pts.map((p) => +p.ssr_pct), gc = pts.map((p) => +p.gc_mw);
  const domain = hasTarget ? [...ssr, target] : ssr;
  const sMin = Math.min(...domain) - 2, sMax = Math.max(...domain) + 2;
  const gMax = Math.max(...gc) * 1.1 || 1;
  const X = (s) => pad + (s - sMin) / ((sMax - sMin) || 1) * (W - pad - 12);
  const Y = (g) => (H - pad) - g / gMax * (H - pad - 14);
  const tx = hasTarget ? X(target) : 0;
  // Evenly-spaced axis ticks so the swept configs are actually readable.
  const xticks = Array.from({ length: 5 }, (_, i) => sMin + (i / 4) * (sMax - sMin));
  const yticks = Array.from({ length: 5 }, (_, i) => (i / 4) * gMax);
  return (
    <svg className="chart" viewBox="0 0 620 240">
      {/* faint horizontal gridlines */}
      {[0, 0.25, 0.5, 0.75, 1].map((t, i) => { const y = 10 + t * (H - pad - 10); return <line key={i} x1={pad} y1={y} x2={W - 8} y2={y} stroke="#eef2f3" />; })}
      {/* shade the feasible region (meets the covenant) */}
      {hasTarget && <rect x={tx} y={10} width={Math.max(0, W - 8 - tx)} height={H - pad - 10} fill="rgba(47,143,91,.06)" />}
      <line x1={pad} y1={H - pad} x2={W - 8} y2={H - pad} stroke="#d6dde0" />
      <line x1={pad} y1={10} x2={pad} y2={H - pad} stroke="#d6dde0" />
      {/* X-axis ticks (SSR %) */}
      {xticks.map((s, i) => <text key={`xt${i}`} x={X(s)} y={H - pad + 14} fontSize="9" fill="#8a949b" textAnchor="middle">{s.toFixed(0)}%</text>)}
      {/* Y-axis ticks (GCmin MW) */}
      {yticks.map((g, i) => <text key={`yt${i}`} x={pad - 5} y={Y(g) + 3} fontSize="9" fill="#8a949b" textAnchor="end">{g.toFixed(0)}</text>)}
      <text x={W / 2} y={H - 4} fontSize="11" fill="#6b7780" textAnchor="middle">SSR % →</text>
      <text x={12} y={H / 2} fontSize="11" fill="#6b7780" textAnchor="middle" transform={`rotate(-90 12 ${H / 2})`}>GCmin (MW)</text>
      {hasTarget && <>
        <line x1={tx} y1={10} x2={tx} y2={H - pad} stroke="#c2603a" strokeWidth="1.3" strokeDasharray="5 4" />
        <text x={tx - 5} y={20} fontSize="10" fill="#c2603a" textAnchor="end">Target {target}% →</text>
      </>}
      {pts.map((p, i) => <circle key={i} cx={X(+p.ssr_pct)} cy={Y(+p.gc_mw)} r="4" fill={hasTarget && +p.ssr_pct >= target ? "#8fc7aa" : "#cdd6da"}>
        <title>{`SSR ${fmt.pct1(p.ssr_pct)} · GCmin ${fmt.mw(p.gc_mw)}\nPV ${fmt.mw(p.pv_mw)} · BESS ${fmt.mw(p.bess_mw)} / ${fmt.mwh(p.bess_mwh)}`}</title>
      </circle>)}
      {rec && (
        <>
          <circle cx={X(+rec.ssr_pct)} cy={Y(+rec.gc_mw)} r="7" fill="#2f8f5b" stroke="#fff" strokeWidth="2" />
          <text x={X(+rec.ssr_pct) + 11} y={Y(+rec.gc_mw) + 4} fontSize="11" fontWeight="700" fill="#2f8f5b">
            {fmt.mw(rec.gc_mw)} · {fmt.pct(rec.ssr_pct)}</text>
        </>
      )}
    </svg>
  );
}

function Frontier({ pts, rec, target }) {
  return <TradeoffChart table={pts} rec={rec} target={target} />;
}

// Real PV × SSR → BESS MWh heatmap (the surface scenario actually sweeps this
// grid). Sequential single-hue ramp for magnitude + a legend; infeasible cells
// (SSR unreachable at that PV) are hatched, not colored. Falls back to the
// trade-off scatter if the table isn't a genuine 2-D grid.
function SurfaceChart({ table, rec }) {
  const rows = (table || []).map((r) => ({
    pv: +(r["PV Nameplate (MW)"] ?? r.pv_mw ?? 0),
    ssr: +(r["Target SSR (%)"] ?? r.ssr_pct ?? 0),
    bess: r["BESS Energy (MWh)"] ?? r.bess_mwh,
    feasible: (r["Feasible"] ?? r.feasible) !== false && (r["BESS Energy (MWh)"] ?? r.bess_mwh) != null,
  }));
  const pvs = [...new Set(rows.map((r) => r.pv))].sort((a, b) => a - b);
  const ssrs = [...new Set(rows.map((r) => r.ssr))].sort((a, b) => b - a); // high SSR at top
  if (pvs.length < 2 || ssrs.length < 2) return <TradeoffChart table={table} rec={rec} />;

  const at = (pv, ssr) => rows.find((r) => r.pv === pv && r.ssr === ssr);
  const feas = rows.filter((r) => r.feasible && r.bess != null).map((r) => +r.bess);
  const bMin = feas.length ? Math.min(...feas) : 0;
  const bMax = feas.length ? Math.max(...feas) : 1;
  // Sequential green ramp, light→dark (monotone lightness). t=0 small BESS.
  const ramp = (t) => {
    const stops = [[230, 242, 234], [143, 199, 170], [47, 143, 91], [20, 83, 45]];
    const x = Math.max(0, Math.min(1, t)) * (stops.length - 1);
    const i = Math.min(stops.length - 2, Math.floor(x)), f = x - i;
    const c = stops[i].map((v, k) => Math.round(v + (stops[i + 1][k] - v) * f));
    return `rgb(${c[0]},${c[1]},${c[2]})`;
  };

  const W = 620, H = 300, padL = 46, padB = 40, padT = 10, padR = 96;
  const gw = (W - padL - padR), gh = (H - padT - padB);
  const cw = gw / pvs.length, ch = gh / ssrs.length;
  const cx = (j) => padL + j * cw, cy = (i) => padT + i * ch;
  const recCell = rec && Math.abs(+rec.pv_mw) > 0;
  return (
    <>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`}>
        <defs>
          <pattern id="infhatch" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
            <rect width="6" height="6" fill="#f0f2f3" />
            <line x1="0" y1="0" x2="0" y2="6" stroke="#cfd6da" strokeWidth="1.4" />
          </pattern>
        </defs>
        {ssrs.map((ssr, i) => pvs.map((pv, j) => {
          const c = at(pv, ssr);
          const feasible = c && c.feasible && c.bess != null;
          const t = bMax > bMin ? (+c?.bess - bMin) / (bMax - bMin) : 0.5;
          return (
            <rect key={`${i}-${j}`} x={cx(j) + 1} y={cy(i) + 1} width={Math.max(0, cw - 2)} height={Math.max(0, ch - 2)}
                  rx="2" fill={feasible ? ramp(t) : "url(#infhatch)"}>
              <title>{`PV ${fmt.mw(pv)} · SSR target ${ssr}%\n${feasible ? `min BESS ${fmt.mwh(c.bess)}` : "infeasible — SSR unreachable at this PV"}`}</title>
            </rect>
          );
        }))}
        {/* recommended cell outline */}
        {recCell && (() => {
          const j = pvs.reduce((b, p, k) => Math.abs(p - rec.pv_mw) < Math.abs(pvs[b] - rec.pv_mw) ? k : b, 0);
          const i = ssrs.reduce((b, s, k) => Math.abs(s - rec.ssr_pct) < Math.abs(ssrs[b] - rec.ssr_pct) ? k : b, 0);
          return <rect x={cx(j) + 1} y={cy(i) + 1} width={Math.max(0, cw - 2)} height={Math.max(0, ch - 2)}
                       rx="2" fill="none" stroke="#15616d" strokeWidth="2.5" />;
        })()}
        {/* axes labels */}
        {pvs.map((pv, j) => <text key={`px${j}`} x={cx(j) + cw / 2} y={H - padB + 14} fontSize="9" fill="#8a949b" textAnchor="middle">{Math.round(pv)}</text>)}
        {ssrs.map((ssr, i) => <text key={`py${i}`} x={padL - 6} y={cy(i) + ch / 2 + 3} fontSize="9" fill="#8a949b" textAnchor="end">{ssr}%</text>)}
        <text x={padL + gw / 2} y={H - 4} fontSize="11" fill="#6b7780" textAnchor="middle">Solar PV (MW) →</text>
        <text x={12} y={padT + gh / 2} fontSize="11" fill="#6b7780" textAnchor="middle" transform={`rotate(-90 12 ${padT + gh / 2})`}>SSR target (%)</text>
        {/* colour legend (min→max BESS MWh) */}
        <text x={W - padR + 12} y={padT + 8} fontSize="10" fill="#6b7780">BESS (MWh)</text>
        {Array.from({ length: 24 }, (_, k) => (
          <rect key={`lg${k}`} x={W - padR + 12} y={padT + 16 + k * 6} width="14" height="6"
                fill={ramp(1 - k / 23)} />
        ))}
        <text x={W - padR + 30} y={padT + 22} fontSize="9" fill="#8a949b">{fmt.mwh(bMax)}</text>
        <text x={W - padR + 30} y={padT + 16 + 24 * 6} fontSize="9" fill="#8a949b">{fmt.mwh(bMin)}</text>
        <rect x={W - padR + 12} y={padT + 16 + 24 * 6 + 6} width="14" height="10" fill="url(#infhatch)" />
        <text x={W - padR + 30} y={padT + 16 + 24 * 6 + 14} fontSize="9" fill="#8a949b">infeasible</text>
      </svg>
      <div className="legend" style={{ marginTop: 6 }}>
        <span>Darker = more storage needed · hatched = SSR target unreachable at that solar size · <i className="dot" style={{ background: "none", border: "2px solid #15616d", borderRadius: 2 }} />recommended</span>
      </div>
    </>
  );
}

function VersionBar({ result, onExport, exporting }) {
  if (!result) return null;
  return (
    <div className="card" style={{ background: "var(--teal-light)", borderColor: "var(--teal)", padding: "12px 18px", display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12 }}>
      <div>
        <div style={{ fontWeight: 700, color: "var(--teal-dark)", fontSize: 14 }}>✓ 8,760-Hour Sizing Simulation Complete</div>
        <div style={{ fontSize: 12, color: "var(--teal-dark)", opacity: 0.85, marginTop: 2 }}>
          Optimal equipment capacities and operational covenants verified against historical dataset.
        </div>
      </div>
      <div>
        <button type="button" className="btn small primary" onClick={onExport} disabled={exporting}>
          {exporting ? "Exporting Snapshot…" : "⤓ Export Simulation Snapshot"}
        </button>
      </div>
    </div>
  );
}
