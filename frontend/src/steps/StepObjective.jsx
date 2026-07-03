// Objective step — asks the target that FITS the system's derived topology. This
// is why it comes AFTER the energy system: an SSR covenant only means something
// with a grid; off-grid uses firmness, standalone curtailment, backup a grid cap.
import { useEffect, useState } from "react";
import { Nav } from "./shared.jsx";
import { ssrRange } from "../lib/api.js";
import { fmt } from "../lib/svg.js";

const TOPO_LABEL = {
  btm: "Grid-connected BTM", backup: "Backup (no generation)",
  off_grid: "Off-grid", standalone: "Standalone export",
};

function Slider({ label, hint, value, min, max, onChange, color, suffix = "%" }) {
  return (
    <div className="card" style={{ borderColor: color, background: color + "12" }}>
      <h3 style={{ color }}>{label}</h3>
      <div className="hint" style={{ color, opacity: 0.85 }}>{hint}</div>
      <div className="slider-wrap">
        <input type="range" min={min} max={max} value={value} onChange={(e) => onChange(+e.target.value)} />
        <span className="ssrval" style={{ color }}>{value}{suffix}</span>
      </div>
    </div>
  );
}

// BTM has three objectives, each with the given⇒single / absent⇒sweep pattern:
//   SSR              target → S11,  sweep → S21
//   Grid connection  target → S12,  sweep → S22,  "lowest achievable" → S51
//   SSR + Grid       both targets  → S71 (co-optimise)
function BtmObjective({ cfg, patch, range }) {
  const obj = cfg.btmObjective || "ssr";
  const TABS = [["ssr", "Self-sufficiency"], ["gc", "Grid connection"], ["both", "SSR + Grid"]];
  // Feasible SSR band → slider bounds + a clamp so an impossible target isn't set.
  const lo = range?.ssr_min != null ? Math.max(0, Math.floor(range.ssr_min)) : 50;
  const hi = range?.ssr_max != null ? Math.min(100, Math.ceil(range.ssr_max)) : 99;
  const overshoot = cfg.ssrTarget != null && range?.ssr_max != null && cfg.ssrTarget > range.ssr_max;
  const bandNote = range === undefined
    ? <span className="subtle"><span className="spinner" /> checking feasible SSR range…</span>
    : range?.ssr_max != null
      ? <>Feasible SSR for this system: <b>{fmt.pct1(range.ssr_min)} – {fmt.pct1(range.ssr_max)}</b> (no battery → max battery). Pick a target inside this band.</>
      : null;
  return (
    <div className="card" style={{ borderColor: "var(--red)", background: "var(--red-light)" }}>
      <h3 style={{ color: "var(--red)" }}>What should the engine size for?</h3>
      <div className="tabs" style={{ marginBottom: 12 }}>
        {TABS.map(([k, lab]) => (
          <button key={k} className={obj === k ? "on" : ""}
            onClick={() => patch({ btmObjective: k,
              ...(k === "both" ? { ssrTarget: cfg.ssrTarget ?? 95, gcTarget: cfg.gcTarget ?? 30 } : {}) })}>{lab}</button>
        ))}
      </div>

      {(obj === "ssr" || obj === "both") && (
        <div style={{ marginBottom: obj === "both" ? 16 : 0 }}>
          <label className="fld">Self-sufficiency (SSR)</label>
          {bandNote && <div style={{ fontSize: 12.5, color: "var(--teal-dark)", margin: "0 0 8px" }}>{bandNote}</div>}
          {obj === "ssr" && (
            <label className="subtle" style={{ display: "block", margin: "2px 0 10px", cursor: "pointer", fontSize: 13 }}>
              <input type="checkbox" checked={cfg.ssrTarget == null}
                     onChange={(e) => patch({ ssrTarget: e.target.checked ? null : Math.min(95, hi) })} />{" "}
              Sweep the full SSR curve (no fixed target)
            </label>
          )}
          {cfg.ssrTarget != null ? (
            <>
              <div className="slider-wrap">
                <input type="range" min={lo} max={hi} value={Math.min(hi, Math.max(lo, cfg.ssrTarget))}
                       onChange={(e) => patch({ ssrTarget: +e.target.value })} />
                <span className="ssrval" style={{ color: overshoot ? "var(--red)" : "var(--red)" }}>{cfg.ssrTarget}%</span>
              </div>
              {overshoot && <div className="banner warn" style={{ marginTop: 8 }}>
                {cfg.ssrTarget}% exceeds the feasible max ({fmt.pct1(range.ssr_max)}). It will be infeasible — lower it, or enlarge the parcel in Step 2.</div>}
            </>
          ) : <div className="subtle">Sweeps SSR from feasible min → max; recommends the knee design.</div>}
        </div>
      )}

      {(obj === "gc" || obj === "both") && (
        <div>
          <label className="fld">Grid connection</label>
          {obj === "gc" && (
            <label className="subtle" style={{ display: "block", margin: "2px 0 10px", cursor: "pointer", fontSize: 13 }}>
              <input type="checkbox" checked={cfg.gcTarget == null}
                     onChange={(e) => patch({ gcTarget: e.target.checked ? null : 30 })} />{" "}
              Sweep the grid-connection curve (no fixed target)
            </label>
          )}
          {cfg.gcTarget != null ? (
            <div style={{ maxWidth: 220 }}>
              <input type="number" value={cfg.gcTarget} min={0} onChange={(e) => patch({ gcTarget: +e.target.value })} />
              <div className="subtle" style={{ marginTop: 4 }}>Minimum BESS to hold grid import ≤ this (MW).</div>
            </div>
          ) : <div className="subtle">Sweeps grid connection down to the lowest the battery can firm.</div>}
        </div>
      )}

      <div className="hint" style={{ color: "var(--red)", opacity: 0.8, marginTop: 12, marginBottom: 0 }}>
        {obj === "ssr"  && "Size for a self-sufficiency covenant — grid connection is the residual output."}
        {obj === "gc"   && "Size for a grid-connection limit — SSR is the residual output."}
        {obj === "both" && "Co-optimise: hit the SSR covenant while staying within the grid-connection cap."}
      </div>
    </div>
  );
}

export function StepObjective({ cfg, patch, profile, step, go }) {
  const derived = cfg.topoSignals?.derived_topology || cfg.topology || "btm";

  // Probe the feasible SSR band up front (only meaningful for BTM/grid-connected).
  const [range, setRange] = useState(null);
  const sig = cfg.topoSignals || {};
  const pvMw = sig.pv_mw ?? cfg.parcels?.solar?.maxMw ?? 150;
  const windMw = sig.wind_mw ?? (cfg.tech?.wind ? cfg.parcels?.wind?.maxMw : 0) ?? 0;
  const loadPeak = (cfg.loads || []).reduce((s, l) => s + (+l.peak_mw || 0), 0);
  useEffect(() => {
    if (derived !== "btm") { setRange(null); return; }
    setRange(undefined);   // loading
    ssrRange({ profile_path: profile?.profile_path, pv_mw: pvMw, wind_mw: windMw,
               load_peak_mw: loadPeak, site_topology: "grid_connected_btm" })
      .then(setRange).catch(() => setRange(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [derived, pvMw, windMw, loadPeak, profile]);

  const objective = {
    btm: <BtmObjective cfg={cfg} patch={patch} range={range} />,
    off_grid: (
      <Slider label="Firmness target" color="var(--ok)"
        hint="With no grid to fall back on, the share of demand PV+BESS must firmly meet on their own."
        value={cfg.firmnessTarget} min={90} max={100} onChange={(v) => patch({ firmnessTarget: v })} />
    ),
    standalone: (
      <Slider label="Maximum curtailment" color="var(--purple)"
        hint="Export-limited generation: how much spilled energy you'll accept. The battery is sized to hold curtailment at or below this."
        value={cfg.curtailmentTarget} min={0} max={30} onChange={(v) => patch({ curtailmentTarget: v })} />
    ),
    backup: (
      <div className="card" style={{ borderColor: "var(--orange)", background: "var(--orange-light)" }}>
        <h3 style={{ color: "var(--orange)" }}>Grid connection target</h3>
        <div className="hint" style={{ color: "var(--orange)", opacity: 0.85 }}>
          No on-site generation — the battery firms a grid connection. Set a target MW, or let the engine find the lowest achievable.
        </div>
        <label className="subtle" style={{ display: "block", margin: "6px 0 10px", cursor: "pointer" }}>
          <input type="checkbox" checked={cfg.gcTarget == null}
                 onChange={(e) => patch({ gcTarget: e.target.checked ? null : 30 })} />{" "}
          Find the lowest achievable grid connection
        </label>
        {cfg.gcTarget != null && (
          <div style={{ maxWidth: 220 }}>
            <label className="fld">Target grid connection (MW)</label>
            <input type="number" value={cfg.gcTarget} min={0}
                   onChange={(e) => patch({ gcTarget: +e.target.value })} />
          </div>
        )}
      </div>
    ),
  };

  return (
    <>
      <div className="pagehead"><h1>Step 3 — Objective</h1>
        <p>The target that fits your system. Detected system type: <b>{TOPO_LABEL[derived] || derived}</b>.</p></div>

      <div className="card" style={{ borderColor: "var(--teal)", background: "var(--teal-light)" }}>
        <h3 style={{ color: "var(--teal-dark)" }}>Why this target?</h3>
        <div className="hint" style={{ color: "var(--teal-dark)", opacity: 0.85, marginBottom: 0 }}>
          Your Step-2 energy design resolves to a <b>{TOPO_LABEL[derived] || derived}</b> system, so the meaningful objective is shown below.
          {derived !== "btm" && " (Change the flow topology in Step 2 to switch objective — e.g. add a grid node for an SSR covenant.)"}
        </div>
      </div>

      {objective[derived] || objective.btm}

      <Nav go={go} step={step} nextLabel="Continue → Size" />
    </>
  );
}
