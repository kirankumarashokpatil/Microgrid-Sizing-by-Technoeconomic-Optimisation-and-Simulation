// Step 6 — Decision pack. IC/lender-ready output for the recommended design:
// KPI tiles, technical configuration, operational verification (Model R), and
// the real dispatch chart from the forward-eval flows.
import { useState, useMemo, useEffect } from "react";
import { NeedRun, DispatchPolicyBadge, StaleBanner, EmptyChart, dispatchPolicy, DISPATCH_LABELS } from "./shared.jsx";
import { column, exportXlsx, dispatchRolling, minGridConnection, sizeTradeoff } from "../lib/api.js";
import { linePath, areaPath, fmt } from "../lib/svg.js";
import { energyTotals, monthlyDispatch, socSeries, dispatchWindow, PALETTE } from "../lib/charts.js";
import ClientShowcaseMap from "./ClientShowcaseMap.jsx";

const TOPO_MAP = { btm: "grid_connected_btm", backup: "bess_load_only", off_grid: "off_grid", standalone: "standalone_gen" };

export function Step6Decision({ cfg, profile, result, flows, step, go, stale }) {
  // Hooks must run before any early return (Rules of Hooks) — a curve run makes
  // rec null below and bails to NeedRun, so this cannot sit after that return.
  const [exporting, setExporting] = useState(false);
  const rawRec = result?.design || {};
  // A curve/surface run returns an empty design ({}) — that is NOT a single
  // recommended design, so don't fabricate an all-zeros one for the lender pack.
  const rec = Object.keys(rawRec).length ? {
    ...rawRec,
    ssr_pct: rawRec.ssr_pct ?? rawRec["Achieved SSR (%)"] ?? rawRec["Operational SSR (%)"] ?? result?.kpis?.["SSR (%)"] ?? 0,
    scr_pct: rawRec.scr_pct ?? rawRec["Achieved SCR (%)"] ?? rawRec["Operational SCR (%)"] ?? result?.kpis?.["SCR (%)"] ?? 0,
    gc_mw: rawRec.gc_mw ?? rawRec["Achieved Peak Grid Import (MW)"] ?? rawRec["Operational Peak Grid (MW)"] ?? result?.kpis?.["GCmin Peak (MW)"] ?? 0,
    pv_mw: rawRec.pv_mw ?? rawRec["PV Nameplate (MW)"] ?? 0,
    wind_mw: rawRec.wind_mw ?? rawRec["Wind Nameplate (MW)"] ?? 0,
    bess_mw: rawRec.bess_mw ?? rawRec["BESS Power (MW)"] ?? 0,
    bess_mwh: rawRec.bess_mwh ?? rawRec["BESS Energy (MWh)"] ?? 0,
    duration_h: rawRec.duration_h ?? rawRec["BESS Duration (h)"] ?? (rawRec.bess_mw > 0 ? rawRec.bess_mwh / rawRec.bess_mw : 0),
  } : null;

  // Rolling-horizon (Model W) comparison for the recommended design. Fetched from
  // the backend only when the user chose the forecast strategy; primitives (not the
  // recreated-each-render `rec` object) drive the dep array so it doesn't re-fetch
  // on every render. Declared before the early returns to keep hook order stable.
  const polSel = dispatchPolicy(cfg);
  const [wComp, setWComp] = useState(null);
  const [wLoading, setWLoading] = useState(false);
  useEffect(() => {
    if (!rec || polSel.model !== "rolling") { setWComp(null); return; }
    const loadPeak = (cfg?.loads || []).reduce((s, l) => s + (+l.peak_mw || 0), 0);
    const derz = cfg?.topoSignals?.derived_topology || cfg?.topology || "btm";
    const target_type = (derz === "btm" && (cfg?.btmObjective || "ssr") === "gc") ? "peak_shaving" : "ssr";
    // Operate the design against its OWN constraint: a peak-shaving design holds its
    // grid-connection target; an SSR design runs open (site limit) so SSR is measured
    // honestly. Without this the comparison mis-reports a peak-shaving design's grid.
    const gcTarget = +cfg?.gcTarget;
    const grid_ceiling_mw = (target_type === "peak_shaving" && gcTarget > 0) ? gcTarget : undefined;
    let cancelled = false;
    setWLoading(true);
    dispatchRolling({
      profile_path: profile?.profile_path || null,
      load_peak_mw: loadPeak > 0 ? loadPeak : null,
      pv_mw: rec.pv_mw, wind_mw: rec.wind_mw, bess_mw: rec.bess_mw, bess_mwh: rec.bess_mwh,
      target_type, grid_ceiling_mw, site_topology: TOPO_MAP[cfg?.topology] || "grid_connected_btm",
      horizon_h: polSel.horizonH, commit_h: polSel.commitH, allow_grid_charge: polSel.allowGridCharge,
    }).then((r) => { if (!cancelled) setWComp(r); })
      .catch(() => { if (!cancelled) setWComp(null); })
      .finally(() => { if (!cancelled) setWLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [polSel.model, polSel.horizonH, polSel.commitH, polSel.allowGridCharge,
      rec?.pv_mw, rec?.wind_mw, rec?.bess_mw, rec?.bess_mwh,
      profile?.profile_path, cfg?.topology, cfg?.btmObjective, cfg?.gcTarget]);

  // Grid-connection minimiser: the smallest grid the recommended design can hold +
  // sensitivity to battery / PV / grid-charging — the levers that actually reduce it.
  const [mg, setMg] = useState(null);
  const [mgLoading, setMgLoading] = useState(false);
  useEffect(() => {
    if (!rec) { setMg(null); return; }
    const loadPeak = (cfg?.loads || []).reduce((s, l) => s + (+l.peak_mw || 0), 0);
    let cancelled = false;
    setMgLoading(true);
    minGridConnection({
      profile_path: profile?.profile_path || null,
      load_peak_mw: loadPeak > 0 ? loadPeak : null,
      pv_mw: rec.pv_mw, wind_mw: rec.wind_mw, bess_mw: rec.bess_mw, bess_mwh: rec.bess_mwh,
      site_topology: TOPO_MAP[cfg?.topology] || "grid_connected_btm",
    }).then((r) => { if (!cancelled) setMg(r); })
      .catch(() => { if (!cancelled) setMg(null); })
      .finally(() => { if (!cancelled) setMgLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rec?.pv_mw, rec?.wind_mw, rec?.bess_mw, rec?.bess_mwh, profile?.profile_path, cfg?.topology]);

  // Grid-charging trade-off: the battery this design's TARGET needs with grid-charging
  // ON vs OFF — what allowing grid pre-charge saves in hardware.
  const [to, setTo] = useState(null);
  const [toLoading, setToLoading] = useState(false);
  useEffect(() => {
    if (!rec) { setTo(null); return; }
    const derz = cfg?.topoSignals?.derived_topology || cfg?.topology || "btm";
    if (derz !== "btm") { setTo(null); return; }   // grid-charging trade-off is a BTM story
    const isGc = (cfg?.btmObjective || "ssr") === "gc";
    const target_type = isGc ? "peak_shaving" : "ssr";
    const target_value = isGc ? (+cfg?.gcTarget || rec.gc_mw) : (+cfg?.ssrTarget || rec.ssr_pct);
    if (!(target_value > 0)) { setTo(null); return; }
    const loadPeak = (cfg?.loads || []).reduce((s, l) => s + (+l.peak_mw || 0), 0);
    let cancelled = false;
    setToLoading(true);
    sizeTradeoff({
      profile_path: profile?.profile_path || null,
      load_peak_mw: loadPeak > 0 ? loadPeak : null,
      pv_mw: rec.pv_mw, wind_mw: rec.wind_mw,
      target_type, target_value, duration_h: rec.duration_h || 4,
      site_topology: TOPO_MAP[cfg?.topology] || "grid_connected_btm",
    }).then((r) => { if (!cancelled) setTo(r); })
      .catch(() => { if (!cancelled) setTo(null); })
      .finally(() => { if (!cancelled) setToLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rec?.pv_mw, rec?.wind_mw, rec?.bess_mw, rec?.bess_mwh, rec?.duration_h,
      profile?.profile_path, cfg?.topology, cfg?.btmObjective, cfg?.gcTarget, cfg?.ssrTarget]);

  if (result && !result.feasible) return <NeedRun go={go} ranInfeasible />;
  if (!rec) return <NeedRun go={go} />;
  const curt = result?.kpis?.["OSR / Curtailment (%)"] ?? result?.kpis?.["Curtailment (%)"];
  const unmet = result?.kpis?.["Total Unmet Load (MWh)"];

  // Honest target check: does the RECOMMENDED design actually meet the goal it was
  // sized against? Operational (Model R) SSR can miss the LP target by the
  // no-foresight gap, so this must be computed, not asserted.
  const derived = cfg?.topoSignals?.derived_topology || cfg?.topology || "btm";
  const btmObj = cfg?.btmObjective || "ssr";
  const tc = (() => {
    const tol = 0.5;
    if (derived === "off_grid") {
      const rel = result?.kpis?.["Reliability (%)"], t = cfg?.firmnessTarget;
      if (rel == null || t == null) return null;
      return { ok: rel >= t - tol, detail: `Firmness ≥ ${t}% — got ${rel.toFixed(1)}%` };
    }
    if (derived === "standalone") {
      const t = cfg?.curtailmentTarget;
      if (curt == null || t == null) return null;
      return { ok: +curt <= t + tol, detail: `Curtailment ≤ ${t}% — got ${(+curt).toFixed(1)}%` };
    }
    if (derived === "backup" || (derived === "btm" && btmObj === "gc")) {
      const t = cfg?.gcTarget;
      if (t == null) return { ok: true, detail: `Lowest achievable grid — ${fmt.mw(rec.gc_mw)}` };
      return { ok: rec.gc_mw <= t + tol, detail: `Peak grid ≤ ${t} MW — got ${fmt.mw(rec.gc_mw)}` };
    }
    if (derived === "btm" && btmObj === "both") {
      const st = cfg?.ssrTarget, gt = cfg?.gcTarget;
      const ssrOk = st == null || rec.ssr_pct >= st - tol;
      const gcOk = gt == null || rec.gc_mw <= gt + tol;
      return { ok: ssrOk && gcOk, detail: `SSR ≥ ${st}% & grid ≤ ${gt} MW — got ${fmt.pct1(rec.ssr_pct)} · ${fmt.mw(rec.gc_mw)}` };
    }
    const t = cfg?.ssrTarget;
    if (t == null) return null;
    return { ok: rec.ssr_pct >= t - tol, detail: `SSR ≥ ${t}% — got ${fmt.pct1(rec.ssr_pct)}` };
  })();

  const pol = dispatchPolicy(cfg);
  const gcLabel = pol.allowGridCharge == null ? "auto" : (pol.allowGridCharge ? "on" : "off");

  async function downloadExcel() {
    setExporting(true);
    try {
      const loadPeak = (cfg?.loads || []).reduce((s, l) => s + (+l.peak_mw || 0), 0);
      const pol = dispatchPolicy(cfg);
      // A peak-shaving (GC) design must be operated at its grid-connection target, not
      // the open site limit, or the exported dispatch mis-reports its grid import.
      const isGc = derived === "btm" && btmObj === "gc";
      const gcT = +cfg?.gcTarget;
      await exportXlsx({
        project_name: cfg?.projectName || "DIP Project",
        scenario_id: result.id, scenario_name: result.name,
        recommended: rec, points: result?.table || [],
        kpis: result.kpis || {},
        assumptions: {
          "Dispatch merit order": pol.order.map((a, i) => `${i + 1}. ${DISPATCH_LABELS[a]}`).join("  |  "),
          "Grid-charging": pol.allowGridCharge == null ? "auto (from objective)" : (pol.allowGridCharge ? "on" : "off"),
        },
        // inputs so the backend can re-derive each point's per-slot dispatch
        profile_path: profile?.profile_path || null,
        load_peak_mw: loadPeak > 0 ? loadPeak : null,
        pv_mw: rec.pv_mw, wind_mw: rec.wind_mw,
        site_topology: TOPO_MAP[cfg?.topology] || "grid_connected_btm",
        ...(isGc && gcT > 0 ? { site_max_grid_mw: gcT } : {}),
        include_timeseries: true,
        dispatch_priority: pol.custom ? pol.order : [],
        allow_grid_charge: pol.allowGridCharge,
        // Rolling-horizon (Model W) comparison sheets use the user's window settings.
        rolling_window: true,
        rolling_horizon_h: pol.horizonH,
        rolling_commit_h: pol.commitH,
      });
    } catch (e) { alert("Export failed: " + (e.message || e)); }
    finally { setExporting(false); }
  }

  return (
    <>
      <div className="pagehead"><h1>Step 6 — Executive Decision Pack</h1>
        <p>Executive and investment-committee summary for the recommended microgrid design. All metrics are derived from hourly historical weather and demand simulations.</p></div>

      <StaleBanner stale={stale} go={go} />

      <div className="tiles">
        <Tile cls="t-scr" k="Self-Consumption (SCR)" v={fmt.pct(rec.scr_pct)} s="of solar generation used on site" />
        <Tile cls="t-ssr" k="Green Energy (SSR)" v={fmt.pct(rec.ssr_pct)} s="of demand met by on-site renewables" />
        <Tile cls="t-gc" k="Peak Grid Import" v={fmt.mw(rec.gc_mw)} s="required external grid connection" />
        <Tile cls="t-curt" k="Storage Duration" v={rec.duration_h ? `${rec.duration_h.toFixed(1)} hrs` : "—"} s="battery energy to power ratio" />
      </div>

      <GridConnectionMinimiser data={mg} loading={mgLoading} gridChargeOn={polSel.allowGridCharge} />

      <GridChargeTradeoff data={to} loading={toLoading} gridChargeOn={polSel.allowGridCharge} />

      <DispatchPolicyBadge cfg={cfg} />

      {polSel.model === "rolling" && (
        <ForesightComparison comp={wComp} loading={wLoading} horizonH={polSel.horizonH} commitH={polSel.commitH} />
      )}

      <ClientShowcaseMap cfg={cfg} rec={rec} result={result} />

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
          <h3>Recommended Equipment Specifications</h3>
          <div className="summary" style={{ gridTemplateColumns: "1fr 1fr" }}>
            <div className="it"><div className="l">Solar PV Capacity</div><div className="v">{fmt.mw(rec.pv_mw)}</div></div>
            <div className="it"><div className="l">Wind Capacity</div><div className="v">{rec.wind_mw > 0 ? fmt.mw(rec.wind_mw) : "—"}</div></div>
            <div className="it"><div className="l">Battery Power</div><div className="v">{fmt.mw(rec.bess_mw)}</div></div>
            <div className="it"><div className="l">Battery Storage</div><div className="v">{fmt.mwh(rec.bess_mwh)}</div></div>
            <div className="it"><div className="l">Storage Duration</div><div className="v">{rec.duration_h ? `${rec.duration_h.toFixed(1)} hrs` : "—"}</div></div>
            <div className="it"><div className="l">Peak Grid Import</div><div className="v" style={{ color: "var(--orange)" }}>{fmt.mw(rec.gc_mw)}</div></div>
          </div>
        </div>
        <div className="card" style={{ marginTop: 0, flex: 1 }}>
          <h3>System Performance Verification</h3>
          <div className="summary" style={{ gridTemplateColumns: "1fr 1fr" }}>
            <div className="it"><div className="l">Green Energy / SSR</div><div className="v" style={{ color: "var(--ok)" }}>{fmt.pct1(rec.ssr_pct)}</div></div>
            <div className="it"><div className="l">Self-Consumption / SCR</div><div className="v">{fmt.pct1(rec.scr_pct)}</div></div>
            <div className="it"><div className="l">Spilled Solar</div><div className="v">{curt != null ? fmt.pct1(curt) : "—"}</div></div>
            <div className="it"><div className="l">Unmet Load</div><div className="v" style={{ color: unmet > 0.05 ? "var(--red)" : "var(--ok)" }}>{unmet != null ? `${(+unmet).toFixed(1)} MWh` : "0.0 MWh"}</div></div>
            <div className="it"><div className="l">Target Check</div><div className="v">
              {tc == null
                ? <span className="badge">n/a</span>
                : <span className={"badge " + (tc.ok ? "teal" : "red")} title={tc.detail}>
                    {tc.ok ? "Compliant" : "Below target"}</span>}
            </div></div>
            <div className="it"><div className="l">Control Strategy</div>
              <div className="v" title={`Grid-charging: ${gcLabel}`}>{pol.custom ? "Custom Priority" : "Real-Time Auto-Dispatch"}</div></div>
          </div>
        </div>
      </div>

      <div className="navbtns">
        <button className="btn" onClick={() => go(step - 1)}>Back</button>
        <div style={{ display: "flex", gap: 10 }}>
          <button className="btn" onClick={() => window.print()}>⤓ Print Executive Pack</button>
          <button className="btn primary" onClick={downloadExcel} disabled={exporting}>
            {exporting ? "Building workbook…" : "⤓ Download Simulation Workbook (.xlsx)"}
          </button>
        </div>
      </div>
    </>
  );
}

function Tile({ cls, k, v, s }) {
  return <div className={"tile " + cls}><div className="k">{k}</div><div className="v">{v}</div><div className="s">{s}</div></div>;
}

// Grid-charging trade-off: the battery this design's target needs WITH vs WITHOUT
// grid-charging. Allowing grid pre-charge lets a smaller battery hold a grid
// connection (big saving for grid goals); it does nothing for a self-sufficiency goal.
function GridChargeTradeoff({ data, loading, gridChargeOn }) {
  if (!loading && !data) return null;   // hidden when N/A (e.g. non-BTM or no target)
  const on = data?.grid_charge_on, off = data?.grid_charge_off;
  const isGrid = data?.target_type === "peak_shaving";
  return (
    <div className="card" style={{ marginTop: 0 }}>
      <h3 style={{ marginTop: 0 }}>Grid-charging trade-off <span style={{ fontWeight: 400, color: "#8a949b", fontSize: 13 }}>
        — the battery this target needs, with vs without letting it pre-charge from the grid</span></h3>
      {loading && <div className="hint">Sizing both ways…</div>}
      {!loading && data && (
        <>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead><tr style={{ textAlign: "right", color: "#8a949b" }}>
                <th style={{ textAlign: "left", padding: "4px 8px" }}>Grid-charging</th>
                <th style={{ padding: "4px 8px" }}>Battery power</th>
                <th style={{ padding: "4px 8px" }}>Battery energy</th>
              </tr></thead>
              <tbody>
                <tr style={{ textAlign: "right", borderTop: "1px solid #eef2f3" }}>
                  <td style={{ textAlign: "left", padding: "5px 8px", fontWeight: 600 }}>OFF (PV-only charging)</td>
                  <td style={{ padding: "5px 8px" }}>{off?.bess_mw} MW</td>
                  <td style={{ padding: "5px 8px" }}>{off?.bess_mwh} MWh</td>
                </tr>
                <tr style={{ textAlign: "right", borderTop: "1px solid #eef2f3" }}>
                  <td style={{ textAlign: "left", padding: "5px 8px", fontWeight: 600 }}>ON (may pre-charge from grid)</td>
                  <td style={{ padding: "5px 8px" }}>{on?.bess_mw} MW</td>
                  <td style={{ padding: "5px 8px" }}>{on?.bess_mwh} MWh</td>
                </tr>
                {data.matters && (
                  <tr style={{ textAlign: "right", borderTop: "1px solid #eef2f3", color: "#177245", fontWeight: 700 }}>
                    <td style={{ textAlign: "left", padding: "5px 8px" }}>Grid-charging saves</td>
                    <td style={{ padding: "5px 8px" }}>{data.saving_mw} MW</td>
                    <td style={{ padding: "5px 8px" }}>{data.saving_mwh} MWh</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="hint" style={{ marginTop: 10 }}>
            {data.matters
              ? `Allowing grid-charging cuts the battery by ${data.saving_mw} MW / ${data.saving_mwh} MWh to hold this grid connection — it pre-charges off-peak instead of buying more storage.${gridChargeOn === false ? " It is currently OFF; turn it ON in Step 4 to capture this." : ""}`
              : "For a self-sufficiency goal, grid-charging does not change the battery — importing from the grid to charge would lower SSR, so the sizer keeps it off. Grid-charging only helps grid-connection goals."}
          </div>
        </>
      )}
    </div>
  );
}

// Grid-connection minimiser: the smallest grid the design can hold, and how the
// real levers (battery size, PV size, grid-charging) move it. This is the tool for
// "get my grid connection down" — the dispatch model does NOT affect these numbers.
function GridConnectionMinimiser({ data, loading, gridChargeOn }) {
  const SweepBars = ({ rows, xKey, xLabel, xUnit, max }) => (
    <div>
      <div style={{ fontSize: 12, fontWeight: 600, color: "#556", marginBottom: 6 }}>{xLabel}</div>
      {rows.map((r, i) => {
        const pct = max > 0 ? (r.min_grid_mw / max) * 100 : 0;
        return (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 3, fontSize: 12 }}>
            <span style={{ width: 78, color: "#8a949b", textAlign: "right" }}>{r[xKey]} {xUnit}</span>
            <div style={{ flex: 1, background: "#eef2f3", borderRadius: 4, height: 16, position: "relative" }}>
              <div style={{ width: `${pct}%`, background: "#15616d", height: "100%", borderRadius: 4 }} />
            </div>
            <span style={{ width: 54, fontWeight: 600 }}>{r.min_grid_mw} MW</span>
          </div>
        );
      })}
    </div>
  );
  const c = data?.current;
  const maxGrid = data?.no_battery_mw || 100;
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Minimum grid connection <span style={{ fontWeight: 400, color: "#8a949b", fontSize: 13 }}>
        — the smallest grid this design can hold, and the levers that shrink it</span></h3>
      {loading && <div className="hint">Computing minimum grid connection…</div>}
      {!loading && !data && <div className="hint">Minimum-grid analysis unavailable for this design.</div>}
      {!loading && data && c && (
        <>
          <div className="tiles" style={{ marginBottom: 12 }}>
            <Tile cls="t-gc" k="Peak demand" v={`${data.no_battery_mw} MW`} s="grid needed with no battery" />
            <Tile cls="t-ssr" k="Minimum grid (this design)" v={`${c.min_grid_on} MW`} s="lowest connection that serves all load" />
            <Tile cls="t-scr" k="Grid-charging saves" v={`${c.grid_charge_saving_mw} MW`} s={`${c.min_grid_off} MW off → ${c.min_grid_on} MW on`} />
          </div>
          {gridChargeOn === false && c.grid_charge_saving_mw > 0.1 && (
            <div className="banner" style={{ marginBottom: 12, background: "#fff5e6", color: "#8a5a00" }}>
              Grid-charging is OFF. Turning it ON (Step 4 → Dispatch &amp; Operation) lowers the required
              grid connection by {c.grid_charge_saving_mw} MW, to {c.min_grid_on} MW.
            </div>
          )}
          {data.binding_shortage && data.binding_shortage.hours > 0 && (
            <div className="banner" style={{ marginBottom: 12, background: "#eef4f5", color: "#2a4a52" }}>
              <b>What limits the connection:</b> the longest shortage is {data.binding_shortage.hours} h needing{" "}
              {data.binding_shortage.deficit_mwh.toLocaleString()} MWh above the connection, and the battery entered it{" "}
              {data.binding_shortage.soc_entering_pct != null ? `${data.binding_shortage.soc_entering_pct}% full` : "as full as it could"}.
              This is an <b>energy</b> limit — more battery hours (duration) or PV rides it out; foresight cannot
              (Model R already pre-charges to the physical maximum).
            </div>
          )}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 16 }}>
            <SweepBars rows={data.battery_sweep} xKey="bess_mw" xLabel="↓ grid vs battery power (fixed duration)" xUnit="MW" max={maxGrid} />
            {data.duration_sweep?.length > 0 && (
              <SweepBars rows={data.duration_sweep} xKey="duration_h" xLabel="↓ grid vs battery hours (energy, fixed power)" xUnit="h" max={maxGrid} />
            )}
            <SweepBars rows={data.pv_sweep} xKey="pv_mw" xLabel="↓ grid vs PV nameplate (battery fixed)" xUnit="MW" max={maxGrid} />
          </div>
          <div className="hint" style={{ marginTop: 10 }}>
            All bars use grid-charging ON. These are true minima (Model R hits the perfect-foresight floor for grid
            size), so the dispatch strategy does not change them — battery power, battery energy (hours), PV, and
            grid-charging are the levers.
          </div>
        </>
      )}
    </div>
  );
}

// The "value of foresight": rolling-horizon (Model W) operation of the SAME
// recommended design, side by side with the causal rule (Model R). Both operate
// the identical battery — the deltas are purely what a forecast buys.
function ForesightComparison({ comp, loading, horizonH, commitH }) {
  const ROWS = [
    ["SSR (%)", "SSR (%)", "pct", +1],
    ["Peak grid import (MW)", "GCmin Peak (MW)", "mw", -1],
    ["Unmet load (MWh)", "Total Unmet Load (MWh)", "mwh", -1],
    ["Grid import (MWh)", "Total Grid Import (MWh)", "mwh", -1],
  ];
  const w = comp?.model_w, r = comp?.model_r;
  const cell = (v, kind) => v == null ? "—"
    : kind === "pct" ? `${(+v).toFixed(1)}%` : kind === "mw" ? `${(+v).toFixed(1)} MW`
    : `${Math.round(+v).toLocaleString()} MWh`;
  // Is foresight actually buying anything here? For self-sufficiency the greedy rule
  // is already near-optimal on SSR (the objective), so W ≈ R and SSR barely moves —
  // say so, so a ~zero table doesn't read as broken. (Peak grid can wiggle a few MW
  // since W optimises energy, not peak; that's contextualised by the note below.)
  const ssrD = (w && r) ? Math.abs((+w["SSR (%)"]) - (+r["SSR (%)"])) : null;
  const negligible = ssrD != null && ssrD < 0.5;
  return (
    <div className="card" style={{ marginTop: 0 }}>
      <h3 style={{ marginTop: 0 }}>Value of foresight <span style={{ fontWeight: 400, color: "#8a949b", fontSize: 13 }}>
        — rolling-horizon (look-ahead {horizonH}h, commit {commitH}h) vs the no-foresight rule, same battery</span></h3>
      {loading && <div className="hint">Simulating rolling-horizon dispatch…</div>}
      {!loading && !comp && <div className="hint">Rolling-horizon comparison unavailable for this design.</div>}
      {!loading && comp && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead><tr style={{ textAlign: "right", color: "#8a949b" }}>
              <th style={{ textAlign: "left", padding: "4px 8px" }}>Metric</th>
              <th style={{ padding: "4px 8px" }}>Rule (R)</th>
              <th style={{ padding: "4px 8px" }}>Rolling (W)</th>
              <th style={{ padding: "4px 8px" }}>Δ (foresight)</th>
            </tr></thead>
            <tbody>
              {ROWS.map(([label, key, kind, better]) => {
                const rv = r?.[key], wv = w?.[key];
                const d = (rv != null && wv != null) ? +wv - +rv : null;
                const good = d != null && Math.abs(d) > 1e-6 && Math.sign(d) === better;
                const bad = d != null && Math.abs(d) > 1e-6 && Math.sign(d) === -better;
                const col = good ? "#177245" : bad ? "#b23b3b" : "#8a949b";
                return (
                  <tr key={key} style={{ textAlign: "right", borderTop: "1px solid #eef2f3" }}>
                    <td style={{ textAlign: "left", padding: "5px 8px", fontWeight: 600 }}>{label}</td>
                    <td style={{ padding: "5px 8px" }}>{cell(rv, kind)}</td>
                    <td style={{ padding: "5px 8px" }}>{cell(wv, kind)}</td>
                    <td style={{ padding: "5px 8px", color: col, fontWeight: 600 }}>
                      {d == null ? "—" : `${d > 0 ? "+" : ""}${kind === "pct" ? d.toFixed(1) : Math.round(d).toLocaleString()}`}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="hint" style={{ marginTop: 10 }}>
            {negligible
              ? "Foresight adds little for this design — a self-sufficiency target is near-optimal even for the no-foresight rule. Rolling-horizon pays off most for peak-shaving and tight grid connections, where it pre-positions the battery ahead of a peak."
              : "Rolling-horizon operates the same battery with a forecast — the deltas above are purely what foresight buys, at no extra hardware."}
          </div>
        </div>
      )}
    </div>
  );
}

// Interactive dispatch from real forward-eval flows. Quantitative MW axes, a
// period control (legible year / representative week), curtailment shown above
// the demand line, and a hover readout of the exact split at any point.
const PERIODS = [["year", "Full year"], ["summer", "Summer week"], ["winter", "Winter week"]];

function DispatchChart({ flows }) {
  const [period, setPeriod] = useState("year");
  const [hi, setHi] = useState(null);
  const d = useMemo(() => dispatchWindow(flows, period), [flows, period]);
  if (!d || !d.n) return (
    <div className="card"><h3>Energy dispatch</h3>
      <EmptyChart msg="No dispatch series for this design — run a feasible sizing first." /></div>
  );

  const W = 680, H = 260, padL = 44, padR = 12, padT = 12, padB = 26;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const yMax = d.yMax;
  const X = (i) => padL + (d.n > 1 ? i / (d.n - 1) : 0.5) * plotW;
  const Y = (v) => (H - padB) - Math.max(0, v) / yMax * plotH;
  // Cumulative stack levels (bottom→top): direct, +bess, +grid, +unmet, then curtail sits above load.
  const lvl1 = d.direct;
  const lvl2 = d.direct.map((v, i) => v + d.bess[i]);
  const lvl3 = lvl2.map((v, i) => v + d.grid[i]);
  const lvl4 = lvl3.map((v, i) => v + (d.unmet[i] || 0));       // = load (met + unmet)
  const lvlC = lvl4.map((v, i) => v + (d.curt[i] || 0));        // spilled generation on top
  const band = (top, bot) => {
    const up = top.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)} ${Y(v).toFixed(1)}`).join(" ");
    const dn = bot.map((v, i) => `L${X(d.n - 1 - i).toFixed(1)} ${Y(bot[d.n - 1 - i]).toFixed(1)}`).join(" ");
    return `${up} ${dn} Z`;
  };
  const zero = d.load.map(() => 0);
  const yticks = Array.from({ length: 5 }, (_, k) => (k / 4) * yMax);
  const anyUnmet = d.unmet.some((u) => u > 1e-6);
  const anyCurt = d.curt.some((c) => c > 1e-6);
  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width;
    setHi(Math.max(0, Math.min(d.n - 1, Math.round(x * (d.n - 1)))));
  };

  return (
    <div className="card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
        <h3 style={{ margin: 0 }}>Energy dispatch — how demand is met</h3>
        <div style={{ display: "flex", border: "1px solid #d6dde0", borderRadius: 6, overflow: "hidden" }}>
          {PERIODS.map(([k, lab]) => (
            <button key={k} className="btn" style={{ padding: "3px 10px", border: "none", borderRadius: 0, fontSize: 12,
              background: period === k ? "#15616d" : "#fff", color: period === k ? "#fff" : "#333" }}
              onClick={() => { setPeriod(k); setHi(null); }}>{lab}</button>
          ))}
        </div>
      </div>
      <div className="hint">Stacked by source ({d.unit}); spilled solar (curtailment) is drawn above the demand line. Hover for the exact split.</div>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} onMouseMove={onMove} onMouseLeave={() => setHi(null)} style={{ cursor: "crosshair" }}>
        {yticks.map((v, k) => <line key={k} x1={padL} y1={Y(v)} x2={W - padR} y2={Y(v)} stroke="#eef2f3" />)}
        {yticks.map((v, k) => <text key={`yl${k}`} x={padL - 5} y={Y(v) + 3} fontSize="9" fill="#8a949b" textAnchor="end">{Math.round(v)}</text>)}
        {d.ticks.map((t, k) => <text key={`xt${k}`} x={X(t.pos)} y={H - padB + 13} fontSize="9" fill="#8a949b" textAnchor="middle">{t.label}</text>)}
        <text x={11} y={padT + plotH / 2} fontSize="10" fill="#6b7780" textAnchor="middle" transform={`rotate(-90 11 ${padT + plotH / 2})`}>MW</text>
        {/* stacked bands */}
        <path d={band(lvl1, zero)} fill={PALETTE.pv} opacity="0.85" />
        <path d={band(lvl2, lvl1)} fill={PALETTE.bess} opacity="0.85" />
        <path d={band(lvl3, lvl2)} fill={PALETTE.grid} opacity="0.8" />
        {anyUnmet && <path d={band(lvl4, lvl3)} fill={PALETTE.unmet} opacity="0.9" />}
        {anyCurt && <path d={band(lvlC, lvl4)} fill={PALETTE.curtail} opacity="0.35" />}
        {/* demand line */}
        <path d={d.load.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)} ${Y(v).toFixed(1)}`).join(" ")}
              fill="none" stroke="#2b3a42" strokeWidth="1.2" strokeDasharray="4 3" />
        {/* hover crosshair + readout */}
        {hi != null && (() => {
          const rows = [["Direct", d.direct[hi], PALETTE.pv], ["BESS", d.bess[hi], PALETTE.bess],
            ["Grid", d.grid[hi], PALETTE.grid], ...(anyUnmet ? [["Unmet", d.unmet[hi], PALETTE.unmet]] : []),
            ["Curtailed", d.curt[hi], PALETTE.curtail]];
          const bw = 132, bh = 20 + rows.length * 14, bx = X(hi) > W - bw - 20 ? X(hi) - bw - 8 : X(hi) + 8;
          return <g>
            <line x1={X(hi)} y1={padT} x2={X(hi)} y2={H - padB} stroke="#94a3ab" strokeWidth="1" />
            <rect x={bx} y={padT} width={bw} height={bh} rx="5" fill="#fff" stroke="#d6dde0" />
            <text x={bx + 8} y={padT + 14} fontSize="10" fontWeight="700" fill="#334">{d.labelAt(hi)}</text>
            {rows.map(([lab, val, col], k) => <g key={lab}>
              <rect x={bx + 8} y={padT + 22 + k * 14} width="8" height="8" rx="1.5" fill={col} />
              <text x={bx + 20} y={padT + 29 + k * 14} fontSize="10" fill="#556">{lab}</text>
              <text x={bx + bw - 8} y={padT + 29 + k * 14} fontSize="10" fontWeight="600" fill="#223" textAnchor="end">{fmt.num(val, 1)} MW</text>
            </g>)}
          </g>;
        })()}
      </svg>
      <div className="legend">
        <span><i className="dot" style={{ background: PALETTE.pv }} />Direct to consumer</span>
        <span><i className="dot" style={{ background: PALETTE.bess }} />BESS discharge</span>
        <span><i className="dot" style={{ background: PALETTE.grid }} />Grid import</span>
        {anyUnmet && <span><i className="dot" style={{ background: PALETTE.unmet }} />Unmet</span>}
        <span><i className="dot" style={{ background: PALETTE.curtail }} />Spilled solar (curtailed)</span>
        <span><i className="dot" style={{ background: "#2b3a42" }} />Demand</span>
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

// BESS state-of-charge across the year (quantitative MWh axis; placeholder when
// the design carries no battery).
function SocChart({ flows }) {
  const soc = socSeries(flows);
  const peak = soc.length ? Math.max(...soc) : 0;
  const W = 620, H = 160, padL = 42, padR = 10, padT = 10, padB = 8, pw = W - padL - padR, ph = H - padT - padB;
  const max = Math.max(peak, 1) * 1.1;
  const Y = (v) => padT + (ph - v / max * ph);
  const yticks = Array.from({ length: 4 }, (_, k) => (k / 3) * max);
  return (
    <div className="card" style={{ marginTop: 0 }}>
      <h3>BESS state of charge — full year</h3>
      <div className="hint">How the battery cycles across the year (MWh stored).</div>
      {peak <= 1e-6 ? (
        <EmptyChart msg="This design has no battery — nothing to charge or discharge." h={160} />
      ) : (
        <svg className="chart" viewBox={`0 0 ${W} ${H}`}>
          {yticks.map((v, k) => <line key={k} x1={padL} y1={Y(v)} x2={W - padR} y2={Y(v)} stroke="#eef2f3" />)}
          {yticks.map((v, k) => <text key={`y${k}`} x={padL - 5} y={Y(v) + 3} fontSize="9" fill="#8a949b" textAnchor="end">{Math.round(v)}</text>)}
          <g transform={`translate(${padL} ${padT})`}>
            <path d={areaPath(soc, pw, ph, max)} fill="rgba(47,143,91,.18)" />
            <path d={linePath(soc, pw, ph, max)} fill="none" stroke={PALETTE.soc} strokeWidth="1.5" />
          </g>
        </svg>
      )}
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
