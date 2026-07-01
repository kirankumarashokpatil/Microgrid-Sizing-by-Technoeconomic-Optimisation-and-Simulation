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
