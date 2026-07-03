// SplitOptimiser — Phase 3 (sweep): "the tool decides the split". Calls
// POST /optimise-split (via runOptimiseSplit), which sweeps solar across the
// available land, sizes BESS for the SSR target per point, costs each, and
// returns the cost-optimal split + the frontier. Shows the frontier as a
// PV → lifetime-cost curve with the recommendation marked.
import { useState } from "react";
import { runOptimiseSplit } from "../lib/api.js";
import { fmt } from "../lib/svg.js";

export function SplitOptimiser({ cfg, profile }) {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  // Land available to generation = solar + wind + unassigned "land" parcels.
  const parcels = cfg.parcels?.list || [];
  const genLand =
    parcels.filter((p) => ["solar", "wind", "land"].includes(p.tech)).reduce((s, p) => s + (p.areaHa || 0), 0) ||
    ((cfg.parcels?.solar?.areaHa || 0) + (cfg.parcels?.wind?.areaHa || 0)) || 184;
  const peakLoad = (cfg.loads || []).reduce((s, l) => s + (+l.peak_mw || 0), 0);
  const windMw = cfg.tech?.wind ? (cfg.parcels?.wind?.maxMw ?? 0) : 0;
  const targetSsr = cfg.ssrTarget ?? 90;

  async function run() {
    setBusy(true); setErr(null);
    try {
      setData(await runOptimiseSplit({
        profile_path: profile?.profile_path,
        load_peak_mw: peakLoad > 0 ? peakLoad : undefined,
        available_land_ha: genLand,
        target_ssr_pct: targetSsr,
        wind_mw: windMw,
        steps: 8,
        ...cfg.econ,
      }));
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  const rec = data?.recommended;

  return (
    <div className="card" style={{ borderColor: "var(--teal)", background: "var(--teal-light)" }}>
      <h3 style={{ color: "var(--teal-dark)" }}>
        Optimise the split — the tool decides
        <span style={{ marginLeft: 8, fontSize: 10.5, fontWeight: 700, letterSpacing: ".04em",
          textTransform: "uppercase", color: "#15616d", background: "#fff",
          border: "1px solid #bcd6d9", borderRadius: 6, padding: "2px 7px", verticalAlign: "middle" }}>
          Land-first · Phase 3
        </span>
      </h3>
      <div className="hint" style={{ color: "var(--teal-dark)", opacity: 0.85 }}>
        Sweeps solar across your <b>{Math.round(genLand)} ha</b> of generation land, sizes the battery to hit
        <b> {targetSsr}% SSR</b> at each point, and picks the <b>cheapest</b> design — using only as much land as it needs.
      </div>

      <button className="btn primary" disabled={busy} onClick={run} style={{ marginTop: 4 }}>
        {busy ? "Sweeping the land…" : "▶ Optimise the split"}
      </button>
      {busy && <span className="subtle" style={{ marginLeft: 10 }}>a full-year solve per point — up to ~a minute</span>}

      {err && <div className="banner err" style={{ marginTop: 12 }}>Engine error: {err}</div>}

      {rec && (
        <>
          <div style={{ display: "flex", gap: 20, flexWrap: "wrap", marginTop: 16 }}>
            <div style={{ flex: "1 1 260px", minWidth: 240 }}>
              <div style={{ background: "#fff", border: "1px solid var(--line)", borderRadius: 12,
                borderTop: "3px solid #2f8f5b", padding: 16 }}>
                <div style={{ fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase", letterSpacing: ".04em" }}>
                  Cost-optimal split
                </div>
                <div style={{ fontSize: 20, fontWeight: 800, color: "#15616d", margin: "4px 0 12px" }}>
                  {fmt.mw(rec.pv_mw)} solar · {fmt.mw(rec.bess_mw)} / {fmt.mwh(rec.bess_mwh)}
                </div>
                <KV k="Solar land used" v={`${rec.pv_land_ha} ha of ${Math.round(genLand)}`} />
                <KV k="Grid connection" v={fmt.mw(rec.grid_mw)} />
                <KV k="Self-sufficiency" v={fmt.pct1(rec.ssr_pct)} />
                <KV k="CAPEX" v={`€${rec.capex_m}M`} />
                <KV k="Lifetime cost" v={`€${rec.lifetime_cost_m}M`} strong />
              </div>
            </div>
            <div style={{ flex: "2 1 340px", minWidth: 300 }}>
              <div className="clab" style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: "#64748b", marginBottom: 4 }}>
                <span>Cost frontier — solar allocation vs lifetime cost</span><span>€M</span>
              </div>
              <Frontier frontier={data.frontier} rec={rec} ceiling={data.pv_ceiling_mw} />
              <div style={{ fontSize: 11.5, color: "#64748b", marginTop: 4 }}>
                Too little solar → costly battery; too much → wasted panels. The dip is the optimum.
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function KV({ k, v, strong }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, fontSize: 13, padding: "3px 0" }}>
      <span style={{ color: "#64748b" }}>{k}</span>
      <span style={{ fontWeight: strong ? 800 : 600, color: strong ? "#15616d" : "#1e293b",
        fontVariantNumeric: "tabular-nums" }}>{v}</span>
    </div>
  );
}

// Simple frontier chart: x = solar MW, y = lifetime cost (€M), recommended marked.
function Frontier({ frontier, rec }) {
  const W = 380, H = 190, pad = 34;
  if (!frontier || frontier.length === 0) return <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%" }} />;
  const xs = frontier.map((f) => f.pv_mw);
  const ys = frontier.map((f) => f.lifetime_cost_m);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMin = Math.min(...ys), yMax = Math.max(...ys);
  const X = (x) => pad + (xMax > xMin ? (x - xMin) / (xMax - xMin) : 0.5) * (W - pad - 12);
  const Y = (y) => (H - pad) - (yMax > yMin ? (y - yMin) / (yMax - yMin) : 0.5) * (H - pad - 14);
  const line = frontier.map((f, i) => `${i ? "L" : "M"}${X(f.pv_mw).toFixed(1)} ${Y(f.lifetime_cost_m).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", background: "#fff", border: "1px solid var(--line)", borderRadius: 10 }}>
      {[0, 0.5, 1].map((t, i) => { const y = 10 + t * (H - pad - 10); return <line key={i} x1={pad} y1={y} x2={W - 8} y2={y} stroke="#eef2f3" />; })}
      <line x1={pad} y1={H - pad} x2={W - 8} y2={H - pad} stroke="#d6dde0" />
      <line x1={pad} y1={10} x2={pad} y2={H - pad} stroke="#d6dde0" />
      <path d={line} fill="none" stroke="#3f7cac" strokeWidth="1.8" />
      {frontier.map((f, i) => (
        <circle key={i} cx={X(f.pv_mw)} cy={Y(f.lifetime_cost_m)} r={f === rec ? 6 : 3.5}
          fill={f === rec ? "#2f8f5b" : "#cdd6da"} stroke={f === rec ? "#fff" : "none"} strokeWidth="2">
          <title>{`Solar ${fmt.mw(f.pv_mw)} · BESS ${fmt.mw(f.bess_mw)}/${fmt.mwh(f.bess_mwh)} · €${f.lifetime_cost_m}M`}</title>
        </circle>
      ))}
      {rec && <text x={X(rec.pv_mw)} y={Y(rec.lifetime_cost_m) - 10} fontSize="10" fontWeight="700" fill="#2f8f5b" textAnchor="middle">optimum</text>}
      <text x={(W + pad) / 2} y={H - 4} fontSize="10" fill="#6b7780" textAnchor="middle">Solar (MW) →</text>
    </svg>
  );
}
