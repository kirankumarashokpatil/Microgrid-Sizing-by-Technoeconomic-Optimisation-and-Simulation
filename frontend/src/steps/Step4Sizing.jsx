// Step 4 — Closed-loop sizing. Runs the real engine: the SSR curve + Phase-2
// economics, then a forward-eval at the recommended size for dispatch flows.
// Everything downstream (Steps 5–6, metric strip) reads what this produces.
import { useEffect, useRef, useState } from "react";
import { Nav } from "./shared.jsx";
import { runScenario, resolveScenario, getScenarios } from "../lib/api.js";
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
  "Overlaying Phase-2 economics (CAPEX / OPEX / NPV / LCOE)…",
  "Forward-eval at the recommended size for dispatch flows…",
  "Selecting the knee design subject to the SSR covenant ✓",
];

export function Step4Sizing({ cfg, patch, profile, summary, result, setResult,
                              setFlows, running, setRunning, step, go }) {
  const [err, setErr] = useState(null);
  const [logIdx, setLogIdx] = useState(0);
  const [detected, setDetected] = useState(null);   // /resolve result
  const [scenarios, setScenarios] = useState([]);    // READY scenarios, for override
  const [override, setOverride] = useState("");      // user-chosen scenario id (or "")
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
        with_economics: true,
        economic_objective: "knee",
        dispatch_priority: cfg.dispatch?.priority || [],
        allow_grid_charge: cfg.dispatch?.allowGridCharge ?? null,
        ...cfg.econ,
      });
      setResult(res);

      // Second pass: forward-eval at the recommended size → real dispatch flows.
      const rec = res?.economics?.recommended;
      if (rec) {
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

  const eco = result?.economics;
  const pts = eco?.points || [];
  const rec = eco?.recommended;
  const ranInfeasible = !running && result && !eco;   // engine ran but no feasible design

  // Result framing adapts to the detected topology (the sweep target differs).
  const FRAME = {
    btm:        { title: "Optimisation frontier — GCmin vs SSR",
                  goal: cfg.ssrTarget != null ? `the design at the ${cfg.ssrTarget}% SSR covenant` : "the knee design across the SSR sweep" },
    backup:     { title: "Backup frontier — BESS vs Grid Connection", goal: "the lowest grid connection the battery can firm" },
    standalone: { title: "Standalone frontier — BESS vs Curtailment", goal: "the battery that holds curtailment within the export limit" },
    off_grid:   { title: "Off-grid frontier — PV+BESS vs Firmness", goal: "the smallest PV+BESS meeting the firmness target with no grid" },
  };
  const frame = FRAME[derived] || FRAME.btm;

  return (
    <>
      <div className="pagehead"><h1>Step 4 — Size</h1>
        <p>The optimiser sizes the system under the causal dispatch rule and derives the residual grid connection. Real engine, real dataset.</p></div>

      <DetectPanel detected={detected} scenarios={scenarios}
                   override={override} setOverride={setOverride}
                   topoSignals={cfg.topoSignals} />

      <DispatchPolicy cfg={cfg} patch={patch} objective={btmObj} derived={derived} />

      <div className="navbtns" style={{ marginTop: 14, borderTop: "none", paddingTop: 0 }}>
        <button className="btn" onClick={() => go(step - 1)}>Back</button>
        <button className="btn primary" disabled={running} onClick={startRun}>
          {running ? "Running…" : "▶ Run detected scenario"}
        </button>
      </div>

      {err && <div className="banner err">Engine error: {err}</div>}

      {running && (
        <div className="card"><div className="running">
          <div className="spin" />
          <div style={{ fontWeight: 700, fontSize: 16 }}>Running sizing sweep…</div>
          <div className="prog"><div className="bar" style={{ width: `${(logIdx + 1) / RUN_MSGS.length * 100}%` }} /></div>
          <div className="runlog">{RUN_MSGS.slice(0, logIdx + 1).map((m) => `▸ ${m}`).join("\n")}</div>
        </div></div>
      )}

      {ranInfeasible && (
        <div className="card" style={{ borderColor: "var(--red)", background: "var(--red-light)" }}>
          <h3 style={{ color: "var(--red)" }}>No feasible design under these inputs</h3>
          <div style={{ fontSize: 13, color: "var(--slate)" }}>
            The engine ran but couldn't meet the target for topology <b>{derived}</b>. Options:
            <ul style={{ margin: "8px 0 0", lineHeight: 1.7 }}>
              {derived === "off_grid" && <li>Off-grid must cover the load from PV+BESS alone — <b>enlarge the parcel</b> (more solar) in Step 2, or add a grid connection (switch topology).</li>}
              <li>Lower the <b>SSR target</b> in Step 1.</li>
              <li>Enlarge the <b>land parcel</b> (more available generation) in Step 2.</li>
              <li>Raise the site BESS limits in Step 3, or relax the export/grid limits.</li>
            </ul>
          </div>
        </div>
      )}

      {!running && eco && (
        <div className="row">
          <div className="card" style={{ flex: 2 }}>
            <h3>{frame.title}</h3>
            <div className="hint">Each dot is a sized configuration from the sweep. The recommendation is {frame.goal}.</div>
            <Frontier pts={pts} rec={rec} target={cfg.ssrTarget} />
            <div className="legend">
              <span><i className="dot" style={{ background: "#cdd6da" }} />Configuration</span>
              <span><i className="dot" style={{ background: "#2f8f5b", borderRadius: "50%" }} />Recommended (knee)</span>
              {cfg.ssrTarget != null && <span><i className="dot" style={{ background: "#c2603a" }} />Target SSR {cfg.ssrTarget}%</span>}
            </div>
          </div>
          <div className="card" style={{ flex: 1, background: "#f8f9fa" }}>
            <h3>Why this answer?</h3>
            <div className="summary" style={{ gridTemplateColumns: "1fr", marginBottom: 12 }}>
              <div className="it"><div className="l">Recommended design</div>
                <div className="v" style={{ fontSize: 14, color: "var(--teal-dark)" }}>
                  PV {fmt.mw(rec?.pv_mw)}{rec?.wind_mw > 0 ? ` · Wind ${fmt.mw(rec.wind_mw)}` : ""} · BESS {fmt.mw(rec?.bess_mw)} / {fmt.mwh(rec?.bess_mwh)}</div></div>
              <div className="it"><div className="l">Residual Grid Connection (GCmin)</div>
                <div className="v" style={{ color: "var(--orange)" }}>{fmt.mw(rec?.gc_mw)}</div></div>
              <div className="it"><div className="l">Achieved SSR (Model R)</div>
                <div className="v" style={{ color: "var(--ok)" }}>{fmt.pct1(rec?.ssr_pct)}</div></div>
              <div className="it"><div className="l">Storage Duration</div>
                <div className="v" style={{ color: "var(--slate)" }}>
                  {rec?.bess_mw > 0 ? `${(rec.bess_mwh / rec.bess_mw).toFixed(1)} hrs` : "—"}</div></div>
            </div>
            <div className="subtle">
              Technical recommendation: <b>knee of sizing curve</b> — optimal physical sizing point balancing Self-Sufficiency (SSR%) against storage volume before diminishing returns. Full financial economics &amp; CAPEX are evaluated next in Step 5.
            </div>
          </div>
        </div>
      )}

      {!running && eco && <Nav go={go} step={step} nextLabel="Continue → Economics" />}
    </>
  );
}

// Shows the scenario the engine DETECTED from the inputs (not asked), why, and
// the discriminator signals — with an override dropdown. Driven by the flow
// topology from the designer rather than manual radio buttons.
function DetectPanel({ detected, scenarios, override, setOverride, topoSignals }) {
  if (!detected) return null;
  const sig = detected.signals || {};
  const ts = topoSignals || {};
  const consumers = ts.consumers || [];
  const chips = [
    ["topology",  sig.topology],
    ["PV",        sig.pv_mode],
    ["BESS",      sig.bess_fixed ? "fixed" : "to size"],
    ["target",    (sig.targets || []).join("+") || "—"],
    ["shape",     sig.shape],
    ts.peak_load_mw != null ? ["peak load", `${ts.peak_load_mw} MW`] : null,
    consumers.length > 1 ? ["consumers", consumers.length] : null,
  ].filter(Boolean);
  const usingOverride = override && override !== detected.scenario_id;
  return (
    <div className="card" style={{ borderColor: "var(--teal)", background: "var(--teal-light)" }}>
      <h3 style={{ color: "var(--teal-dark)" }}>Detected scenario — from your flow topology</h3>
      <div className="hint" style={{ color: "var(--teal-dark)", opacity: 0.85 }}>
        No questions asked — the flow designer encodes the topology; this drives the scenario automatically. Override below if needed.
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", margin: "4px 0 10px" }}>
        <span className="badge teal" style={{ fontSize: 12 }}>{detected.scenario_id}</span>
        <b style={{ color: "var(--slate)" }}>{detected.name}</b>
        {!detected.ready && <span className="badge red">not runnable yet</span>}
      </div>
      <div style={{ fontSize: 12.5, color: "var(--slate)", marginBottom: 10 }}>{detected.reason}</div>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 12 }}>
        {chips.map(([k, v]) => (
          <span key={k} style={{ fontSize: 11, background: "#fff", border: "1px solid var(--line2)",
            borderRadius: 8, padding: "3px 9px", color: "var(--grey)" }}>
            {k}: <b style={{ color: "var(--slate)" }}>{String(v)}</b></span>
        ))}
      </div>
      {consumers.length > 1 && (
        <div style={{ fontSize: 12, color: "var(--teal-dark)", marginBottom: 10,
          background: "#fff", border: "1px solid var(--line)", borderRadius: 8, padding: "8px 12px" }}>
          <b>Multi-consumer site:</b>{" "}
          {consumers.map(c => `${c.name} (${c.peak_mw} MW peak)`).join(" + ")}
        </div>
      )}
      <label className="fld">Override scenario (optional)</label>
      <select value={override} onChange={(e) => setOverride(e.target.value)} style={{ maxWidth: 460 }}>
        <option value="">Use detected — {detected.name}</option>
        {scenarios.map((s) => (
          <option key={s.id} value={s.id}>{s.id} — {s.name}</option>
        ))}
      </select>
      {usingOverride && <div className="banner warn" style={{ marginTop: 10 }}>
        Overriding the detected scenario — running <b>{override}</b> instead.</div>}
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

  const gcVal = d.allowGridCharge;   // null | true | false
  const gcSeg = [["Auto", null], ["On", true], ["Off", false]];

  return (
    <div className="card" style={{ marginTop: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", cursor: "pointer" }}
           onClick={() => setOpen((o) => !o)}>
        <h3 style={{ margin: 0 }}>Dispatch policy <span style={{ fontWeight: 400, color: "#8a949b", fontSize: 13 }}>
          — {custom ? "custom merit order" : "automatic (grid as last resort)"}
          {gcVal != null ? ` · grid-charging ${gcVal ? "on" : "off"}` : ""}</span></h3>
        <span style={{ color: "#8a949b" }}>{open ? "▲" : "▼ configure"}</span>
      </div>

      {open && <div style={{ marginTop: 12 }}>
        <div className="hint" style={{ marginBottom: 8 }}>
          The causal controller applies these transfers in order, every timestep, with no foresight.
          Reorder to change policy (e.g. Grid → Load above Battery → Load preserves the battery).
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
          <span style={{ color: "#8a949b" }}>Add step: </span>
          {missing.map((a) => <button key={a} className="btn" style={{ padding: "2px 8px", marginRight: 6 }}
                                      onClick={() => add(a)}>+ {ACTIONS[a][0]}</button>)}
        </div>}

        <div style={{ display: "flex", alignItems: "center", gap: 16, marginTop: 14, flexWrap: "wrap" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: 13, fontWeight: 600 }}>Grid-charging</span>
            <div style={{ display: "flex", border: "1px solid #d6dde0", borderRadius: 6, overflow: "hidden" }}>
              {gcSeg.map(([label, val]) => (
                <button key={label} className="btn" style={{
                  padding: "3px 12px", border: "none", borderRadius: 0,
                  background: gcVal === val ? "#15616d" : "#fff", color: gcVal === val ? "#fff" : "#333" }}
                  onClick={() => setDispatch({ allowGridCharge: val })}>{label}</button>
              ))}
            </div>
            <span style={{ fontSize: 11, color: "#8a949b" }}>Auto = infer from objective</span>
          </div>
          {custom && <button className="btn" onClick={() => setDispatch({ priority: [] })}>Reset to automatic</button>}
        </div>
      </div>}
    </div>
  );
}

function Frontier({ pts, rec, target }) {
  const W = 620, H = 240, pad = 36;
  if (!pts.length) return <svg className="chart" viewBox="0 0 620 240" />;
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
