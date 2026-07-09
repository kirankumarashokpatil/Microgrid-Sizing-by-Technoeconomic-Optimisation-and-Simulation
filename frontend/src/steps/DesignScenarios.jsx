// DesignScenarios — the land-first evaluation core (Phase 1) output view.
// One call to POST /design (via runDesign) turns the land + demand you've set
// into a with/without-BESS comparison, so a developer sees what the battery
// buys before committing to the full sizing sweep below.
import { useState } from "react";
import { runDesign } from "../lib/api.js";
import { fmt } from "../lib/svg.js";

// Stripe colours mirror the parcel/scenario palette used across the app + spec.
const STRIPE = {
  "Grid-only": "#7a8794",
  "Generation, no BESS": "#e0922f",
  "Generation + BESS": "#2f8f5b",
};

// `data`/`setData` are lifted to App so a completed assessment survives leaving
// and returning to the Size step (and a page refresh).
export function DesignScenarios({ cfg, profile, summary, data, setData }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  // Same input derivation the Size step uses: demand peak from the loads,
  // generation nameplates from the parcels, SSR target from the objective.
  const peakLoad = (cfg.loads || []).reduce((s, l) => s + (+l.peak_mw || 0), 0);
  const pvMw = cfg.tech?.solar ? (cfg.parcels?.solar?.maxMw ?? summary?.pv_nameplate_mw ?? 150) : 0;
  const windMw = cfg.tech?.wind ? (cfg.parcels?.wind?.maxMw ?? 0) : 0;
  const targetSsr = cfg.ssrTarget ?? 95;
  const landHa =
    (cfg.parcels?.list || []).reduce((s, p) => s + (p.areaHa || 0), 0) ||
    ((cfg.parcels?.solar?.areaHa || 0) + (cfg.parcels?.wind?.areaHa || 0));

  async function run() {
    setBusy(true); setErr(null);
    try {
      setData(await runDesign({
        profile_path: profile?.profile_path,
        load_peak_mw: peakLoad > 0 ? peakLoad : undefined,
        pv_mw: pvMw, wind_mw: windMw,
        target_ssr_pct: targetSsr,
        available_land_ha: landHa || undefined,
      }));
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ borderColor: "var(--teal)", background: "var(--teal-light)" }}>
      <h3 style={{ color: "var(--teal-dark)" }}>
        Quick Assessment — With vs. Without Battery Storage
        <span style={{ marginLeft: 8, fontSize: 10.5, fontWeight: 700, letterSpacing: ".04em",
          textTransform: "uppercase", color: "#15616d", background: "#fff",
          border: "1px solid #bcd6d9", borderRadius: 6, padding: "2px 7px", verticalAlign: "middle" }}>
          Instant Baseline Check
        </span>
      </h3>
      <div className="hint" style={{ color: "var(--teal-dark)", opacity: 0.85 }}>
        Evaluate your <b>{fmt.mw(peakLoad)}</b> data centre running purely on solar &amp; grid versus adding energy storage to hit your <b>{targetSsr}%</b> green energy target
        {landHa > 0 ? <> across <b>{Math.round(landHa)} ha</b> of land</> : null}.
      </div>

      <button className="btn primary" disabled={busy} onClick={run} style={{ marginTop: 4 }}>
        {busy ? "Analyzing configurations…" : "▶ Run Quick Assessment"}
      </button>

      {err && <div className="banner err" style={{ marginTop: 12 }}>Simulation error: {err}</div>}

      {data && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))",
                        gap: 12, marginTop: 14 }}>
            {data.scenarios.map((s) => (
              <div key={s.label} style={{ background: "#fff", border: "1px solid var(--line)",
                borderRadius: 12, borderTop: `3px solid ${STRIPE[s.label] || "#7a8794"}`, padding: 16 }}>
                <div style={{ fontWeight: 700, fontSize: 15, letterSpacing: "-.01em" }}>{s.label}</div>
                <div style={{ fontSize: 12, color: "#64748b", marginBottom: 10 }}>{s.config}</div>
                {s.feasible === false ? (
                  <div style={{ marginTop: 8, fontSize: 12.5, color: "var(--red)", fontWeight: 600, lineHeight: 1.4 }}>
                    ⚠ Infeasible at {targetSsr}% — the target can&apos;t be reached with this generation.
                    Add solar / land or lower the target.
                  </div>
                ) : (<>
                  <KV k="Green Energy / SSR" v={fmt.pct1(s.ssr_pct)} strong />
                  <KV k="Peak Grid Import" v={fmt.mw(s.grid_peak_mw)} />
                  {s.curtailment_pct != null && <KV k="Spilled Solar" v={fmt.pct1(s.curtailment_pct)} />}
                  <KV k="Battery Storage" v={s.bess_mw > 0 ? `${fmt.mw(s.bess_mw)} · ${fmt.mwh(s.bess_mwh)}` : "—"} />
                  {s.duration_h > 0 && <KV k="Storage Duration" v={`${s.duration_h.toFixed(1)} h`} />}
                </>)}
              </div>
            ))}
          </div>
          <div className="subtle" style={{ marginTop: 10 }}>
            Comparing these baselines demonstrates how energy storage reduces peak grid import and captures solar surplus.
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
      <span style={{ fontWeight: strong ? 700 : 600, color: strong ? "#15616d" : "#1e293b",
                     fontVariantNumeric: "tabular-nums" }}>{v}</span>
    </div>
  );
}
