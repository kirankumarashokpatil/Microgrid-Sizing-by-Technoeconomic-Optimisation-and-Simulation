// SplitOptimiser — Phase 1 (sweep): "the tool decides the split". Calls
// POST /optimise-split (via runOptimiseSplit), which sweeps solar across the
// available land, sizes BESS for the SSR target per point, and returns the
// optimal physical split + the frontier. Shows the frontier as a
// PV → storage volume curve with the recommendation marked.
import { useState } from "react";
import { runOptimiseSplit } from "../lib/api.js";
import { fmt } from "../lib/svg.js";

// `data`/`setData` are lifted to App so the ~minute-long optimisation result
// survives leaving and returning to the Size step (and a page refresh).
export function SplitOptimiser({ cfg, profile, data, setData }) {
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
        Smart Land Optimization — Solar vs. Storage Balance
        <span style={{ marginLeft: 8, fontSize: 10.5, fontWeight: 700, letterSpacing: ".04em",
          textTransform: "uppercase", color: "#15616d", background: "#fff",
          border: "1px solid #bcd6d9", borderRadius: 6, padding: "2px 7px", verticalAlign: "middle" }}>
          Recommended Strategy
        </span>
      </h3>
      <div className="hint" style={{ color: "var(--teal-dark)", opacity: 0.85 }}>
        Our AI optimizer evaluates all combinations across your <b>{Math.round(genLand)} ha</b> site to find the perfect physical balance between solar panels and battery storage, minimizing your reliance on the external power grid while hitting your <b>{targetSsr}%</b> green energy target.
      </div>

      <button className="btn primary" disabled={busy} onClick={run} style={{ marginTop: 4 }}>
        {busy ? "Optimizing land & equipment…" : "▶ Run Smart Optimization"}
      </button>
      {busy && <span className="subtle" style={{ marginLeft: 10 }}>simulating 8,760 hourly weather &amp; demand variations — up to ~a minute</span>}

      {err && <div className="banner err" style={{ marginTop: 12 }}>Simulation error: {err}</div>}

      {rec && (
        <>
          <div style={{ display: "flex", gap: 20, flexWrap: "wrap", marginTop: 16 }}>
            <div style={{ flex: "1 1 260px", minWidth: 240 }}>
              <div style={{ background: "#fff", border: "1px solid var(--line)", borderRadius: 12,
                borderTop: "3px solid #2f8f5b", padding: 16 }}>
                <div style={{ fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase", letterSpacing: ".04em" }}>
                  Recommended Equipment Mix
                </div>
                <div style={{ fontSize: 20, fontWeight: 800, color: "#15616d", margin: "4px 0 12px" }}>
                  {fmt.mw(rec.pv_mw)} solar · {fmt.mw(rec.bess_mw)} / {fmt.mwh(rec.bess_mwh)}
                </div>
                <KV k="Solar land used" v={`${rec.pv_land_ha} ha of ${Math.round(genLand)}`} />
                <KV k="Peak Grid Import" v={fmt.mw(rec.gc_mw)} strong />
                <KV k="Green Energy / SSR" v={fmt.pct1(rec.ssr_pct)} />
                <KV k="Storage Duration" v={rec.duration_h ? `${rec.duration_h.toFixed(1)} hrs` : "—"} />
                <KV k="Spilled Solar" v={rec.curtailment_pct != null ? `${rec.curtailment_pct.toFixed(1)}%` : "—"} />
                <div style={{ marginTop: 10, paddingTop: 8, borderTop: "1px dashed var(--line)", fontSize: 11, color: "#64748b" }}>
                  {rec.verified ? (
                    <>
                      ✓ Verified under real-time control (no forecast).
                      {rec.ssr_gap_pp > 0.5 &&
                        <> Perfect-foresight bound {fmt.pct1(rec.lp_ssr_pct)} — a {rec.ssr_gap_pp.toFixed(1)}pp gap to what a real controller delivers.</>}
                    </>
                  ) : (
                    <>⚠ Theoretical (perfect-foresight) — not verified under real-time control for this topology.</>
                  )}
                </div>
              </div>
            </div>
            <div style={{ flex: "2 1 340px", minWidth: 300 }}>
              <div className="clab" style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: "#64748b", marginBottom: 4 }}>
                <span>Optimization Curve — Solar Allocation vs. Required Storage</span><span>MWh</span>
              </div>
              <Frontier frontier={data.frontier} rec={rec} ceiling={data.pv_ceiling_mw} />
              <div style={{ fontSize: 11.5, color: "#64748b", marginTop: 4 }}>
                Too little solar requires a massive battery; too much solar wastes land and causes excess spilling. The highlighted point is the ideal balance.
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

// Frontier chart: x = solar MW, y = required storage (BESS MWh), recommended
// marked. Single measure on y (storage) — the grid import lives in the tooltip.
function Frontier({ frontier, rec }) {
  const W = 400, H = 210, padL = 46, padB = 34, padT = 12, padR = 12;
  if (!frontier || frontier.length === 0) return <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%" }} />;
  const xs = frontier.map((f) => f.pv_mw);
  const ys = frontier.map((f) => f.bess_mwh);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMax = Math.max(...ys, 1) * 1.08, yMin = 0;   // anchor storage axis at 0
  const X = (x) => padL + (xMax > xMin ? (x - xMin) / (xMax - xMin) : 0.5) * (W - padL - padR);
  const Y = (y) => (H - padB) - (y - yMin) / ((yMax - yMin) || 1) * (H - padB - padT);
  const line = frontier.map((f, i) => `${i ? "L" : "M"}${X(f.pv_mw).toFixed(1)} ${Y(f.bess_mwh).toFixed(1)}`).join(" ");
  const yticks = Array.from({ length: 4 }, (_, i) => yMin + (i / 3) * (yMax - yMin));
  const xticks = Array.from({ length: 4 }, (_, i) => xMin + (i / 3) * (xMax - xMin));
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", background: "#fff", border: "1px solid var(--line)", borderRadius: 10 }}>
      {yticks.map((v, i) => <line key={i} x1={padL} y1={Y(v)} x2={W - padR} y2={Y(v)} stroke="#eef2f3" />)}
      <line x1={padL} y1={H - padB} x2={W - padR} y2={H - padB} stroke="#d6dde0" />
      <line x1={padL} y1={padT} x2={padL} y2={H - padB} stroke="#d6dde0" />
      {yticks.map((v, i) => <text key={`yt${i}`} x={padL - 5} y={Y(v) + 3} fontSize="9" fill="#8a949b" textAnchor="end">{Math.round(v)}</text>)}
      {xticks.map((v, i) => <text key={`xt${i}`} x={X(v)} y={H - padB + 13} fontSize="9" fill="#8a949b" textAnchor="middle">{Math.round(v)}</text>)}
      <path d={line} fill="none" stroke="#3f7cac" strokeWidth="1.8" />
      {frontier.map((f, i) => {
        // rec is a separate JSON object from the frontier items, so match on the
        // swept variable (pv_mw), not object identity (which never holds post-parse).
        const isRec = rec && f.pv_mw === rec.pv_mw;
        return (
          <circle key={i} cx={X(f.pv_mw)} cy={Y(f.bess_mwh)} r={isRec ? 6 : 3.5}
            fill={isRec ? "#2f8f5b" : "#cdd6da"} stroke={isRec ? "#fff" : "none"} strokeWidth="2">
            <title>{`Solar ${fmt.mw(f.pv_mw)} · BESS ${fmt.mw(f.bess_mw)}/${fmt.mwh(f.bess_mwh)} · GCmin ${fmt.mw(f.gc_mw)}`}</title>
          </circle>
        );
      })}
      {rec && <text x={X(rec.pv_mw)} y={Y(rec.bess_mwh) - 10} fontSize="10" fontWeight="700" fill="#2f8f5b" textAnchor="middle">optimum</text>}
      <text x={padL + (W - padL - padR) / 2} y={H - 3} fontSize="10" fill="#6b7780" textAnchor="middle">Solar (MW) →</text>
      <text x={11} y={padT + (H - padB - padT) / 2} fontSize="10" fill="#6b7780" textAnchor="middle" transform={`rotate(-90 11 ${padT + (H - padB - padT) / 2})`}>Storage (MWh)</text>
    </svg>
  );
}
