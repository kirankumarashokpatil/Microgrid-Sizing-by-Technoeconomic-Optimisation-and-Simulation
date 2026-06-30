// Shows one scenario's contract + a conditional form (only the inputs it needs),
// runs it, and renders results. Used by both the wizard leaf and the direct
// picker. The React twin of the Streamlit render_scenario().
import { useEffect, useMemo, useState } from "react";
import { getScenario, getProfileSummary, runScenario } from "./api.js";
import { FIELDS, DEFAULT_FIELDS } from "./fields.js";
import Results from "./Results.jsx";

export default function ScenarioForm({ scenarioId, profilePath }) {
  const [detail, setDetail] = useState(null);
  const [summary, setSummary] = useState(null);
  const [form, setForm] = useState({});
  const [advanced, setAdvanced] = useState({
    eff_charge: 0.95, eff_discharge: 0.95, min_soc_pct: 10, max_soc_pct: 90,
    eol_capacity_retention_pct: 80, site_max_grid_mw: 200, solver_time_limit: 120,
  });
  const [showAdv, setShowAdv] = useState(false);
  const [result, setResult] = useState(null);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState(null);

  const fields = FIELDS[scenarioId] || DEFAULT_FIELDS;

  useEffect(() => {
    setDetail(null); setResult(null); setErr(null);
    getScenario(scenarioId).then(setDetail).catch((e) => setErr(String(e)));
  }, [scenarioId]);

  useEffect(() => {
    getProfileSummary(profilePath).then(setSummary).catch(() => setSummary(null));
  }, [profilePath]);

  // Seed sensible defaults once we know the dataset (e.g. PV nameplate, peak GC).
  const defaults = useMemo(() => {
    const pv = summary?.pv_nameplate_mw ?? 150;
    const peak = summary?.peak_load_mw ?? 100;
    return {
      pv_mw: pv, target_ssr_pct: 60, target_gc_mw: Math.round(peak * 0.8),
      target_firmness_pct: 99, deliverable_target: "ssr",
      bess_mw: 20, bess_mwh: 80, grid_ceiling_mw: 0,
      pv_sweep_mw: "50,100,150,200,250", ssr_targets_pct: "10,20,30,40,50,60,70,80",
    };
  }, [summary]);

  useEffect(() => { setForm(defaults); }, [defaults]);

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));
  const val = (k) => (form[k] ?? defaults[k]);

  async function run() {
    setRunning(true); setErr(null);
    try {
      const payload = { scenario_id: scenarioId, ...advanced };
      if (profilePath) payload.profile_path = profilePath;
      for (const f of fields) {
        const v = val(f);
        if (f === "pv_sweep_mw" || f === "ssr_targets_pct") {
          payload[f] = String(v).split(",").map((x) => parseFloat(x.trim())).filter((n) => !isNaN(n));
        } else if (f === "deliverable_target") {
          payload[f] = v;
        } else if (f === "grid_ceiling_mw") {
          if (Number(v) > 0) payload[f] = Number(v);
        } else {
          payload[f] = Number(v);
        }
      }
      const res = await runScenario(payload);
      setResult(res);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setRunning(false);
    }
  }

  if (err && !detail) return <div className="banner err">{err}</div>;
  if (!detail) return <div className="subtle"><span className="spinner" /> loading scenario…</div>;

  const ready = detail.status === "ready";

  return (
    <div>
      <div className="card">
        <h3>{scenarioId} — {detail.name}</h3>
        <div className="subtle" style={{ marginBottom: 8 }}>
          topology <code>{detail.topology}</code> · answer mode <code>{detail.answer_mode}</code>
        </div>
        <div style={{ fontSize: 13, lineHeight: 1.6 }}>
          <div><strong>Inputs:</strong> {detail.inputs}</div>
          <div><strong>Outputs:</strong> {detail.outputs}</div>
          <div><strong>How:</strong> {detail.how}</div>
        </div>
        {!ready && <div className="banner warn" style={{ marginTop: 10 }}>⏳ Not runnable yet — {detail.needs}</div>}
      </div>

      {ready && (
        <>
          {summary && (
            <div className="banner info">
              Dataset: peak load <strong>{summary.peak_load_mw?.toFixed?.(0)} MW</strong>,
              {" "}PV nameplate <strong>{summary.pv_nameplate_mw?.toFixed?.(0)} MW</strong>,
              {" "}{summary.n_timesteps?.toLocaleString()} steps · {summary.dt_hours}h
            </div>
          )}

          <div className="card">
            <h3>Inputs</h3>
            <div className="form-grid">
              {fields.includes("pv_mw") && <Num label="PV nameplate (MW)" v={val("pv_mw")} on={(x) => set("pv_mw", x)} />}
              {fields.includes("target_ssr_pct") && <Num label="Target SSR (%)" v={val("target_ssr_pct")} on={(x) => set("target_ssr_pct", x)} />}
              {fields.includes("target_gc_mw") && <Num label="Target grid connection (MW)" v={val("target_gc_mw")} on={(x) => set("target_gc_mw", x)} />}
              {fields.includes("target_firmness_pct") && <Num label="Firmness target (%)" v={val("target_firmness_pct")} on={(x) => set("target_firmness_pct", x)} />}
              {fields.includes("bess_mw") && <Num label="BESS power (MW)" v={val("bess_mw")} on={(x) => set("bess_mw", x)} />}
              {fields.includes("bess_mwh") && <Num label="BESS energy (MWh)" v={val("bess_mwh")} on={(x) => set("bess_mwh", x)} />}
              {fields.includes("grid_ceiling_mw") && <Num label="Grid ceiling (MW) — 0 = site max" v={val("grid_ceiling_mw")} on={(x) => set("grid_ceiling_mw", x)} />}
              {fields.includes("deliverable_target") && (
                <Sel label="Deliverable target" v={val("deliverable_target")} opts={["ssr", "gc"]} on={(x) => set("deliverable_target", x)} />
              )}
              {fields.includes("pv_sweep_mw") && <Txt label="PV sweep (MW, comma-sep)" v={val("pv_sweep_mw")} on={(x) => set("pv_sweep_mw", x)} />}
              {fields.includes("ssr_targets_pct") && <Txt label="Targets (comma-sep)" v={val("ssr_targets_pct")} on={(x) => set("ssr_targets_pct", x)} />}
            </div>

            <button className="ghost small" onClick={() => setShowAdv((s) => !s)} style={{ marginTop: 4 }}>
              {showAdv ? "▾" : "▸"} Battery & site assumptions
            </button>
            {showAdv && (
              <div className="form-grid" style={{ marginTop: 12 }}>
                <Num label="Charge eff." step={0.01} v={advanced.eff_charge} on={(x) => setAdvanced((a) => ({ ...a, eff_charge: x }))} />
                <Num label="Discharge eff." step={0.01} v={advanced.eff_discharge} on={(x) => setAdvanced((a) => ({ ...a, eff_discharge: x }))} />
                <Num label="Min SOC (%)" v={advanced.min_soc_pct} on={(x) => setAdvanced((a) => ({ ...a, min_soc_pct: x }))} />
                <Num label="Max SOC (%)" v={advanced.max_soc_pct} on={(x) => setAdvanced((a) => ({ ...a, max_soc_pct: x }))} />
                <Num label="EoL retention (%)" v={advanced.eol_capacity_retention_pct} on={(x) => setAdvanced((a) => ({ ...a, eol_capacity_retention_pct: x }))} />
                <Num label="Site max grid (MW)" v={advanced.site_max_grid_mw} on={(x) => setAdvanced((a) => ({ ...a, site_max_grid_mw: x }))} />
                <Num label="Solver time limit (s)" v={advanced.solver_time_limit} on={(x) => setAdvanced((a) => ({ ...a, solver_time_limit: x }))} />
              </div>
            )}

            <div style={{ marginTop: 14 }}>
              <button onClick={run} disabled={running}>
                {running ? <><span className="spinner" /> Solving…</> : "🚀 Run scenario"}
              </button>
            </div>
          </div>

          {err && <div className="banner err">Run failed: {err}</div>}
          {result && result.id === scenarioId && <Results result={result} />}
        </>
      )}
    </div>
  );
}

function Num({ label, v, on, step = 1 }) {
  return (
    <label className="field">
      <span className="lab">{label}</span>
      <input type="number" step={step} value={v ?? ""} onChange={(e) => on(e.target.value === "" ? "" : Number(e.target.value))} />
    </label>
  );
}
function Txt({ label, v, on }) {
  return (
    <label className="field">
      <span className="lab">{label}</span>
      <input type="text" value={v ?? ""} onChange={(e) => on(e.target.value)} />
    </label>
  );
}
function Sel({ label, v, opts, on }) {
  return (
    <label className="field">
      <span className="lab">{label}</span>
      <select value={v} onChange={(e) => on(e.target.value)}>
        {opts.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    </label>
  );
}
