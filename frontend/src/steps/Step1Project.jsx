// Step 2 — Consumer & Demand: choose technologies, select load types, configure
// demand envelopes and review the annual energy profile from the dataset.
import { useEffect, useState } from "react";
import { Nav } from "./shared.jsx";
import { getProfileSeries } from "../lib/api.js";
import { LOAD_TYPES, makeLoad } from "../lib/loads.js";
import { linePath, areaPath, downsample, fmt } from "../lib/svg.js";

const TECH = [
  { k: "solar", ic: "☀️", t: "Solar PV",  d: "Sized by the engine (nameplate from the parcel)" },
  { k: "wind",  ic: "🌬️", t: "Wind",      d: "Folded into generation (fixed nameplate from the parcel)" },
  { k: "bess",  ic: "🔋", t: "BESS",      d: "Battery storage — the sizing variable" },
];

export function Step1Project({ cfg, patch, profile, summary, step, go }) {
  const [rawSeries, setRawSeries] = useState({ load: [], pv: [], wind: [] });
  const [horizon, setHorizon] = useState("year"); // "year" | "month" | "week" | "day"
  const [offset, setOffset] = useState(0);        // starting hour offset
  const [genFilter, setGenFilter] = useState("all"); // "all" | "solar" | "wind"

  useEffect(() => {
    getProfileSeries(profile?.profile_path, 8760)
      .then((s) => {
        setRawSeries({
          load: (s.load_mw || []).map(Number),
          pv: (s.pv_pu || []).map(Number),
          wind: (s.wind_pu || []).map(Number),
        });
      })
      .catch(() => setRawSeries({ load: [], pv: [], wind: [] }));
  }, [profile]);

  const loads = cfg.loads || [];
  const totalPeak = loads.reduce((s, l) => s + (+l.peak_mw || 0), 0);

  // The engine sizes against the dataset's real SHAPE scaled to the selected peak
  const datasetPeak = summary?.peak_load_mw || 0;
  const scale = datasetPeak > 0 && totalPeak > 0 ? totalPeak / datasetPeak : 1;
  const sizedAnnual = summary?.total_load_mwh != null ? summary.total_load_mwh * scale : null;

  // Horizon slicing
  const duration = horizon === "year" ? 8760 : horizon === "month" ? 720 : horizon === "week" ? 168 : 24;
  const sliceEnd = Math.min(rawSeries.load.length || 8760, offset + duration);

  const slicedLoadRaw = rawSeries.load.slice(offset, sliceEnd);
  const slicedPvRaw = rawSeries.pv.slice(offset, sliceEnd);
  const slicedWindRaw = rawSeries.wind.slice(offset, sliceEnd);

  const sizingLoadRaw = slicedLoadRaw.map((v) => v * scale);

  // Downsample for clean, lag-free SVG rendering
  const sizingLoad = downsample(sizingLoadRaw, 300);
  const chartPv = downsample(slicedPvRaw, 300);
  const chartWind = downsample(slicedWindRaw, 300);

  // Horizon KPIs
  const hMinLoad = sizingLoadRaw.length ? Math.min(...sizingLoadRaw) : 0;
  const hMaxLoad = sizingLoadRaw.length ? Math.max(...sizingLoadRaw) : 0;
  const hMeanLoad = sizingLoadRaw.length ? sizingLoadRaw.reduce((a, b) => a + b, 0) / sizingLoadRaw.length : 0;
  const hLoadFactor = hMaxLoad > 0 ? (hMeanLoad / hMaxLoad) * 100 : 0;

  const hPvCf = slicedPvRaw.length ? (slicedPvRaw.reduce((a, b) => a + b, 0) / slicedPvRaw.length) : 0;
  const hWindCf = slicedWindRaw.length ? (slicedWindRaw.reduce((a, b) => a + b, 0) / slicedWindRaw.length) : 0;

  const max = sizingLoad.length ? Math.max(...sizingLoad) * 1.1 : 1;

  const setLoadField = (id, k, v) =>
    patch({ loads: loads.map((l) => (l.id === id ? { ...l, [k]: v } : l)) });

  const selectHorizon = (k) => {
    setHorizon(k);
    if (k === "year") setOffset(0);
    else if (k === "month" && horizon === "year") setOffset(4344); // default to July (summer peak)
    else if (k === "week" && horizon === "year") setOffset(4368);  // default to July peak week
    else if (k === "day" && horizon === "year") setOffset(4368);   // default to July peak day
  };

  const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const getHorizonLabel = () => {
    if (horizon === "year") return "Full Year (8,760 Hours)";
    if (horizon === "month") {
      const mIdx = Math.min(11, Math.floor(offset / 730));
      return `Month: ${MONTH_NAMES[mIdx]} (Hours ${offset}–${Math.min(8760, offset + 720)})`;
    }
    if (horizon === "week") {
      const wIdx = Math.min(52, Math.floor(offset / 168) + 1);
      return `Week ${wIdx} (Hours ${offset}–${Math.min(8760, offset + 168)})`;
    }
    const dIdx = Math.min(365, Math.floor(offset / 24) + 1);
    return `Day ${dIdx} (Hours ${offset}–${Math.min(8760, offset + 24)})`;
  };

  const renderHorizonBar = (title) => (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 10, background: "var(--bg2)", padding: "10px 14px", borderRadius: 8, marginBottom: 14, border: "1px solid var(--line2)" }}>
      <div style={{ fontSize: 13, fontWeight: 700, color: "var(--teal-dark)" }}>
        ⏱️ {title}: <span style={{ fontWeight: 600, color: "var(--text)" }}>{getHorizonLabel()}</span>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <div className="tabs" style={{ margin: 0, display: "inline-flex", background: "#fff", border: "1px solid var(--line2)", borderRadius: 6, padding: 2 }}>
          {[
            { k: "year", l: "Year" },
            { k: "month", l: "Month" },
            { k: "week", l: "Week" },
            { k: "day", l: "Day" },
          ].map((t) => (
            <button
              key={t.k}
              type="button"
              onClick={() => selectHorizon(t.k)}
              style={{
                border: "none",
                background: horizon === t.k ? "var(--teal)" : "transparent",
                color: horizon === t.k ? "#fff" : "var(--text)",
                padding: "4px 10px",
                borderRadius: 5,
                fontSize: 12,
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              {t.l}
            </button>
          ))}
        </div>

        {horizon !== "year" && (
          <div style={{ display: "inline-flex", alignItems: "center", gap: 4, background: "#fff", border: "1px solid var(--line2)", borderRadius: 6, padding: "2px 6px" }}>
            <button
              type="button"
              className="btn small"
              style={{ padding: "2px 8px", minWidth: "auto", fontSize: 11 }}
              onClick={() => {
                const step = horizon === "month" ? 720 : horizon === "week" ? 168 : 24;
                setOffset(Math.max(0, offset - step));
              }}
              disabled={offset <= 0}
            >
              ← Prev
            </button>
            <button
              type="button"
              className="btn small"
              style={{ padding: "2px 8px", minWidth: "auto", fontSize: 11 }}
              onClick={() => {
                const step = horizon === "month" ? 720 : horizon === "week" ? 168 : 24;
                const maxOff = 8760 - step;
                setOffset(Math.min(maxOff, offset + step));
              }}
              disabled={offset >= 8760 - (horizon === "month" ? 720 : horizon === "week" ? 168 : 24)}
            >
              Next →
            </button>
          </div>
        )}
      </div>
    </div>
  );

  return (
    <>
      <div className="pagehead"><h1>Step 2 — Consumer &amp; Demand</h1>
        <p>Select technologies and facility types, then configure operational demand profiles. These drive the AI sizing engine in Step 4.</p>
      </div>

      {/* ── Technology selection ── */}
      <div className="card">
        <h3>Technologies to Consider</h3>
        <div className="hint">Select which technologies to include in your microgrid layout. The AI simulation will optimise equipment capacities against historical weather data.</div>
        <div className="choices">
          {TECH.map((tch) => (
            <div key={tch.k} className={"choice" + (cfg.tech[tch.k] ? " sel" : "")} onClick={() => patch({ tech: { ...cfg.tech, [tch.k]: !cfg.tech[tch.k] } })}>
              <div className="ic">{tch.ic}</div><div className="t">{tch.t}</div><div className="d">{tch.d}</div>
            </div>
          ))}
        </div>
      </div>

      {/* ── Load type selection ── */}
      <div className="card" style={{ borderColor: "var(--teal)", background: "var(--teal-light)" }}>
        <h3 style={{ color: "var(--teal-dark)" }}>Facility Consumers &amp; Load Types (Select all that apply)</h3>
        <div className="hint" style={{ color: "var(--teal-dark)", opacity: 0.8 }}>
          Select which electrical loads will consume energy on your microgrid.
          Combined peak: <b>{totalPeak} MW</b>. Configure exact profiles below.
        </div>
        <div className="choices">
          {Object.entries(LOAD_TYPES).map(([t, m]) => (
            <div key={t} className={"choice" + (loads.some(l => l.load_type === t) ? " sel" : "")} onClick={() => {
              const has = loads.some(l => l.load_type === t);
              if (has) { if (loads.length <= 1) return; patch({ loads: loads.filter(l => l.load_type !== t) }); }
              else patch({ loads: [...loads, makeLoad(t)] });
            }}>
              <div className="ic">{m.icon}</div><div className="t">{m.label}</div>
              <div className="d">{m.peak} MW peak · {m.base} MW base</div>
            </div>
          ))}
        </div>
      </div>


      {loads.length > 0 && (
        <div className="card">
          <h3>Configured Consumer Profiles</h3>
          <div className="hint">Customise demand envelopes for each facility. Removing a profile adjusts the total site energy requirement.</div>
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
        <h3>Consumer Demand Profile <span className="pill2">Timeseries Analysis</span></h3>
        <div className="hint">This is the exact electrical load profile your microgrid is sized to meet. The curve reflects historical hourly behavior from {profile?.filename || "the bundled dataset"}, scaled precisely to your combined facility peak ({fmt.mw(totalPeak)}).</div>
        
        {renderHorizonBar("Demand Horizon")}

        <div className="feaskpis" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))" }}>
          <div className="kp"><div className="n">{fmt.mw(hMaxLoad)}</div><div className="l">Peak Demand (Max)</div></div>
          <div className="kp"><div className="n">{fmt.mw(hMinLoad)}</div><div className="l">Minimum Load (Min)</div></div>
          <div className="kp"><div className="n">{fmt.mw(hMeanLoad)}</div><div className="l">Average Demand (Mean)</div></div>
          <div className="kp"><div className="n">{hLoadFactor ? hLoadFactor.toFixed(1) + "%" : "—"}</div><div className="l">Load Factor (%)</div></div>
          <div className="kp"><div className="n">{sizedAnnual != null ? fmt.gwh(sizedAnnual) : "—"}</div><div className="l">Annual Energy (Total)</div></div>
          <div className="kp"><div className="n">{horizon === "year" ? "8,760 hrs" : horizon === "month" ? "720 hrs" : horizon === "week" ? "168 hrs" : "24 hrs"}</div><div className="l">View Horizon</div></div>
        </div>
        <div className="chartbox" style={{ marginTop: 14 }}>
          <div className="clab">
            <span>Consumer demand — {horizon === "year" ? "hourly curve across 1 year" : getHorizonLabel()}</span>
            <span>MW</span>
          </div>
          {sizingLoad.length ? (
            <svg className="chart" viewBox="0 0 620 170" preserveAspectRatio="none">
              <path d={areaPath(sizingLoad, 620, 170, max)} fill="rgba(63,124,172,.15)" />
              <path d={linePath(sizingLoad, 620, 170, max)} fill="none" stroke="#3f7cac" strokeWidth="1.5" />
            </svg>
          ) : <div className="subtle" style={{ padding: 20 }}><span className="spinner" /> loading real load series…</div>}
          <div className="legend"><span><i className="dot" style={{ background: "#3f7cac" }} />Consumer demand ({fmt.mw(hMaxLoad)} max in view)</span></div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 20 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12, marginBottom: 6 }}>
          <h3 style={{ margin: 0 }}>Renewable Generation Profiles <span className="pill2">Weather Series</span></h3>
          
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span style={{ fontSize: 12, fontWeight: 600, color: "var(--grey)" }}>Technology Filter:</span>
            <div className="tabs" style={{ margin: 0, display: "inline-flex", background: "#fff", border: "1px solid var(--line2)", borderRadius: 6, padding: 2 }}>
              {[
                { k: "all", l: "⚡ Both (Solar + Wind)" },
                { k: "solar", l: "☀️ Solar PV Only" },
                { k: "wind", l: "🌬️ Wind Only" },
              ].map((t) => (
                <button
                  key={t.k}
                  type="button"
                  onClick={() => setGenFilter(t.k)}
                  style={{
                    border: "none",
                    background: genFilter === t.k ? "var(--teal)" : "transparent",
                    color: genFilter === t.k ? "#fff" : "var(--text)",
                    padding: "4px 10px",
                    borderRadius: 5,
                    fontSize: 12,
                    fontWeight: 600,
                    cursor: "pointer",
                  }}
                >
                  {t.l}
                </button>
              ))}
            </div>
          </div>
        </div>
        <div className="hint" style={{ marginTop: 4 }}>
          Historical weather data determining on-site renewable generation across the year. The curves show normalized output (0 to 100% of capacity) for solar PV and wind.
        </div>

        {renderHorizonBar("Generation Horizon")}

        <div className="feaskpis" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))" }}>
          <div className="kp" style={{ opacity: genFilter === "wind" ? 0.4 : 1 }}><div className="n">{hPvCf != null ? (hPvCf * 100).toFixed(1) + "%" : "—"}</div><div className="l">Solar PV Avg CF</div></div>
          <div className="kp" style={{ opacity: genFilter === "solar" ? 0.4 : 1 }}><div className="n">{hWindCf != null ? (hWindCf * 100).toFixed(1) + "%" : "—"}</div><div className="l">Wind Avg CF</div></div>
          <div className="kp" style={{ opacity: genFilter === "wind" ? 0.4 : 1 }}><div className="n">{summary?.pv_capacity_factor != null ? Math.round(summary.pv_capacity_factor * 8760) + " MWh/MW" : "—"}</div><div className="l">Annual Solar Yield</div></div>
          <div className="kp" style={{ opacity: genFilter === "solar" ? 0.4 : 1 }}><div className="n">{summary?.wind_capacity_factor != null ? Math.round(summary.wind_capacity_factor * 8760) + " MWh/MW" : "—"}</div><div className="l">Annual Wind Yield</div></div>
        </div>
        <div className="chartbox" style={{ marginTop: 14 }}>
          <div className="clab">
            <span>Renewable generation intensity — {horizon === "year" ? "hourly weather profile across 1 year" : getHorizonLabel()}</span>
            <span>Output %</span>
          </div>
          {chartPv.length || chartWind.length ? (
            <svg className="chart" viewBox="0 0 620 170" preserveAspectRatio="none">
              {(genFilter === "all" || genFilter === "wind") && chartWind.length > 0 && (
                <path d={linePath(chartWind.map(v => v * 100), 620, 170, 105)} fill="none" stroke="#2f8f5b" strokeWidth={genFilter === "wind" ? "1.8" : "1.2"} opacity="0.85" />
              )}
              {(genFilter === "all" || genFilter === "solar") && chartPv.length > 0 && (
                <path d={linePath(chartPv.map(v => v * 100), 620, 170, 105)} fill="none" stroke="#e0922f" strokeWidth={genFilter === "solar" ? "1.8" : "1.5"} />
              )}
            </svg>
          ) : <div className="subtle" style={{ padding: 20 }}><span className="spinner" /> loading weather series…</div>}
          <div className="legend">
            {(genFilter === "all" || genFilter === "solar") && <span><i className="dot" style={{ background: "#e0922f" }} />Solar PV Output (% of nameplate)</span>}
            {(genFilter === "all" || genFilter === "wind") && chartWind.length > 0 && <span><i className="dot" style={{ background: "#2f8f5b" }} />Wind Output (% of nameplate)</span>}
          </div>
        </div>
      </div>

      <Nav go={go} step={step} nextLabel="Continue → Strategy & Goals" />
    </>
  );
}
