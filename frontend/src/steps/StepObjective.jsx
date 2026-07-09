// Objective step — asks the target that FITS the system's derived topology. This
// is why it comes AFTER the energy system: an SSR covenant only means something
// with a grid; off-grid uses firmness, standalone curtailment, backup a grid cap.
import { useEffect, useState } from "react";
import { Nav } from "./shared.jsx";
import { ssrRange } from "../lib/api.js";
import { fmt } from "../lib/svg.js";

const TOPO_LABEL = {
  btm: "Behind the Meter (Renewables + Grid)", backup: "Backup Storage (Grid + Battery)",
  off_grid: "Off-Grid / Islanded (No Grid)", standalone: "Standalone Generation (Export Only)",
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
function BtmObjective({ cfg, patch, range }) {
  const obj = cfg.btmObjective || "ssr";
  const TABS = [["ssr", "Green Energy / SSR"], ["gc", "Peak Grid Import"], ["both", "Green Energy + Grid Limit"]];
  // Feasible SSR band → slider bounds + a clamp so an impossible target isn't set.
  const lo = range?.ssr_min != null ? Math.max(0, Math.floor(range.ssr_min)) : 50;
  const hi = range?.ssr_max != null ? Math.min(100, Math.ceil(range.ssr_max)) : 99;
  const overshoot = cfg.ssrTarget != null && range?.ssr_max != null && cfg.ssrTarget > range.ssr_max;
  const bandNote = range === undefined
    ? <span className="subtle"><span className="spinner" /> calculating achievable green energy range…</span>
    : range?.ssr_max != null
      ? <>Achievable Green Energy (SSR): <b>{fmt.pct1(range.ssr_min)} – {fmt.pct1(range.ssr_max)}</b> (from no battery to maximum battery). Select a target within this range.</>
      : null;
  return (
    <div className="card" style={{ borderColor: "var(--red)", background: "var(--red-light)" }}>
      <h3 style={{ color: "var(--red)" }}>What is your primary design goal?</h3>
      <div className="tabs" style={{ marginBottom: 12 }}>
        {TABS.map(([k, lab]) => (
          <button key={k} className={obj === k ? "on" : ""}
            onClick={() => patch({ btmObjective: k,
              ...(k === "both" ? { ssrTarget: cfg.ssrTarget ?? 95, gcTarget: cfg.gcTarget ?? 30 } : {}) })}>{lab}</button>
        ))}
      </div>

      {(obj === "ssr" || obj === "both") && (
        <div style={{ marginBottom: obj === "both" ? 16 : 0 }}>
          <label className="fld">Green Energy Target (SSR %)</label>
          {bandNote && <div style={{ fontSize: 12.5, color: "var(--teal-dark)", margin: "0 0 8px" }}>{bandNote}</div>}
          {obj === "ssr" && (
            <label className="subtle" style={{ display: "block", margin: "2px 0 10px", cursor: "pointer", fontSize: 13 }}>
              <input type="checkbox" checked={cfg.ssrTarget == null}
                     onChange={(e) => patch({ ssrTarget: e.target.checked ? null : Math.min(95, hi) })} />{" "}
              Analyze full range of green energy performance (no fixed target)
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
                {cfg.ssrTarget}% exceeds the maximum achievable ({fmt.pct1(range.ssr_max)}). Lower your target or add more solar generation area in Step 1.</div>}
            </>
          ) : <div className="subtle">Simulates all green energy levels from minimum to maximum; automatically recommends the optimal balance point.</div>}
        </div>
      )}

      {(obj === "gc" || obj === "both") && (
        <div>
          <label className="fld">Peak Grid Import Limit (MW)</label>
          {obj === "gc" && (
            <label className="subtle" style={{ display: "block", margin: "2px 0 10px", cursor: "pointer", fontSize: 13 }}>
              <input type="checkbox" checked={cfg.gcTarget == null}
                     onChange={(e) => patch({ gcTarget: e.target.checked ? null : 30 })} />{" "}
              Analyze full range of grid connection capacities (no fixed target)
            </label>
          )}
          {cfg.gcTarget != null ? (
            <div style={{ maxWidth: 220 }}>
              <input type="number" value={cfg.gcTarget} min={0} onChange={(e) => patch({ gcTarget: +e.target.value })} />
              <div className="subtle" style={{ marginTop: 4 }}>Minimum battery storage required to keep peak grid import ≤ this limit.</div>
            </div>
          ) : <div className="subtle">Simulates grid connection limits down to the lowest peak import achievable with battery storage.</div>}
        </div>
      )}

      <div className="hint" style={{ color: "var(--red)", opacity: 0.8, marginTop: 12, marginBottom: 0 }}>
        {obj === "ssr"  && "Optimize equipment to meet your green energy target — required grid connection is calculated automatically."}
        {obj === "gc"   && "Optimize equipment to fit within your grid connection limit — green energy self-sufficiency is calculated automatically."}
        {obj === "both" && "Optimize equipment to meet your green energy target while staying within your grid connection limit."}
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
          No on-site solar generation — battery storage is used to minimize required grid connection. Set a target import limit, or let the simulation find the lowest achievable peak import.
        </div>
        <label className="subtle" style={{ display: "block", margin: "6px 0 10px", cursor: "pointer" }}>
          <input type="checkbox" checked={cfg.gcTarget == null}
                 onChange={(e) => patch({ gcTarget: e.target.checked ? null : 30 })} />{" "}
          Find lowest achievable peak grid import
        </label>
        {cfg.gcTarget != null && (
          <div style={{ maxWidth: 220 }}>
            <label className="fld">Target peak grid import (MW)</label>
            <input type="number" value={cfg.gcTarget} min={0}
                   onChange={(e) => patch({ gcTarget: +e.target.value })} />
          </div>
        )}
      </div>
    ),
  };

  return (
    <>
      <div className="pagehead"><h1>Step 3 — Strategy &amp; Goals</h1>
        <p>Set your green energy targets and grid import limits. Detected system layout: <b>{TOPO_LABEL[derived] || derived}</b>.</p></div>

      <div className="card" style={{ borderColor: "var(--teal)", background: "var(--teal-light)" }}>
        <h3 style={{ color: "var(--teal-dark)" }}>Why this strategy?</h3>
        <div className="hint" style={{ color: "var(--teal-dark)", opacity: 0.85, marginBottom: 0 }}>
          Your Step 1 site layout configures a <b>{TOPO_LABEL[derived] || derived}</b> system. The relevant strategy controls are shown below.
          {derived !== "btm" && " (To change your available strategies, adjust your site layout in Step 1 — e.g., add a grid connection to enable green energy target tracking.)"}
        </div>
      </div>

      {objective[derived] || objective.btm}

      <Nav go={go} step={step} nextLabel="Continue → Equipment Sizing" />
    </>
  );
}
