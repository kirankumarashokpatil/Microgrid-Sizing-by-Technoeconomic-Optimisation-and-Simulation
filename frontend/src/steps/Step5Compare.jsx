// Step 5 — Scenario comparison. Derives objective rows (knee / min-GCmin /
// min-CAPEX / max-SSR) from the real swept points and flags any design that
// fails the SSR covenant. No synthetic numbers — all rows come from the sweep.
import { Nav, NeedRun } from "./shared.jsx";
import { fmt } from "../lib/svg.js";

export function Step5Compare({ cfg, result, step, go }) {
  const eco = result?.economics;
  if (!eco) return <NeedRun go={go} ranInfeasible={!!result} />;
  const pts = eco.points || [];
  const target = cfg.ssrTarget;

  const pick = (arr, better) => arr.reduce((b, p) => (better(p, b) ? p : b), arr[0]);
  const meets = pts.filter((p) => +p.ssr_pct >= target - 0.05);
  const rows = [
    { name: "Recommended (knee)", p: eco.recommended, best: true },
    { name: "Min GCmin", p: (meets.length ? pick(meets, (p, b) => +p.gc_mw < +b.gc_mw) : pick(pts, (p, b) => +p.gc_mw < +b.gc_mw)) },
    { name: "Min CAPEX", p: pick(pts, (p, b) => +p.capex_m < +b.capex_m) },
    { name: "Max SSR", p: pick(pts, (p, b) => +p.ssr_pct > +b.ssr_pct) },
  ];

  return (
    <>
      <div className="pagehead"><h1>Step 6 — Comparison</h1>
        <p>Multi-objective trade-offs across the sized configurations. Designs failing the {target}% SSR covenant are flagged.</p></div>

      <div className="grid2" style={{ marginTop: 20 }}>
        <div className="card">
          <h3>Cost vs grid connection</h3>
          <div className="hint">Each dot is a sized design — lower-left is cheaper + smaller grid. Green = meets covenant.</div>
          <TradeoffScatter pts={pts} rec={eco.recommended} target={target} />
          <div className="legend">
            <span><i className="dot" style={{ background: "#2f8f5b" }} />Meets {target}% SSR</span>
            <span><i className="dot" style={{ background: "#c2603a" }} />Below covenant</span>
            <span><i className="dot" style={{ background: "#15616d", borderRadius: "50%" }} />Recommended</span>
          </div>
        </div>
        <div className="card">
          <h3>CAPEX vs SSR</h3>
          <div className="hint">The cost of self-sufficiency — CAPEX climbs as the SSR target rises.</div>
          <CapexVsSsr pts={pts} target={target} />
        </div>
      </div>

      <div className="card">
        <h3>Objective comparison</h3>
        <div className="hint">All rows are real points from the sweep. Pushing SSR up raises CAPEX and curtailment and lowers GCmin.</div>
        <table>
          <thead><tr>
            <th>Objective</th><th>PV</th><th>BESS</th><th>GCmin</th><th>SSR</th><th>CAPEX</th><th>LCOE</th><th>Covenant</th>
          </tr></thead>
          <tbody>
            {rows.map((r, i) => {
              const p = r.p || {};
              const pass = +p.ssr_pct >= target - 0.05;
              return (
                <tr key={i} className={r.best ? "best" : (pass ? "" : "rejected")}>
                  <td><b>{r.name}</b></td>
                  <td>{fmt.mw(p.pv_mw)}</td>
                  <td>{fmt.mw(p.bess_mw)} / {fmt.mwh(p.bess_mwh)}</td>
                  <td>{fmt.mw(p.gc_mw)}</td>
                  <td style={{ color: pass ? "var(--ok)" : "var(--red)", fontWeight: 700 }}>{fmt.pct1(p.ssr_pct)}</td>
                  <td>{fmt.eurM(p.capex_m)}</td>
                  <td>{fmt.num(p.lcoe, 0)} €/MWh</td>
                  <td>{r.best ? <span className="badge teal">Optimal</span>
                    : pass ? <span className="badge">Passes</span>
                    : <span className="badge red">Fails {target}% SSR</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <Nav go={go} step={step} nextLabel="Continue → Decision Pack" />
    </>
  );
}

// Axes helper: min/max with a little padding.
function axis(vals) { const lo = Math.min(...vals), hi = Math.max(...vals); const p = (hi - lo) * 0.08 || 1; return [lo - p, hi + p]; }

// Scatter: x = GCmin (MW), y = CAPEX (€M), colour by covenant, ring the recommended.
function TradeoffScatter({ pts, rec, target }) {
  if (!pts.length) return <svg className="chart" viewBox="0 0 620 240" />;
  const W = 620, H = 240, pad = 40;
  const [gx0, gx1] = axis(pts.map((p) => +p.gc_mw));
  const [cy0, cy1] = axis(pts.map((p) => +p.capex_m));
  const X = (v) => pad + (v - gx0) / ((gx1 - gx0) || 1) * (W - pad - 12);
  const Y = (v) => (H - pad) - (v - cy0) / ((cy1 - cy0) || 1) * (H - pad - 14);
  return (
    <svg className="chart" viewBox="0 0 620 240">
      {[0, 0.25, 0.5, 0.75, 1].map((t, i) => { const y = 10 + t * (H - pad - 10); return <line key={i} x1={pad} y1={y} x2={W} y2={y} stroke="#eef2f3" />; })}
      <line x1={pad} y1={H - pad} x2={W} y2={H - pad} stroke="#d6dde0" />
      <line x1={pad} y1={10} x2={pad} y2={H - pad} stroke="#d6dde0" />
      <text x={W / 2} y={H - 6} fontSize="11" fill="#6b7780" textAnchor="middle">Grid connection GCmin (MW) →</text>
      <text x={12} y={H / 2} fontSize="11" fill="#6b7780" textAnchor="middle" transform={`rotate(-90 12 ${H / 2})`}>CAPEX (€M) →</text>
      {pts.map((p, i) => (
        <circle key={i} cx={X(+p.gc_mw)} cy={Y(+p.capex_m)} r="5"
                fill={+p.ssr_pct >= target - 0.05 ? "#2f8f5b" : "#c2603a"} opacity="0.75" />
      ))}
      {rec && <circle cx={X(+rec.gc_mw)} cy={Y(+rec.capex_m)} r="8" fill="none" stroke="#15616d" strokeWidth="2.5" />}
    </svg>
  );
}

// Line: x = SSR %, y = CAPEX €M, with the covenant marked.
function CapexVsSsr({ pts, target }) {
  if (!pts.length) return <svg className="chart" viewBox="0 0 620 240" />;
  const W = 620, H = 240, pad = 40;
  const sorted = [...pts].sort((a, b) => +a.ssr_pct - +b.ssr_pct);
  const [sx0, sx1] = axis(sorted.map((p) => +p.ssr_pct));
  const [cy0, cy1] = axis(sorted.map((p) => +p.capex_m));
  const X = (v) => pad + (v - sx0) / ((sx1 - sx0) || 1) * (W - pad - 12);
  const Y = (v) => (H - pad) - (v - cy0) / ((cy1 - cy0) || 1) * (H - pad - 14);
  const d = sorted.map((p, i) => `${i ? "L" : "M"}${X(+p.ssr_pct).toFixed(1)} ${Y(+p.capex_m).toFixed(1)}`).join(" ");
  const tx = X(target);
  return (
    <svg className="chart" viewBox="0 0 620 240">
      <line x1={pad} y1={H - pad} x2={W} y2={H - pad} stroke="#d6dde0" />
      <line x1={pad} y1={10} x2={pad} y2={H - pad} stroke="#d6dde0" />
      <text x={W / 2} y={H - 6} fontSize="11" fill="#6b7780" textAnchor="middle">SSR % →</text>
      <text x={12} y={H / 2} fontSize="11" fill="#6b7780" textAnchor="middle" transform={`rotate(-90 12 ${H / 2})`}>CAPEX (€M) →</text>
      {target != null && tx >= pad && tx <= W && <>
        <line x1={tx} y1={10} x2={tx} y2={H - pad} stroke="#c2603a" strokeWidth="1.2" strokeDasharray="5 4" />
        <text x={tx - 4} y={20} fontSize="10" fill="#c2603a" textAnchor="end">covenant {target}%</text>
      </>}
      <path d={d} fill="none" stroke="#e0922f" strokeWidth="2" />
      {sorted.map((p, i) => <circle key={i} cx={X(+p.ssr_pct)} cy={Y(+p.capex_m)} r="3.5" fill="#e0922f" />)}
    </svg>
  );
}
