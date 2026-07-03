// Step 2 — Project definition: consumer(s), reliability covenant, demand. Loads
// selected here are the single source of truth — they feed the Step 1 flow
// designer as consumer nodes. The demand chart shows the REAL dataset series.
import { useEffect, useState } from "react";
import { Nav } from "./shared.jsx";
import { getProfileSeries } from "../lib/api.js";
import { LOAD_TYPES, makeLoad } from "../lib/loads.js";
import { linePath, areaPath, downsample, fmt } from "../lib/svg.js";

export function Step1Project({ cfg, patch, profile, summary, step, go }) {
  const [load, setLoad] = useState([]);   // real load series from the backend

  useEffect(() => {
    getProfileSeries(profile?.profile_path, 336)
      .then((s) => setLoad(downsample((s.load_mw || []).map(Number), 300)))
      .catch(() => setLoad([]));
  }, [profile]);

  const loads = cfg.loads || [];
  const hasType = (t) => loads.some((l) => l.load_type === t);
  const totalPeak = loads.reduce((s, l) => s + (+l.peak_mw || 0), 0);

  // The engine sizes against the dataset's real SHAPE scaled to the selected peak,
  // so show that here (not the raw dataset magnitude) — the same series it uses.
  const datasetPeak = summary?.peak_load_mw || 0;
  const scale = datasetPeak > 0 && totalPeak > 0 ? totalPeak / datasetPeak : 1;
  const sizingLoad = load.map((v) => v * scale);
  const sizedMean = summary?.mean_load_mw != null ? summary.mean_load_mw * scale : null;
  const sizedAnnual = summary?.total_load_mwh != null ? summary.total_load_mwh * scale : null;

  const toggleType = (t) => {
    if (hasType(t)) {
      if (loads.length <= 1) return;                       // keep at least one load
      patch({ loads: loads.filter((l) => l.load_type !== t) });
    } else {
      patch({ loads: [...loads, makeLoad(t)] });
    }
  };
  const setLoadField = (id, k, v) =>
    patch({ loads: loads.map((l) => (l.id === id ? { ...l, [k]: v } : l)) });

  const max = sizingLoad.length ? Math.max(...sizingLoad) * 1.1 : 1;

  return (
    <>
      <div className="pagehead"><h1>Step 2 — Consumer & Load</h1>
        <p>Define the consumer(s) and demand. These loads feed the energy-flow design from Step 1 as consumer nodes and set the demand the engine sizes against.</p></div>

      <div className="card">
        <h3>Project Details</h3>
        <div className="row">
          <div><label className="fld">Project Name</label>
            <input type="text" value={cfg.projectName} onChange={(e) => patch({ projectName: e.target.value })} /></div>
          <div><label className="fld">Site Location</label>
            <input type="text" value={cfg.location} onChange={(e) => patch({ location: e.target.value })} /></div>
        </div>
      </div>

      <div className="card" style={{ borderColor: "var(--teal)", background: "var(--teal-light)" }}>
        <h3 style={{ color: "var(--teal-dark)" }}>Consumers (select one or more)</h3>
        <div className="hint" style={{ color: "var(--teal-dark)", opacity: 0.8 }}>
          Each selected load becomes a consumer node in the Step 1 flow designer. Total peak: <b>{fmt.mw(totalPeak)}</b>.
        </div>
        <div className="choices">
          {Object.entries(LOAD_TYPES).map(([t, m]) => (
            <div key={t} className={"choice" + (hasType(t) ? " sel" : "")} onClick={() => toggleType(t)}>
              <div className="ic">{m.icon}</div><div className="t">{m.label}</div>
              <div className="d">{m.peak} MW peak · {m.base} MW base</div>
            </div>
          ))}
        </div>
      </div>

      {loads.length > 0 && (
        <div className="card">
          <h3>Selected loads</h3>
          <div className="hint">Tune each consumer's name and demand envelope. Removing brings the project to its remaining loads.</div>
          <table>
            <thead><tr><th>Load</th><th>Name</th><th>Peak MW</th><th>Base MW</th><th></th></tr></thead>
            <tbody>
              {loads.map((l) => {
                const m = LOAD_TYPES[l.load_type] || LOAD_TYPES.generic;
                return (
                  <tr key={l.id}>
                    <td style={{ whiteSpace: "nowrap" }}>{m.icon} {m.label}</td>
                    <td><input type="text" value={l.name} onChange={(e) => setLoadField(l.id, "name", e.target.value)} /></td>
                    <td><input type="number" value={l.peak_mw} onChange={(e) => setLoadField(l.id, "peak_mw", +e.target.value)} style={{ width: 90 }} /></td>
                    <td><input type="number" value={l.baseline_mw} onChange={(e) => setLoadField(l.id, "baseline_mw", +e.target.value)} style={{ width: 90 }} /></td>
                    <td>{loads.length > 1 && (
                      <button className="btn small" onClick={() => patch({ loads: loads.filter((x) => x.id !== l.id) })}>Remove</button>
                    )}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="card">
        <h3>Demand profile <span className="pill2">as sized</span></h3>
        <div className="hint">This is the exact load the engine sizes against: the real 15-min/hourly <b>shape</b> from {profile?.filename || "the bundled dataset"}, <b>scaled to your selected loads' peak ({fmt.mw(totalPeak)})</b> — magnitude from your loads, shape from real data. Upload metered data (top bar) to change the shape.</div>
        <div className="feaskpis">
          <div className="kp"><div className="n">{fmt.mw(totalPeak)}</div><div className="l">Peak (as sized)</div></div>
          <div className="kp"><div className="n">{fmt.mw(sizedMean)}</div><div className="l">Mean (as sized)</div></div>
          <div className="kp"><div className="n">{sizedAnnual != null ? fmt.gwh(sizedAnnual) : "—"}</div><div className="l">Annual energy (as sized)</div></div>
          <div className="kp"><div className="n">{fmt.mw(summary?.peak_load_mw)}</div><div className="l">Dataset peak (shape source)</div></div>
        </div>
        <div className="chartbox">
          <div className="clab"><span>Consumer demand — real shape scaled to {fmt.mw(totalPeak)} (downsampled across the year)</span><span>MW</span></div>
          {sizingLoad.length ? (
            <svg className="chart" viewBox="0 0 620 170" preserveAspectRatio="none">
              <path d={areaPath(sizingLoad, 620, 170, max)} fill="rgba(63,124,172,.15)" />
              <path d={linePath(sizingLoad, 620, 170, max)} fill="none" stroke="#3f7cac" strokeWidth="1.5" />
            </svg>
          ) : <div className="subtle" style={{ padding: 20 }}><span className="spinner" /> loading real load series…</div>}
          <div className="legend"><span><i className="dot" style={{ background: "#3f7cac" }} />Consumer demand ({fmt.mw(totalPeak)} peak)</span></div>
        </div>
      </div>

      <Nav go={go} step={step} nextLabel="Continue → Objective" />
    </>
  );
}
