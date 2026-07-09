// Step 5 — Scenario comparison. Derives objective rows (recommended / min-GCmin /
// min-BESS / max-SSR) from the real swept points and flags any design that
// fails the SSR covenant. No synthetic numbers — all rows come from the sweep.
import { Nav, NeedRun, DispatchPolicyBadge, StaleBanner, EmptyChart } from "./shared.jsx";
import { fmt } from "../lib/svg.js";
import ClientShowcaseMap from "./ClientShowcaseMap.jsx";

export function Step5Compare({ cfg, result, step, go, stale }) {
  if (result && !result.feasible) return <NeedRun go={go} ranInfeasible />;
  const rawPts = result?.table || [];
  if (!rawPts.length && !result?.design) return <NeedRun go={go} />;
  const pts = rawPts.map(p => ({
    ...p,
    ssr_pct: p.ssr_pct ?? p["Achieved SSR (%)"] ?? p["Operational SSR (%)"] ?? 0,
    gc_mw: p.gc_mw ?? p["Achieved Peak Grid Import (MW)"] ?? p["Operational Peak Grid (MW)"] ?? 0,
    pv_mw: p.pv_mw ?? p["PV Nameplate (MW)"] ?? 0,
    bess_mw: p.bess_mw ?? p["BESS Power (MW)"] ?? 0,
    bess_mwh: p.bess_mwh ?? p["BESS Energy (MWh)"] ?? 0,
    duration_h: p.duration_h ?? p["BESS Duration (h)"] ?? (p.bess_mw > 0 ? p.bess_mwh / p.bess_mw : 0),
  }));
  const rawRec = result?.design || {};
  // A curve/surface run has an empty design ({}); fall back to swept points below
  // rather than fabricating an all-zeros "recommended" row.
  const rec = Object.keys(rawRec).length ? {
    ...rawRec,
    ssr_pct: rawRec.ssr_pct ?? rawRec["Achieved SSR (%)"] ?? rawRec["Operational SSR (%)"] ?? result?.kpis?.["SSR (%)"] ?? 0,
    gc_mw: rawRec.gc_mw ?? rawRec["Achieved Peak Grid Import (MW)"] ?? rawRec["Operational Peak Grid (MW)"] ?? result?.kpis?.["GCmin Peak (MW)"] ?? 0,
    pv_mw: rawRec.pv_mw ?? rawRec["PV Nameplate (MW)"] ?? 0,
    bess_mw: rawRec.bess_mw ?? rawRec["BESS Power (MW)"] ?? 0,
    bess_mwh: rawRec.bess_mwh ?? rawRec["BESS Energy (MWh)"] ?? 0,
    duration_h: rawRec.duration_h ?? rawRec["BESS Duration (h)"] ?? (rawRec.bess_mw > 0 ? rawRec.bess_mwh / rawRec.bess_mw : 0),
  } : null;
  const target = cfg.ssrTarget;

  const pick = (arr, better) => arr.reduce((b, p) => (better(p, b) ? p : b), arr[0]);
  const meets = target != null ? pts.filter((p) => +p.ssr_pct >= target - 0.05) : pts;
  const rows = [
    { name: "Recommended design", p: rec || (pts.length ? pts[0] : {}), best: true },
    { name: "Min GCmin", p: (meets.length ? pick(meets, (p, b) => +p.gc_mw < +b.gc_mw) : pick(pts, (p, b) => +p.gc_mw < +b.gc_mw)) },
    { name: "Min BESS Volume", p: (meets.length ? pick(meets, (p, b) => +p.bess_mwh < +b.bess_mwh) : pick(pts, (p, b) => +p.bess_mwh < +b.bess_mwh)) },
    { name: "Max SSR", p: pick(pts, (p, b) => +p.ssr_pct > +b.ssr_pct) },
  ];

  return (
    <>
      <div className="pagehead"><h1>Step 5 — Design Comparison &amp; Performance Analysis</h1>
        <p>Compare technical trade-offs across different equipment sizing configurations. Designs that do not meet your {target ?? 90}% green energy target are flagged.</p></div>

      <StaleBanner stale={stale} go={go} />

      <ClientShowcaseMap cfg={cfg} rec={rec} result={result} />

      <div className="grid2" style={{ marginTop: 20 }}>
        <div className="card">
          <h3>Storage Volume vs. Peak Grid Import</h3>
          <div className="hint">Each dot is a simulated design — lower-left requires a smaller battery and less grid connection. Green = meets green energy target.</div>
          <TradeoffScatter pts={pts} rec={rec} target={target} />
          <div className="legend">
            <span><i className="dot" style={{ background: "#2f8f5b" }} />Meets target</span>
            <span><i className="dot" style={{ background: "#c2603a" }} />Below target</span>
            <span><i className="dot" style={{ background: "#15616d", borderRadius: "50%" }} />Recommended</span>
          </div>
        </div>
        <div className="card">
          <h3>Storage Volume vs. Green Energy (SSR)</h3>
          <div className="hint">The physical storage requirement — battery capacity (MWh) climbs exponentially as the green energy target rises.</div>
          <VolumeVsSsr pts={pts} target={target} />
        </div>
      </div>

      <div className="card">
        <h3>Design Options Overview</h3>
        <div className="hint">Each row represents a simulated design option. Increasing your green energy target (SSR) requires larger battery storage capacity (MWh) and reduces peak grid dependency.</div>
        <table>
          <thead><tr>
            <th>Strategy / Goal</th><th>Solar PV</th><th>Battery Storage</th><th>Duration</th><th>Peak Grid Import</th><th>Green Energy / SSR</th><th>Target Status</th>
          </tr></thead>
          <tbody>
            {rows.map((r, i) => {
              const p = r.p || {};
              const pass = target == null || +p.ssr_pct >= target - 0.05;
              const dur = p.duration_h ? +p.duration_h : (p.bess_mw > 0 ? +p.bess_mwh / +p.bess_mw : 0);
              return (
                <tr key={i} className={r.best ? "best" : (pass ? "" : "rejected")}>
                  <td><b>{r.name}</b></td>
                  <td>{fmt.mw(p.pv_mw)}</td>
                  <td>{fmt.mw(p.bess_mw)} / {fmt.mwh(p.bess_mwh)}</td>
                  <td>{dur > 0 ? `${dur.toFixed(1)} hrs` : "—"}</td>
                  <td>{fmt.mw(p.gc_mw)}</td>
                  <td style={{ color: pass ? "var(--ok)" : "var(--red)", fontWeight: 700 }}>{fmt.pct1(p.ssr_pct)}</td>
                  <td>{r.best ? <span className="badge teal">Recommended</span>
                    : pass ? <span className="badge">Passes</span>
                    : <span className="badge red">Below {target}%</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h3>All Simulated Configurations</h3>
        <div className="hint">All {pts.length} simulated configurations, ordered by green energy self-sufficiency (SSR). The recommended design is highlighted; rows below the {target ?? 90}% target are flagged.</div>
        <table>
          <thead><tr>
            <th>#</th><th>Solar PV</th><th>Battery Storage</th><th>Duration</th><th>Peak Grid Import</th><th>Green Energy / SSR</th><th>Target Status</th>
          </tr></thead>
          <tbody>
            {[...pts].sort((a, b) => +a.ssr_pct - +b.ssr_pct).map((p, i) => {
              const pass = target == null || +p.ssr_pct >= target - 0.05;
              const isRec = rec && +p.ssr_pct === +rec.ssr_pct && +p.gc_mw === +rec.gc_mw;
              const dur = p.duration_h ? +p.duration_h : (p.bess_mw > 0 ? +p.bess_mwh / +p.bess_mw : 0);
              return (
                <tr key={i} className={isRec ? "best" : (pass ? "" : "rejected")}>
                  <td>{i + 1}{isRec ? " ★" : ""}</td>
                  <td>{fmt.mw(p.pv_mw)}</td>
                  <td>{fmt.mw(p.bess_mw)} / {fmt.mwh(p.bess_mwh)}</td>
                  <td>{dur > 0 ? `${dur.toFixed(1)} hrs` : "—"}</td>
                  <td>{fmt.mw(p.gc_mw)}</td>
                  <td style={{ color: pass ? "var(--ok)" : "var(--red)", fontWeight: 700 }}>{fmt.pct1(p.ssr_pct)}</td>
                  <td>{isRec ? <span className="badge teal">Recommended</span>
                    : pass ? <span className="badge">Passes</span>
                    : <span className="badge red">Below {target ?? 90}%</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <DispatchPolicyBadge cfg={cfg} />
      <Nav go={go} step={step} nextLabel="Continue → Decision Pack" />
    </>
  );
}

// Axes helper: min/max with a little padding.
function axis(vals) { const lo = Math.min(...vals), hi = Math.max(...vals); const p = (hi - lo) * 0.08 || 1; return [lo - p, hi + p]; }

// Scatter: x = GCmin (MW), y = BESS MWh, colour by covenant, ring the recommended.
function TradeoffScatter({ pts, rec, target }) {
  if (!pts.length) return <EmptyChart h={240} msg="No swept designs to compare. This is a single-point run — uncheck the fixed SSR target in Step 3 to sweep a full range." />;
  const W = 620, H = 240, pad = 40;
  const [gx0, gx1] = axis(pts.map((p) => +p.gc_mw));
  const [cy0, cy1] = axis(pts.map((p) => +p.bess_mwh));
  const X = (v) => pad + (v - gx0) / ((gx1 - gx0) || 1) * (W - pad - 12);
  const Y = (v) => (H - pad) - (v - cy0) / ((cy1 - cy0) || 1) * (H - pad - 14);
  return (
    <svg className="chart" viewBox="0 0 620 240">
      {[0, 0.25, 0.5, 0.75, 1].map((t, i) => { const y = 10 + t * (H - pad - 10); return <line key={i} x1={pad} y1={y} x2={W} y2={y} stroke="#eef2f3" />; })}
      <line x1={pad} y1={H - pad} x2={W} y2={H - pad} stroke="#d6dde0" />
      <line x1={pad} y1={10} x2={pad} y2={H - pad} stroke="#d6dde0" />
      <text x={W / 2} y={H - 6} fontSize="11" fill="#6b7780" textAnchor="middle">Peak Grid Import (MW) →</text>
      <text x={12} y={H / 2} fontSize="11" fill="#6b7780" textAnchor="middle" transform={`rotate(-90 12 ${H / 2})`}>BESS Energy (MWh) →</text>
      {pts.map((p, i) => (
        <circle key={i} cx={X(+p.gc_mw)} cy={Y(+p.bess_mwh)} r="5"
                fill={target == null || +p.ssr_pct >= target - 0.05 ? "#2f8f5b" : "#c2603a"} opacity="0.75">
          <title>{`SSR ${fmt.pct1(p.ssr_pct)} · GCmin ${fmt.mw(p.gc_mw)} · BESS ${fmt.mwh(p.bess_mwh)}\nPV ${fmt.mw(p.pv_mw)} · BESS Power ${fmt.mw(p.bess_mw)}`}</title>
        </circle>
      ))}
      {rec && <circle cx={X(+rec.gc_mw)} cy={Y(+rec.bess_mwh)} r="8" fill="none" stroke="#15616d" strokeWidth="2.5" />}
    </svg>
  );
}

// Line: x = SSR %, y = BESS MWh, with the covenant marked.
function VolumeVsSsr({ pts, target }) {
  if (!pts.length) return <EmptyChart h={240} msg="No swept designs — run a full-range analysis to see how storage scales with the green-energy target." />;
  const W = 620, H = 240, pad = 40;
  const sorted = [...pts].sort((a, b) => +a.ssr_pct - +b.ssr_pct);
  const [sx0, sx1] = axis(sorted.map((p) => +p.ssr_pct));
  const [cy0, cy1] = axis(sorted.map((p) => +p.bess_mwh));
  const X = (v) => pad + (v - sx0) / ((sx1 - sx0) || 1) * (W - pad - 12);
  const Y = (v) => (H - pad) - (v - cy0) / ((cy1 - cy0) || 1) * (H - pad - 14);
  const d = sorted.map((p, i) => `${i ? "L" : "M"}${X(+p.ssr_pct).toFixed(1)} ${Y(+p.bess_mwh).toFixed(1)}`).join(" ");
  const tx = target != null ? X(target) : -1;
  return (
    <svg className="chart" viewBox="0 0 620 240">
      <line x1={pad} y1={H - pad} x2={W} y2={H - pad} stroke="#d6dde0" />
      <line x1={pad} y1={10} x2={pad} y2={H - pad} stroke="#d6dde0" />
      <text x={W / 2} y={H - 6} fontSize="11" fill="#6b7780" textAnchor="middle">SSR % →</text>
      <text x={12} y={H / 2} fontSize="11" fill="#6b7780" textAnchor="middle" transform={`rotate(-90 12 ${H / 2})`}>BESS Energy (MWh) →</text>
      {target != null && tx >= pad && tx <= W && <>
        <line x1={tx} y1={10} x2={tx} y2={H - pad} stroke="#c2603a" strokeWidth="1.2" strokeDasharray="5 4" />
        <text x={tx - 4} y={20} fontSize="10" fill="#c2603a" textAnchor="end">covenant {target}%</text>
      </>}
      <path d={d} fill="none" stroke="#e0922f" strokeWidth="2" />
      {sorted.map((p, i) => <circle key={i} cx={X(+p.ssr_pct)} cy={Y(+p.bess_mwh)} r="3.5" fill="#e0922f">
        <title>{`SSR ${fmt.pct1(p.ssr_pct)} · BESS ${fmt.mwh(p.bess_mwh)}\nPV ${fmt.mw(p.pv_mw)} · BESS Power ${fmt.mw(p.bess_mw)}`}</title>
      </circle>)}
    </svg>
  );
}

