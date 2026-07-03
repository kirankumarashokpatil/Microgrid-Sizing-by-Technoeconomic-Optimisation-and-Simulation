// Step 6 — Decision pack. IC/lender-ready output for the recommended design:
// KPI tiles, economics, CAPEX breakdown, the real dispatch chart from the
// forward-eval flows, and value vs a 100%-grid baseline.
import { useState } from "react";
import { NeedRun, DispatchPolicyBadge, dispatchPolicy, DISPATCH_LABELS } from "./shared.jsx";
import { column, exportXlsx } from "../lib/api.js";
import { linePath, areaPath, downsample, fmt } from "../lib/svg.js";
import { energyTotals, monthlyDispatch, socSeries, PALETTE } from "../lib/charts.js";

const TOPO_MAP = { btm: "grid_connected_btm", backup: "bess_load_only", off_grid: "off_grid", standalone: "standalone_gen" };

export function Step6Decision({ cfg, profile, result, flows, step, go }) {
  const eco = result?.economics;
  if (!eco) return <NeedRun go={go} ranInfeasible={!!result} />;
  const rec = eco.recommended, vg = eco.vs_grid_default;
  const curt = result?.kpis?.["OSR / Curtailment (%)"];
  const [exporting, setExporting] = useState(false);

  async function downloadExcel() {
    setExporting(true);
    try {
      const loadPeak = (cfg?.loads || []).reduce((s, l) => s + (+l.peak_mw || 0), 0);
      const pol = dispatchPolicy(cfg);
      await exportXlsx({
        project_name: cfg?.projectName || "DIP Project",
        scenario_id: result.id, scenario_name: result.name,
        recommended: rec, vs_grid_default: vg, points: eco.points || [],
        kpis: result.kpis || {},
        assumptions: {
          ...(cfg?.econ || {}),
          "Dispatch merit order": pol.order.map((a, i) => `${i + 1}. ${DISPATCH_LABELS[a]}`).join("  |  "),
          "Grid-charging": pol.allowGridCharge == null ? "auto (from objective)" : (pol.allowGridCharge ? "on" : "off"),
        },
        // inputs so the backend can re-derive each point's per-slot dispatch
        profile_path: profile?.profile_path || null,
        load_peak_mw: loadPeak > 0 ? loadPeak : null,
        pv_mw: rec.pv_mw, wind_mw: rec.wind_mw,
        site_topology: TOPO_MAP[cfg?.topology] || "grid_connected_btm",
        include_timeseries: true,
        dispatch_priority: pol.custom ? pol.order : [],
        allow_grid_charge: pol.allowGridCharge,
      });
    } catch (e) { alert("Export failed: " + (e.message || e)); }
    finally { setExporting(false); }
  }

  return (
    <>
      <div className="pagehead"><h1>Step 7 — Decision Pack</h1>
        <p>Investment-committee output for the recommended design. Every number traces to the engine run and the Step 3 assumptions.</p></div>

      <div className="tiles">
        <Tile cls="t-scr" k="SCR · Self-consumption" v={fmt.pct(rec.scr_pct)} s="of generation used on site" />
        <Tile cls="t-ssr" k="SSR · Self-sufficiency" v={fmt.pct(rec.ssr_pct)} s="of demand met behind the meter" />
        <Tile cls="t-gc" k="GCmin · Min Grid Connection" v={fmt.mw(rec.gc_mw)} s="residual output" />
        <Tile cls="t-curt" k="Curtailment" v={curt != null ? fmt.pct(curt) : "—"} s="generation spilled" />
      </div>

      <DispatchPolicyBadge cfg={cfg} />

      {flows && <>
        <DispatchChart flows={flows} />
        <div className="grid2">
          <EnergyReconciliation flows={flows} />
          <SocChart flows={flows} />
        </div>
        <MonthlyDispatch flows={flows} />
      </>}

      <div className="row" style={{ marginTop: 20 }}>
        <div className="card" style={{ marginTop: 0, flex: 1 }}>
          <h3>Economics</h3>
          <div className="summary" style={{ gridTemplateColumns: "1fr 1fr" }}>
            <div className="it"><div className="l">Total CAPEX</div><div className="v">{fmt.eurM(rec.capex_m)}</div></div>
            <div className="it"><div className="l">Annual OPEX</div><div className="v">{fmt.eurM1(rec.opex_m_yr)}/yr</div></div>
            <div className="it"><div className="l">LCOE</div><div className="v">{fmt.num(rec.lcoe, 0)} €/MWh</div></div>
            <div className="it"><div className="l">NPV of costs</div><div className="v">{fmt.eurM(rec.npv_cost_m)}</div></div>
          </div>
        </div>
        <div className="card" style={{ marginTop: 0, flex: 1 }}>
          <h3>CAPEX by asset</h3>
          <CapexBars rec={rec} />
        </div>
      </div>

      <div className="card">
        <h3>Commercial baseline — vs 100% grid-default</h3>
        <div className="hint">Value created against a fully grid-dependent design (import sized to peak load). IRR assumes a flat annual saving.</div>
        <div className="summary">
          <div className="it"><div className="l">Grid connection ask</div>
            <div className="v" style={{ color: "var(--ok)" }}>{fmt.mw(rec.gc_mw)} <span className="subtle">vs {fmt.mw(vg.baseline_grid_conn_mw)}</span></div></div>
          <div className="it"><div className="l">Annual savings</div><div className="v">{fmt.eurM1(vg.annual_savings_m)}/yr</div></div>
          <div className="it"><div className="l">Avoided grid reinforcement</div><div className="v">{fmt.eurM1(vg.avoided_grid_conn_m)}</div></div>
          <div className="it"><div className="l">Simple payback</div><div className="v">{vg.payback_years != null ? `${vg.payback_years} yr` : "—"}</div></div>
          <div className="it"><div className="l">IRR (flat-benefit)</div><div className="v" style={{ color: "var(--ok)" }}>{vg.irr_pct != null ? `${vg.irr_pct}%` : "—"}</div></div>
          <div className="it"><div className="l">NPV of savings</div><div className="v">{fmt.eurM1(vg.npv_savings_m)}</div></div>
        </div>
      </div>

      <div className="navbtns">
        <button className="btn" onClick={() => go(step - 1)}>Back</button>
        <div style={{ display: "flex", gap: 10 }}>
          <button className="btn" onClick={() => window.print()}>⤓ IC pack (print)</button>
          <button className="btn primary" onClick={downloadExcel} disabled={exporting}>
            {exporting ? "Building workbook…" : "⤓ Download Excel (.xlsx)"}
          </button>
        </div>
      </div>
    </>
  );
}

function Tile({ cls, k, v, s }) {
  return <div className={"tile " + cls}><div className="k">{k}</div><div className="v">{v}</div><div className="s">{s}</div></div>;
}

function CapexBars({ rec }) {
  const items = [
    ["PV", rec.capex_pv_m, "#e0922f"],
    ...(rec.capex_wind_m > 0 ? [["Wind", rec.capex_wind_m, "#1f8a8a"]] : []),
    ["BESS MW", rec.capex_bess_mw_m, "#2f8f5b"],
    ["BESS MWh", rec.capex_bess_mwh_m, "#15616d"], ["Grid", rec.capex_grid_m, "#7a6f9b"],
  ];
  const max = Math.max(...items.map((i) => +i[1] || 0), 1);
  const W = 300, n = items.length, slot = (W - 20) / n, bw = Math.min(46, slot - 10);
  return (
    <svg className="chart" viewBox="0 0 300 150">
      {items.map((it, i) => {
        const bh = (+it[1] || 0) / max * 110, x = 12 + i * slot + (slot - bw) / 2, y = 130 - bh;
        return (
          <g key={i}>
            <rect x={x} y={y} width={bw} height={bh} rx="4" fill={it[2]} />
            <text x={x + bw / 2} y="145" fontSize="11" fill="#6b7780" textAnchor="middle">{it[0]}</text>
            <text x={x + bw / 2} y={y - 5} fontSize="10.5" fontWeight="700" fill="#2f3a41" textAnchor="middle">€{(+it[1] || 0).toFixed(0)}M</text>
          </g>
        );
      })}
    </svg>
  );
}

// Stacked dispatch from real forward-eval flows: direct-to-load, BESS discharge,
// grid import — summing to demand by energy balance each timestep.
function DispatchChart({ flows }) {
  const load = downsample(column(flows, "load_mw").map(Number), 300);
  const grid = downsample(column(flows, "grid_import_mw").map(Number), 300);
  const disc = downsample(column(flows, "bess_discharge_mw").map(Number), 300);
  if (!load.length) return null;
  const W = 620, H = 200;
  const direct = load.map((l, i) => Math.max(0, l - (grid[i] || 0) - (disc[i] || 0)));
  const stack2 = direct.map((d, i) => d + (disc[i] || 0));            // direct + discharge
  const max = Math.max(...load) * 1.1 || 1;
  return (
    <div className="card">
      <h3>Energy dispatch — full year (downsampled)</h3>
      <div className="hint">How demand is met each timestep, from the recommended design's real dispatch: direct generation → BESS discharge → grid residual.</div>
      <svg className="chart" viewBox="0 0 620 200" preserveAspectRatio="none">
        <path d={areaPath(load, W, H, max)} fill="rgba(194,96,58,.30)" />
        <path d={areaPath(stack2, W, H, max)} fill="rgba(47,143,91,.45)" />
        <path d={areaPath(direct, W, H, max)} fill="rgba(224,146,47,.55)" />
        <path d={linePath(load, W, H, max)} fill="none" stroke="#3f7cac" strokeWidth="1.5" strokeDasharray="5 3" />
      </svg>
      <div className="legend">
        <span><i className="dot" style={{ background: "#e0922f" }} />Direct to consumer</span>
        <span><i className="dot" style={{ background: "#2f8f5b" }} />BESS discharge</span>
        <span><i className="dot" style={{ background: "#c2603a" }} />Grid import</span>
        <span><i className="dot" style={{ background: "#3f7cac" }} />Demand</span>
      </div>
    </div>
  );
}

// Horizontal stacked bar helper.
function HBar({ segs, total }) {
  const W = 620, H = 26, sum = total || segs.reduce((s, x) => s + x.v, 0) || 1;
  let x = 0;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} style={{ display: "block", marginTop: 6 }}>
      {segs.map((s, i) => {
        const w = (s.v / sum) * W; const el = <rect key={i} x={x} y={4} width={Math.max(0, w - 1)} height={H - 8} rx="2" fill={s.color} />;
        x += w; return el;
      })}
    </svg>
  );
}

// Annual energy reconciliation — where demand is met, and where generation goes.
function EnergyReconciliation({ flows }) {
  const t = energyTotals(flows, 1);
  const d = t.demand, g = t.gen;
  const Legend = ({ items }) => (
    <div className="legend" style={{ marginTop: 6 }}>
      {items.map(([l, c, v]) => <span key={l}><i className="dot" style={{ background: c }} />{l} {fmt.gwh(v)}</span>)}
    </div>
  );
  return (
    <div className="card" style={{ marginTop: 0 }}>
      <h3>Annual energy reconciliation</h3>
      <div className="hint">Every MWh accounted for — no static averages.</div>
      <div style={{ fontSize: 12, fontWeight: 600, color: "var(--grey)" }}>Demand met — {fmt.gwh(d.total)}</div>
      <HBar total={d.total} segs={[{ v: d.direct, color: PALETTE.pv }, { v: d.bess, color: PALETTE.bess }, { v: d.grid, color: PALETTE.grid }, { v: d.unmet, color: PALETTE.unmet }]} />
      <Legend items={[["Direct", PALETTE.pv, d.direct], ["BESS", PALETTE.bess, d.bess], ["Grid", PALETTE.grid, d.grid], ...(d.unmet > 0 ? [["Unmet", PALETTE.unmet, d.unmet]] : [])]} />
      <div style={{ fontSize: 12, fontWeight: 600, color: "var(--grey)", marginTop: 12 }}>Generation used — {fmt.gwh(g.avail)}</div>
      <HBar total={g.avail} segs={[{ v: g.used, color: PALETTE.pv }, { v: g.curtail, color: PALETTE.curtail }]} />
      <Legend items={[["Used on site", PALETTE.pv, g.used], ["Curtailed", PALETTE.curtail, g.curtail]]} />
    </div>
  );
}

// BESS state-of-charge across the year.
function SocChart({ flows }) {
  const soc = socSeries(flows);
  const max = soc.length ? Math.max(...soc, 1) * 1.1 : 1;
  return (
    <div className="card" style={{ marginTop: 0 }}>
      <h3>BESS state of charge — full year</h3>
      <div className="hint">How the battery cycles across the year (MWh stored).</div>
      <svg className="chart" viewBox="0 0 620 150" preserveAspectRatio="none">
        <path d={areaPath(soc, 620, 150, max)} fill="rgba(47,143,91,.18)" />
        <path d={linePath(soc, 620, 150, max)} fill="none" stroke={PALETTE.soc} strokeWidth="1.5" />
      </svg>
      <div className="legend"><span><i className="dot" style={{ background: PALETTE.soc }} />SOC (MWh)</span></div>
    </div>
  );
}

// Monthly stacked dispatch — direct / BESS / grid contribution to load.
function MonthlyDispatch({ flows }) {
  const m = monthlyDispatch(flows, 1);
  const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const W = 620, H = 190, pad = 24, bw = (W - pad) / 12 - 8;
  const max = Math.max(...m.map((x) => x.direct + x.bess + x.grid), 1) * 1.05;
  const Y = (v) => (H - pad) - (v / max) * (H - pad - 10);
  return (
    <div className="card">
      <h3>Monthly dispatch mix</h3>
      <div className="hint">How demand is met each month — seasonal shift from PV toward grid/BESS.</div>
      <svg className="chart" viewBox="0 0 620 190">
        <line x1={pad} y1={H - pad} x2={W} y2={H - pad} stroke="#d6dde0" />
        {m.map((x, i) => {
          const bx = pad + i * ((W - pad) / 12) + 4;
          const segs = [[x.direct, PALETTE.pv], [x.bess, PALETTE.bess], [x.grid, PALETTE.grid]];
          let yTop = H - pad;
          return (
            <g key={i}>
              {segs.map(([v, c], k) => { const h = (H - pad) - Y(v); yTop -= h; return <rect key={k} x={bx} y={yTop} width={bw} height={h} fill={c} />; })}
              <text x={bx + bw / 2} y={H - pad + 12} fontSize="9" fill="#6b7780" textAnchor="middle">{names[i]}</text>
            </g>
          );
        })}
      </svg>
      <div className="legend">
        <span><i className="dot" style={{ background: PALETTE.pv }} />Direct</span>
        <span><i className="dot" style={{ background: PALETTE.bess }} />BESS</span>
        <span><i className="dot" style={{ background: PALETTE.grid }} />Grid</span>
      </div>
    </div>
  );
}
